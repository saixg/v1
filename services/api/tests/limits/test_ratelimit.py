"""The rate limiter: the counter itself, the route-to-bucket policy, and the
two dependencies — the Clerk-keyed one and the MCP token-keyed one.

No database and no model. The dependencies are exercised against a throwaway
FastAPI app so the real `Depends` wiring runs, with `app.auth.current_user`
overridden rather than a token minted: what is under test here is the
throttling, not verification (`tests/test_auth.py` owns that).
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import AuthUser, current_user
from app.limits.ratelimit import (
    DEFAULT_BUCKET,
    DEFAULT_BUCKETS,
    FixedWindowLimiter,
    RateLimit,
    bucket_for,
    enforce_mcp_rate_limit,
    enforce_rate_limit,
    limit_for,
    reset_limits,
    token_bucket_key,
)

# --------------------------------------------------------------- the counter


def test_the_window_allows_exactly_the_limit_then_refuses():
    clock = [0.0]
    limiter = FixedWindowLimiter(clock=lambda: clock[0])
    limit = RateLimit(3, 60)

    allowed = [limiter.check("k", limit).allowed for _ in range(4)]
    assert allowed == [True, True, True, False]


def test_a_refused_request_does_not_extend_the_window():
    """Hammering a closed bucket must not push the reset further out."""
    clock = [0.0]
    limiter = FixedWindowLimiter(clock=lambda: clock[0])
    limit = RateLimit(1, 60)

    assert limiter.check("k", limit).allowed
    for _ in range(50):
        assert not limiter.check("k", limit).allowed

    clock[0] = 60.0
    assert limiter.check("k", limit).allowed


def test_the_window_rolls_over():
    clock = [0.0]
    limiter = FixedWindowLimiter(clock=lambda: clock[0])
    limit = RateLimit(2, 10)

    assert limiter.check("k", limit).allowed
    assert limiter.check("k", limit).allowed
    assert not limiter.check("k", limit).allowed
    clock[0] = 10.0
    assert limiter.check("k", limit).allowed


def test_keys_are_independent():
    limiter = FixedWindowLimiter()
    limit = RateLimit(1, 60)
    assert limiter.check("a", limit).allowed
    assert limiter.check("b", limit).allowed
    assert not limiter.check("a", limit).allowed


def test_retry_after_counts_down_and_is_never_zero():
    clock = [0.0]
    limiter = FixedWindowLimiter(clock=lambda: clock[0])
    limit = RateLimit(1, 60)
    limiter.check("k", limit)

    assert limiter.check("k", limit).retry_after_seconds == 60
    clock[0] = 59.9
    assert limiter.check("k", limit).retry_after_seconds == 1


def test_expired_keys_are_pruned_so_memory_is_bounded():
    clock = [0.0]
    limiter = FixedWindowLimiter(clock=lambda: clock[0])
    limit = RateLimit(1, 1)
    for i in range(FixedWindowLimiter._PRUNE_AT):
        limiter.check(f"k{i}", limit)
    clock[0] = 10.0
    limiter.check("fresh", limit)
    assert len(limiter._windows) < FixedWindowLimiter._PRUNE_AT


# ---------------------------------------------------------------- the policy


@pytest.mark.parametrize(
    ("method", "path", "bucket"),
    [
        ("POST", "/repos/stage", "ingest"),
        ("POST", "/repos/upload", "ingest"),
        ("POST", "/repos/{repo_id}/confirm", "ingest"),
        ("POST", "/repos/{repo_id}/chat", "chat"),
        ("GET", "/repos/{repo_id}/search", "search"),
        ("POST", "/tasks/{task_id}/requirements/extract", "extract"),
        ("POST", "/blueprints/{blueprint_id}/regenerate", "blueprint"),
        # Anything not named falls back to the generous read bucket.
        ("GET", "/repos/{repo_id}/graph", DEFAULT_BUCKET),
    ],
)
def test_every_expensive_route_is_in_the_bucket_it_should_be(method, path, bucket):
    assert bucket_for(method, path) == bucket


def test_every_named_bucket_has_a_configured_limit():
    for bucket in DEFAULT_BUCKETS:
        assert limit_for(bucket).limit > 0


def test_a_limit_can_be_overridden_by_environment(monkeypatch):
    monkeypatch.setenv("CODEAXIS_RATE_LIMIT_CHAT", "2/30")
    assert limit_for("chat") == RateLimit(2, 30)


@pytest.mark.parametrize("bad", ["5", "x/30", "0/30", "5/0"])
def test_a_malformed_override_is_a_configuration_error(monkeypatch, bad):
    monkeypatch.setenv("CODEAXIS_RATE_LIMIT_CHAT", bad)
    with pytest.raises(RuntimeError):
        limit_for("chat")


# ----------------------------------------------------------- the dependency


@pytest.fixture
def limited_app() -> FastAPI:
    app = FastAPI()

    @app.post("/repos/stage", dependencies=[Depends(enforce_rate_limit)])
    def stage() -> dict:
        return {"ok": True}

    @app.get("/repos/{repo_id}/graph", dependencies=[Depends(enforce_rate_limit)])
    def graph(repo_id: str) -> dict:
        return {"ok": True}

    @app.post("/mcp/{repo_id}", dependencies=[Depends(enforce_mcp_rate_limit)])
    def mcp(repo_id: str) -> dict:
        return {"ok": True}

    return app


def _as_user(app: FastAPI, user_id: str) -> None:
    app.dependency_overrides[current_user] = lambda: AuthUser(id=user_id, claims={})


def test_a_user_over_the_ingest_limit_gets_429_with_retry_after(limited_app, monkeypatch):
    monkeypatch.setenv("CODEAXIS_RATE_LIMIT_INGEST", "2/3600")
    reset_limits()
    _as_user(limited_app, "user_a")
    client = TestClient(limited_app)

    assert client.post("/repos/stage").status_code == 200
    assert client.post("/repos/stage").status_code == 200
    blocked = client.post("/repos/stage")
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "3600"
    body = blocked.json()["detail"]
    assert body["error"] == "rate_limited"
    assert body["bucket"] == "ingest" and body["limit"] == 2


def test_the_limit_is_per_user_not_global(limited_app, monkeypatch):
    monkeypatch.setenv("CODEAXIS_RATE_LIMIT_INGEST", "1/3600")
    reset_limits()
    client = TestClient(limited_app)

    _as_user(limited_app, "user_a")
    assert client.post("/repos/stage").status_code == 200
    assert client.post("/repos/stage").status_code == 429

    _as_user(limited_app, "user_b")
    assert client.post("/repos/stage").status_code == 200


def test_buckets_do_not_share_an_allowance(limited_app, monkeypatch):
    monkeypatch.setenv("CODEAXIS_RATE_LIMIT_INGEST", "1/3600")
    reset_limits()
    _as_user(limited_app, "user_a")
    client = TestClient(limited_app)

    assert client.post("/repos/stage").status_code == 200
    assert client.post("/repos/stage").status_code == 429
    # A read draws on the `read` bucket and is unaffected.
    assert client.get("/repos/abc/graph").status_code == 200


# ------------------------------------------------------------------- the MCP


def test_the_mcp_endpoint_is_limited_on_its_own_token_not_on_clerk(
    limited_app, monkeypatch
):
    monkeypatch.setenv("CODEAXIS_RATE_LIMIT_MCP", "2/60")
    reset_limits()
    client = TestClient(limited_app)
    headers = {"Authorization": "Bearer cxa_token_one"}

    assert client.post("/mcp/repo-1", headers=headers).status_code == 200
    assert client.post("/mcp/repo-1", headers=headers).status_code == 200
    assert client.post("/mcp/repo-1", headers=headers).status_code == 429

    # A different token has its own allowance ...
    other = {"Authorization": "Bearer cxa_token_two"}
    assert client.post("/mcp/repo-1", headers=other).status_code == 200
    # ... and so does the same token against a different repo.
    assert client.post("/mcp/repo-2", headers=headers).status_code == 200


def test_the_mcp_key_never_contains_the_raw_token():
    key = token_bucket_key("repo-1", "Bearer cxa_supersecret")
    assert "cxa_supersecret" not in key
    assert key.startswith("mcp:repo-1:")
    # Stable, so a long-lived token keeps one bucket across requests.
    assert key == token_bucket_key("repo-1", "Bearer cxa_supersecret")


def test_a_missing_or_malformed_mcp_token_is_still_throttled(limited_app, monkeypatch):
    """Brute-forcing the per-repo token must itself be rate limited."""
    monkeypatch.setenv("CODEAXIS_RATE_LIMIT_MCP", "1/60")
    reset_limits()
    client = TestClient(limited_app)
    assert client.post("/mcp/repo-1").status_code == 200
    assert client.post("/mcp/repo-1").status_code == 429
