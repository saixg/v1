"""Default UsageRecorder: one codeaxis.llm_calls row per provider attempt.

Goes through app.knowledge (the only module that knows SQL) on the worker
connection, which is the role granted insert on llm_calls.
"""

from __future__ import annotations

from app.knowledge import insert_llm_call, worker_connection
from app.models import LlmCall


def record_llm_call(call: LlmCall) -> None:
    with worker_connection() as conn:
        insert_llm_call(conn, call)
