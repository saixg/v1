"""Task persistence and re-grounding (build-plan.md Part 4.F step 6, Phase 4
exit criterion "context rebuilt after session end"). Live database; no model.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.context.package import assemble_context_package
from app.context.reground import rebuild_task_context, reground_to_commit
from app.knowledge import (
    api_connection,
    get_task,
    insert_constraints,
    list_task_snapshots,
    replace_requirements,
    worker_connection,
)
from app.models import Constraint, Requirement


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
    scanned = fx.create_scanned_repo(tmp_path_factory.mktemp("reground_repo"))
    yield scanned
    fx.delete_scanned_repo(scanned)


def _seed(repo, task_id: str) -> None:
    with worker_connection() as conn:
        insert_constraints(
            conn,
            [
                Constraint(
                    task_id=task_id,
                    repo_id=repo.repo_id,
                    commit_sha=repo.commit_sha,
                    kind="do_not_touch",
                    scope="svc/store.py",
                    statement="Do not edit svc/store.py.",
                    severity="hard",
                    source="user_stated",
                    origin="configured",
                )
            ],
        )
        replace_requirements(
            conn,
            task_id,
            [
                Requirement(
                    task_id=task_id,
                    repo_id=repo.repo_id,
                    commit_sha=repo.commit_sha,
                    kind="functional",
                    statement="place_order rejects empty carts",
                    origin="parsed",
                    analyzer="test",
                    analyzer_version="0",
                )
            ],
        )


@fx.needs_db
def test_context_is_rebuilt_after_session_end_from_a_fresh_connection(repo) -> None:
    task = fx.new_task(repo, "Change svc/orders.py so place_order rejects empty carts")
    _seed(repo, task.id)
    with worker_connection() as conn:  # "session one"
        built = assemble_context_package(conn, task.id, token_budget=5000)

    # Session two: a brand-new read-only connection, nothing carried over.
    with api_connection() as conn:
        state = rebuild_task_context(conn, task.id)
    assert state is not None
    assert state.task.commit_sha == repo.commit_sha
    assert state.problems == []
    assert state.active_snapshot is not None
    assert state.active_snapshot.commit_sha == repo.commit_sha
    assert [r.statement for r in state.requirements] == ["place_order rejects empty carts"]
    assert [c.statement for c in state.constraints][:1] == ["Do not edit svc/store.py."]
    assert state.components
    assert state.current_package is not None
    assert state.current_package.id == built.package.id
    assert len(state.current_items) == len(built.items)
    assert state.pinned_elsewhere == []

    with api_connection() as conn:
        assert rebuild_task_context(conn, "00000000-0000-0000-0000-000000000000") is None


@fx.needs_db
def test_reground_marks_old_packages_stale_and_lists_what_needs_rederiving(repo) -> None:
    task = fx.new_task(repo, "Change svc/orders.py")
    _seed(repo, task.id)
    new_sha = f"{repo.commit_sha}-v2"
    with worker_connection() as conn:
        built = assemble_context_package(conn, task.id, token_budget=5000)
        report = reground_to_commit(conn, task.id, new_sha, "new commit landed")
        again = reground_to_commit(conn, task.id, new_sha, "new commit landed")

    assert report.previous_commit_sha == repo.commit_sha
    assert report.snapshot.commit_sha == new_sha
    assert report.stale_package_ids == [built.package.id]
    assert again.stale_package_ids == []  # idempotent
    kinds = {p.kind for p in report.needs_rederivation}
    assert {"requirement", "constraint", "task_component"} <= kinds
    assert all(p.commit_sha == repo.commit_sha for p in report.needs_rederivation)

    with api_connection() as conn:
        moved = get_task(conn, task.id)
        snaps = {s.commit_sha: s.status for s in list_task_snapshots(conn, task.id)}
        state = rebuild_task_context(conn, task.id)
    assert moved is not None and moved.commit_sha == new_sha
    assert snaps == {repo.commit_sha: "stale", new_sha: "active"}
    assert state is not None
    # No stale package stands in as current, and the old rows are still
    # reported as pinned to the older commit rather than silently migrated.
    assert state.current_package is None
    assert state.requirements == []
    assert {p.kind for p in state.pinned_elsewhere} >= {"requirement", "constraint"}


@fx.needs_db
def test_reground_requires_a_reason_and_a_known_task(repo) -> None:
    task = fx.new_task(repo, "Change svc/store.py")
    with worker_connection() as conn:
        with pytest.raises(ValueError):
            reground_to_commit(conn, task.id, "x", "   ")
        with pytest.raises(LookupError):
            reground_to_commit(conn, "00000000-0000-0000-0000-000000000000", "x", "why")
