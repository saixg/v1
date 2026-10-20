"""Connection helpers. Reads the role-scoped URLs from the environment —
WORKER_DATABASE_URL (read/write) for app.analysis / app.repository writers,
API_DATABASE_URL (read-only) for the FastAPI read routes. Never DATABASE_URL:
that is the project owner connection used only by migrations and
scripts/setup_db_roles.py.

Pooling (Phase 6 hardening)
---------------------------
These two context managers used to open a brand-new TCP + TLS + Postgres
session per call, and hundreds of call sites call them — a request that reads
the graph and then writes a usage row paid for two full handshakes. They are
now backed by one `psycopg_pool.ConnectionPool` per role, created lazily.
`psycopg[binary,pool]` was already the declared dependency, so this adds
nothing new to install.

Two properties were preserved deliberately, because the call sites depend on
them and must not be touched:

- **The signatures and the yielded object are unchanged**: still
  `with api_connection() as conn:`, still a plain `psycopg.Connection`.
- **Exit does not commit.** `ConnectionPool.connection()` commits on a clean
  exit, but `conn.close()` — what these helpers used to do — discards
  uncommitted work. Code here commits explicitly (see
  `app.knowledge.llm_calls.insert_llm_call`), and some call sites and tests
  rely on an uncommitted statement *not* persisting. So the pool is driven
  through `getconn`/`putconn` with an explicit `rollback()` on the way back,
  which reproduces the old semantics exactly rather than quietly changing
  what a missing `commit()` means.

Nothing connects at import time: the pool is built on first use and with
`min_size=0`, so `import app.main` in a test with no database configured
still works, and an idle API machine holds no connections.

Transient failures
------------------
Two different things, handled differently on purpose:

- **A connection that died while it sat idle in the pool** (the common case
  behind a pooler or a NAT idle timeout). The pool is configured with
  `check=ConnectionPool.check_connection`, so a stale connection is
  discarded and replaced *before* it is handed to a caller. One extra
  round trip per checkout buys the caller never seeing an
  `OperationalError` it cannot distinguish from a real outage.
- **A statement that failed mid-flight.** Only `retry_read` retries, and it
  is for read-only, idempotent queries. There is deliberately no retry
  wrapper around these context managers: the block a caller runs inside one
  may have written and may have committed part of its work, and re-running
  it could apply that write twice. CLAUDE.md's rule is to admit a gap rather
  than produce a plausible wrong result; silently replaying a possibly
  half-committed transaction is the write-side version of a guessed edge.
  A caller that wants a retryable write must make it idempotent and ask for
  the retry itself.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

CONNECT_TIMEOUT_SECONDS = 10
# How long a caller waits for a free slot before giving up. Matches the
# connect timeout: waiting longer than it would take to open a new connection
# is worse than failing and letting the caller's own timeout fire.
POOL_WAIT_TIMEOUT_SECONDS = 10
# An idle connection above min_size is closed after this long. min_size is 0,
# so an idle machine drifts down to zero connections.
POOL_MAX_IDLE_SECONDS = 300
# Recycled regardless of health: bounds the damage from a server-side
# change (role grants, a failover) that an open session would not notice.
POOL_MAX_LIFETIME_SECONDS = 1800

# Sizing. These are per *process*, and gunicorn/uvicorn workers multiply them,
# as does every Fly machine — so they are set for a small container and for a
# Supabase pooler shared with migrations and psql, not for throughput. The API
# role serves concurrent HTTP reads from FastAPI's threadpool; the worker role
# is used by a handful of analysis passes that are already serialized per job.
DEFAULT_POOL_MAX = {"API_DATABASE_URL": 10, "WORKER_DATABASE_URL": 5}
_POOL_MAX_ENV = {
    "API_DATABASE_URL": "CODEAXIS_DB_POOL_MAX_API",
    "WORKER_DATABASE_URL": "CODEAXIS_DB_POOL_MAX_WORKER",
}

_pools: dict[str, ConnectionPool[psycopg.Connection]] = {}
_pools_lock = threading.Lock()


def _pool_max_size(env_var: str) -> int:
    raw = os.environ.get(_POOL_MAX_ENV[env_var], "").strip()
    if not raw:
        return DEFAULT_POOL_MAX[env_var]
    try:
        size = int(raw)
    except ValueError:
        raise RuntimeError(f"{_POOL_MAX_ENV[env_var]} must be an integer, got {raw!r}") from None
    if size < 1:
        raise RuntimeError(f"{_POOL_MAX_ENV[env_var]} must be at least 1, got {size}")
    return size


def _pool(env_var: str) -> ConnectionPool[psycopg.Connection]:
    """The process-wide pool for one role, built on first use."""
    pool = _pools.get(env_var)
    if pool is not None:
        return pool
    with _pools_lock:
        pool = _pools.get(env_var)
        if pool is not None:
            return pool
        url = os.environ.get(env_var)
        if not url:
            raise RuntimeError(f"{env_var} is not set. Copy .env.example to .env and fill it in.")
        pool = ConnectionPool(
            url,
            connection_class=psycopg.Connection,
            name=env_var.lower(),
            min_size=0,
            max_size=_pool_max_size(env_var),
            timeout=POOL_WAIT_TIMEOUT_SECONDS,
            max_idle=POOL_MAX_IDLE_SECONDS,
            max_lifetime=POOL_MAX_LIFETIME_SECONDS,
            kwargs={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
            check=ConnectionPool.check_connection,
            open=False,
        )
        pool.open()
        _pools[env_var] = pool
        return pool


@contextmanager
def _pooled(env_var: str) -> Iterator[psycopg.Connection]:
    pool = _pool(env_var)
    conn = pool.getconn()
    try:
        yield conn
    finally:
        # Same effect the old `conn.close()` had on uncommitted work, and it
        # is also what makes the connection safe for the next caller: a
        # left-open transaction would otherwise hold locks and a snapshot for
        # as long as the connection stayed in the pool. A connection that is
        # already broken cannot roll back; putconn discards it either way.
        with contextlib.suppress(psycopg.Error):
            conn.rollback()
        pool.putconn(conn)


@contextmanager
def worker_connection() -> Iterator[psycopg.Connection]:
    with _pooled("WORKER_DATABASE_URL") as conn:
        yield conn


@contextmanager
def api_connection() -> Iterator[psycopg.Connection]:
    with _pooled("API_DATABASE_URL") as conn:
        yield conn


def close_pools() -> None:
    """Close every pool. For process shutdown and for tests that change the
    database URLs — a pool caches its conninfo, so a changed env var is only
    picked up after this."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


# ----------------------------------------------------------- read retries

RETRYABLE_READ_ERRORS = (psycopg.OperationalError,)
READ_RETRY_ATTEMPTS = 3
READ_RETRY_BASE_DELAY_SECONDS = 0.05


def retry_read[T](fn: Callable[[], T], *, attempts: int = READ_RETRY_ATTEMPTS) -> T:
    """Run a **read-only, idempotent** query function, retrying it on a
    connection-level failure with exponential backoff.

    Only safe because the caller's `fn` neither writes nor commits: running it
    twice is indistinguishable from running it once. Do not reach for this to
    retry a write — see this module's docstring. `psycopg.OperationalError`
    is the class that covers a dropped connection, a pooler that closed the
    session, and a failover; a `ProgrammingError` or `IntegrityError` is a bug
    or a constraint and is re-raised immediately.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except RETRYABLE_READ_ERRORS as exc:
            last = exc
            if attempt == attempts - 1:
                break
            time.sleep(READ_RETRY_BASE_DELAY_SECONDS * (2**attempt))
    assert last is not None
    raise last
