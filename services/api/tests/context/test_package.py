"""Context-package assembly (build-plan.md Part 4.F step 5): the budget maths
(pure, no database) and a real assembly over a scanned fixture repository.
No model is called anywhere.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.context.package import (
    SECTION_ORDER,
    TAIL_SECTIONS,
    Candidate,
    PackageInputs,
    assemble_context_package,
    current_package,
    plan_package,
)
from app.context.reground import reground_to_commit
from app.knowledge import (
    api_connection,
    insert_constraints,
    list_context_items,
    replace_requirements,
    worker_connection,
)
from app.models import (
    Citation,
    Constraint,
    ContextEvidence,
    ContextItem,
    ContextItemKind,
    Requirement,
    Task,
)


def _load_fixture_module():
    name = "taskc_fixture_repo"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("taskc_fixture_repo.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


fx = _load_fixture_module()


# ----------------------------------------------------------- pure budgeting


def _cand(kind: ContextItemKind, ref: str, tokens: int, severity: str | None = None) -> Candidate:
    docs = [{"source": "constraint_ledger", "severity": severity}] if severity else []
    spans = [Citation(path="a.py", line_start=1, line_end=2)] if kind in ("code", "test") else []
    item = ContextItem(
        task_id="t",
        repo_id="r",
        commit_sha="c1",
        kind=kind,
        content=f"{kind} {ref}",
        origin="parsed",
        evidence=ContextEvidence(documents=docs, spans=spans),
        token_count=tokens,
        analyzer="test",
        analyzer_version="0",
    )
    return Candidate(item, ref)


def _inputs(sections: dict[str, list[Candidate]], retrieval_dropped: int = 0) -> PackageInputs:
    task = Task(project_id="p", repo_id="r", commit_sha="c1", title="t", intent="i", created_by="u")
    full: dict[str, list[Candidate]] = {k: [] for k in SECTION_ORDER}
    full.update(sections)
    return PackageInputs(task=task, sections=full, retrieval_dropped=retrieval_dropped)


def test_constraints_survive_a_tiny_budget_and_overflow_is_reported() -> None:
    constraints = [_cand("constraint", f"c{i}", 40, "hard" if i < 2 else "soft") for i in range(5)]
    inputs = _inputs(
        {
            "task_intent": [_cand("task_intent", "intent", 10)],
            "constraint": constraints,
            "code": [_cand("code", "x", 5)],
        }
    )
    plan = plan_package(inputs, token_budget=20)
    kept = [i for i in plan.items if i.kind == "constraint"]
    assert len(kept) == 5  # nothing dropped from the ledger
    overflow = next(u for u in plan.unresolved if u["kind"] == "constraints_exceed_budget")
    assert overflow["hard_constraint_count"] == 2
    assert plan.status == "invalid"
    # The tail got zero budget, and what it could not hold is counted.
    code = next(s for s in plan.manifest["sections"] if s["kind"] == "code")
    assert code["included"] == 0 and code["dropped"] == 1


def test_within_budget_package_is_assembled_and_tail_respects_budget() -> None:
    sections = {
        "constraint": [_cand("constraint", "c", 30, "hard")],
        "requirement": [_cand("requirement", f"r{i}", 20) for i in range(10)],
        "code": [_cand("code", f"k{i}", 25) for i in range(10)],
    }
    plan = plan_package(_inputs(sections, retrieval_dropped=3), token_budget=230)
    assert plan.status == "assembled"
    budget = plan.manifest["budget"]
    assert budget["tail_budget"] == 200
    tail_tokens = sum(i.token_count for i in plan.items if i.kind in TAIL_SECTIONS)
    assert tail_tokens <= budget["tail_budget"]
    assert plan.token_count == 30 + tail_tokens <= 230
    assert plan.manifest["dropped_total"] > 3  # tail drops + the retrieval's own 3


def test_dropped_items_are_counted_per_section_by_reference_only() -> None:
    sections = {"requirement": [_cand("requirement", f"r{i}", 50) for i in range(6)]}
    plan = plan_package(_inputs(sections), token_budget=100)
    req = next(s for s in plan.manifest["sections"] if s["kind"] == "requirement")
    assert req["included"] + req["dropped"] == 6
    assert req["dropped"] == len(req["dropped_items"]) > 0
    # The manifest names what was dropped but never copies its text.
    assert all(set(d) == {"ref", "tokens"} for d in req["dropped_items"])


def test_sections_are_emitted_in_spec_order_with_hard_constraints_first() -> None:
    sections = {
        "open_question": [_cand("open_question", "q", 5)],
        "test": [_cand("test", "t", 5)],
        "code": [_cand("code", "k", 5)],
        "architecture": [_cand("architecture", "a", 5)],
        "requirement": [_cand("requirement", "r", 5)],
        "constraint": [
            _cand("constraint", "hard", 5, "hard"),
            _cand("constraint", "soft", 5, "soft"),
        ],
        "task_intent": [_cand("task_intent", "i", 5)],
        "limitation": [_cand("limitation", "l", 5)],
        "convention": [_cand("convention", "v", 5)],
    }
    plan = plan_package(_inputs(sections), token_budget=1000)
    rank = {k: n for n, k in enumerate(SECTION_ORDER)}
    ranks = [rank[i.kind] for i in plan.items]
    assert ranks == sorted(ranks)
    assert [i.ordinal for i in plan.items] == list(range(len(plan.items)))
    assert plan.items[0].kind == "task_intent"
    assert plan.manifest["order"] == list(SECTION_ORDER)


# ------------------------------------------------------- database assembly


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    if not fx.db_available():
        pytest.skip("DATABASE_URL / WORKER_DATABASE_URL not set")
    scanned = fx.create_scanned_repo(tmp_path_factory.mktemp("package_repo"))
    yield scanned
    fx.delete_scanned_repo(scanned)


def _constraint(repo, task_id: str, severity: str, statement: str) -> Constraint:
    return Constraint(
        task_id=task_id,
        repo_id=repo.repo_id,
        commit_sha=repo.commit_sha,
        kind="do_not_touch",
        scope="svc/store.py",
        statement=statement,
        severity=severity,  # pyright: ignore[reportArgumentType]
        source="user_stated",
        origin="configured",
    )


def _requirement(repo, task_id: str, statement: str) -> Requirement:
    return Requirement(
        task_id=task_id,
        repo_id=repo.repo_id,
        commit_sha=repo.commit_sha,
        kind="functional",
        statement=statement,
        origin="parsed",
        analyzer="test",
        analyzer_version="0",
    )


@fx.needs_db
def test_assembly_orders_sections_persists_retrieval_and_cites_real_lines(repo) -> None:
    task = fx.new_task(repo, "Make place_order in svc/orders.py reject empty carts")
    with worker_connection() as conn:
        insert_constraints(
            conn,
            [
                _constraint(repo, task.id, "soft", "Prefer small diffs."),
                _constraint(repo, task.id, "hard", "Do not edit svc/store.py."),
            ],
        )
        replace_requirements(
            conn, task.id, [_requirement(repo, task.id, "place_order rejects an empty cart")]
        )
        built = assemble_context_package(conn, task.id, token_budget=6000)
    pkg = built.package
    assert pkg.status == "assembled"
    assert pkg.commit_sha == repo.commit_sha
    # Retrieval stage outcomes are persisted, not discarded.
    assert pkg.retrieval["stages"]

    with api_connection() as conn:
        stored = list_context_items(conn, task.id, package_id=pkg.id)
    kinds = [i.kind for i in stored]
    rank = {k: n for n, k in enumerate(SECTION_ORDER)}
    assert [rank[k] for k in kinds] == sorted(rank[k] for k in kinds)
    assert kinds[0] == "task_intent"
    constraint_items = [i for i in stored if i.kind == "constraint"]
    assert [i.content for i in constraint_items][:2] == [
        "Do not edit svc/store.py.",
        "Prefer small diffs.",
    ]
    assert any(i.kind == "requirement" for i in stored)
    code = [i for i in stored if i.kind == "code"]
    assert code
    for item in stored:
        assert item.origin in ("parsed", "inferred", "configured")
        assert item.token_count > 0
        assert item.commit_sha == repo.commit_sha
    for item in code:
        assert item.evidence.spans
    # The deliberately unresolvable call in place_order is shown, not hidden.
    assert any(i.kind == "limitation" for i in stored)
    tail_tokens = sum(i.token_count for i in stored if i.kind in TAIL_SECTIONS)
    assert tail_tokens <= pkg.manifest["budget"]["tail_budget"]


@fx.needs_db
def test_tiny_budget_assembly_keeps_every_constraint_and_marks_package_invalid(repo) -> None:
    task = fx.new_task(repo, "Change svc/orders.py")
    with worker_connection() as conn:
        insert_constraints(
            conn,
            [
                _constraint(repo, task.id, "hard", f"Rule number {i}: keep it stable.")
                for i in range(6)
            ],
        )
        built = assemble_context_package(conn, task.id, token_budget=10)
    assert built.package.status == "invalid"
    assert any(u["kind"] == "constraints_exceed_budget" for u in built.package.unresolved)
    with api_connection() as conn:
        stored = list_context_items(conn, task.id, package_id=built.package.id)
    rules = [i for i in stored if i.kind == "constraint" and "Rule number" in i.content]
    assert len(rules) == 6
    # Tail got nothing; every section's loss is on the record.
    assert not [i for i in stored if i.kind in TAIL_SECTIONS]
    assert sum(s["dropped"] for s in built.package.manifest["sections"]) > 0


@fx.needs_db
def test_package_is_pinned_to_commit_and_goes_stale_when_snapshot_moves(repo) -> None:
    task = fx.new_task(repo, "Change svc/store.py")
    new_sha = f"{repo.commit_sha}-next"
    with worker_connection() as conn:
        built = assemble_context_package(conn, task.id, token_budget=4000)
        fresh = current_package(conn, task.id)
        assert fresh is not None and fresh.current and fresh.package.id == built.package.id
        report = reground_to_commit(conn, task.id, new_sha, "repository moved on")
        assert built.package.id in report.stale_package_ids
        after = current_package(conn, task.id)
    assert after is not None
    assert not after.current
    assert after.package.status == "stale"
