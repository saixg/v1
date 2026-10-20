"""Phase 6 failure mode: a failed model call, through the real HTTP routes.

`tests/llm/test_model_service.py` proves the service's contract (validate, one
retry, then raise; every attempt recorded). This file proves what each route
that sits on top of it does with that failure. For every one the promise is the
same: the caller gets a typed error that says which kind of failure it was, and
nothing the model said, nor any partial result, is stored or shown. No network
and no API key: the provider is scripted.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app import blueprint_api
from app.context_api import get_context_model_service
from app.knowledge import (
    create_version,
    insert_components,
    set_current_version,
    worker_connection,
)
from app.llm import Completion, CompletionProvider, CompletionRequest, RoutedModelService
from app.main import app, get_model_service
from app.models import BlueprintComponent, BlueprintVersion

load_dotenv()


def _load_fixture_module():
    name = "taskc_fixture_repo"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent.parent / "context" / "taskc_fixture_repo.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fx = _load_fixture_module()

pytestmark = fx.needs_db


class Provider(CompletionProvider):
    """Plays a script of completions and exceptions, then fails loudly if the
    code under test asks for more than the script allows (a retry loop would)."""

    name = "fake"

    def __init__(self, *script: Completion | Exception | str | dict) -> None:
        self.script = list(script)
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("the code under test made more model calls than the script has")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, Completion):
            return item
        text = item if isinstance(item, str) else json.dumps(item)
        return done(text)


def done(text: str, stop: str = "end_turn") -> Completion:
    return Completion(
        text=text, stop_reason=stop, served_model="claude-sonnet-5-5",
        input_tokens=10, output_tokens=10,
    )


def service_for(provider: Provider) -> RoutedModelService:
    return RoutedModelService(provider=provider, recorder=lambda _call: None)


@pytest.fixture(autouse=True)
def _clean_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_context_model_service, None)
    app.dependency_overrides.pop(get_model_service, None)


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> Iterator:
    repo = fx.create_scanned_repo(tmp_path_factory.mktemp("modelfail"))
    yield repo
    fx.delete_scanned_repo(repo)


# The three ways a call can fail that the service distinguishes. The second and
# third are the schema-validation failure that exhausts the single retry.
FAILURES = {
    "provider_error": (
        [ConnectionError("upstream down")],
        "model_call_failed",
    ),
    "malformed_json_twice": (["this is not json", "{ nor is this"], "model_output_invalid"),
    "wrong_schema_twice": (
        # Every field of these schemas has a default, so a bare `{}` is a valid
        # (empty) answer; only a wrong *type* is a validation failure.
        [
            {"requirements": "not a list", "components": "nope", "claims": "nope"},
            {"requirements": 7, "components": 7, "claims": 7},
        ],
        "model_output_invalid",
    ),
    "refusal": ([done("", stop="refusal")], "model_call_failed"),
    "truncated": ([done('{"requirements": [', stop="max_tokens")], "model_call_failed"),
}


# --------------------------------------------------------------- extraction

INTENT = "Limit each user to ten checkouts per minute.\n\nChange place_order in svc/orders.py."
GOOD_EXTRACTION = {
    "requirements": [
        {
            "key": "R1",
            "kind": "objective",
            "statement": "Rate limit checkout.",
            "source_chunk": "T.1",
            "quote": "Rate limit checkout",
        }
    ],
    "open_questions": [],
}


@pytest.fixture
def task(client, auth_header, scanned) -> str:
    resp = client.post(
        f"/repos/{scanned.repo_id}/tasks",
        json={"title": "Rate limit checkout", "intent": INTENT},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.parametrize(("script", "error"), FAILURES.values(), ids=FAILURES.keys())
def test_extraction_failure_is_a_typed_502_and_keeps_the_previous_requirements(
    client, auth_header, scanned, task, script, error
) -> None:
    h = auth_header(scanned.user_id)
    good = Provider(GOOD_EXTRACTION, {"contradictions": []})
    app.dependency_overrides[get_context_model_service] = lambda: service_for(good)
    assert client.post(f"/tasks/{task}/requirements/extract", headers=h).status_code == 200
    before = client.get(f"/tasks/{task}", headers=h).json()

    bad = Provider(*script)
    app.dependency_overrides[get_context_model_service] = lambda: service_for(bad)
    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h)

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["error"] == error
    assert resp.json()["detail"]["task_id"] == task
    assert len(bad.requests) <= 2, "at most the original call and one retry"
    after = client.get(f"/tasks/{task}", headers=h).json()
    assert after["requirements"] == before["requirements"]
    assert after["task"]["status"] == before["task"]["status"]


def test_failure_of_the_second_extraction_call_discards_the_first(
    client, auth_header, scanned, task
) -> None:
    """Extraction is two model calls: the statements, then the contradiction
    check. If the check cannot run, the set must not be stored as though it had
    been checked."""
    h = auth_header(scanned.user_id)
    two_requirements = {
        "requirements": [
            *GOOD_EXTRACTION["requirements"],
            {
                "key": "R2",
                "kind": "functional",
                "statement": "Allow each user ten checkouts per minute.",
                "source_chunk": "T.2",
                "quote": "Limit each user to ten checkouts per minute",
            },
        ],
        "open_questions": [],
    }
    provider = Provider(two_requirements, ConnectionError("dropped between calls"))
    app.dependency_overrides[get_context_model_service] = lambda: service_for(provider)

    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h)

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["error"] == "model_call_failed"
    assert len(provider.requests) == 2
    assert client.get(f"/tasks/{task}", headers=h).json()["requirements"] == []


# --------------------------------------------------------------------- chat

QUESTION = "What does place_order do with the cart?"


@pytest.mark.parametrize(("script", "error"), FAILURES.values(), ids=FAILURES.keys())
def test_chat_failure_is_a_typed_502_and_stores_no_answer(
    client, auth_header, scanned, script, error
) -> None:
    h = auth_header(scanned.user_id)
    provider = Provider(*script)
    app.dependency_overrides[get_model_service] = lambda: service_for(provider)

    resp = client.post(
        f"/repos/{scanned.repo_id}/chat",
        params={"commit_sha": scanned.commit_sha},
        json={"message": QUESTION},
        headers=h,
    )

    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == error
    # Guards against passing vacuously: retrieval found evidence, so the model was asked.
    assert provider.requests, "the route never reached the model"
    history = client.get(
        f"/repos/{scanned.repo_id}/chat/{detail['thread_id']}",
        params={"commit_sha": scanned.commit_sha},
        headers=h,
    )
    assert history.status_code == 200, history.text
    assert [m for m in history.json()["messages"] if m["role"] == "assistant"] == []


# ---------------------------------------------------------------- blueprints


@pytest.fixture
def blueprint(client, auth_header, scanned, task) -> str:
    resp = client.post(
        f"/repos/{scanned.repo_id}/blueprints",
        json={"task_id": task, "title": "Rate limiting"},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_version(blueprint_id: str, scanned) -> None:
    with worker_connection() as conn:
        version = create_version(
            conn,
            BlueprintVersion(
                blueprint_id=blueprint_id, version=1, analyzer="t", analyzer_version="0"
            ),
        )
        assert version.id is not None
        insert_components(
            conn,
            version.id,
            [
                BlueprintComponent(
                    key="limiter",
                    name="Limiter",
                    kind="proposed",
                    mapping="new",
                    repo_id=scanned.repo_id,
                    commit_sha=scanned.commit_sha,
                )
            ],
        )
        set_current_version(conn, blueprint_id, version.id)


def _versions(client, h, blueprint_id: str) -> list[dict]:
    resp = client.get(f"/blueprints/{blueprint_id}/versions", headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["versions"] if isinstance(body, dict) else body


@pytest.mark.parametrize(("script", "_error"), FAILURES.values(), ids=FAILURES.keys())
def test_failed_generation_fails_the_job_visibly_and_creates_no_version(
    client, auth_header, scanned, blueprint, monkeypatch, script, _error
) -> None:
    h = auth_header(scanned.user_id)
    provider = Provider(*script)
    monkeypatch.setattr(blueprint_api, "get_blueprint_model_service", lambda: service_for(provider))

    resp = client.post(f"/blueprints/{blueprint}/generate", json={}, headers=h)

    assert resp.status_code == 202, resp.text
    job = client.get(f"/jobs/{resp.json()['job_id']}", headers=h).json()
    assert job["status"] == "failed"
    assert job["error"], "a failed job must say why"
    assert _versions(client, h, blueprint) == []
    # Not a dead end: a failed job does not block the next attempt.
    again = Provider(*script)
    monkeypatch.setattr(blueprint_api, "get_blueprint_model_service", lambda: service_for(again))
    assert client.post(f"/blueprints/{blueprint}/generate", json={}, headers=h).status_code == 202


@pytest.mark.parametrize(("script", "_error"), FAILURES.values(), ids=FAILURES.keys())
def test_failed_regeneration_is_a_502_and_writes_nothing(
    client, auth_header, scanned, blueprint, monkeypatch, script, _error
) -> None:
    h = auth_header(scanned.user_id)
    _seed_version(blueprint, scanned)
    before = _versions(client, h, blueprint)
    provider = Provider(*script)
    monkeypatch.setattr(blueprint_api, "get_blueprint_model_service", lambda: service_for(provider))

    resp = client.post(
        f"/blueprints/{blueprint}/regenerate", json={"component_keys": ["limiter"]}, headers=h
    )

    assert resp.status_code == 502, resp.text
    assert "design model call failed" in resp.json()["detail"]
    assert provider.requests, "regeneration never reached the model"
    assert _versions(client, h, blueprint) == before


def test_an_unknown_blueprint_never_reaches_the_model(client, auth_header, scanned, monkeypatch):
    provider = Provider()
    monkeypatch.setattr(blueprint_api, "get_blueprint_model_service", lambda: service_for(provider))
    resp = client.post(
        f"/blueprints/{uuid.uuid4()}/regenerate", json={}, headers=auth_header(scanned.user_id)
    )
    assert resp.status_code == 404
    assert provider.requests == []
