"""Shared auth + project fixtures.

JWT verification runs the real `app.auth.JwtVerifier` code path against a
locally generated keypair: `LocalJWKClient` is a genuine `jwt.PyJWKClient`
(kid matching, key parsing) whose JWKS fetch returns an in-memory key set
instead of calling `{frontend_api}/.well-known/jwks.json`.

DB-backed fixtures create real `codeaxis.profiles` rows directly (Clerk has
no signup hook into this database, so there is no trigger to exercise —
`app.knowledge.ensure_profile` is what provisions a profile for real, and
`test_projects_membership.py` covers that path through the API itself).
Everything a fixture creates is deleted at teardown.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from app.auth import JwtVerifier, get_verifier
from app.knowledge import create_project, worker_connection
from app.main import app

load_dotenv()

TEST_ISSUER = "https://test-instance.clerk.invalid"
RS_KID = "test-rs256"


class LocalJWKClient(jwt.PyJWKClient):
    def __init__(self, jwks: dict[str, Any]) -> None:
        super().__init__("https://jwks.invalid/.well-known/jwks.json", cache_jwk_set=False)
        self._jwks = jwks

    def fetch_data(self) -> Any:
        return self._jwks


def _public_jwk(private_key: Any, kid: str, alg: str) -> dict[str, Any]:
    algo = jwt.get_algorithm_by_name(alg)
    jwk = json.loads(algo.to_jwk(private_key.public_key()))
    return {**jwk, "kid": kid, "alg": alg, "use": "sig"}


@pytest.fixture(scope="session")
def signing_keys() -> dict[str, Any]:
    return {RS_KID: rsa.generate_private_key(public_exponent=65537, key_size=2048)}


ALLOWED_ORIGIN = "http://localhost:3000"


@pytest.fixture(scope="session")
def verifier(signing_keys: dict[str, Any]) -> JwtVerifier:
    jwks = {"keys": [_public_jwk(signing_keys[RS_KID], RS_KID, "RS256")]}
    return JwtVerifier(
        LocalJWKClient(jwks), issuer=TEST_ISSUER, allowed_origins=frozenset({ALLOWED_ORIGIN})
    )


def _new_user_id() -> str:
    # Clerk user ids: opaque, not a uuid. Shaped like a real one so a test
    # that accidentally assumed uuid-ness would fail loudly.
    return f"user_{uuid.uuid4().hex}"


MintToken = Callable[..., str]


@pytest.fixture(scope="session")
def mint_token(signing_keys: dict[str, Any]) -> MintToken:
    """mint_token(sub, *, key=None, alg=None, **claim_overrides).
    A claim override of None removes that claim."""

    def _mint(
        sub: str | None,
        *,
        key: Any = None,
        alg: str | None = None,
        **overrides: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "azp": ALLOWED_ORIGIN,
            "iss": TEST_ISSUER,
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(
            claims,
            key if key is not None else signing_keys[RS_KID],
            algorithm=alg or "RS256",
            headers={"kid": RS_KID},
        )

    return _mint


@pytest.fixture
def client(verifier: JwtVerifier) -> Iterator[TestClient]:
    """The real app, with only the JWKS source swapped for the local one."""
    app.dependency_overrides[get_verifier] = lambda: verifier
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_verifier, None)


@pytest.fixture
def auth_header(mint_token: MintToken) -> Callable[[str], dict[str, str]]:
    return lambda user_id: {"Authorization": f"Bearer {mint_token(user_id)}"}


# ---------------------------------------------------------------- live DB


def _require_db() -> str:
    owner = os.environ.get("DATABASE_URL")
    if not owner or not os.environ.get("WORKER_DATABASE_URL"):
        pytest.skip("DATABASE_URL / WORKER_DATABASE_URL not set")
    return owner


_GRAPH_TABLES = ("blobs", "edges", "nodes", "coverage", "pass_runs")


def _delete_user_data(owner_url: str, user_ids: list[str]) -> None:
    with psycopg.connect(owner_url, connect_timeout=10) as conn:
        projects = [
            r[0]
            for r in conn.execute(
                "select id from codeaxis.projects where owner_id = any(%s::text[])", (user_ids,)
            ).fetchall()
        ]
        if projects:
            repos = [
                r[0]
                for r in conn.execute(
                    "select id from codeaxis.repos where project_id = any(%s::uuid[])",
                    (projects,),
                ).fetchall()
            ]
            if repos:
                for table in _GRAPH_TABLES:
                    conn.execute(
                        f"delete from codeaxis.{table} where repo_id = any(%s::uuid[])", (repos,)
                    )
            conn.execute(
                "delete from codeaxis.jobs where project_id = any(%s::uuid[])", (projects,)
            )
            conn.execute(
                "delete from codeaxis.repos where project_id = any(%s::uuid[])", (projects,)
            )
            conn.execute("delete from codeaxis.projects where id = any(%s::uuid[])", (projects,))
        # Cascades to any remaining codeaxis.project_memberships rows.
        conn.execute("delete from codeaxis.profiles where id = any(%s::text[])", (user_ids,))
        conn.commit()


@pytest.fixture
def make_user() -> Iterator[Callable[[], str]]:
    """Factory: create a real codeaxis.profiles row (standing in for a Clerk
    user — Clerk has no signup hook into this database, so there is nothing
    to create upstream of it) and return its id. All created users and
    everything they own are removed at teardown."""
    owner_url = _require_db()
    created: list[str] = []

    def _make() -> str:
        user_id = _new_user_id()
        with psycopg.connect(owner_url, connect_timeout=10) as conn:
            conn.execute(
                "insert into codeaxis.profiles (id, email) values (%s, %s)",
                (user_id, f"{user_id}@codeaxis.invalid"),
            )
            conn.commit()
        created.append(user_id)
        return user_id

    yield _make
    if created:
        _delete_user_data(owner_url, created)


@pytest.fixture
def db_project(make_user: Callable[[], str]) -> str:
    """A project owned by a fresh user, for tests that write repos directly."""
    with worker_connection() as conn:
        project = create_project(conn, "test project", make_user())
    return project.id


# ------------------------------------------- no live model calls from tests

@pytest.fixture(autouse=True)
def _no_live_embedding_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `VOYAGE_API_KEY` out of every test's environment.

    `load_dotenv()` above pulls in the real `.env`, which on a developer
    machine may hold a working Voyage key. Without this, any test that runs a
    full scan pass would embed for real: money spent, a network dependency in
    the suite, and repository text leaving the machine during a test run.
    Tests that need the key present set it themselves with `monkeypatch`;
    tests that need a provider inject a fake one.
    """
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
