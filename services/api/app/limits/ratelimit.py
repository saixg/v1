"""Request rate limiting on the expensive routes (build-plan.md Part 10
Phase 6 "rate limits").

Honesty first: this limiter is **per process, not global.**
--------------------------------------------------------
The counters live in this process's memory. The API is deployed to Fly.io,
where more than one machine can run, and each uvicorn worker is its own
process. So with M machines running W workers each, the limit a client
actually experiences is up to `M * W * limit`, not `limit`.

That is a real weakness and it is marked as one below, not papered over. It
was still chosen over a Postgres-backed counter:

- A Postgres counter costs a write (an upsert, plus WAL, plus eventual
  vacuum) on the hot path of *every* request to a protected route, against
  the same database the request is about. A rate limiter that makes the
  database busier is a strange way to protect the database.
- It would not be exact either without a lock or a serializable retry, so the
  honest comparison is "approximate and free" against "approximate, slower,
  and a new failure mode" — an unreachable database would have to either fail
  requests or fall open.
- It needs no new dependency. `slowapi` was considered and rejected: what it
  adds over the forty lines here is a Redis/memcached backend and a decorator
  API, and this repo adds dependencies deliberately (`anthropic`, `networkx`,
  `detect-secrets` each had a stated reason). "Per-machine counters" is not a
  reason to take a dependency; "a shared store" is, and when that day comes
  the dependency to take is a Redis client, not a wrapper.

What this does buy, even per machine: a single client cannot loop on repo
import or chat and exhaust a machine's threadpool, its model budget or its
database pool. The absolute ceiling across the fleet is enforced elsewhere —
by the cost cap in `app/limits/budget.py`, which *is* global because it reads
a shared ledger. That division is the design: the rate limiter protects a
machine, the cost cap protects the bill.

The weakness is marked `# STUB(CodeAxis):` at the limiter itself.

Keys and identity
-----------------
Clerk-authenticated routes are keyed on the verified `sub` from
`app.auth.CurrentUser`. Keying on an *unverified* token or on a parsed-but-
unverified `sub` would be forgeable — a caller could rotate the value and get
a fresh allowance per request — so the limiter is a dependency that runs
after verification rather than middleware that runs before it. Keying on the
bearer token itself is no good either: Clerk session tokens are short-lived
and refresh, so the bucket would reset every minute.

The MCP endpoint (`app/mcp_server.py`) authenticates with a long-lived
per-repo bearer token and never sees a Clerk identity, so it gets its own
limiter keyed on `sha256(token)` scoped to the repo in the path. That is easy
to overlook and is exactly the surface a leaked token would be abused
through. The raw token is never used as a key and never logged.
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.auth import CurrentUser

# ------------------------------------------------------------------ policy


@dataclass(frozen=True)
class RateLimit:
    """`limit` requests per `window_seconds`, per key."""

    limit: int
    window_seconds: int


# Named buckets. Numbers are chosen per unit of cost, not uniformly:
# - `ingest` clones or unpacks a repository and writes a whole graph. Hourly,
#   because the abuse case is "register a hundred repos", not "click twice".
# - `chat` and `extract` each make model calls; the cost cap is the real
#   ceiling, this keeps a client from reaching it in thirty seconds.
# - `search` and `read` are database reads; generous, but bounded so a loop
#   cannot drain the connection pool.
# - `mcp` is an agent's tool loop, which is legitimately chatty.
DEFAULT_BUCKETS: dict[str, RateLimit] = {
    "ingest": RateLimit(10, 3600),
    "chat": RateLimit(15, 60),
    "search": RateLimit(60, 60),
    "extract": RateLimit(30, 3600),
    "blueprint": RateLimit(60, 3600),
    "mcp": RateLimit(120, 60),
    "read": RateLimit(300, 60),
}

# Which bucket a route falls in, matched on (method, FastAPI path template).
# Kept here rather than as a decorator on each route so that the policy for
# the whole HTTP surface is readable in one place, and so the routers owned by
# other modules need no edit to be protected.
_ROUTE_BUCKETS: dict[tuple[str, str], str] = {
    ("POST", "/repos/stage"): "ingest",
    ("POST", "/repos/upload"): "ingest",
    ("POST", "/repos/{repo_id}/confirm"): "ingest",
    ("POST", "/repos/{repo_id}/chat"): "chat",
    ("GET", "/repos/{repo_id}/search"): "search",
    ("POST", "/tasks/{task_id}/requirements/extract"): "extract",
    ("POST", "/tasks/{task_id}/context"): "extract",
    ("POST", "/tasks/{task_id}/documents"): "extract",
    ("POST", "/repos/{repo_id}/blueprints"): "blueprint",
    ("POST", "/blueprints/{blueprint_id}/generate"): "blueprint",
    ("POST", "/blueprints/{blueprint_id}/regenerate"): "blueprint",
    ("POST", "/blueprints/{blueprint_id}/export"): "blueprint",
}

# Everything else on a protected router: a read. Writes that are cheap (an
# edit, a decision) land here too, which is intentional -- they are bounded by
# the same ceiling as a read because they cost about as much.
DEFAULT_BUCKET = "read"

_ENV_PREFIX = "CODEAXIS_RATE_LIMIT_"


def bucket_for(method: str, path_template: str) -> str:
    return _ROUTE_BUCKETS.get((method.upper(), path_template), DEFAULT_BUCKET)


def _parse_override(raw: str, name: str) -> RateLimit:
    """`"<limit>/<window_seconds>"`, e.g. `CODEAXIS_RATE_LIMIT_CHAT=5/60`."""
    try:
        limit_text, window_text = raw.split("/", 1)
        limit, window = int(limit_text), int(window_text)
    except ValueError:
        raise RuntimeError(
            f"{_ENV_PREFIX}{name.upper()} must look like '<count>/<seconds>', got {raw!r}"
        ) from None
    if limit < 1 or window < 1:
        raise RuntimeError(f"{_ENV_PREFIX}{name.upper()} must be positive, got {raw!r}")
    return RateLimit(limit, window)


def limit_for(bucket: str) -> RateLimit:
    """The configured limit for a bucket, env override first."""
    raw = os.environ.get(f"{_ENV_PREFIX}{bucket.upper()}", "").strip()
    if raw:
        return _parse_override(raw, bucket)
    return DEFAULT_BUCKETS[bucket]


# ----------------------------------------------------------------- limiter


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class FixedWindowLimiter:
    """Fixed-window counters, one per key, in this process's memory.

    Fixed window rather than sliding or token bucket: it is the only one of
    the three whose state is two numbers and whose behaviour can be stated in
    a sentence a user can be told ("N per minute, resetting on the minute").
    Its known flaw is the boundary burst — up to `2 * limit` across two
    adjacent windows — which for these buckets costs at most one extra
    window's worth of work and is not worth sliding-window bookkeeping.

    Thread-safe: FastAPI runs the sync routes here in a threadpool, so these
    counters really are touched concurrently.
    """

    # Prune expired keys once the table passes this size, so a long-running
    # machine's memory is bounded by its active callers rather than by every
    # caller it has ever seen.
    _PRUNE_AT = 4096

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (window_started_at, count, window_seconds)
        self._windows: dict[str, tuple[float, int, int]] = {}

    def check(self, key: str, limit: RateLimit) -> RateLimitDecision:
        """Count one request against `key` and say whether it may proceed.

        A refused request is **not** counted: the window is not extended by
        rejections, so a client that keeps hammering a closed bucket still
        gets in as soon as the window rolls over.
        """
        now = self._clock()
        with self._lock:
            if len(self._windows) >= self._PRUNE_AT:
                self._prune(now)
            started, count, _ = self._windows.get(key, (now, 0, limit.window_seconds))
            if now - started >= limit.window_seconds:
                started, count = now, 0
            if count >= limit.limit:
                retry_after = max(1, math.ceil(limit.window_seconds - (now - started)))
                return RateLimitDecision(False, 0, retry_after)
            self._windows[key] = (started, count + 1, limit.window_seconds)
            return RateLimitDecision(True, limit.limit - (count + 1), 0)

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()

    def _prune(self, now: float) -> None:
        self._windows = {
            key: entry
            for key, entry in self._windows.items()
            if now - entry[0] < entry[2]
        }


# STUB(CodeAxis): these counters are per process. The limit is therefore
# per-machine, not global: with M Fly machines running W uvicorn workers each,
# a client's effective allowance is up to M * W * limit. Making it global needs
# a shared store (Redis/Upstash, or a `codeaxis.rate_limit_counters` table with
# an atomic upsert) swapped in behind `FixedWindowLimiter`. Until then, no copy
# anywhere -- API response, docs or UI -- may describe these as global limits.
_limiter = FixedWindowLimiter()


def limiter() -> FixedWindowLimiter:
    """The process-wide limiter. A function so tests can reset it without
    reaching into module state."""
    return _limiter


def reset_limits() -> None:
    _limiter.reset()


# ------------------------------------------------------------ dependencies


def _too_many(bucket: str, decision: RateLimitDecision, limit: RateLimit) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        {
            "error": "rate_limited",
            "detail": (
                f"too many {bucket} requests: the limit is {limit.limit} per "
                f"{limit.window_seconds}s. Retry in {decision.retry_after_seconds}s."
            ),
            "bucket": bucket,
            "limit": limit.limit,
            "window_seconds": limit.window_seconds,
            "retry_after_seconds": decision.retry_after_seconds,
        },
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


def enforce_rate_limit(request: Request, user: CurrentUser) -> None:
    """Throttle one Clerk-authenticated request, keyed on the verified `sub`.

    Takes `CurrentUser` so it cannot run before verification — see the module
    docstring on why an unverified key is worthless. Applied per route in
    `app/main.py` and to whole routers at include time, with `bucket_for`
    deciding which allowance the route draws from.
    """
    bucket = bucket_for(request.method, _route_template(request))
    limit = limit_for(bucket)
    decision = limiter().check(f"{bucket}:user:{user.id}", limit)
    if not decision.allowed:
        raise _too_many(bucket, decision, limit)


def token_bucket_key(repo_id: str, authorization: str | None) -> str:
    """The MCP limiter's key: the repo plus a hash of the presented token.

    Hashed because the raw token is a credential; the key ends up in a
    process-wide dict and in nothing else, but a secret with no reason to be
    stored in a second place does not get stored in a second place.
    """
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    return f"mcp:{repo_id}:{digest}"


def enforce_mcp_rate_limit(repo_id: str, request: Request) -> None:
    """Throttle the MCP endpoint, which has no Clerk identity at all.

    Runs *before* the token is verified, unlike the Clerk dependency, and that
    is the right order here: the token is long-lived and stable, so hashing
    the presented value gives a usable key whether or not it turns out to be
    valid — which also means a brute-force attempt against the token is itself
    throttled. The reply is an HTTP 429, not a JSON-RPC error object: the
    request never reached the JSON-RPC layer, and MCP's transport is HTTP.
    """
    limit = limit_for("mcp")
    decision = limiter().check(
        token_bucket_key(repo_id, request.headers.get("authorization")), limit
    )
    if not decision.allowed:
        raise _too_many("mcp", decision, limit)


RateLimited = Annotated[None, Depends(enforce_rate_limit)]
