"""codeaxis.repos: the repository being analyzed. Every repo belongs to one
project (codeaxis.projects); callers must have checked membership in that
project before registering a repo under it."""

from __future__ import annotations

import psycopg
from psycopg.rows import class_row

from app.models import Repo

_REPO_COLUMNS = "id, project_id, remote_url, local_path, default_branch, staged_commit_sha"


def create_repo(conn: psycopg.Connection, repo: Repo) -> Repo:
    if repo.project_id is None:
        raise ValueError("a repo must be registered under a project")
    with conn.cursor(row_factory=class_row(Repo)) as cur:
        row = cur.execute(
            f"""
            insert into codeaxis.repos
              (project_id, remote_url, local_path, default_branch, staged_commit_sha)
            values (%s, %s, %s, %s, %s)
            returning {_REPO_COLUMNS}
            """,
            (
                repo.project_id,
                repo.remote_url,
                repo.local_path,
                repo.default_branch,
                repo.staged_commit_sha,
            ),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


def get_repo(conn: psycopg.Connection, repo_id: str) -> Repo | None:
    with conn.cursor(row_factory=class_row(Repo)) as cur:
        return cur.execute(
            f"select {_REPO_COLUMNS} from codeaxis.repos where id = %s",
            (repo_id,),
        ).fetchone()


def delete_repo(conn: psycopg.Connection, repo_id: str) -> None:
    """Permanently remove a repo and everything analyzed from it. Deletes
    its graph data before the repo row itself; `codeaxis.jobs` rows cascade
    on delete from `codeaxis.repos` (see the jobs migration) so they need no
    explicit delete here."""
    with conn.cursor() as cur:
        cur.execute("delete from codeaxis.edges where repo_id = %s", (repo_id,))
        cur.execute("delete from codeaxis.blobs where repo_id = %s", (repo_id,))
        cur.execute("delete from codeaxis.coverage where repo_id = %s", (repo_id,))
        cur.execute("delete from codeaxis.nodes where repo_id = %s", (repo_id,))
        cur.execute("delete from codeaxis.pass_runs where repo_id = %s", (repo_id,))
        cur.execute("delete from codeaxis.repos where id = %s", (repo_id,))
    conn.commit()
