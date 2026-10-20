"""Clerk JWT verification, without a live Clerk instance or database.

Tokens are signed with a locally generated RS256 key and verified by the
real `JwtVerifier` through a real `PyJWKClient` (see conftest.py). Every
protected route must 401 before it touches the database, so the route sweep
below needs no DB either.
"""

from __future__ import annotations

import base64
import json
import re
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
# Authenticated by a per-repo API token (cxa_...), not a Clerk JWT: swept separately below.
_TOKEN_AUTH_PREFIXES = ("/mcp/",)
_INTEGER_PARAMS = {"node_id", "sid"}


def _api_routes(routes, prefix: str = ""):
    """Every APIRoute reachable from `routes`, including those mounted with
    `include_router`. FastAPI 0.143 keeps an included router as a single opaque
    `_IncludedRouter` entry in `app.routes` instead of flattening it, so a sweep
    over `app.routes` alone silently skips every route that lives in
    context_api, blueprint_api and mcp_server."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            mounted = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            yield from _api_routes(inner.routes, prefix + mounted)


def _concrete(template: str) -> str:
    def fill(match: re.Match[str]) -> str:
        name, _, kind = match.group(1).partition(":")
        if kind == "path":
            return "a/b.py"
        return "1" if name in _INTEGER_PARAMS else str(uuid.uuid4())

    return re.sub(r"\{([^}]+)\}", fill, template)


def _routes_matching(token_auth: bool) -> list[tuple[str, str, str]]:
    found = []
    for template, route in _api_routes(app.routes):
        if template in _PUBLIC or template.startswith(_TOKEN_AUTH_PREFIXES) != token_auth:
            continue
        for method in route.methods or ():
            found.append((method, _concrete(template), template))
    return sorted(found)


def _protected_routes() -> list[tuple[str, str]]:
    return [(m, p) for m, p, _ in _routes_matching(token_auth=False)]


def test_route_sweep_covers_every_router_the_app_mounts():
    templates = {t for _, _, t in _routes_matching(token_auth=False)}
    assert any(t.startswith("/repos/") for t in templates)
    assert "/projects" in templates
    assert any(t.startswith("/jobs/") for t in templates)
    # The routers mounted with include_router: a sweep that cannot see these is
    # the exact failure this test exists to catch.
    assert any(t.startswith("/tasks/") for t in templates), "context_api routes not swept"
    assert any(t.startswith("/blueprints/") for t in templates), "blueprint_api routes not swept"
    assert any(t.startswith("/mcp/") for _, _, t in _routes_matching(token_auth=True))


def test_sweep_matches_the_openapi_schema_exactly():
    """The schema is generated by a different traversal than the sweep, so any
    route one can see and the other cannot shows up here."""
    # OpenAPI renders `{path:path}` as `{path}`.
    all_routes = _routes_matching(False) + _routes_matching(True)
    swept = {(m, re.sub(r":\w+\}", "}", t)) for m, _, t in all_routes}
    spec = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        for method in ops
        if path not in _PUBLIC
    }
    assert spec <= swept, f"not swept: {sorted(spec - swept)}"


@pytest.mark.parametrize(("method", "path"), _protected_routes())
def test_every_non_public_route_rejects_missing_and_bad_tokens(client, method, path):
    assert client.request(method, path).status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert client.request(method, path, headers=bad).status_code == 401


@pytest.mark.parametrize(
    ("method", "path"), [(m, p) for m, p, _ in _routes_matching(token_auth=True)]
)
@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic Zm9vOmJhcg==", "Token cxa_abc", "Bearer " + "x" * 500],
)
def test_token_authenticated_routes_reject_absent_and_malformed_credentials(
    client, method, path, header
):
    """Each of these is refused before the token store is consulted, so no
    database is needed. A well-formed but unknown token is covered in
    test_mcp.py."""
    headers = {"Authorization": header} if header is not None else {}
    resp = client.request(method, path, headers=headers, json={})
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_a_clerk_jwt_is_not_accepted_as_an_mcp_token(client, mint_token):
    """The two credential types must not be interchangeable: a user's session
    JWT carries no repo scope, so honouring it on /mcp would widen it."""
    path = f"/mcp/{uuid.uuid4()}"
    resp = client.post(path, headers=_bearer(mint_token("user_abc")), json={})
    assert resp.status_code == 401


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200
