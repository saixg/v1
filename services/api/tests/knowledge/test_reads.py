"""Integration tests for the relationship/coverage read functions
(`neighborhood(direction="reverse")`, `list_coverage`, `unresolved_summary`)
against the live Supabase database. Skipped when WORKER_DATABASE_URL isn't
set, same convention as test_repo_model.py."""

from __future__ import annotations

import os
import uuid

import pytest
from dotenv import load_dotenv

from app.knowledge import (
    create_repo,
    insert_edges,
    insert_nodes,
    list_coverage,
    neighborhood,
    unresolved_summary,
    upsert_coverage,
    worker_connection,
)
from app.models import Coverage, Edge, Node, Repo

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)


def test_reverse_neighborhood_coverage_and_unresolved_summary(db_project):
    commit_sha = f"test-{uuid.uuid4().hex[:12]}"

    with worker_connection() as conn:
        repo = create_repo(
            conn, Repo(project_id=db_project, remote_url="https://example.com/octo/cat.git")
        )
    assert repo.id is not None
    repo_id = repo.id

    try:
        with worker_connection() as conn:
            # a.py imports b.py; b.py's `helper` is called by a.py's `main`,
            # and a `calls` edge that could not be resolved to anything.
            file_a, file_b, sym_main, sym_helper = insert_nodes(
                conn,
                [
                    Node(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        kind="file",
                        name="a.py",
                        path="a.py",
                        analyzer="tree-sitter",
                        analyzer_version="0.1.0",
                        status="verified",
                    ),
                    Node(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        kind="file",
                        name="b.py",
                        path="b.py",
                        analyzer="tree-sitter",
                        analyzer_version="0.1.0",
                        status="verified",
                    ),
                    Node(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        kind="symbol",
                        name="main",
                        path="a.py",
                        line_start=1,
                        line_end=2,
                        analyzer="tree-sitter",
                        analyzer_version="0.1.0",
                        status="verified",
                    ),
                    Node(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        kind="symbol",
                        name="helper",
                        path="b.py",
                        line_start=1,
                        line_end=2,
                        analyzer="tree-sitter",
                        analyzer_version="0.1.0",
                        status="verified",
                    ),
                ],
            )

            insert_edges(
                conn,
                [
                    Edge(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        kind="imports",
                        src_id=file_a,
                        dst_id=file_b,
                        analyzer="resolve",
                        analyzer_version="0.1.0",
                        status="verified",
                        resolution_status="resolved",
                    ),
                    Edge(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        kind="calls",
                        src_id=sym_main,
                        dst_id=sym_helper,
                        analyzer="resolve",
                        analyzer_version="0.1.0",
                        status="verified",
                        resolution_status="resolved",
                        attrs={"callee_name": "helper"},
                    ),
                    Edge(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        kind="calls",
                        src_id=sym_main,
                        dst_id=None,
                        analyzer="resolve",
                        analyzer_version="0.1.0",
                        status="verified",
                        resolution_status="unresolved",
                        attrs={"callee_name": "mystery"},
                    ),
                ],
            )

            upsert_coverage(
                conn,
                [
                    Coverage(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        area="repo",
                        metric="files_parsed",
                        resolved=2,
                        total=2,
                    )
                ],
            )

            # Reverse neighborhood off `helper`: what points at it.
            nodes, edges, truncated = neighborhood(
                conn,
                repo_id,
                commit_sha,
                sym_helper,
                depth=1,
                kinds=["calls"],
                direction="reverse",
            )
            assert not truncated
            assert {e.kind for e in edges} == {"calls"}
            assert any(e.src_id == sym_main and e.dst_id == sym_helper for e in edges)
            assert {n.id for n in nodes} >= {sym_helper, sym_main}

            # Forward neighborhood off `main` still works unchanged (default
            # direction="forward" — the existing /graph behaviour).
            fwd_nodes, fwd_edges, _ = neighborhood(
                conn, repo_id, commit_sha, sym_main, depth=1, kinds=["calls"]
            )
            assert {e.dst_id for e in fwd_edges} == {sym_helper, None}

            coverage_rows = list_coverage(conn, repo_id, commit_sha)
            assert [(c.metric, c.resolved, c.total) for c in coverage_rows] == [
                ("files_parsed", 2, 2)
            ]

            summary = unresolved_summary(conn, repo_id, commit_sha)
            by_kind_status = {
                (row["kind"], row["resolution_status"]): row["count"] for row in summary
            }
            assert by_kind_status["imports", "resolved"] == 1
            assert by_kind_status["calls", "resolved"] == 1
            assert by_kind_status["calls", "unresolved"] == 1
    finally:
        with worker_connection() as conn, conn.cursor() as cur:
            for table in ("blobs", "edges", "nodes", "coverage", "pass_runs", "repos"):
                col = "id" if table == "repos" else "repo_id"
                cur.execute(f"delete from codeaxis.{table} where {col} = %s", (repo_id,))
            conn.commit()
