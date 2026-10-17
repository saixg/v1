"""DELETE /repos/{repo_id} against the live Supabase database. Skipped when
WORKER_DATABASE_URL isn't set, matching every other DB-integration test in
this suite."""

from __future__ import annotations

import os
import time

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from app.knowledge import create_job, worker_connection
from app.main import app

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)

TABLES = ("blobs", "edges", "nodes", "coverage", "pass_runs", "jobs")


@pytest.fixture
def member(client, make_user, auth_header) -> tuple[TestClient, str]:
    headers = auth_header(make_user())
    resp = client.post("/projects", json={"name": "delete-flow"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return TestClient(app, headers=headers), resp.json()["id"]


def _row_counts(repo_id: str) -> dict[str, int]:
    with worker_connection() as conn, conn.cursor() as cur:
        counts = {}
        for table in TABLES:
            row = cur.execute(
                f"select count(*) from codeaxis.{table} where repo_id = %s", (repo_id,)
            ).fetchone()
            assert row is not None
            counts[table] = row[0]
        return counts


def _wait_for_scan(client: TestClient, repo_id: str) -> list:
    runs: list = []
    for _ in range(40):
        runs = client.get(f"/repos/{repo_id}/status").json()["runs"]
        if runs and runs[0]["status"] in ("succeeded", "failed"):
            return runs
        time.sleep(0.25)
    pytest.fail(f"scan did not finish: {runs}")


def test_delete_removes_all_graph_data_for_the_repo(tmp_path, member):
    (tmp_path / "mod.py").write_text("def hello():\n    return 'hi'\n")
    client, project_id = member

    staged = client.post(
        "/repos/stage",
        params={"project_id": project_id},
        json={"kind": "local", "local_path": str(tmp_path), "commit_sha": "c0ffee"},
    )
    assert staged.status_code == 200, staged.text
    repo_id = staged.json()["id"]

    confirmed = client.post(f"/repos/{repo_id}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    runs = _wait_for_scan(client, repo_id)
    assert runs[0]["status"] == "succeeded", runs

    before = _row_counts(repo_id)
    assert before["nodes"] > 0  # the scan really wrote something to delete

    resp = client.delete(f"/repos/{repo_id}")
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    assert client.get(f"/repos/{repo_id}/status").status_code == 404

    after = _row_counts(repo_id)
    assert all(count == 0 for count in after.values()), after


def test_delete_blocked_while_a_scan_is_queued(tmp_path, member):
    client, project_id = member
    staged = client.post(
        "/repos/stage",
        params={"project_id": project_id},
        json={"kind": "local", "local_path": str(tmp_path), "commit_sha": "c0ffee"},
    )
    assert staged.status_code == 200, staged.text
    repo_id = staged.json()["id"]

    # Insert a queued job directly rather than racing a real scan.
    with worker_connection() as conn:
        create_job(conn, project_id, "scan", repo_id=repo_id)

    resp = client.delete(f"/repos/{repo_id}")
    assert resp.status_code == 409, resp.text

    with worker_connection() as conn, conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"delete from codeaxis.{table} where repo_id = %s", (repo_id,))
        cur.execute("delete from codeaxis.repos where id = %s", (repo_id,))
        conn.commit()


def test_delete_is_blocked_for_a_non_member(tmp_path, client, make_user, auth_header):
    alice, bob = make_user(), make_user()
    a, b = auth_header(alice), auth_header(bob)

    project_id = client.post("/projects", json={"name": "p"}, headers=a).json()["id"]
    staged = client.post(
        "/repos/stage",
        params={"project_id": project_id},
        json={"kind": "local", "local_path": str(tmp_path), "commit_sha": "c0ffee"},
        headers=a,
    )
    assert staged.status_code == 200, staged.text
    repo_id = staged.json()["id"]

    resp = client.delete(f"/repos/{repo_id}", headers=b)
    assert resp.status_code == 403, resp.text

    with worker_connection() as conn, conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"delete from codeaxis.{table} where repo_id = %s", (repo_id,))
        cur.execute("delete from codeaxis.repos where id = %s", (repo_id,))
        conn.commit()
