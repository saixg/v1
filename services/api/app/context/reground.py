"""Task persistence across sessions and snapshots (build-plan.md Part 4.F.6;
Phase 4 exit criterion "context rebuilt after session end").

Two operations, both reading only what the database holds:

* `rebuild_task_context` reconstructs a task's whole state — grounding
  history, requirements, the constraint ledger, decisions, task components,
  packages and verification results — from rows alone. There is no client
  memory and no in-process cache to consult, so a new session, a new process
  or a new connection gets the same answer the old one would have. It is
  read-only and safe on `api_connection`.
* `reground_to_commit` moves a task to a newer snapshot via Task A's
  `reground_task`, marks every package built at another commit `stale`, and
  **reports** which requirements, constraints and task components are still
  pinned to an older commit and need re-derivation. It does not rewrite their
  `commit_sha`: evidence checked against one snapshot says nothing about
  another, so silently migrating those rows would claim verification that
  never happened (the same reasoning `reground_task`'s docstring gives).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import psycopg

from app.context.package import (
    PackageFreshness,
    check_package,
    mark_stale_if_snapshot_moved,
)
from app.knowledge import (
    get_task,
    list_constraints,
    list_context_items,
    list_context_packages,
    list_decisions,
    list_requirements,
    list_task_components,
    list_task_snapshots,
    list_verification_results,
    reground_task,
)
from app.knowledge.context import MAX_LIST_LIMIT
from app.models import (
    Constraint,
    ContextItem,
    ContextPackage,
    Decision,
    Requirement,
    Task,
    TaskComponent,
    TaskSnapshot,
    VerificationResult,
)

PinnedKind = Literal["requirement", "constraint", "task_component"]


@dataclass(frozen=True)
class PinnedRecord:
    """A row derived at a commit other than the task's active one. Listed so
    a human (or the extraction pass) re-derives it; never migrated."""

    kind: PinnedKind
    ref: str
    commit_sha: str


@dataclass
class TaskState:
    """Everything persisted about one task, rebuilt from the database."""

    task: Task
    active_snapshot: TaskSnapshot | None
    snapshots: list[TaskSnapshot]
    # At the task's active commit.
    requirements: list[Requirement]
    constraints: list[Constraint]
    components: list[TaskComponent]
    decisions: list[Decision]
    verification_results: list[VerificationResult]
    packages: list[PackageFreshness]
    # The newest package that may be served as current, with its items; None
    # when there is none (never a stale one standing in for it).
    current_package: ContextPackage | None
    current_items: list[ContextItem]
    pinned_elsewhere: list[PinnedRecord] = field(default_factory=list)
    # Inconsistencies between the task row and its grounding history, stated
    # rather than repaired here (this function is read-only).
    problems: list[str] = field(default_factory=list)


def _pinned(
    conn: psycopg.Connection, task: Task, snapshots: list[TaskSnapshot]
) -> list[PinnedRecord]:
    assert task.id is not None
    out: list[PinnedRecord] = []
    for r in list_requirements(conn, task.id, limit=MAX_LIST_LIMIT):
        if r.commit_sha != task.commit_sha and r.status != "rejected":
            out.append(PinnedRecord("requirement", f"requirement:{r.id}", r.commit_sha))
    older = sorted({s.commit_sha for s in snapshots if s.commit_sha != task.commit_sha})
    for sha in older:
        # Task-owned entries only: repo-level detections at an old commit
        # belong to that snapshot and are re-detected for the new one by the
        # detector, not re-derived per task.
        for c in list_constraints(
            conn, task.repo_id, sha, task_id=task.id, include_repo_level=False, limit=MAX_LIST_LIMIT
        ):
            out.append(PinnedRecord("constraint", f"constraint:{c.id}", c.commit_sha))
    for tc in list_task_components(conn, task.id, limit=MAX_LIST_LIMIT):
        if tc.commit_sha != task.commit_sha:
            out.append(PinnedRecord("task_component", f"node:{tc.node_id}", tc.commit_sha))
    return out


def rebuild_task_context(conn: psycopg.Connection, task_id: str) -> TaskState | None:
    """Reconstruct `task_id`'s state from the database only. Returns None for
    an unknown task. Read-only."""
    task = get_task(conn, task_id)
    if task is None or task.id is None:
        return None
    sha = task.commit_sha
    snapshots = list_task_snapshots(conn, task.id)
    active = [s for s in snapshots if s.status == "active"]
    problems: list[str] = []
    if not active:
        problems.append("no active grounding row in task_snapshots")
    elif active[0].commit_sha != sha:
        problems.append(f"active grounding is {active[0].commit_sha} but the task row says {sha}")

    packages = [
        check_package(conn, p, task)
        for p in list_context_packages(conn, task.id, limit=MAX_LIST_LIMIT)
    ]
    current = next((f.package for f in packages if f.current), None)
    items = (
        list_context_items(conn, task.id, package_id=current.id, limit=MAX_LIST_LIMIT)
        if current is not None and current.id is not None
        else []
    )
    return TaskState(
        task=task,
        active_snapshot=active[0] if active else None,
        snapshots=snapshots,
        requirements=list_requirements(conn, task.id, commit_sha=sha, limit=MAX_LIST_LIMIT),
        constraints=list_constraints(
            conn, task.repo_id, sha, task_id=task.id, limit=MAX_LIST_LIMIT
        ),
        components=list_task_components(conn, task.id, commit_sha=sha, limit=MAX_LIST_LIMIT),
        decisions=list_decisions(conn, task.id, limit=MAX_LIST_LIMIT),
        verification_results=list_verification_results(conn, task.id, limit=MAX_LIST_LIMIT),
        packages=packages,
        current_package=current,
        current_items=items,
        pinned_elsewhere=_pinned(conn, task, snapshots),
        problems=problems,
    )


@dataclass(frozen=True)
class RegroundReport:
    snapshot: TaskSnapshot
    previous_commit_sha: str
    stale_package_ids: list[str]
    # Rows that still describe an older commit. A list, not a migration.
    needs_rederivation: list[PinnedRecord]


def reground_to_commit(
    conn: psycopg.Connection, task_id: str, commit_sha: str, reason: str
) -> RegroundReport:
    """Move `task_id` to `commit_sha` (needs `worker_connection`).

    Marks every package not built at `commit_sha` stale and reports what is
    still pinned to an older commit. Re-grounding to the commit the task is
    already on is a no-op apart from the (idempotent) stale sweep."""
    task = get_task(conn, task_id)
    if task is None or task.id is None:
        raise LookupError(f"task {task_id} not found")
    if not reason.strip():
        # task_snapshots requires a reason on every non-active row.
        raise ValueError("a reground must state why the snapshot moved")
    previous = task.commit_sha
    snapshot = reground_task(conn, task.id, commit_sha, reason)
    stale = mark_stale_if_snapshot_moved(conn, task.id, include_input_changes=False)
    moved = get_task(conn, task.id)
    assert moved is not None
    return RegroundReport(
        snapshot=snapshot,
        previous_commit_sha=previous,
        stale_package_ids=stale,
        needs_rederivation=_pinned(conn, moved, list_task_snapshots(conn, task.id)),
    )
