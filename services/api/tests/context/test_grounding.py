"""Task grounding (build-plan.md Part 4.F step 4): literal mentions become
`parsed` components, retrieval-ranked guesses become `inferred` with a
confidence, and nothing but a real node id ever lands in `task_components`.

Needs the live database (the fixture repo is really scanned); no model call.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.context.grounding import ground_task, path_candidates
from app.knowledge import api_connection, list_nodes, list_task_components, worker_connection


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


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    if not fx.db_available():
        pytest.skip("DATABASE_URL / WORKER_DATABASE_URL not set")
    scanned = fx.create_scanned_repo(tmp_path_factory.mktemp("ground_repo"))
    yield scanned
    fx.delete_scanned_repo(scanned)


def test_path_candidates_find_paths_in_prose_only() -> None:
    found = path_candidates("Edit `svc/orders.py` and ./web/ui.py, e.g. soon. Also svc/")
    assert "svc/orders.py" in found
    assert "web/ui.py" in found
    assert found.index("svc/orders.py") < found.index("web/ui.py")


@fx.needs_db
def test_literal_path_is_primary_parsed_and_dependents_are_impacted(repo) -> None:
    task = fx.new_task(repo, "Change svc/orders.py so place_order rejects empty carts")
    with worker_connection() as conn:
        result = ground_task(conn, task.id, write=True, use_retrieval=False)
    by_path = {}
    with api_connection() as conn:
        nodes = {n.id: n for n in list_nodes(conn, repo.repo_id, repo.commit_sha, kind="file")}
        stored = list_task_components(conn, task.id, commit_sha=repo.commit_sha)
    for c in stored:
        by_path[nodes[c.node_id].path] = c
    assert by_path["svc/orders.py"].relevance == "primary"
    assert by_path["svc/orders.py"].origin == "parsed"
    # api.py and the test import orders.py: reverse dependencies, not guesses.
    assert by_path["svc/api.py"].relevance == "impacted"
    assert by_path["svc/test_orders.py"].relevance == "impacted"
    assert result.literals.unmatched_paths == []


@fx.needs_db
def test_every_component_references_a_real_node_in_this_snapshot(repo) -> None:
    task = fx.new_task(repo, "Add a discount to checkout orders in svc/orders.py")
    with worker_connection() as conn:
        ground_task(conn, task.id, write=True)  # retrieval on: inferred rows allowed
    with api_connection() as conn:
        all_ids = {
            n.id
            for kind in ("file", "directory", "symbol")
            for n in list_nodes(conn, repo.repo_id, repo.commit_sha, kind=kind)
        }
        stored = list_task_components(conn, task.id, commit_sha=repo.commit_sha)
    assert stored
    assert all(c.node_id in all_ids for c in stored)
    for c in stored:
        if c.origin == "inferred":
            # A guess without a confidence would read as a fact.
            assert c.confidence is not None
            assert c.relevance in ("supporting", "impacted")
        else:
            assert c.origin == "parsed"


@fx.needs_db
def test_ambiguous_and_unmatched_paths_are_reported_not_guessed(repo) -> None:
    task = fx.new_task(repo, "Fix helpers.py and also svc/missing_module.py")
    with worker_connection() as conn:
        result = ground_task(conn, task.id, write=True, use_retrieval=False)
    assert set(result.literals.ambiguous_paths["helpers.py"]) == {
        "svc/helpers.py",
        "web/helpers.py",
    }
    assert "svc/missing_module.py" in result.literals.unmatched_paths
    with api_connection() as conn:
        stored = list_task_components(conn, task.id, commit_sha=repo.commit_sha)
    # Neither helpers file may be claimed from a path that fits both.
    assert stored == []


@fx.needs_db
def test_regrounding_replaces_rather_than_accumulates(repo) -> None:
    task = fx.new_task(repo, "Change svc/store.py")
    with worker_connection() as conn:
        ground_task(conn, task.id, write=True, use_retrieval=False)
        first = len(list_task_components(conn, task.id, commit_sha=repo.commit_sha))
        ground_task(conn, task.id, write=True, use_retrieval=False)
        second = len(list_task_components(conn, task.id, commit_sha=repo.commit_sha))
    assert first == second > 0
