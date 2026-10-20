"""Live-database checks for the pooled connections and the spend read.

Skipped without the role URLs, following the convention in
`tests/knowledge/`. These are the two things that cannot be proved against a
fake: that the pooled context managers behave exactly as the unpooled ones
did (same object, same no-commit-on-exit semantics, connections reused), and
that the ledger read and the cap column agree with what the route reports.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from dotenv import load_dotenv

from app.knowledge import api_connection, worker_connection
from app.knowledge.db import _pool, retry_read
from app.knowledge.spend import project_cost_cap_usd, project_spend_since
from app.limits.budget import month_start, read_budget_status
from app.models import LlmCall

load_dotenv()

pytestmark = pytest.mark.skipif(
    not (os.environ.get("WORKER_DATABASE_URL") and os.environ.get("API_DATABASE_URL")),
    reason="WORKER_DATABASE_URL / API_DATABASE_URL not set",
)


# ------------------------------------------------------------------- pooling


def test_both_helpers_still_yield_a_plain_connection():
    with api_connection() as conn:
        assert isinstance(conn, psycopg.Connection)
        assert conn.execute("select 1").fetchone() == (1,)
    with worker_connection() as conn:
        assert isinstance(conn, psycopg.Connection)


def test_a_connection_is_returned_to_the_pool_and_reused():
    with api_connection() as first:
        first_id = id(first)
    with api_connection() as second:
        assert id(second) == first_id, "the connection was not reused"
    stats = _pool("API_DATABASE_URL").get_stats()
    assert stats["connections_num"] >= 1
    assert stats["pool_size"] >= 1


def test_exiting_the_context_does_not_commit_uncommitted_work():
    """The semantics every existing call site was written against: without an
    explicit commit, nothing persists. `ConnectionPool.connection()` would
    have committed here, which is why this module drives getconn/putconn."""
    marker = f"pool-semantics-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        conn.execute(
            "insert into codeaxis.profiles (id, email) values (%s, %s)",
            (marker, f"{marker}@codeaxis.invalid"),
        )
        # deliberately no conn.commit()
    with api_connection() as conn:
        row = conn.execute(
            "select 1 from codeaxis.profiles where id = %s", (marker,)
        ).fetchone()
    assert row is None, "exiting the context manager must not commit"


def test_a_reused_connection_carries_no_open_transaction():
    with worker_connection() as conn:
        conn.execute("select 1")
    with worker_connection() as conn:
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_retry_read_runs_the_query_and_returns_its_value():
    assert retry_read(lambda: _one()) == 1


def _one() -> int:
    with api_connection() as conn:
        row = conn.execute("select 1").fetchone()
        assert row is not None
        return row[0]


def test_retry_read_retries_a_connection_error_and_then_gives_up():
    attempts = []

    def _flaky() -> int:
        attempts.append(1)
        if len(attempts) < 3:
            raise psycopg.OperationalError("connection reset")
        return 7

    assert retry_read(_flaky) == 7
    assert len(attempts) == 3

    def _always() -> int:
        raise psycopg.OperationalError("down")

    with pytest.raises(psycopg.OperationalError):
        retry_read(_always, attempts=2)


def test_retry_read_does_not_retry_a_programming_error():
    """A malformed query is a bug; retrying it three times only delays the
    failure. Only connection-level errors are retryable."""
    calls = []

    def _bug() -> int:
        calls.append(1)
        raise psycopg.ProgrammingError("syntax error")

    with pytest.raises(psycopg.ProgrammingError):
        retry_read(_bug)
    assert len(calls) == 1


# --------------------------------------------------------------- spend reads


@pytest.fixture
def ledger_project(db_project: str):
    """A project with three usage rows: one priced success, one priced
    failure, one unpriced call."""
    from app.knowledge import insert_llm_call

    def _call(cost: float | None, status: str) -> LlmCall:
        return LlmCall(
            role="bulk",
            provider="test",
            model="test-model",
            operation="text",
            status=status,  # type: ignore[arg-type]
            input_tokens=10,
            output_tokens=1,
            cost_usd=cost,
            latency_ms=1,
            project_id=db_project,
        )

    with worker_connection() as conn:
        for call in (_call(0.25, "succeeded"), _call(0.75, "error"), _call(None, "succeeded")):
            insert_llm_call(conn, call)
    yield db_project
    # llm_calls is append-only for the worker role by design, so cleanup goes
    # through the owner connection, like the rest of the suite's teardown.
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        conn.execute("delete from codeaxis.llm_calls where project_id = %s", (db_project,))
        conn.commit()


def test_the_ledger_read_counts_failures_and_unpriced_calls(ledger_project):
    with api_connection() as conn:
        window = project_spend_since(conn, ledger_project, month_start())
    assert window.calls == 3
    assert window.spend_usd == pytest.approx(1.0)
    assert window.failed_calls == 1, "a paid-for failure still counts towards spend"
    assert window.unpriced_calls == 1


def test_the_cap_override_column_defaults_to_null_meaning_the_deployment_default(
    db_project, monkeypatch
):
    monkeypatch.setenv("CODEAXIS_PROJECT_MONTHLY_COST_CAP_USD", "3.5")
    with api_connection() as conn:
        assert project_cost_cap_usd(conn, db_project) is None
    status = read_budget_status(db_project)
    assert status.cap_usd == 3.5
    assert status.cap_source == "deployment_default"


def test_a_per_project_override_wins_over_the_deployment_default(db_project, monkeypatch):
    monkeypatch.setenv("CODEAXIS_PROJECT_MONTHLY_COST_CAP_USD", "3.5")
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        conn.execute(
            "update codeaxis.projects set monthly_cost_cap_usd = %s where id = %s",
            (0.5, db_project),
        )
        conn.commit()
    status = read_budget_status(db_project)
    assert status.cap_usd == 0.5
    assert status.cap_source == "project"


def test_a_zero_cap_means_no_model_work_at_all(db_project):
    """`0` is a usable state: suspend a project's model spend without
    deleting it. NULL, by contrast, never means unlimited."""
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        conn.execute(
            "update codeaxis.projects set monthly_cost_cap_usd = 0 where id = %s",
            (db_project,),
        )
        conn.commit()
    status = read_budget_status(db_project)
    assert status.cap_usd == 0.0
    assert status.exceeded is True


def test_a_negative_cap_is_rejected_by_the_database(db_project):
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:  # noqa: SIM117
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "update codeaxis.projects set monthly_cost_cap_usd = -1 where id = %s",
                (db_project,),
            )


# ---------------------------------------------------------------- the route


def test_the_spend_route_reports_the_cap_and_the_honesty_counters(
    client, auth_header, ledger_project, monkeypatch
):
    monkeypatch.setenv("CODEAXIS_PROJECT_MONTHLY_COST_CAP_USD", "10")
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        owner = conn.execute(
            "select owner_id from codeaxis.projects where id = %s", (ledger_project,)
        ).fetchone()
        assert owner is not None

    response = client.get(
        f"/projects/{ledger_project}/spend", headers=auth_header(owner[0])
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["window"] == "calendar_month_utc"
    assert body["cap_usd"] == 10.0
    assert body["spend_usd"] == pytest.approx(1.0)
    assert body["remaining_usd"] == pytest.approx(9.0)
    assert body["failed_calls"] == 1
    assert body["unpriced_calls"] == 1
    # An unpriced call means the total is a lower bound, and the route says so.
    assert body["spend_is_complete"] is False
    assert body["exceeded"] is False


def test_a_non_member_cannot_read_another_project_s_spend(
    client, auth_header, make_user, db_project
):
    outsider = make_user()
    response = client.get(f"/projects/{db_project}/spend", headers=auth_header(outsider))
    assert response.status_code == 403
