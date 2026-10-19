"""End-to-end Step 1 flow against the live Supabase database: stage a
source, read its exclusion preview, confirm, wait for the scan to land,
walk the graph, and read one symbol's mechanics. Skipped when
WORKER_DATABASE_URL isn't set."""

import io
import os
import time
import zipfile
from pathlib import Path

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from app.knowledge import worker_connection
from app.main import app

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)

TABLES = ("blobs", "edges", "nodes", "coverage", "pass_runs", "jobs", "repos")

# Local-path ingestion is allowlisted for every test by default via the
# `_allow_local_ingest` autouse fixture in conftest.py.


@pytest.fixture
def member(client, make_user, auth_header) -> tuple[TestClient, str]:
    """A signed-in user who owns a fresh project: a client that sends their
    bearer token on every request, and the project's id."""
    headers = auth_header(make_user())
    resp = client.post("/projects", json={"name": "flow"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return TestClient(app, headers=headers), resp.json()["id"]


def _assert_no_rows_for(repo_ids: list[str]) -> None:
    """Confirm cleanup actually removed every row these specific repos
    touched. Scoped by repo_id rather than a whole-table count snapshot,
    so it stays correct under concurrent DB writers (other tests, other
    processes) instead of racing on unrelated rows."""
    with worker_connection() as conn, conn.cursor() as cur:
        for repo_id in repo_ids:
            for table in TABLES:
                col = "id" if table == "repos" else "repo_id"
                row = cur.execute(
                    f"select count(*) from codeaxis.{table} where {col} = %s", (repo_id,)
                ).fetchone()
                assert row is not None
                assert row[0] == 0, f"{table} still has rows for repo {repo_id}"


def _cleanup(repo_ids: list[str]) -> None:
    with worker_connection() as conn, conn.cursor() as cur:
        for repo_id in repo_ids:
            for table in TABLES:
                col = "id" if table == "repos" else "repo_id"
                cur.execute(f"delete from codeaxis.{table} where {col} = %s", (repo_id,))
        conn.commit()


def _wait_for_scan(client: TestClient, repo_id: str) -> list:
    runs: list = []
    for _ in range(40):
        runs = client.get(f"/repos/{repo_id}/status").json()["runs"]
        if runs and runs[0]["status"] in ("succeeded", "failed"):
            return runs
        time.sleep(0.25)
    pytest.fail(f"scan did not finish: {runs}")


def test_stage_confirm_scan_and_browse(tmp_path, member):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def hello():\n    return 'hi'\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "keys.py").write_text("def leaked():\n    pass\n")
    (tmp_path / "node_modules").mkdir()

    client, project_id = member
    commit_sha = "deadbeef"
    repo_ids: list[str] = []

    try:
        resp = client.post(
            "/repos/stage",
            params={"project_id": project_id},
            json={"kind": "local", "local_path": str(tmp_path), "commit_sha": commit_sha},
        )
        assert resp.status_code == 200, resp.text
        staged = resp.json()
        repo_id = staged["id"]
        repo_ids.append(repo_id)

        assert staged["commit_sha"] == commit_sha
        assert Path(staged["local_path"]) == tmp_path.resolve()
        by_path = {e["path"]: e for e in staged["preview"]}
        assert by_path["pkg/mod.py"]["included"] is True
        assert by_path["pkg/mod.py"]["language"] == "python"
        assert by_path["secrets/keys.py"]["included"] is True  # not excluded yet
        assert by_path["node_modules"] == {
            "path": "node_modules",
            "kind": "directory",
            "language": None,
            "size_bytes": None,
            "included": False,
            "reason": "excluded_dir",
        }

        # Nothing is scanned until confirm.
        assert client.get(f"/repos/{repo_id}/status").json()["runs"] == []

        # A forged commit_sha in the request body is simply ignored — the
        # scan always runs against the SHA the server resolved at stage time.
        resp = client.post(
            f"/repos/{repo_id}/confirm",
            json={"commit_sha": "forged-sha", "excluded_paths": ["secrets"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "scan_started"
        assert client.get(f"/jobs/{resp.json()['job_id']}").status_code == 200

        runs = _wait_for_scan(client, repo_id)
        assert runs[0]["status"] == "succeeded", runs

        # A local folder is the user's own: never deleted after scanning.
        assert (tmp_path / "pkg" / "mod.py").exists()

        # The pass_run was recorded under the staged SHA, not the forged one.
        assert runs[0]["commit_sha"] == commit_sha

        # A second confirm must not double-scan.
        again = client.post(f"/repos/{repo_id}/confirm", json={})
        assert again.status_code == 409

        graph = client.get(
            f"/repos/{repo_id}/graph", params={"commit_sha": commit_sha, "depth": 3}
        ).json()
        names = {n["name"] for n in graph["nodes"]}
        assert {"pkg", "mod.py", "hello"} <= names
        assert not {"secrets", "keys.py", "leaked"} & names  # user exclusion honoured
        assert any(e["kind"] == "contains" for e in graph["edges"])

        symbol = next(n for n in graph["nodes"] if n["name"] == "hello")
        detail = client.get(
            f"/repos/{repo_id}/symbols/{symbol['id']}", params={"commit_sha": commit_sha}
        ).json()
        assert detail["name"] == "hello"
        assert detail["status"] == "verified"
        assert detail["analyzer"] == "tree-sitter"

        # Explorer: root, then one lazy level at a time.
        root = client.get(f"/repos/{repo_id}/tree-root", params={"commit_sha": commit_sha})
        assert root.status_code == 200, root.text
        assert root.json()["kind"] == "directory" and root.json()["path"] == ""
        top = client.get(
            f"/repos/{repo_id}/nodes/{root.json()['id']}/children",
            params={"commit_sha": commit_sha},
        ).json()["children"]
        assert [c["name"] for c in top] == ["pkg"]
        pkg_children = client.get(
            f"/repos/{repo_id}/nodes/{top[0]['id']}/children", params={"commit_sha": commit_sha}
        ).json()["children"]
        assert [c["path"] for c in pkg_children] == ["pkg/mod.py"]

        # Pinned source, byte-for-byte what was on disk at scan time.
        src = client.get(f"/repos/{repo_id}/files/pkg/mod.py", params={"commit_sha": commit_sha})
        assert src.status_code == 200, src.text
        body = src.json()
        assert body["path"] == "pkg/mod.py"
        assert body["content"] == (tmp_path / "pkg" / "mod.py").read_bytes().decode()
        assert body["content_hash"] == pkg_children[0]["attrs"]["content_hash"]

        # Excluded and unknown paths have no source — a 404, not an empty file.
        assert (
            client.get(
                f"/repos/{repo_id}/files/secrets/keys.py", params={"commit_sha": commit_sha}
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/repos/not-a-uuid/tree-root", params={"commit_sha": commit_sha}
            ).status_code
            == 404
        )
    finally:
        _cleanup(repo_ids)

    _assert_no_rows_for(repo_ids)


def test_upload_zip_stage_confirm_and_cleanup(member):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/main.py", "def main():\n    return 1\n")
    payload = buf.getvalue()

    client, project_id = member
    repo_ids: list[str] = []

    try:
        resp = client.post(
            "/repos/upload",
            params={"project_id": project_id},
            files={"file": ("repo.zip", payload, "application/zip")},
        )
        assert resp.status_code == 200, resp.text
        staged = resp.json()
        repo_ids.append(staged["id"])

        assert staged["commit_sha"].startswith("zip:")
        assert len(staged["commit_sha"]) == len("zip:") + 12
        checkout = Path(staged["local_path"])
        assert (checkout / "app" / "main.py").is_file()
        assert [e["path"] for e in staged["preview"] if e["included"]] == ["app/main.py"]

        resp = client.post(f"/repos/{staged['id']}/confirm", json={})
        assert resp.status_code == 200, resp.text
        runs = _wait_for_scan(client, staged["id"])
        assert runs[0]["status"] == "succeeded", runs

        # The scratch checkout is deleted once the scan has finished.
        assert not checkout.exists()

        graph = client.get(
            f"/repos/{staged['id']}/graph",
            params={"commit_sha": staged["commit_sha"], "depth": 3},
        ).json()
        assert "main" in {n["name"] for n in graph["nodes"]}
    finally:
        _cleanup(repo_ids)

    _assert_no_rows_for(repo_ids)


def test_coverage_and_symbol_relationships(tmp_path, member):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "b.py").write_text("def helper():\n    return 'hi'\n")
    (tmp_path / "pkg" / "a.py").write_text(
        "from pkg.b import helper\n\ndef main():\n    return helper()\n"
    )

    client, project_id = member
    commit_sha = "coverage-test"
    repo_ids: list[str] = []

    try:
        resp = client.post(
            "/repos/stage",
            params={"project_id": project_id},
            json={"kind": "local", "local_path": str(tmp_path), "commit_sha": commit_sha},
        )
        assert resp.status_code == 200, resp.text
        repo_id = resp.json()["id"]
        repo_ids.append(repo_id)

        resp = client.post(f"/repos/{repo_id}/confirm", json={})
        assert resp.status_code == 200, resp.text
        runs = _wait_for_scan(client, repo_id)
        assert runs[0]["status"] == "succeeded", runs

        coverage_resp = client.get(f"/repos/{repo_id}/coverage", params={"commit_sha": commit_sha})
        assert coverage_resp.status_code == 200, coverage_resp.text
        body = coverage_resp.json()
        assert "coverage" in body and "unresolved" in body
        assert isinstance(body["coverage"], list) and len(body["coverage"]) > 0
        assert isinstance(body["unresolved"], list)
        for row in body["unresolved"]:
            assert {"kind", "resolution_status", "count"} <= row.keys()

        graph = client.get(
            f"/repos/{repo_id}/graph", params={"commit_sha": commit_sha, "depth": 3}
        ).json()
        # The relationship extractor attributes imports/calls/extends/
        # implements/tests edges to the *file* node (one per-file pass),
        # not the individual symbol inside it, so outgoing relationships
        # are inspected from a.py's file node.
        a_file = next(n for n in graph["nodes"] if n["path"] == "pkg/a.py")

        detail = client.get(
            f"/repos/{repo_id}/symbols/{a_file['id']}", params={"commit_sha": commit_sha}
        ).json()
        assert "relationships" in detail
        outgoing = detail["relationships"]["outgoing"]
        assert any(
            e["kind"] == "calls" and e["node"] is not None and e["node"]["name"] == "helper"
            for e in outgoing
        )
        for entry in outgoing:
            assert {"kind", "resolution_status", "attrs", "node"} <= entry.keys()

        b_file = next(n for n in graph["nodes"] if n["path"] == "pkg/b.py")
        b_detail = client.get(
            f"/repos/{repo_id}/symbols/{b_file['id']}", params={"commit_sha": commit_sha}
        ).json()
        incoming = b_detail["relationships"]["incoming"]
        assert any(
            e["kind"] == "imports" and e["node"] is not None and e["node"]["path"] == "pkg/a.py"
            for e in incoming
        )
    finally:
        _cleanup(repo_ids)

    _assert_no_rows_for(repo_ids)


def test_stage_rejects_bad_sources(tmp_path, member):
    client, project_id = member

    missing = client.post(
        "/repos/stage",
            params={"project_id": project_id},
        json={"kind": "local", "local_path": str(tmp_path / "nope"), "commit_sha": "x"},
    )
    assert missing.status_code == 400
    assert "does not exist" in missing.json()["detail"]

    bad_scheme = client.post(
        "/repos/stage",
        params={"project_id": project_id},
        json={"kind": "github", "remote_url": "file:///etc"},
    )
    assert bad_scheme.status_code == 400

    not_zip = client.post(
        "/repos/upload",
        params={"project_id": project_id},
        files={"file": ("x.zip", b"not a zip", "application/zip")},
    )
    assert not_zip.status_code == 400

    unknown = client.post("/repos/00000000-0000-0000-0000-000000000000/confirm", json={})
    assert unknown.status_code == 404


def test_stage_local_path_disabled_by_default(tmp_path, member, monkeypatch):
    monkeypatch.delenv("LOCAL_INGEST_ALLOWED_ROOTS", raising=False)
    client, project_id = member

    resp = client.post(
        "/repos/stage",
        params={"project_id": project_id},
        json={"kind": "local", "local_path": str(tmp_path), "commit_sha": "x"},
    )
    assert resp.status_code == 400
    assert "disabled" in resp.json()["detail"]


def test_stage_local_path_outside_allowlist_rejected(
    tmp_path, member, monkeypatch, tmp_path_factory
):
    other_root = tmp_path_factory.mktemp("not-allowed-root")
    monkeypatch.setenv("LOCAL_INGEST_ALLOWED_ROOTS", str(other_root))
    client, project_id = member

    resp = client.post(
        "/repos/stage",
        params={"project_id": project_id},
        json={"kind": "local", "local_path": str(tmp_path), "commit_sha": "x"},
    )
    assert resp.status_code == 400
    assert "outside the allowed roots" in resp.json()["detail"]
