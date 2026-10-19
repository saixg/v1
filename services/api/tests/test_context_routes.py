"""Context Engine HTTP routes (build-plan.md Part 8): authorization on every
route, then the happy path against a really scanned fixture repo.

Real JWT verification (local keypair), live database, a real scan of the Task C
fixture tree. The ModelService is a scripted fake injected through the
`get_context_model_service` dependency: no key is read, no network call made.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

from app.context import EXPORT_FILENAMES
from app.context.intake import DEFAULT_LIMITS
from app.context_api import get_context_model_service
from app.knowledge import create_project, list_pass_runs, worker_connection
from app.knowledge.context import insert_decisions
from app.llm import Completion, CompletionProvider, CompletionRequest, RoutedModelService
from app.main import app
from app.models import Decision

load_dotenv()


def _load_fixture_module():
    name = "taskc_fixture_repo"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("context") / "taskc_fixture_repo.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


fx = _load_fixture_module()

pytestmark = fx.needs_db


class Scripted(CompletionProvider):
    name = "fake"

    def __init__(self, *payloads: object) -> None:
        self.payloads = [p if isinstance(p, str) else json.dumps(p) for p in payloads]
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return Completion(
            text=self.payloads.pop(0),
            stop_reason="end_turn",
            served_model="claude-sonnet-5-5",
            input_tokens=10,
            output_tokens=10,
        )


def use_model(*payloads: object) -> Scripted:
    provider = Scripted(*payloads)
    service = RoutedModelService(provider=provider, recorder=lambda _c: None)
    app.dependency_overrides[get_context_model_service] = lambda: service
    return provider


@pytest.fixture(autouse=True)
def _clear_model_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_context_model_service, None)


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> Iterator:
    repo = fx.create_scanned_repo(tmp_path_factory.mktemp("ctxapi"))
    yield repo
    fx.delete_scanned_repo(repo)


TITLE = "Rate limit checkout"
INTENT = (
    "Limit each user to ten checkouts per minute.\n\n"
    "Change place_order in svc/orders.py and keep svc/store.py untouched."
)
EXTRACTION = {
    "requirements": [
        {
            "key": "R1",
            "kind": "objective",
            "statement": "Rate limit checkout.",
            "source_chunk": "T.1",
            "quote": "Rate limit checkout",
        },
        {
            "key": "R2",
            "kind": "functional",
            "statement": "Allow each user ten checkouts per minute.",
            "source_chunk": "T.2",
            "quote": "Limit each user to ten checkouts per minute",
            "priority": "must",
        },
    ],
    "open_questions": [],
}
NO_CONTRADICTIONS = {"contradictions": []}


@pytest.fixture
def task(client, auth_header, scanned) -> str:
    resp = client.post(
        f"/repos/{scanned.repo_id}/tasks",
        json={"title": TITLE, "intent": INTENT},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit_sha"] == scanned.commit_sha
    return body["id"]


# --------------------------------------------------------- authorization

NOUUID = "00000000-0000-4000-8000-000000000001"
TASK_ROUTES = [
    ("GET", "/tasks/{t}"),
    ("POST", "/tasks/{t}/documents"),
    ("POST", "/tasks/{t}/requirements/extract"),
    ("GET", "/tasks/{t}/constraints"),
    ("POST", "/tasks/{t}/constraints"),
    ("PATCH", f"/tasks/{{t}}/constraints/{NOUUID}"),
    ("DELETE", f"/tasks/{{t}}/constraints/{NOUUID}"),
    ("POST", f"/tasks/{{t}}/constraints/{NOUUID}/confirm"),
    ("POST", f"/tasks/{{t}}/decisions/{NOUUID}/resolve"),
    ("POST", "/tasks/{t}/context"),
    ("GET", "/tasks/{t}/context"),
    ("GET", "/tasks/{t}/export?format=claude_md"),
    ("POST", "/tasks/{t}/reground"),
]
REPO_ROUTES = [
    ("POST", "/repos/{r}/tasks"),
    ("GET", "/repos/{r}/constraints"),
    ("POST", "/repos/{r}/api-tokens"),
    ("GET", "/repos/{r}/api-tokens"),
    ("DELETE", f"/repos/{{r}}/api-tokens/{NOUUID}"),
]


@pytest.mark.parametrize(("method", "path"), TASK_ROUTES)
def test_task_routes_require_a_token(client, task, method, path) -> None:
    assert client.request(method, path.format(t=task)).status_code == 401


@pytest.mark.parametrize(("method", "path"), REPO_ROUTES)
def test_repo_routes_require_a_token(client, scanned, method, path) -> None:
    assert client.request(method, path.format(r=scanned.repo_id)).status_code == 401


@pytest.mark.parametrize(("method", "path"), TASK_ROUTES)
def test_non_member_is_forbidden_on_every_task_route(
    client, auth_header, make_user, task, method, path
) -> None:
    outsider = make_user()
    resp = client.request(method, path.format(t=task), headers=auth_header(outsider), json={})
    assert resp.status_code == 403, (method, path, resp.text)


@pytest.mark.parametrize(("method", "path"), REPO_ROUTES)
def test_non_member_is_forbidden_on_repo_routes(
    client, auth_header, make_user, scanned, method, path
) -> None:
    resp = client.request(
        method, path.format(r=scanned.repo_id), headers=auth_header(make_user()), json={}
    )
    assert resp.status_code == 403, (method, path, resp.text)


@pytest.mark.parametrize(("method", "path"), TASK_ROUTES)
def test_unknown_task_is_404(client, auth_header, scanned, method, path) -> None:
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        resp = client.request(
            method, path.format(t=bad), headers=auth_header(scanned.user_id), json={}
        )
        assert resp.status_code == 404, (method, path, bad, resp.text)


def test_member_of_another_project_cannot_reach_the_task(
    client, auth_header, make_user, task
) -> None:
    other = make_user()
    with worker_connection() as conn:
        create_project(conn, "project Y", other)  # a member of Y, not of X
    h = auth_header(other)
    assert client.get(f"/tasks/{task}", headers=h).status_code == 403
    assert client.get(f"/tasks/{task}/context", headers=h).status_code == 403
    assert client.get(f"/tasks/{task}/export", headers=h).status_code == 403
    body = {"kind": "other", "scope": "repo", "statement": "x", "severity": "soft"}
    assert client.post(f"/tasks/{task}/constraints", headers=h, json=body).status_code == 403


# ------------------------------------------------------------- happy path


def test_ledger_pass_is_recorded_as_its_own_pass_run(scanned) -> None:
    with worker_connection() as conn:
        runs = {r.analyzer: r for r in list_pass_runs(conn, scanned.repo_id)}
    assert runs["constraint-ledger"].status.value == "succeeded"


def test_create_task_validates_commit_and_title(client, auth_header, scanned) -> None:
    h = auth_header(scanned.user_id)
    url = f"/repos/{scanned.repo_id}/tasks"
    assert client.post(url, json={"title": "x", "commit_sha": "nope"}, headers=h).status_code == 404
    assert client.post(url, json={"title": " "}, headers=h).status_code == 422


def test_full_flow_task_constraints_context_export(client, auth_header, scanned, task) -> None:
    h = auth_header(scanned.user_id)

    # --- extraction through the (fake) ModelService
    use_model(EXTRACTION, NO_CONTRADICTIONS)
    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_status"] == "ready"
    assert len(body["requirements"]) >= 2
    assert {"objective", "functional"} <= {r["kind"] for r in body["requirements"]}

    # --- user-stated constraint CRUD
    resp = client.post(
        f"/tasks/{task}/constraints",
        headers=h,
        json={
            "kind": "do_not_touch",
            "scope": "svc/store.py",
            "statement": "Do not modify the store layer.",
            "severity": "hard",
        },
    )
    assert resp.status_code == 201, resp.text
    cons = resp.json()
    assert cons["source"] == "user_stated" and cons["origin"] == "configured"
    assert cons["task_id"] == task and cons["analyzer"] is None and not cons["confirmed"]
    cid = cons["id"]

    resp = client.patch(f"/tasks/{task}/constraints/{cid}", headers=h, json={"severity": "soft"})
    assert resp.status_code == 200 and resp.json()["severity"] == "soft"
    blank = client.patch(f"/tasks/{task}/constraints/{cid}", headers=h, json={"statement": ""})
    assert blank.status_code == 422

    resp = client.post(f"/tasks/{task}/constraints/{cid}/confirm", headers=h)
    assert resp.status_code == 200 and resp.json()["confirmed"] is True

    listed = client.get(f"/tasks/{task}/constraints", headers=h).json()["constraints"]
    assert {c["source"] for c in listed} == {"user_stated", "auto_detected"}

    repo_wide = client.get(f"/repos/{scanned.repo_id}/constraints", headers=h).json()
    assert repo_wide["commit_sha"] == scanned.commit_sha
    assert repo_wide["constraints"]
    assert all(c["task_id"] is None for c in repo_wide["constraints"])
    assert cid not in {c["id"] for c in repo_wide["constraints"]}

    # a repo-level detection can be confirmed through the task, never rewritten
    detected = next(c for c in listed if c["source"] == "auto_detected")
    confirm_url = f"/tasks/{task}/constraints/{detected['id']}/confirm"
    assert client.post(confirm_url, headers=h).status_code == 200
    edit = client.patch(
        f"/tasks/{task}/constraints/{detected['id']}", headers=h, json={"severity": "soft"}
    )
    assert edit.status_code == 409
    assert client.delete(f"/tasks/{task}/constraints/{uuid.uuid4()}", headers=h).status_code == 404

    # --- decisions
    with worker_connection() as conn:
        (decision_id,) = insert_decisions(
            conn,
            [
                Decision(
                    task_id=task,
                    repo_id=scanned.repo_id,
                    commit_sha=scanned.commit_sha,
                    question="Which limiter?",
                    origin="inferred",
                    analyzer="test",
                    analyzer_version="1",
                )
            ],
        )
    resolve = f"/tasks/{task}/decisions/{decision_id}/resolve"
    resp = client.post(resolve, headers=h, json={"decision": "Use a token bucket."})
    assert resp.status_code == 200, resp.text
    assert resp.json()["locked"] is True and resp.json()["status"] == "decided"
    assert client.post(resolve, headers=h, json={"decision": "again"}).status_code == 409

    # --- no package yet
    assert client.get(f"/tasks/{task}/context", headers=h).status_code == 404
    assert client.get(f"/tasks/{task}/export", headers=h).status_code == 404

    # --- assemble
    small = client.post(f"/tasks/{task}/context", headers=h, json={"token_budget": 10})
    assert small.status_code == 422
    resp = client.post(f"/tasks/{task}/context", headers=h, json={"token_budget": 12000})
    assert resp.status_code == 201, resp.text
    pkg = resp.json()
    assert pkg["analysis"] == "static"
    assert pkg["package"]["status"] == "assembled"
    assert pkg["freshness"]["state"] == "current"
    assert {"task_intent", "constraint"} <= {i["kind"] for i in pkg["items"]}

    got = client.get(f"/tasks/{task}/context", headers=h).json()
    assert got["freshness"]["state"] == "current"
    assert got["package"]["id"] == pkg["package"]["id"]

    state = client.get(f"/tasks/{task}", headers=h).json()
    assert state["task"]["id"] == task
    assert state["current_package_id"] == pkg["package"]["id"]
    assert state["latest_package"]["state"] == "current"
    assert state["requirements"] and state["constraints"] and state["decisions"]

    # --- export
    for fmt, filename in EXPORT_FILENAMES.items():
        resp = client.get(f"/tasks/{task}/export", params={"format": fmt}, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/markdown")
        assert filename in resp.headers["content-disposition"]
        assert "Static analysis" in resp.text
        assert scanned.commit_sha in resp.text
    assert client.get(f"/tasks/{task}/export?format=pdf", headers=h).status_code == 422

    # --- a change after assembly makes the package stale: labelled, export refused
    extra = {"kind": "other", "scope": "repo", "statement": "Keep it small.", "severity": "soft"}
    assert client.post(f"/tasks/{task}/constraints", headers=h, json=extra).status_code == 201
    stale = client.get(f"/tasks/{task}/context", headers=h).json()
    assert stale["freshness"]["state"] == "stale" and stale["freshness"]["current"] is False
    assert stale["freshness"]["reason"]
    refused = client.get(f"/tasks/{task}/export", headers=h)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "package_not_current"
    allowed = client.get(f"/tasks/{task}/export", params={"allow_stale": "true"}, headers=h)
    assert allowed.status_code == 200 and "STALE" in allowed.text

    # --- regenerating makes it current again
    again = client.post(f"/tasks/{task}/context", headers=h).json()
    assert again["package"]["version"] == pkg["package"]["version"] + 1
    assert again["freshness"]["state"] == "current"
    assert client.get(f"/tasks/{task}/export", headers=h).status_code == 200

    # --- deleting a user constraint makes the package stale again
    assert client.delete(f"/tasks/{task}/constraints/{cid}", headers=h).status_code == 204
    assert client.get(f"/tasks/{task}/context", headers=h).json()["freshness"]["state"] == "stale"

    # --- reground
    unknown = client.post(
        f"/tasks/{task}/reground", headers=h, json={"commit_sha": "nope", "reason": "x"}
    )
    assert unknown.status_code == 404
    resp = client.post(
        f"/tasks/{task}/reground",
        headers=h,
        json={"commit_sha": scanned.commit_sha, "reason": "re-check"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["previous_commit_sha"] == scanned.commit_sha


# ------------------------------------------------------- extraction errors


def test_extraction_failure_is_502_and_stores_nothing(client, auth_header, scanned, task) -> None:
    h = auth_header(scanned.user_id)
    use_model("not json", "still not json")
    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h)
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["error"] == "model_output_invalid"
    assert client.get(f"/tasks/{task}", headers=h).json()["requirements"] == []


def test_images_need_an_explicit_flag(client, auth_header, scanned, task) -> None:
    h = auth_header(scanned.user_id)
    use_model()
    files = {"files": ("diagram.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")}
    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h, files=files)
    assert resp.status_code == 400 and "include_images" in resp.json()["detail"]


def test_document_caps_and_types(client, auth_header, scanned, task) -> None:
    h = auth_header(scanned.user_id)
    url = f"/tasks/{task}/documents"
    bad_type = {"files": ("tool.exe", b"MZ", "application/octet-stream")}
    assert client.post(url, headers=h, files=bad_type).status_code == 400

    big = {"files": ("big.txt", b"a" * (DEFAULT_LIMITS.max_document_bytes + 1), "text/plain")}
    assert client.post(url, headers=h, files=big).status_code == 413

    ok = {"files": ("notes.md", b"# Notes\n\nMust be fast.\n", "text/markdown")}
    resp = client.post(url, headers=h, files=ok)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] is False
    assert resp.json()["documents"][1]["name"] == "notes.md"


# -------------------------------------------------------------- API tokens


def _add_member(owner_url: str, project_id: str, user_id: str) -> None:
    with psycopg.connect(owner_url, connect_timeout=10) as conn:
        conn.execute(
            "insert into codeaxis.project_memberships (project_id, user_id, role) "
            "values (%s, %s, 'member')",
            (project_id, user_id),
        )
        conn.commit()


def test_token_is_shown_once_and_owner_only(client, auth_header, make_user, scanned) -> None:
    h = auth_header(scanned.user_id)
    base = f"/repos/{scanned.repo_id}/api-tokens"
    resp = client.post(base, headers=h, json={"name": "ci agent"})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    token = created["token"]
    assert token.startswith("cxa_") and created["shown_once"] is True
    assert "token_hash" not in json.dumps(created)
    token_id = created["record"]["id"]

    listing = client.get(base, headers=h)
    assert listing.status_code == 200
    assert token not in listing.text and "token_hash" not in listing.text
    assert token_id in {t["id"] for t in listing.json()["tokens"]}

    # a plain member of the project may read the repo but not manage tokens
    member = make_user()
    _add_member(os.environ["DATABASE_URL"], scanned.project_id, member)
    hm = auth_header(member)
    assert client.get(f"/repos/{scanned.repo_id}/constraints", headers=hm).status_code == 200
    assert client.get(base, headers=hm).status_code == 403
    assert client.post(base, headers=hm, json={"name": "x"}).status_code == 403
    assert client.delete(f"{base}/{token_id}", headers=hm).status_code == 403

    assert client.delete(f"{base}/{uuid.uuid4()}", headers=h).status_code == 404
    assert client.delete(f"{base}/{token_id}", headers=h).status_code == 204
    after = client.get(base, params={"include_revoked": "true"}, headers=h)
    assert token not in after.text
    revoked = next(t for t in after.json()["tokens"] if t["id"] == token_id)
    assert revoked["revoked_at"] is not None
    assert token_id not in {t["id"] for t in client.get(base, headers=h).json()["tokens"]}
