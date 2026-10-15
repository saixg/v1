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

from codeaxis_api.main import app
from codeaxis_graph import worker_connection

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)

TABLES = ("blobs", "edges", "nodes", "coverage", "pass_runs", "repos")


def _row_counts() -> dict[str, int]:
    with worker_connection() as conn, conn.cursor() as cur:
        counts = {}
        for table in TABLES:
            row = cur.execute(f"select count(*) from codeaxis.{table}").fetchone()
            assert row is not None
            counts[table] = row[0]
        return counts


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


def test_stage_confirm_scan_and_browse(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def hello():\n    return 'hi'\n")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "keys.py").write_text("def leaked():\n    pass\n")
    (tmp_path / "node_modules").mkdir()

    client = TestClient(app)
    commit_sha = "deadbeef"
    before = _row_counts()
    repo_ids: list[str] = []

    try:
        resp = client.post(
            "/repos/stage",
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

        resp = client.post(
            f"/repos/{repo_id}/confirm",
            json={"commit_sha": commit_sha, "excluded_paths": ["secrets"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "scan_started"}

        runs = _wait_for_scan(client, repo_id)
        assert runs[0]["status"] == "succeeded", runs

        # A local folder is the user's own: never deleted after scanning.
        assert (tmp_path / "pkg" / "mod.py").exists()

        # A second confirm must not double-scan.
        again = client.post(f"/repos/{repo_id}/confirm", json={"commit_sha": commit_sha})
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

    assert _row_counts() == before


def test_upload_zip_stage_confirm_and_cleanup():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/main.py", "def main():\n    return 1\n")
    payload = buf.getvalue()

    client = TestClient(app)
    before = _row_counts()
    repo_ids: list[str] = []

    try:
        resp = client.post(
            "/repos/upload", files={"file": ("repo.zip", payload, "application/zip")}
        )
        assert resp.status_code == 200, resp.text
        staged = resp.json()
        repo_ids.append(staged["id"])

        assert staged["commit_sha"].startswith("zip:")
        assert len(staged["commit_sha"]) == len("zip:") + 12
        checkout = Path(staged["local_path"])
        assert (checkout / "app" / "main.py").is_file()
        assert [e["path"] for e in staged["preview"] if e["included"]] == ["app/main.py"]

        resp = client.post(
            f"/repos/{staged['id']}/confirm", json={"commit_sha": staged["commit_sha"]}
        )
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

    assert _row_counts() == before


def test_stage_rejects_bad_sources(tmp_path):
    client = TestClient(app)
    before = _row_counts()

    missing = client.post(
        "/repos/stage",
        json={"kind": "local", "local_path": str(tmp_path / "nope"), "commit_sha": "x"},
    )
    assert missing.status_code == 400
    assert "does not exist" in missing.json()["detail"]

    bad_scheme = client.post("/repos/stage", json={"kind": "github", "remote_url": "file:///etc"})
    assert bad_scheme.status_code == 400

    not_zip = client.post(
        "/repos/upload", files={"file": ("x.zip", b"not a zip", "application/zip")}
    )
    assert not_zip.status_code == 400

    unknown = client.post(
        "/repos/00000000-0000-0000-0000-000000000000/confirm", json={"commit_sha": "x"}
    )
    assert unknown.status_code == 404

    assert _row_counts() == before
