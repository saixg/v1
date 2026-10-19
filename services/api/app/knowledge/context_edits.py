"""Small reads/writes the Context routes need that the Task A data layer does
not provide: editing or removing a *user-stated* ledger entry, and resolving
which commits of a repo have a finished scan.

Kept in its own module so `app.knowledge.context` stays exactly the contract
Tasks B and C were built against.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import class_row

from app.models import Constraint

# The tree-sitter scan is the pass that writes the graph; a snapshot exists
# for a commit only once that pass_run has succeeded. Later passes (semantic,
# ledger) are best-effort and may be recorded as failed without invalidating
# the graph, so they do not decide this.
SCAN_ANALYZER = "tree-sitter"


def scanned_commits(conn: psycopg.Connection, repo_id: str) -> list[str]:
    """Commits with a succeeded scan, newest first."""
    rows = conn.execute(
        """
        select commit_sha from codeaxis.pass_runs
        where repo_id = %s and analyzer = %s and status = 'succeeded'
        order by started_at desc, id desc
        """,
        (repo_id, SCAN_ANALYZER),
    ).fetchall()
    return [str(r[0]) for r in rows]


def latest_scanned_commit(conn: psycopg.Connection, repo_id: str) -> str | None:
    commits = scanned_commits(conn, repo_id)
    return commits[0] if commits else None


def is_scanned_commit(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> bool:
    row = conn.execute(
        """
        select 1 from codeaxis.pass_runs
        where repo_id = %s and commit_sha = %s and analyzer = %s and status = 'succeeded'
        limit 1
        """,
        (repo_id, commit_sha, SCAN_ANALYZER),
    ).fetchone()
    return row is not None


_COLUMNS = """
    id, task_id, repo_id, commit_sha, kind, scope, statement, severity, source,
    origin, confirmed, confirmed_by, confirmed_at, rationale, evidence,
    analyzer, analyzer_version, created_at
"""


def get_constraint(conn: psycopg.Connection, constraint_id: str) -> Constraint | None:
    with conn.cursor(row_factory=class_row(Constraint)) as cur:
        return cur.execute(
            f"select {_COLUMNS} from codeaxis.constraints where id = %s", (constraint_id,)
        ).fetchone()


def update_user_constraint(
    conn: psycopg.Connection, constraint_id: str, task_id: str, fields: dict[str, object]
) -> Constraint | None:
    """Patch a user-stated constraint of one task. The `source`/`task_id`
    guard is in the SQL, so an auto-detected row (whose statement is the
    analyzer's finding, not the user's to rewrite) can never be edited here.
    `fields` keys come from a fixed whitelist, never from the request."""
    allowed = {"kind", "scope", "statement", "severity", "rationale"}
    sets = [k for k in fields if k in allowed]
    if not sets:
        return get_constraint(conn, constraint_id)
    assignments = ", ".join(f"{k} = %s" for k in sets)
    query = (
        f"update codeaxis.constraints set {assignments} "  # noqa: S608 - whitelisted columns
        f"where id = %s and task_id = %s and source = 'user_stated' returning {_COLUMNS}"
    )
    with conn.cursor(row_factory=class_row(Constraint)) as cur:
        row = cur.execute(
            query,  # pyright: ignore[reportArgumentType]
            [*(fields[k] for k in sets), constraint_id, task_id],
        ).fetchone()
    conn.commit()
    return row


def delete_user_constraint(conn: psycopg.Connection, constraint_id: str, task_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "delete from codeaxis.constraints "
            "where id = %s and task_id = %s and source = 'user_stated'",
            (constraint_id, task_id),
        )
        deleted = cur.rowcount
    conn.commit()
    return deleted == 1
