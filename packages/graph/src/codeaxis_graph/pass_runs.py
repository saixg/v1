"""codeaxis.pass_runs: lifecycle of one analyzer run against one commit.

Lets re-analysis be a diff against prior runs instead of a blind re-scan —
see the `pass_runs` note in docs/04-graph-schema.md.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import class_row

from codeaxis_contracts import PassRun, PassRunStatus

_PASS_RUN_COLUMNS = "id, repo_id, commit_sha, analyzer, analyzer_version, status, error"


def start_pass_run(conn: psycopg.Connection, run: PassRun) -> PassRun:
    with conn.cursor(row_factory=class_row(PassRun)) as cur:
        row = cur.execute(
            """
            insert into codeaxis.pass_runs (repo_id, commit_sha, analyzer, analyzer_version, status)
            values (%s, %s, %s, %s, %s)
            returning id, repo_id, commit_sha, analyzer, analyzer_version, status, error
            """,
            (run.repo_id, run.commit_sha, run.analyzer, run.analyzer_version, run.status.value),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


def finish_pass_run(
    conn: psycopg.Connection, run_id: int, status: PassRunStatus, error: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update codeaxis.pass_runs
            set status = %s, error = %s, finished_at = now()
            where id = %s
            """,
            (status.value, error, run_id),
        )
    conn.commit()


def list_pass_runs(conn: psycopg.Connection, repo_id: str) -> list[PassRun]:
    with conn.cursor(row_factory=class_row(PassRun)) as cur:
        return cur.execute(
            f"select {_PASS_RUN_COLUMNS} from codeaxis.pass_runs where repo_id = %s "
            "order by started_at",
            (repo_id,),
        ).fetchall()
