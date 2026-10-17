"""Integration test against the live Supabase database. Skipped when
WORKER_DATABASE_URL isn't set (e.g. sandboxed CI with no network)."""

import os
import uuid

import pytest
from dotenv import load_dotenv

from app.knowledge import (
    create_repo,
    finish_pass_run,
    get_node,
    insert_edges,
    insert_nodes,
    list_children,
    start_pass_run,
    upsert_coverage,
    worker_connection,
)
from app.models import Coverage, Edge, Node, PassRun, PassRunStatus, Repo

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)


def test_insert_and_read_round_trip(db_project):
    commit_sha = f"test-{uuid.uuid4().hex[:12]}"

    with worker_connection() as conn:
        repo = create_repo(
            conn, Repo(project_id=db_project, remote_url="https://example.com/octo/cat.git")
        )
    assert repo.id is not None

    try:
        _run_round_trip(repo.id, commit_sha)
    finally:
        with worker_connection() as conn, conn.cursor() as cur:
            for table in ("blobs", "edges", "nodes", "coverage", "pass_runs", "repos"):
                col = "id" if table == "repos" else "repo_id"
                cur.execute(f"delete from codeaxis.{table} where {col} = %s", (repo.id,))
            conn.commit()


def _run_round_trip(repo_id: str, commit_sha: str) -> None:
    with worker_connection() as conn:
        run = start_pass_run(
            conn,
            PassRun(
                repo_id=repo_id,
                commit_sha=commit_sha,
                analyzer="tree-sitter",
                analyzer_version="0.1.0",
                status=PassRunStatus.RUNNING,
            ),
        )

        file_node, symbol_node = insert_nodes(
            conn,
            [
                Node(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="file",
                    name="main.py",
                    path="main.py",
                    analyzer="tree-sitter",
                    analyzer_version="0.1.0",
                    status="verified",
                ),
                Node(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="symbol",
                    name="main",
                    path="main.py",
                    line_start=1,
                    line_end=3,
                    analyzer="tree-sitter",
                    analyzer_version="0.1.0",
                    status="verified",
                ),
            ],
        )
        file_id = file_node
        symbol_id = symbol_node

        insert_edges(
            conn,
            [
                Edge(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="contains",
                    src_id=file_id,
                    dst_id=symbol_id,
                    analyzer="tree-sitter",
                    analyzer_version="0.1.0",
                    status="verified",
                )
            ],
        )

        upsert_coverage(
            conn,
            [
                Coverage(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    area="main.py",
                    metric="symbols_resolved",
                    resolved=1,
                    total=1,
                )
            ],
        )

        assert run.id is not None
        finish_pass_run(conn, run.id, PassRunStatus.SUCCEEDED)

        fetched = get_node(conn, repo_id, commit_sha, symbol_id)
        assert fetched is not None
        assert fetched.name == "main"

        children = list_children(conn, repo_id, commit_sha, file_id)
        assert [c.id for c in children] == [symbol_id]
