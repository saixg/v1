"""codeaxis.llm_calls: one row per model-provider request made by app.llm.

Usage and cost only — never prompt or completion text (build-plan.md Part 0.3:
secrets never reach logs; repo text is untrusted). Written by the worker
role; the api role may read it for cost/failure observability (Part 11).
"""

from __future__ import annotations

import psycopg
from psycopg.rows import class_row

from app.models import LlmCall

_LLM_CALL_COLUMNS = (
    "id, role, provider, model, served_model, operation, attempt, status, "
    "input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, "
    "cost_usd::float8 as cost_usd, latency_ms, stop_reason, request_id, schema_name, error, "
    "project_id, repo_id, job_id"
)


def insert_llm_call(conn: psycopg.Connection, call: LlmCall) -> int:
    """Insert one usage row, returning its assigned id."""
    with conn.cursor() as cur:
        row = cur.execute(
            """
            insert into codeaxis.llm_calls (
                role, provider, model, served_model, operation, attempt, status,
                input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens,
                cost_usd, latency_ms, stop_reason, request_id, schema_name, error,
                project_id, repo_id, job_id
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                call.role,
                call.provider,
                call.model,
                call.served_model,
                call.operation,
                call.attempt,
                call.status,
                call.input_tokens,
                call.output_tokens,
                call.cache_read_input_tokens,
                call.cache_creation_input_tokens,
                call.cost_usd,
                call.latency_ms,
                call.stop_reason,
                call.request_id,
                call.schema_name,
                call.error,
                call.project_id,
                call.repo_id,
                call.job_id,
            ),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def get_llm_call(conn: psycopg.Connection, call_id: int) -> LlmCall | None:
    with conn.cursor(row_factory=class_row(LlmCall)) as cur:
        return cur.execute(
            f"select {_LLM_CALL_COLUMNS} from codeaxis.llm_calls where id = %s", (call_id,)
        ).fetchone()
