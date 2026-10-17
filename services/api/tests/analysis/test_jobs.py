"""Integration test against the live Supabase database. Skipped when
WORKER_DATABASE_URL isn't set (e.g. sandboxed CI with no network)."""

import hashlib
import os
import uuid

import pytest
from dotenv import load_dotenv
from psycopg.rows import class_row

from app.analysis.jobs import run_scan_pass
from app.knowledge import (
    create_repo,
    get_blob,
    get_root_node,
    list_nodes,
    list_pass_runs,
    neighborhood,
    worker_connection,
)
from app.models import Edge, Node, PassRunStatus, Repo

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)


def test_run_scan_pass_populates_graph(tmp_path, db_project):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def hello():\n    return 'hi'\n")

    commit_sha = f"test-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        repo = create_repo(conn, Repo(project_id=db_project, local_path=str(tmp_path)))
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


def test_scan_pass_redacts_secrets_from_stored_content(tmp_path, db_project):
    # AWS's own published example access key id -- safe to use in tests.
    original = b'aws_key = "AKIAIOSFODNN7EXAMPLE"\n'
    (tmp_path / "config.py").write_bytes(original)
    original_hash = hashlib.sha256(original).hexdigest()

    commit_sha = f"test-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        repo = create_repo(conn, Repo(project_id=db_project, local_path=str(tmp_path)))
    assert repo.id is not None

    try:
        run_scan_pass(repo.id, commit_sha, str(tmp_path))

        with worker_connection() as conn:
            file_nodes = list_nodes(conn, repo.id, commit_sha, kind="file", path="config.py")
            assert len(file_nodes) == 1
            node = file_nodes[0]
            assert node.attrs["secrets_redacted"] is True
            assert node.attrs["secrets_redacted_count"] >= 1
            # The hash is of the original, unredacted bytes -- it must not
            # change just because the stored content was redacted.
            assert node.attrs["content_hash"] == original_hash

            blob = get_blob(conn, repo.id, commit_sha, "config.py")
            assert blob is not None
            assert "AKIAIOSFODNN7EXAMPLE" not in blob.content
    finally:
        with worker_connection() as conn, conn.cursor() as cur:
            for table in ("blobs", "edges", "nodes", "coverage", "pass_runs", "repos"):
                col = "id" if table == "repos" else "repo_id"
                cur.execute(f"delete from codeaxis.{table} where {col} = %s", (repo.id,))
            conn.commit()


def _edges(conn, repo_id, commit_sha, kind=None) -> list[Edge]:
    clauses = ["repo_id = %s", "commit_sha = %s"]
    params: list[object] = [repo_id, commit_sha]
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    query = (
        "select id, repo_id, commit_sha, kind, src_id, dst_id, line_start, line_end, "
        "col_start, col_end, analyzer, analyzer_version, status, source_file, "
        "confidence, reason, resolution_status, attrs from codeaxis.edges where "
        + " and ".join(clauses)
    )
    with conn.cursor(row_factory=class_row(Edge)) as cur:
        return cur.execute(query, params).fetchall()


def test_run_scan_pass_produces_relationship_edges_and_dependency_nodes(tmp_path, db_project):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "foo.py").write_text(
        "def helper():\n    return 1\n\ndef local_caller():\n    return helper()\n"
    )
    (tmp_path / "pkg" / "bar.py").write_text(
        "from .foo import helper\nimport requests\n\n"
        "def caller():\n    return helper()\n"
    )
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")

    commit_sha = f"test-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        repo = create_repo(conn, Repo(project_id=db_project, local_path=str(tmp_path)))
    assert repo.id is not None

    try:
        run_scan_pass(repo.id, commit_sha, str(tmp_path))

        with worker_connection() as conn:
            import_edges = _edges(conn, repo.id, commit_sha, kind="imports")
            assert import_edges
            assert all(e.resolution_status is not None for e in import_edges)
            assert any(e.resolution_status == "resolved" for e in import_edges)
            assert any(e.resolution_status == "external" for e in import_edges)

            call_edges = _edges(conn, repo.id, commit_sha, kind="calls")
            assert call_edges
            assert any(e.resolution_status == "resolved" for e in call_edges)

            dependency_nodes = list_nodes(conn, repo.id, commit_sha, kind="dependency")
            assert any(n.name == "requests" for n in dependency_nodes)
    finally:
        with worker_connection() as conn, conn.cursor() as cur:
            for table in ("blobs", "edges", "nodes", "coverage", "pass_runs", "repos"):
                col = "id" if table == "repos" else "repo_id"
                cur.execute(f"delete from codeaxis.{table} where {col} = %s", (repo.id,))
            conn.commit()


def test_run_scan_pass_produces_clusters_and_layers(tmp_path, db_project):
    # Two files that import/call each other, under a routes/ dir so the
    # layer classifier has something to match, plus a sibling file in the
    # same directory the directory-co-membership bias should pull in too.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "routes").mkdir()
    (tmp_path / "app" / "routes" / "users.py").write_text(
        "from app.routes.orders import helper\n\ndef list_users():\n    return helper()\n"
    )
    (tmp_path / "app" / "routes" / "orders.py").write_text(
        "def helper():\n    return 1\n"
    )

    commit_sha = f"test-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        repo = create_repo(conn, Repo(project_id=db_project, local_path=str(tmp_path)))
    assert repo.id is not None

    try:
        run_scan_pass(repo.id, commit_sha, str(tmp_path))

        with worker_connection() as conn:
            runs = list_pass_runs(conn, repo.id)
            assert len(runs) == 1
            assert runs[0].status == PassRunStatus.SUCCEEDED

            cluster_nodes: list[Node] = list_nodes(conn, repo.id, commit_sha, kind="cluster")
            assert cluster_nodes, "expected at least one cluster node"
            assert all(n.attrs.get("origin") == "inferred" for n in cluster_nodes)

            belongs_to_edges = _edges(conn, repo.id, commit_sha, kind="belongs_to")
            assert belongs_to_edges, "expected at least one belongs_to edge"
            assert all(e.status == "verified" for e in belongs_to_edges)
            assert all(e.attrs.get("origin") == "inferred" for e in belongs_to_edges)

            users_node = list_nodes(
                conn, repo.id, commit_sha, kind="file", path="app/routes/users.py"
            )
            assert len(users_node) == 1
            assert users_node[0].attrs.get("layer") == "api"
            assert users_node[0].attrs.get("layer_origin") == "parsed"
    finally:
        with worker_connection() as conn, conn.cursor() as cur:
            for table in ("blobs", "edges", "nodes", "coverage", "pass_runs", "repos"):
                col = "id" if table == "repos" else "repo_id"
                cur.execute(f"delete from codeaxis.{table} where {col} = %s", (repo.id,))
            conn.commit()
