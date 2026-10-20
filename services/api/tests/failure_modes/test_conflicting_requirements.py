"""Phase 6 failure mode: conflicting requirements.

The context engine's rule (Part 4.F.3, `app/context/validate.py`) is that a
contradiction is *stored*, never resolved: both sides stay in the ledger,
unchanged, flagged ambiguous with a question naming the other, and the task is
blocked until a human decides. The unit tests in `tests/context/` prove each
check in isolation. These prove the property end to end: through the HTTP
route, into the database, and out the other side in the context package that
an agent would be handed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.context_api import get_context_model_service
from app.llm import Completion, CompletionProvider, CompletionRequest, RoutedModelService
from app.main import app

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


class Scripted(CompletionProvider):
    name = "fake"

    def __init__(self, *payloads: object) -> None:
        self.payloads = [p if isinstance(p, str) else json.dumps(p) for p in payloads]
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return Completion(
            text=self.payloads.pop(0), stop_reason="end_turn", served_model="claude-sonnet-5-5",
            input_tokens=10, output_tokens=10,
        )


def use_model(*payloads: object) -> Scripted:
    provider = Scripted(*payloads)
    service = RoutedModelService(provider=provider, recorder=lambda _c: None)
    app.dependency_overrides[get_context_model_service] = lambda: service
    return provider


@pytest.fixture(autouse=True)
def _clear_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_context_model_service, None)


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> Iterator:
    repo = fx.create_scanned_repo(tmp_path_factory.mktemp("conflict"))
    yield repo
    fx.delete_scanned_repo(repo)


TITLE = "Checkout limits"
INTENT = (
    "Orders over one hundred dollars must be approved by a manager.\n\n"
    "Orders of any size must complete without waiting for approval."
)
STATEMENT_A = "Require manager approval for orders over one hundred dollars."
STATEMENT_B = "Never block an order on approval, whatever its size."

OBJECTIVE = {
    "key": "R1",
    "kind": "objective",
    "statement": "Define how checkout treats approval.",
    "source_chunk": "T.1",
    "quote": "Checkout limits",
}
FIRST = {
    "key": "R2",
    "kind": "functional",
    "statement": STATEMENT_A,
    "source_chunk": "T.2",
    "quote": "Orders over one hundred dollars must be approved by a manager",
}
SECOND = {
    "key": "R3",
    "kind": "functional",
    "statement": STATEMENT_B,
    "source_chunk": "T.3",
    "quote": "Orders of any size must complete without waiting for approval",
}
CRITERION = {
    "key": "R4",
    "kind": "acceptance_criteria",
    "statement": "A two hundred dollar order is either held or completed, as decided.",
    "source_chunk": "T.2",
    "quote": "Orders over one hundred dollars must be approved by a manager",
    "verifies": ["R2"],
}
EXTRACTION = {"requirements": [OBJECTIVE, FIRST, SECOND, CRITERION], "open_questions": []}
EXPLANATION = "One requires approval above a threshold, the other forbids any approval wait."


@pytest.fixture
def task(client, auth_header, scanned) -> str:
    resp = client.post(
        f"/repos/{scanned.repo_id}/tasks",
        json={"title": TITLE, "intent": INTENT},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def by_statement(requirements: list[dict], statement: str) -> dict:
    (row,) = [r for r in requirements if r["statement"] == statement]
    return row


def test_a_contradiction_is_stored_flagged_and_left_unresolved(
    client, auth_header, scanned, task
) -> None:
    h = auth_header(scanned.user_id)
    # The model flags the real pair twice (once reversed), and also invents a
    # pair, a self-pair and a pair naming a requirement that does not exist.
    use_model(
        EXTRACTION,
        {
            "contradictions": [
                {"first": "R2", "second": "R3", "explanation": EXPLANATION},
                {"first": "R3", "second": "R2", "explanation": EXPLANATION},
                {"first": "R2", "second": "R9", "explanation": "invented"},
                {"first": "R3", "second": "R3", "explanation": "self"},
            ]
        },
    )

    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_status"] == "blocked"
    assert [(c["first"], c["second"]) for c in body["contradictions"]] == [("R2", "R3")]
    assert [d["reason"] for d in body["dropped"]] == ["invalid_pair", "invalid_pair"]

    a = by_statement(body["requirements"], STATEMENT_A)
    b = by_statement(body["requirements"], STATEMENT_B)
    # Neither side was dropped, merged, reworded or ranked.
    assert a["status"] == b["status"] == "ambiguous"
    assert a["source_ref"]["contradicts"] == ["R3"]
    assert b["source_ref"]["contradicts"] == ["R2"]
    # Each question names the *other* side and says the flag is unverified model output.
    assert "R3" in a["question"] and "R2" in b["question"]
    assert "unverified" in a["question"] and "unverified" in b["question"]
    # The reversed duplicate did not double the question.
    assert a["question"].count("Possibly contradicts") == 1
    # The requirement that conflicts with nothing is not dragged into it.
    objective = by_statement(body["requirements"], OBJECTIVE["statement"])
    assert objective["status"] == "open"
    assert "contradicts" not in objective["source_ref"]

    # What the API returns is what was stored.
    state = client.get(f"/tasks/{task}", headers=h).json()
    assert state["task"]["status"] == "blocked"
    assert by_statement(state["requirements"], STATEMENT_A)["status"] == "ambiguous"


def test_the_context_package_carries_the_conflict_as_unresolved_not_as_a_choice(
    client, auth_header, scanned, task
) -> None:
    h = auth_header(scanned.user_id)
    use_model(
        EXTRACTION,
        {"contradictions": [{"first": "R2", "second": "R3", "explanation": EXPLANATION}]},
    )
    assert client.post(f"/tasks/{task}/requirements/extract", headers=h).status_code == 200

    resp = client.post(f"/tasks/{task}/context", headers=h, json={})

    assert resp.status_code == 201, resp.text[:500]
    pkg = resp.json()
    kinds = [u["kind"] for u in pkg["package"]["unresolved"]]
    assert kinds.count("ambiguous_requirement") >= 2, kinds
    questions = [i["content"] for i in pkg["items"] if i["kind"] == "open_question"]
    assert any("R3" in q and "Possibly contradicts" in q for q in questions)
    assert any("R2" in q and "Possibly contradicts" in q for q in questions)
    # Both contradictory statements stay in the package as requirements; neither
    # was picked as the winner.
    required = " ".join(i["content"] for i in pkg["items"] if i["kind"] == "requirement")
    assert STATEMENT_A in required and STATEMENT_B in required


def test_a_rerun_with_no_contradiction_clears_the_flag_instead_of_carrying_it(
    client, auth_header, scanned, task
) -> None:
    h = auth_header(scanned.user_id)
    use_model(
        EXTRACTION,
        {"contradictions": [{"first": "R2", "second": "R3", "explanation": EXPLANATION}]},
    )
    assert client.post(f"/tasks/{task}/requirements/extract", headers=h).json()["task_status"] == (
        "blocked"
    )

    # The developer edits the task so the second statement is gone; extraction runs again.
    use_model({"requirements": [OBJECTIVE, FIRST, CRITERION], "open_questions": []},
              {"contradictions": []})
    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contradictions"] == []
    assert by_statement(body["requirements"], STATEMENT_A)["status"] == "open"
    assert not any(r["statement"] == STATEMENT_B for r in body["requirements"])
    assert body["task_status"] == "ready"


def test_a_contradiction_that_only_exists_in_a_dropped_requirement_blocks_nothing(
    client, auth_header, scanned, task
) -> None:
    """R3's quote is not in the source, so grounding drops it. The contradiction
    check never sees it, and a stale reference to it must not block the task."""
    h = auth_header(scanned.user_id)
    ungrounded = {**SECOND, "quote": "a sentence that appears nowhere in the task input"}
    use_model(
        {"requirements": [OBJECTIVE, FIRST, ungrounded, CRITERION], "open_questions": []},
        {"contradictions": [{"first": "R2", "second": "R3", "explanation": EXPLANATION}]},
    )

    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contradictions"] == []
    assert [d["reason"] for d in body["dropped"]].count("quote_not_in_source") == 1
    assert [d["reason"] for d in body["dropped"]].count("invalid_pair") == 1
    assert body["task_status"] == "ready"
    assert not any(r["statement"] == STATEMENT_B for r in body["requirements"])


def test_a_requirement_against_a_hard_do_not_touch_is_flagged_and_the_constraint_is_untouched(
    client, auth_header, scanned, task
) -> None:
    h = auth_header(scanned.user_id)
    made = client.post(
        f"/tasks/{task}/constraints",
        headers=h,
        json={
            "kind": "do_not_touch",
            "scope": "svc/store.py",
            "statement": "The store layer is frozen.",
            "severity": "hard",
        },
    )
    assert made.status_code == 201, made.text
    constraint = made.json()
    rewrite = {
        "key": "R2",
        "kind": "functional",
        "statement": "Rewrite svc/store.py so orders are held for approval.",
        "source_chunk": "T.2",
        "quote": "Orders over one hundred dollars must be approved by a manager",
    }
    use_model(
        {"requirements": [OBJECTIVE, rewrite, CRITERION], "open_questions": []},
        {"contradictions": []},
    )

    resp = client.post(f"/tasks/{task}/requirements/extract", headers=h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_status"] == "blocked"
    (conflict,) = body["constraint_conflicts"]
    assert conflict["constraint_id"] == constraint["id"]
    flagged = by_statement(body["requirements"], rewrite["statement"])
    assert flagged["status"] == "ambiguous"
    assert "svc/store.py" in flagged["question"]
    # The conflict was surfaced, not settled: the constraint is exactly as it was.
    listed = client.get(f"/tasks/{task}/constraints", headers=h).json()["constraints"]
    after = next(c for c in listed if c["id"] == constraint["id"])
    assert (after["severity"], after["scope"], after["statement"]) == (
        "hard", "svc/store.py", "The store layer is frozen.",
    )
