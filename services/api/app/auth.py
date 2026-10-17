"""Clerk JWT verification and project-membership authorization.

build-plan.md Part 2: "FastAPI verifies the [identity provider's] JWT" and
"app-layer project membership checks **and** RLS". FastAPI's database roles
are not scoped to an end user, so the dependencies here are the primary gate.

Tokens are verified against the Clerk instance's public JWKS
(`{frontend_api}/.well-known/jwks.json`) — Clerk's RS256 signing keys —
never against a shared secret. The frontend API host is derived from the
publishable key rather than configured separately, so there is nothing to
keep in sync by hand: Clerk publishable keys are `pk_(test|live)_<base64>`,
where the base64 payload decodes to `"<frontend-api-host>$"`.

Testability: the verifier is itself a FastAPI dependency (`get_verifier`).
Tests override it with a `JwtVerifier` built from a locally generated
keypair, so the real verification code path runs without a live Clerk
instance.
"""

from __future__ import annotations

import base64
import binascii
import os
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any, Protocol

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.knowledge import api_connection, get_membership_role, get_repo
from app.models import ProjectRole, Repo

ALLOWED_ALGORITHMS = ("RS256",)
JWKS_CACHE_SECONDS = 600


class SigningKeySource(Protocol):
    """Anything that maps a JWT to its verification key. `jwt.PyJWKClient`
    satisfies this (it fetches and caches the JWKS, matching on `kid`)."""

    def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK: ...


@dataclass(frozen=True)
class AuthUser:
    id: str  # the token's `sub`: the Clerk user id (e.g. "user_2abc..."),
    # opaque and not a uuid — it is only ever used as a key, never parsed.
    claims: dict[str, Any] = field(repr=False)


class AuthError(Exception):
    """The token is missing, malformed, expired, or not ours."""


def frontend_api_from_publishable_key(publishable_key: str) -> str:
    """Decode a Clerk publishable key (`pk_test_...` / `pk_live_...`) to its
    instance's Frontend API URL, the same way Clerk's own SDKs derive it when
    no explicit Frontend API URL is configured."""
    try:
        _, _, encoded = publishable_key.split("_", 2)
        padded = encoded + "=" * (-len(encoded) % 4)
        host = base64.b64decode(padded).decode("ascii").rstrip("$")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"malformed Clerk publishable key: {publishable_key!r}") from exc
    if not host:
        raise ValueError(f"malformed Clerk publishable key: {publishable_key!r}")
    return f"https://{host}"


class JwtVerifier:
    def __init__(
        self,
        keys: SigningKeySource,
        issuer: str,
        allowed_origins: frozenset[str] = frozenset(),
        leeway_seconds: int = 0,
    ) -> None:
        self._keys = keys
        self._issuer = issuer
        self._allowed_origins = allowed_origins
        self._leeway = leeway_seconds

    @classmethod
    def for_clerk(
        cls, publishable_key: str, allowed_origins: frozenset[str] = frozenset()
    ) -> JwtVerifier:
        issuer = frontend_api_from_publishable_key(publishable_key)
        client = jwt.PyJWKClient(
            f"{issuer}/.well-known/jwks.json",
            cache_jwk_set=True,
            lifespan=JWKS_CACHE_SECONDS,
            timeout=10,
        )
        return cls(client, issuer=issuer, allowed_origins=allowed_origins)

    def verify(self, token: str) -> AuthUser:
        try:
            # Reject a non-RS256 alg before any key lookup. Clerk session
            # tokens are always RS256; accepting anything else (including
            # Supabase's ES256) would widen the trust surface for no reason.
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ALLOWED_ALGORITHMS:
                raise AuthError("unsupported token algorithm")
            signing_key = self._keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                issuer=self._issuer,
                leeway=self._leeway,
                # Clerk session tokens carry no fixed `aud`; `azp` (checked
                # below) is Clerk's own equivalent of an audience check.
                options={"require": ["exp", "sub", "iss"]},
            )
        except jwt.PyJWKClientConnectionError:
            # The JWKS endpoint is unreachable: that's our outage, not a bad
            # token. Surfaced as 503 by `current_user`.
            raise
        except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
            raise AuthError(str(exc)) from exc

        azp = claims.get("azp")
        if azp is not None and self._allowed_origins and azp not in self._allowed_origins:
            raise AuthError("token's authorized party is not this app")

        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub.strip():
            raise AuthError("token has no subject")
        return AuthUser(id=sub, claims=claims)


@lru_cache(maxsize=1)
def _clerk_verifier(publishable_key: str, allowed_origins: frozenset[str]) -> JwtVerifier:
    return JwtVerifier.for_clerk(publishable_key, allowed_origins)


def get_verifier() -> JwtVerifier:
    publishable_key = os.environ.get("CLERK_PUBLISHABLE_KEY", "").strip()
    if not publishable_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is not configured")
    allowed_origins = frozenset(
        o.strip() for o in os.environ.get("CLERK_ALLOWED_ORIGINS", "").split(",") if o.strip()
    )
    return _clerk_verifier(publishable_key, allowed_origins)


_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[JwtVerifier, Depends(get_verifier)],
) -> AuthUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing bearer token", headers=_UNAUTHORIZED_HEADERS
        )
    try:
        return verifier.verify(credentials.credentials)
    except AuthError:
        # The reason is deliberately not echoed back to the caller.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid or expired token",
            headers=_UNAUTHORIZED_HEADERS,
        ) from None
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "could not reach the token key set"
        ) from None


CurrentUser = Annotated[AuthUser, Depends(current_user)]


# ------------------------------------------------------------- membership


def require_uuid(value: str, what: str) -> None:
    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found") from None


def require_project_member(project_id: str, user: AuthUser) -> ProjectRole:
    """403 unless `user` is a member of `project_id`."""
    try:
        uuid.UUID(project_id)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this project") from None
    with api_connection() as conn:
        role = get_membership_role(conn, project_id, user.id)
    if role is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this project")
    return role


def repo_access(repo_id: str, user: CurrentUser) -> Repo:
    """Dependency for every `/repos/{repo_id}/...` route: 401 without a valid
    token (via `current_user`), 404 for an unknown repo, 403 unless the caller
    is a member of the project that owns it."""
    require_uuid(repo_id, "repo")
    with api_connection() as conn:
        repo = get_repo(conn, repo_id)
        if repo is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")
        role = (
            None if repo.project_id is None else get_membership_role(conn, repo.project_id, user.id)
        )
    if role is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this repo's project")
    return repo


RepoAccess = Annotated[Repo, Depends(repo_access)]
