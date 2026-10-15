"""Integration test against the live Supabase database. Skipped when
WORKER_DATABASE_URL isn't set (e.g. sandboxed CI with no network)."""

import os
import uuid

import pytest
from dotenv import load_dotenv

from codeaxis_contracts import PassRunStatus, Repo
from codeaxis_graph import (
    create_repo,
    get_root_node,
    list_pass_runs,
    neighborhood,
    worker_connection,
)
from codeaxis_worker import run_scan_pass

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)


def test_run_scan_pass_populates_graph(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def hello():\n    return 'hi'\n")

    commit_sha = f"test-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        repo = create_repo(conn, Repo(local_path=str(tmp_path)))
    assert repo.id is not None

    try:
        run_scan_pass(repo.id, commit_sha, str(tmp_path))

        with worker_connection() as conn:
            runs = list_pass_runs(conn, repo.id)
            assert len(runs) == 1
            assert runs[0].status == PassRunStatus.SUCCEEDED

            root = get_root_node(conn, repo.id, commit_sha)
            assert root is not None and root.id is not None

            nodes, edges, truncated = neighborhood(conn, repo.id, commit_sha, root.id, depth=3)
            assert not truncated
            names = {n.name for n in nodes}
            assert {"pkg", "mod.py", "hello"} <= names
            assert any(e.kind == "contains" for e in edges)
    finally:
        with worker_connection() as conn, conn.cursor() as cur:
            for table in ("blobs", "edges", "nodes", "coverage", "pass_runs", "repos"):
                col = "id" if table == "repos" else "repo_id"
                cur.execute(f"delete from codeaxis.{table} where {col} = %s", (repo.id,))
            conn.commit()
