"""codeaxis.repos: the repository being analyzed."""

from __future__ import annotations

import psycopg
from psycopg.rows import class_row

from codeaxis_contracts import Repo


def create_repo(conn: psycopg.Connection, repo: Repo) -> Repo:
    with conn.cursor(row_factory=class_row(Repo)) as cur:
        row = cur.execute(
            """
            insert into codeaxis.repos (remote_url, local_path, default_branch)
            values (%s, %s, %s)
            returning id, remote_url, local_path, default_branch
            """,
            (repo.remote_url, repo.local_path, repo.default_branch),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


def get_repo(conn: psycopg.Connection, repo_id: str) -> Repo | None:
    with conn.cursor(row_factory=class_row(Repo)) as cur:
        return cur.execute(
            "select id, remote_url, local_path, default_branch from codeaxis.repos where id = %s",
            (repo_id,),
        ).fetchone()
