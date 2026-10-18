"""Project membership and job tracking against the live Supabase database
(build-plan.md Phase 0 exit: "user signs in, registers a repo, sees a job
with status; unauthorized access blocked (tested)").

Users are real codeaxis.profiles rows (conftest.make_user, standing in for a
Clerk user — Clerk has no signup hook into this database); tokens are signed
with a local test key and verified by the real app.auth code path. Skipped
when DATABASE_URL / WORKER_DATABASE_URL aren't set.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid

import psycopg
import pytest


def _wait_for_job(client, job_id: str, headers: dict[str, str]) -> list[str]:
    """Poll GET /jobs/{id}; return every distinct status seen, in order."""
    seen: list[str] = []
    for _ in range(80):
        resp = client.get(f"/jobs/{job_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        status = resp.json()["status"]
        if not seen or seen[-1] != status:
            seen.append(status)
        if status in ("succeeded", "failed"):
            return seen
        time.sleep(0.25)
    pytest.fail(f"job did not finish: {seen}")


def _stage_local(client, tmp_path, project_id: str, headers: dict[str, str]):
    return client.post(
        "/repos/stage",
        params={"project_id": project_id},
        json={"kind": "local", "local_path": str(tmp_path), "commit_sha": "c0ffee"},
        headers=headers,
    )


def test_projects_create_and_list_are_per_user(client, make_user, auth_header):
    alice, bob = make_user(), make_user()

    created = client.post("/projects", json={"name": "  Alice's app "}, headers=auth_header(alice))
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["name"] == "Alice's app"
    assert project["owner_id"] == alice
    assert project["role"] == "owner"

    mine = client.get("/projects", headers=auth_header(alice)).json()["projects"]
    assert [p["id"] for p in mine] == [project["id"]]
    assert client.get("/projects", headers=auth_header(bob)).json() == {"projects": []}

    assert (
        client.post("/projects", json={"name": "   "}, headers=auth_header(alice)).status_code
        == 422
    )
    assert (
        client.post("/projects", json={"name": ""}, headers=auth_header(alice)).status_code == 422
    )


def test_first_project_lazily_provisions_a_profile(client, auth_header):
    """A validly signed token for a brand-new Clerk user, never seen before
    by this database. Clerk has no signup hook into Postgres, so the first
    project a user creates is what provisions their codeaxis.profiles row
    (app.knowledge.ensure_profile) — this must succeed, not 403."""
    owner_url = os.environ.get("DATABASE_URL")
    if not owner_url or not os.environ.get("WORKER_DATABASE_URL"):
        pytest.skip("DATABASE_URL / WORKER_DATABASE_URL not set")
    user_id = f"user_{uuid.uuid4().hex}"
    try:
        resp = client.post(
            "/projects", json={"name": "ghost's first project"}, headers=auth_header(user_id)
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["owner_id"] == user_id

        with psycopg.connect(owner_url, connect_timeout=10) as conn:
            row = conn.execute(
                "select id from codeaxis.profiles where id = %s", (user_id,)
            ).fetchone()
        assert row is not None
    finally:
        with psycopg.connect(owner_url, connect_timeout=10) as conn:
            conn.execute(
                "delete from codeaxis.projects where owner_id = %s", (user_id,)
            )
            conn.execute("delete from codeaxis.profiles where id = %s", (user_id,))
            conn.commit()


def test_member_scan_job_lifecycle_and_non_member_is_blocked(
    client, make_user, auth_header, tmp_path
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def hello():\n    return 'hi'\n")
    alice, bob = make_user(), make_user()
    a, b = auth_header(alice), auth_header(bob)

    project_id = client.post("/projects", json={"name": "p"}, headers=a).json()["id"]

    # Bob can't register a repo under Alice's project — rejected before any
    # fetch or write happens.
    assert _stage_local(client, tmp_path, project_id, b).status_code == 403
    # A malformed project id is also a 403 (not a member), not a 500.
    assert _stage_local(client, tmp_path, "not-a-uuid", a).status_code == 403

    staged = _stage_local(client, tmp_path, project_id, a)
    assert staged.status_code == 200, staged.text
    repo_id = staged.json()["id"]
    commit = {"commit_sha": "c0ffee"}

    # Bob is blocked from every repo-scoped route, including starting a scan.
    assert client.post(f"/repos/{repo_id}/confirm", json={}, headers=b).status_code == 403
    for path in (
        f"/repos/{repo_id}/status",
        f"/repos/{repo_id}/graph",
        f"/repos/{repo_id}/tree-root",
        f"/repos/{repo_id}/symbols/1",
        f"/repos/{repo_id}/nodes/1/children",
        f"/repos/{repo_id}/files/pkg/mod.py",
    ):
        resp = client.get(path, params=commit, headers=b)
        assert resp.status_code == 403, (path, resp.text)

    confirmed = client.post(f"/repos/{repo_id}/confirm", json={}, headers=a)
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["status"] == "scan_started"
    job_id = body["job_id"]

    seen = _wait_for_job(client, job_id, a)
    assert seen[-1] == "succeeded", seen
    assert set(seen) <= {"queued", "running", "succeeded"}

    job = client.get(f"/jobs/{job_id}", headers=a).json()
    assert job["project_id"] == project_id
    assert job["repo_id"] == repo_id
    assert job["kind"] == "scan"
    assert job["error"] is None
    assert job["updated_at"] >= job["created_at"]

    # The job's outcome agrees with the analyzer-level pass_run. The scan is
    # the first run; the semantic pass appends its own run and, with no model
    # key configured in tests, records an honest `skipped:` failure.
    runs = client.get(f"/repos/{repo_id}/status", headers=a).json()["runs"]
    scan = runs[0]
    assert scan["analyzer"] == "tree-sitter", runs
    assert scan["status"] == "succeeded", runs
    for run in runs[1:]:
        assert run["analyzer"].startswith("semantic"), runs

    # Members read the graph; Bob still can't, nor can he read the job.
    assert client.get(f"/repos/{repo_id}/tree-root", params=commit, headers=a).status_code == 200
    assert client.get(f"/jobs/{job_id}", headers=b).status_code == 403

    # One scan per repo.
    assert client.post(f"/repos/{repo_id}/confirm", json={}, headers=a).status_code == 409

    # Unknown ids are 404 for an authenticated caller.
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/jobs/{missing}", headers=a).status_code == 404
    assert client.get("/jobs/not-a-uuid", headers=a).status_code == 404
    assert client.get(f"/repos/{missing}/status", headers=a).status_code == 404


def test_failed_scan_is_reported_on_the_job(client, make_user, auth_header, tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / "a.py").write_text("x = 1\n")
    alice = make_user()
    a = auth_header(alice)
    project_id = client.post("/projects", json={"name": "p"}, headers=a).json()["id"]
    staged = client.post(
        "/repos/stage",
        params={"project_id": project_id},
        json={"kind": "local", "local_path": str(checkout), "commit_sha": "c0ffee"},
        headers=a,
    )
    assert staged.status_code == 200, staged.text
    repo_id = staged.json()["id"]

    # The folder disappears between stage and confirm: the scan must fail
    # loudly (app.analysis.jobs refuses to "succeed" on a missing checkout).
    shutil.rmtree(checkout)

    job_id = client.post(f"/repos/{repo_id}/confirm", json={}, headers=a).json()["job_id"]
    seen = _wait_for_job(client, job_id, a)
    assert seen[-1] == "failed", seen
    job = client.get(f"/jobs/{job_id}", headers=a).json()
    assert "checkout not found" in job["error"]


def test_authenticated_role_has_no_direct_access(client, make_user, auth_header):
    """Clerk never populates Postgres's `request.jwt.claims` (that machinery
    is Supabase Auth/PostgREST-specific), so there is no way for the
    `authenticated` role to ever be scoped to a Clerk user — the migration
    that removed Supabase Auth also revoked `authenticated`'s grants and
    auth.uid()-based policies on these tables entirely, rather than leaving
    a policy that looks like protection but can never evaluate true. This
    confirms that revocation actually happened: as role `authenticated`
    with no claims at all (nothing else could set any), every table here is
    fully inaccessible — not merely scoped to nothing.
    """
    alice = make_user()
    client.post("/projects", json={"name": "a"}, headers=auth_header(alice))

    owner_url = os.environ["DATABASE_URL"]
    with psycopg.connect(owner_url, connect_timeout=10) as conn:
        conn.execute("set local role authenticated")

        for table in ("profiles", "projects", "project_memberships", "jobs"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"select 1 from codeaxis.{table}")
            conn.rollback()
            conn.execute("set local role authenticated")

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into codeaxis.projects (name, owner_id) values ('x', %s)", (alice,)
            )
        conn.rollback()
