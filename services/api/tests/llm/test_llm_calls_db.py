"""llm_calls round trip against the live Supabase database, through the real
DB-backed recorder. Skipped when WORKER_DATABASE_URL isn't set, or when the
llm_calls migration hasn't been pushed to that database yet."""

import os

import pytest
from dotenv import load_dotenv

from app.knowledge import get_llm_call, insert_llm_call, worker_connection
from app.models import LlmCall

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)


def _table_exists() -> bool:
    with worker_connection() as conn:
        row = conn.execute("select to_regclass('codeaxis.llm_calls')").fetchone()
    return row is not None and row[0] is not None


def test_insert_and_read_llm_call():
    if not _table_exists():
        pytest.skip("codeaxis.llm_calls not migrated on this database yet")

    call = LlmCall(
        role="synth",
        provider="test",
        model="claude-sonnet-5-5",
        served_model="claude-sonnet-5-5",
        operation="structured",
        attempt=2,
        status="validation_failed",
        input_tokens=1234,
        output_tokens=567,
        cost_usd=0.008138,
        latency_ms=42,
        stop_reason="end_turn",
        schema_name="Summary",
        error="points: Field required",
    )
    # The worker role is append-only on llm_calls (no delete grant), so the
    # test row is left in place; provider="test" marks it.
    with worker_connection() as conn:
        call_id = insert_llm_call(conn, call)
        stored = get_llm_call(conn, call_id)
    assert stored is not None
    assert stored.model_dump(exclude={"id"}) == call.model_dump(exclude={"id"})
