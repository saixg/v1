"""Clerk JWT verification, without a live Clerk instance or database.

Tokens are signed with a locally generated RS256 key and verified by the
real `JwtVerifier` through a real `PyJWKClient` (see conftest.py). Every
protected route must 401 before it touches the database, so the route sweep
below needs no DB either.
"""

from __future__ import annotations

import base64
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth import (
    AuthError,
    CurrentUser,
    JwtVerifier,
    frontend_api_from_publishable_key,
    get_verifier,
)
from app.main import app

# Must match conftest.py (conftest modules aren't importable under
# --import-mode=importlib).
RS_KID = "test-rs256"
TEST_ISSUER = "https://test-instance.clerk.invalid"
ALLOWED_ORIGIN = "http://localhost:3000"


@pytest.fixture
def whoami(verifier: JwtVerifier) -> TestClient:
    """A minimal app using the real `current_user` dependency."""
    probe = FastAPI()

    @probe.get("/whoami")
    def _whoami(user: CurrentUser) -> dict[str, str]:
        return {"id": user.id}

    probe.dependency_overrides[get_verifier] = lambda: verifier
    return TestClient(probe)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------- verifier


def test_valid_token_yields_sub(whoami, mint_token):
    user_id = "user_abc123"
    resp = whoami.get("/whoami", headers=_bearer(mint_token(user_id)))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": user_id}


def test_missing_token_is_401(whoami):
    resp = whoami.get("/whoami")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz", "Bearer a.b.c"],
)
def test_malformed_authorization_is_401(whoami, header):
    assert whoami.get("/whoami", headers={"Authorization": header}).status_code == 401


def _invalid_tokens(mint_token) -> dict[str, str]:
    sub = "user_abc123"
    now = int(time.time())
    attacker_rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker_ec_key = ec.generate_private_key(ec.SECP256R1())
    return {
        "expired": mint_token(sub, iat=now - 600, exp=now - 60),
        "wrong_azp": mint_token(sub, azp="https://evil.example"),
        "wrong_issuer": mint_token(sub, iss="https://evil.example"),
        "no_exp": mint_token(sub, exp=None),
        "no_sub": mint_token(None),
        "empty_sub": mint_token(""),
        # Right kid (so the real public key is found), wrong private key:
        # signature must not verify.
        "forged_signature": mint_token(sub, key=attacker_rsa_key),
        # A kid the JWKS doesn't contain, otherwise a well-formed RS256 token.
        "unknown_kid": jwt.encode(
            {"sub": sub, "iss": TEST_ISSUER, "exp": now + 300},
            attacker_rsa_key,
            algorithm="RS256",
            headers={"kid": "rotated-away"},
        ),
        # Symmetric tokens are never accepted, whatever the secret.
        "hs256": jwt.encode(
            {"sub": sub, "iss": TEST_ISSUER, "exp": now + 300},
            "a-shared-secret-that-is-long-enough-for-hs256",
            algorithm="HS256",
            headers={"kid": RS_KID},
        ),
        # Clerk is RS256-only; a correctly-signed ES256 token must still be
        # rejected on algorithm alone.
        "es256": jwt.encode(
            {"sub": sub, "iss": TEST_ISSUER, "exp": now + 300},
            attacker_ec_key,
            algorithm="ES256",
            headers={"kid": RS_KID},
        ),
        "alg_none": _unsigned_token({"sub": sub, "iss": TEST_ISSUER, "exp": now + 300}),
    }


def _unsigned_token(claims: dict) -> str:
    def b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'typ': 'JWT', 'kid': RS_KID})}.{b64(claims)}."


@pytest.mark.parametrize(
    "case",
    [
        "expired",
        "wrong_azp",
        "wrong_issuer",
        "no_exp",
        "no_sub",
        "empty_sub",
        "forged_signature",
        "unknown_kid",
        "hs256",
        "es256",
        "alg_none",
    ],
)
def test_invalid_token_is_401(whoami, mint_token, case):
    token = _invalid_tokens(mint_token)[case]
    resp = whoami.get("/whoami", headers=_bearer(token))
    assert resp.status_code == 401, (case, resp.text)
    # The rejection reason is not leaked to the caller.
    assert resp.json() == {"detail": "invalid or expired token"}


def test_azp_absent_is_allowed(whoami, mint_token):
    """`azp` is optional in Clerk's own token shape; a token that simply
    omits it must not be rejected for that reason."""
    resp = whoami.get("/whoami", headers=_bearer(mint_token("user_abc123", azp=None)))
    assert resp.status_code == 200, resp.text


def test_verifier_raises_auth_error_directly(verifier, mint_token):
    with pytest.raises(AuthError):
        verifier.verify(mint_token("user_abc123", azp="https://evil.example"))


def test_unconfigured_clerk_key_is_503_not_open(monkeypatch, mint_token):
    """Without CLERK_PUBLISHABLE_KEY the API must fail closed, not skip auth."""
    monkeypatch.delenv("CLERK_PUBLISHABLE_KEY", raising=False)
    probe = FastAPI()

    @probe.get("/whoami")
    def _whoami(user: CurrentUser) -> dict[str, str]:
        return {"id": user.id}

    resp = TestClient(probe).get("/whoami", headers=_bearer(mint_token("user_abc123")))
    assert resp.status_code == 503


def test_frontend_api_derived_from_publishable_key():
    # base64("instance.clerk.accounts.dev$")
    encoded = base64.b64encode(b"instance.clerk.accounts.dev$").decode().rstrip("=")
    assert (
        frontend_api_from_publishable_key(f"pk_test_{encoded}")
        == "https://instance.clerk.accounts.dev"
    )


def test_for_clerk_points_at_frontend_api_jwks():
    encoded = base64.b64encode(b"instance.clerk.accounts.dev$").decode().rstrip("=")
    v = JwtVerifier.for_clerk(f"pk_test_{encoded}")
    assert v._issuer == "https://instance.clerk.accounts.dev"
    assert isinstance(v._keys, jwt.PyJWKClient)
    assert v._keys.uri == "https://instance.clerk.accounts.dev/.well-known/jwks.json"


# ------------------------------------------------------------- route sweep

_PUBLIC = {"/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _protected_routes() -> list[tuple[str, str]]:
    routes = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in _PUBLIC:
            continue
        for method in route.methods or ():
            path = (
                route.path.replace("{repo_id}", str(uuid.uuid4()))
                .replace("{job_id}", str(uuid.uuid4()))
                .replace("{path:path}", "a/b.py")
                .replace("{sid}", "1")
                .replace("{node_id}", "1")
            )
            routes.append((method, path))
    return sorted(routes)


def test_route_sweep_covers_repos_projects_and_jobs():
    paths = {p for _, p in _protected_routes()}
    assert any(p.startswith("/repos/") for p in paths)
    assert "/projects" in paths
    assert any(p.startswith("/jobs/") for p in paths)


@pytest.mark.parametrize(("method", "path"), _protected_routes())
def test_every_non_public_route_rejects_missing_and_bad_tokens(client, method, path):
    assert client.request(method, path).status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert client.request(method, path, headers=bad).status_code == 401


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200
