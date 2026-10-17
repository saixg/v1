"""codeaxis.jobs: platform job tracking for GET /jobs/{id}.

Generic and user-facing, unlike codeaxis.pass_runs (one analyzer run against
one commit). A scan job is created when the user confirms a scan and is moved
queued -> running -> succeeded|failed by the job runner in app.analysis.jobs.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import class_row

from app.models import Job, JobKind, JobStatus

_JOB_COLUMNS = "id, project_id, repo_id, kind, status, error, created_at, updated_at"


class JobAlreadyExistsError(Exception):
    """A job of this kind already exists for this repo (one scan per repo)."""


def create_job(
    conn: psycopg.Connection, project_id: str, kind: JobKind, repo_id: str | None = None
) -> Job:
    try:
        with conn.cursor(row_factory=class_row(Job)) as cur:
            row = cur.execute(
                f"""
                insert into codeaxis.jobs (project_id, repo_id, kind, status)
                values (%s, %s, %s, %s)
                returning {_JOB_COLUMNS}
                """,
                (project_id, repo_id, kind, JobStatus.QUEUED.value),
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        conn.rollback()
        raise JobAlreadyExistsError(repo_id) from exc
    conn.commit()
    assert row is not None
    return row


def set_job_status(
    conn: psycopg.Connection, job_id: str, status: JobStatus, error: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.jobs set status = %s, error = %s, updated_at = now() where id = %s",
            (status.value, error, job_id),
        )
    conn.commit()


def get_job(conn: psycopg.Connection, job_id: str) -> Job | None:
    with conn.cursor(row_factory=class_row(Job)) as cur:
        return cur.execute(
            f"select {_JOB_COLUMNS} from codeaxis.jobs where id = %s", (job_id,)
        ).fetchone()


def get_active_job_for_repo(conn: psycopg.Connection, repo_id: str) -> Job | None:
    """The repo's queued or running job, if it has one. Used to block a
    delete while a scan is still in flight."""
    with conn.cursor(row_factory=class_row(Job)) as cur:
        return cur.execute(
            f"""
            select {_JOB_COLUMNS} from codeaxis.jobs
            where repo_id = %s and status in (%s, %s)
            """,
            (repo_id, JobStatus.QUEUED.value, JobStatus.RUNNING.value),
        ).fetchone()
