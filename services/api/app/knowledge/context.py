"""Agent Context Engine tables: tasks, requirements, the constraint ledger,
decisions, context items/packages, task-snapshot grounding, verification
results and the per-repo MCP tokens (build-plan.md Part 4.F, Part 6).

Same rules as the rest of `app.knowledge`: this package is the only place that
knows SQL, every caller-supplied value is a bound `%s` parameter, and dynamic
WHERE fragments are assembled from a fixed internal whitelist only. Reads are
safe on the read-only `api_connection`; every write needs
`worker_connection`.

Two things are load-bearing across every function here:

* **Snapshot scoping.** The snapshot key in this repository is
  `(repo_id, commit_sha)`, not a `snapshot_id` uuid (see the comment block at
  the top of `supabase/migrations/20261020000000_context_engine.sql`). Every
  list function that can be snapshot-scoped takes `commit_sha` and filters on
  it, so rows derived at one commit can never answer a question about another.
* **Nothing here generates anything.** These functions persist and return what
  the extraction, detection and assembly passes produced, already validated.
  A missing row is an honest "not extracted", never a cue to invent one
  (Part 0.3).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Sequence
from datetime import datetime

import psycopg
from psycopg.rows import class_row

from app.models import (
    ApiToken,
    Constraint,
    ContextItem,
    ContextPackage,
    Decision,
    IssuedApiToken,
    Requirement,
    Task,
    TaskComponent,
    TaskSnapshot,
    VerificationResult,
)

_TASK_COLUMNS = """
    id, project_id, repo_id, commit_sha, title, intent, status,
    created_by, created_at, updated_at
"""

_REQUIREMENT_COLUMNS = """
    id, task_id, repo_id, commit_sha, kind, statement, priority, status,
    question, origin, source_ref, evidence, ordinal,
    confidence::float8 as confidence, model, analyzer, analyzer_version, created_at
"""

_CONSTRAINT_COLUMNS = """
    id, task_id, repo_id, commit_sha, kind, scope, statement, severity, source,
    origin, confirmed, confirmed_by, confirmed_at, rationale, evidence,
    analyzer, analyzer_version, created_at
"""

_DECISION_COLUMNS = """
    id, task_id, repo_id, commit_sha, question, decision, status, locked,
    decided_by, decided_at, rationale, supersedes_id, origin, evidence,
    analyzer, analyzer_version, created_at
"""

_CONTEXT_ITEM_COLUMNS = """
    id, task_id, package_id, repo_id, commit_sha, kind, content, origin,
    evidence, token_count, ordinal, analyzer, analyzer_version, created_at
"""

_TASK_COMPONENT_COLUMNS = """
    task_id, node_id, repo_id, commit_sha, relevance, rationale, origin,
    confidence::float8 as confidence, evidence, analyzer, analyzer_version, created_at
"""

_TASK_SNAPSHOT_COLUMNS = "id, task_id, repo_id, commit_sha, status, reason, created_at"

_VERIFICATION_COLUMNS = """
    id, task_id, repo_id, commit_sha, requirement_id, criterion, outcome,
    detail, source, evidence, created_at
"""

_PACKAGE_COLUMNS = """
    id, task_id, repo_id, commit_sha, version, status, token_budget,
    token_count, manifest, retrieval, unresolved, analyzer, analyzer_version, created_at
"""

_API_TOKEN_COLUMNS = """
    id, project_id, repo_id, name, token_prefix, token_hash, scope,
    created_by, created_at, last_used_at, expires_at, revoked_at
"""

# Ceiling on any single list call. A caller that wants more rows than this is
# asking for a page, and an unbounded read of a task's history has no honest
# consumer: the context package is budgeted (Part 4.F.5), so it cannot use
# them anyway.
MAX_LIST_LIMIT = 500


def _capped(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


# --- tasks ------------------------------------------------------------------


def create_task(conn: psycopg.Connection, task: Task) -> Task:
    """Create a task and, in the same transaction, the `active` row in
    `task_snapshots` for the snapshot it is grounded against.

    The two are written together on purpose: a task whose grounding history
    does not include its own `commit_sha` would make "which snapshot is this
    task about" answerable two different ways (Part 4.F.6).
    """
    with conn.cursor(row_factory=class_row(Task)) as cur:
        row = cur.execute(
            f"""
            insert into codeaxis.tasks
              (project_id, repo_id, commit_sha, title, intent, status, created_by)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning {_TASK_COLUMNS}
            """,
            (
                task.project_id,
                task.repo_id,
                task.commit_sha,
                task.title,
                task.intent,
                task.status,
                task.created_by,
            ),
        ).fetchone()
        assert row is not None
        cur.execute(
            """
            insert into codeaxis.task_snapshots (task_id, repo_id, commit_sha, status)
            values (%s, %s, %s, 'active')
            """,
            (row.id, row.repo_id, row.commit_sha),
        )
    conn.commit()
    return row


def get_task(conn: psycopg.Connection, task_id: str) -> Task | None:
    with conn.cursor(row_factory=class_row(Task)) as cur:
        return cur.execute(
            f"select {_TASK_COLUMNS} from codeaxis.tasks where id = %s",
            (task_id,),
        ).fetchone()


def list_tasks(
    conn: psycopg.Connection,
    project_id: str,
    *,
    repo_id: str | None = None,
    commit_sha: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[Task]:
    """Tasks in one project, newest first. `project_id` is required rather
    than optional because authorization is decided per project: a list query
    that could span projects would be a route waiting to leak one."""
    clauses = ["project_id = %s"]
    params: list[object] = [project_id]
    if repo_id is not None:
        clauses.append("repo_id = %s")
        params.append(repo_id)
    if commit_sha is not None:
        clauses.append("commit_sha = %s")
        params.append(commit_sha)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    query = (
        f"select {_TASK_COLUMNS} from codeaxis.tasks where "
        + " and ".join(clauses)
        + " order by created_at desc, id limit %s"
    )
    with conn.cursor(row_factory=class_row(Task)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


def set_task_status(conn: psycopg.Connection, task_id: str, status: str) -> None:
    """Move a task's lifecycle state. `blocked` is a real destination, not an
    error: Part 4.F.3 requires a contradiction or a missing-information gap to
    stop the task rather than be filled in by a model."""
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.tasks set status = %s, updated_at = now() where id = %s",
            (status, task_id),
        )
    conn.commit()


def reground_task(
    conn: psycopg.Connection, task_id: str, commit_sha: str, reason: str
) -> TaskSnapshot:
    """Re-ground a task on a newer snapshot: mark the current `active` row
    stale with `reason`, point the task at `commit_sha`, and record the new
    active grounding.

    Does not touch requirements, constraints or packages. Those were derived
    at the old commit and keep saying so; marking them stale (or re-deriving
    them) is the extraction pass's decision, not this layer's — rewriting
    their `commit_sha` here would silently claim evidence that was never
    checked against the new snapshot.
    """
    with conn.cursor(row_factory=class_row(TaskSnapshot)) as cur:
        cur.execute(
            """
            update codeaxis.task_snapshots set status = 'stale', reason = %s
            where task_id = %s and status = 'active' and commit_sha <> %s
            """,
            (reason, task_id, commit_sha),
        )
        cur.execute(
            "update codeaxis.tasks set commit_sha = %s, updated_at = now() where id = %s",
            (commit_sha, task_id),
        )
        row = cur.execute(
            f"""
            insert into codeaxis.task_snapshots (task_id, repo_id, commit_sha, status)
            select %s, repo_id, %s, 'active' from codeaxis.tasks where id = %s
            on conflict (task_id, repo_id, commit_sha)
              do update set status = 'active', reason = null
            returning {_TASK_SNAPSHOT_COLUMNS}
            """,
            (task_id, commit_sha, task_id),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


# --- requirements -----------------------------------------------------------


def replace_requirements(
    conn: psycopg.Connection, task_id: str, rows: Sequence[Requirement]
) -> list[str]:
    """Replace a task's requirements wholesale, returning the new ids in
    order.

    Replace rather than upsert: extraction re-runs produce a whole set, and a
    statement that disappeared from the new set is a statement the extractor
    no longer stands behind. Keeping it would leave a requirement in the
    ledger that nothing supports. `verification_results.requirement_id` is
    `on delete set null`, so verification history survives the swap with its
    `criterion` text intact.
    """
    with conn.cursor() as cur:
        cur.execute("delete from codeaxis.requirements where task_id = %s", (task_id,))
        ids: list[str] = []
        for row in rows:
            got = cur.execute(
                """
                insert into codeaxis.requirements (
                    task_id, repo_id, commit_sha, kind, statement, priority, status,
                    question, origin, source_ref, evidence, ordinal, confidence,
                    model, analyzer, analyzer_version
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    task_id,
                    row.repo_id,
                    row.commit_sha,
                    row.kind,
                    row.statement,
                    row.priority,
                    row.status,
                    row.question,
                    row.origin,
                    json.dumps(row.source_ref),
                    row.evidence.evidence_json(),
                    row.ordinal,
                    row.confidence,
                    row.model,
                    row.analyzer,
                    row.analyzer_version,
                ),
            ).fetchone()
            assert got is not None
            ids.append(str(got[0]))
    conn.commit()
    return ids


def list_requirements(
    conn: psycopg.Connection,
    task_id: str,
    *,
    commit_sha: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[Requirement]:
    clauses = ["task_id = %s"]
    params: list[object] = [task_id]
    if commit_sha is not None:
        clauses.append("commit_sha = %s")
        params.append(commit_sha)
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    query = (
        f"select {_REQUIREMENT_COLUMNS} from codeaxis.requirements where "
        + " and ".join(clauses)
        + " order by kind, ordinal, id limit %s"
    )
    with conn.cursor(row_factory=class_row(Requirement)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


# --- constraints (the Constraint Ledger, Part 4.F.8) ------------------------


def insert_constraints(
    conn: psycopg.Connection, rows: Sequence[Constraint]
) -> list[str]:
    """Append ledger entries, returning their ids in order. Each row carries
    its own `task_id` (or None for a repo-level detection)."""
    with conn.cursor() as cur:
        ids = [_insert_constraint(cur, row) for row in rows]
    conn.commit()
    return ids


def _insert_constraint(cur: psycopg.Cursor, row: Constraint) -> str:
    got = cur.execute(
        """
        insert into codeaxis.constraints (
            task_id, repo_id, commit_sha, kind, scope, statement, severity, source,
            origin, confirmed, confirmed_by, confirmed_at, rationale, evidence,
            analyzer, analyzer_version
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        returning id
        """,
        (
            row.task_id,
            row.repo_id,
            row.commit_sha,
            row.kind,
            row.scope,
            row.statement,
            row.severity,
            row.source,
            row.origin,
            row.confirmed,
            row.confirmed_by,
            row.confirmed_at,
            row.rationale,
            row.evidence.evidence_json(),
            row.analyzer,
            row.analyzer_version,
        ),
    ).fetchone()
    assert got is not None
    return str(got[0])


def replace_task_constraints(
    conn: psycopg.Connection, task_id: str, rows: Sequence[Constraint]
) -> list[str]:
    """Replace the constraints attached to one task. Repo-level detections
    (`task_id is null`) are untouched: they belong to the snapshot and are
    shared by every task."""
    with conn.cursor() as cur:
        cur.execute("delete from codeaxis.constraints where task_id = %s", (task_id,))
        ids = [_insert_constraint(cur, row) for row in rows]
    conn.commit()
    return ids


def replace_detected_constraints(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, rows: Sequence[Constraint]
) -> list[str]:
    """Replace the auto-detected, repo-level ledger for one snapshot.

    Scoped to `source = 'auto_detected'` and `task_id is null` so a detection
    re-run can never delete a constraint a human typed or confirmed — the
    ledger's whole value is that a user-stated rule outlives the analyzer that
    did not notice it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            delete from codeaxis.constraints
            where repo_id = %s and commit_sha = %s
              and task_id is null and source = 'auto_detected'
            """,
            (repo_id, commit_sha),
        )
        ids = [_insert_constraint(cur, row) for row in rows]
    conn.commit()
    return ids


def list_constraints(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    *,
    task_id: str | None = None,
    include_repo_level: bool = True,
    severity: str | None = None,
    confirmed_only: bool = False,
    limit: int = 300,
) -> list[Constraint]:
    """The ledger for one snapshot. With `task_id`, returns that task's own
    entries plus (by default) the repo-level detections that apply to every
    task — which together are exactly what the context package's
    never-truncated constraint section needs.

    Hard constraints sort first: if anything downstream ever has to cut, it
    must cut the soft ones.
    """
    clauses = ["repo_id = %s", "commit_sha = %s"]
    params: list[object] = [repo_id, commit_sha]
    if task_id is not None:
        clauses.append(
            "(task_id = %s or task_id is null)" if include_repo_level else "task_id = %s"
        )
        params.append(task_id)
    elif not include_repo_level:
        clauses.append("task_id is not null")
    if severity is not None:
        clauses.append("severity = %s")
        params.append(severity)
    if confirmed_only:
        clauses.append("confirmed")
    query = (
        f"select {_CONSTRAINT_COLUMNS} from codeaxis.constraints where "
        + " and ".join(clauses)
        + " order by (severity = 'hard') desc, kind, scope, id limit %s"
    )
    with conn.cursor(row_factory=class_row(Constraint)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


def confirm_constraint(
    conn: psycopg.Connection, constraint_id: str, user_id: str, *, confirmed: bool = True
) -> Constraint | None:
    """Record that a human accepted (or un-accepted) a ledger entry. Returns
    the updated row, or None if the id does not exist."""
    with conn.cursor(row_factory=class_row(Constraint)) as cur:
        row = cur.execute(
            f"""
            update codeaxis.constraints
            set confirmed = %s,
                confirmed_by = case when %s then %s else null end,
                confirmed_at = case when %s then now() else null end
            where id = %s
            returning {_CONSTRAINT_COLUMNS}
            """,
            (confirmed, confirmed, user_id, confirmed, constraint_id),
        ).fetchone()
    conn.commit()
    return row


# --- decisions --------------------------------------------------------------


def insert_decisions(conn: psycopg.Connection, rows: Sequence[Decision]) -> list[str]:
    with conn.cursor() as cur:
        ids: list[str] = []
        for row in rows:
            got = cur.execute(
                """
                insert into codeaxis.decisions (
                    task_id, repo_id, commit_sha, question, decision, status, locked,
                    decided_by, decided_at, rationale, supersedes_id, origin, evidence,
                    analyzer, analyzer_version
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                returning id
                """,
                (
                    row.task_id,
                    row.repo_id,
                    row.commit_sha,
                    row.question,
                    row.decision,
                    row.status,
                    row.locked,
                    row.decided_by,
                    row.decided_at,
                    row.rationale,
                    row.supersedes_id,
                    row.origin,
                    row.evidence.evidence_json(),
                    row.analyzer,
                    row.analyzer_version,
                ),
            ).fetchone()
            assert got is not None
            ids.append(str(got[0]))
    conn.commit()
    return ids


def replace_open_decisions(
    conn: psycopg.Connection, task_id: str, rows: Sequence[Decision]
) -> list[str]:
    """Replace a task's *unanswered* questions, leaving decided and locked
    rows alone.

    This is the one table where wholesale replacement would be wrong: Part
    4.G.9 requires approved decisions to survive regeneration, so a
    re-extraction may only restate what is still open.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            delete from codeaxis.decisions
            where task_id = %s and status = 'open' and not locked
            """,
            (task_id,),
        )
    return insert_decisions(conn, rows)


def resolve_decision(
    conn: psycopg.Connection,
    decision_id: str,
    decision: str,
    user_id: str,
    *,
    rationale: str | None = None,
    locked: bool = True,
) -> Decision | None:
    """Answer an open question. Locked by default: an answered decision is
    what later passes read and must not rewrite."""
    with conn.cursor(row_factory=class_row(Decision)) as cur:
        row = cur.execute(
            f"""
            update codeaxis.decisions
            set decision = %s, status = 'decided', decided_by = %s, decided_at = now(),
                rationale = coalesce(%s, rationale), locked = %s
            where id = %s
            returning {_DECISION_COLUMNS}
            """,
            (decision, user_id, rationale, locked, decision_id),
        ).fetchone()
    conn.commit()
    return row


def list_decisions(
    conn: psycopg.Connection,
    task_id: str,
    *,
    commit_sha: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[Decision]:
    clauses = ["task_id = %s"]
    params: list[object] = [task_id]
    if commit_sha is not None:
        clauses.append("commit_sha = %s")
        params.append(commit_sha)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    query = (
        f"select {_DECISION_COLUMNS} from codeaxis.decisions where "
        + " and ".join(clauses)
        + " order by created_at, id limit %s"
    )
    with conn.cursor(row_factory=class_row(Decision)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


# --- context items ----------------------------------------------------------


def replace_context_items(
    conn: psycopg.Connection,
    task_id: str,
    rows: Sequence[ContextItem],
    *,
    package_id: str | None = None,
) -> list[str]:
    """Replace a task's context items, or just those of one package.

    With `package_id`, only that package's items are replaced — assembling
    version 3 must not delete the rows version 2 was rendered from, because a
    package already handed to an agent has to stay reconstructable
    (Part 4.F.6). Without it, the task's unassembled items (`package_id is
    null`) are the ones swapped.
    """
    with conn.cursor() as cur:
        if package_id is None:
            cur.execute(
                "delete from codeaxis.context_items where task_id = %s and package_id is null",
                (task_id,),
            )
        else:
            cur.execute(
                "delete from codeaxis.context_items where task_id = %s and package_id = %s",
                (task_id, package_id),
            )
        ids: list[str] = []
        for row in rows:
            got = cur.execute(
                """
                insert into codeaxis.context_items (
                    task_id, package_id, repo_id, commit_sha, kind, content, origin,
                    evidence, token_count, ordinal, analyzer, analyzer_version
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                returning id
                """,
                (
                    task_id,
                    package_id if package_id is not None else row.package_id,
                    row.repo_id,
                    row.commit_sha,
                    row.kind,
                    row.content,
                    row.origin,
                    row.evidence.evidence_json(),
                    row.token_count,
                    row.ordinal,
                    row.analyzer,
                    row.analyzer_version,
                ),
            ).fetchone()
            assert got is not None
            ids.append(str(got[0]))
    conn.commit()
    return ids


def list_context_items(
    conn: psycopg.Connection,
    task_id: str,
    *,
    package_id: str | None = None,
    commit_sha: str | None = None,
    kind: str | None = None,
    limit: int = 500,
) -> list[ContextItem]:
    clauses = ["task_id = %s"]
    params: list[object] = [task_id]
    if package_id is not None:
        clauses.append("package_id = %s")
        params.append(package_id)
    if commit_sha is not None:
        clauses.append("commit_sha = %s")
        params.append(commit_sha)
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    query = (
        f"select {_CONTEXT_ITEM_COLUMNS} from codeaxis.context_items where "
        + " and ".join(clauses)
        + " order by ordinal, kind, id limit %s"
    )
    with conn.cursor(row_factory=class_row(ContextItem)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


# --- task components --------------------------------------------------------


def replace_task_components(
    conn: psycopg.Connection, task_id: str, rows: Sequence[TaskComponent]
) -> None:
    """Replace the graph entities a task is about. No ids are returned: the
    key is `(task_id, node_id)`, which the caller already holds."""
    with conn.cursor() as cur:
        cur.execute("delete from codeaxis.task_components where task_id = %s", (task_id,))
        for row in rows:
            cur.execute(
                """
                insert into codeaxis.task_components (
                    task_id, node_id, repo_id, commit_sha, relevance, rationale,
                    origin, confidence, evidence, analyzer, analyzer_version
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    task_id,
                    row.node_id,
                    row.repo_id,
                    row.commit_sha,
                    row.relevance,
                    row.rationale,
                    row.origin,
                    row.confidence,
                    row.evidence.evidence_json(),
                    row.analyzer,
                    row.analyzer_version,
                ),
            )
    conn.commit()


def list_task_components(
    conn: psycopg.Connection,
    task_id: str,
    *,
    commit_sha: str | None = None,
    relevance: str | None = None,
    limit: int = 200,
) -> list[TaskComponent]:
    clauses = ["task_id = %s"]
    params: list[object] = [task_id]
    if commit_sha is not None:
        clauses.append("commit_sha = %s")
        params.append(commit_sha)
    if relevance is not None:
        clauses.append("relevance = %s")
        params.append(relevance)
    query = (
        f"select {_TASK_COMPONENT_COLUMNS} from codeaxis.task_components where "
        + " and ".join(clauses)
        + " order by relevance, node_id limit %s"
    )
    with conn.cursor(row_factory=class_row(TaskComponent)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


# --- task snapshots ---------------------------------------------------------


def record_task_snapshot(conn: psycopg.Connection, row: TaskSnapshot) -> TaskSnapshot:
    """Insert or update one grounding row. `reground_task` is the normal way
    a task moves commits; this exists for the observer that notices a commit
    went away and has to mark a grounding stale without moving the task."""
    with conn.cursor(row_factory=class_row(TaskSnapshot)) as cur:
        got = cur.execute(
            f"""
            insert into codeaxis.task_snapshots (task_id, repo_id, commit_sha, status, reason)
            values (%s, %s, %s, %s, %s)
            on conflict (task_id, repo_id, commit_sha)
              do update set status = excluded.status, reason = excluded.reason
            returning {_TASK_SNAPSHOT_COLUMNS}
            """,
            (row.task_id, row.repo_id, row.commit_sha, row.status, row.reason),
        ).fetchone()
    conn.commit()
    assert got is not None
    return got


def list_task_snapshots(
    conn: psycopg.Connection, task_id: str, *, limit: int = 100
) -> list[TaskSnapshot]:
    with conn.cursor(row_factory=class_row(TaskSnapshot)) as cur:
        return cur.execute(
            f"""
            select {_TASK_SNAPSHOT_COLUMNS} from codeaxis.task_snapshots
            where task_id = %s order by created_at desc, id desc limit %s
            """,
            (task_id, _capped(limit)),
        ).fetchall()


# --- verification results ---------------------------------------------------


def insert_verification_results(
    conn: psycopg.Connection, rows: Sequence[VerificationResult]
) -> list[int]:
    """Append reported outcomes. Append-only by design: these are observations
    that arrived at a point in time (Part 4.F.6), and CodeAxis never runs the
    repository's code to produce them, so there is nothing to recompute and
    overwrite."""
    with conn.cursor() as cur:
        ids: list[int] = []
        for row in rows:
            got = cur.execute(
                """
                insert into codeaxis.verification_results (
                    task_id, repo_id, commit_sha, requirement_id, criterion,
                    outcome, detail, source, evidence
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                returning id
                """,
                (
                    row.task_id,
                    row.repo_id,
                    row.commit_sha,
                    row.requirement_id,
                    row.criterion,
                    row.outcome,
                    row.detail,
                    row.source,
                    row.evidence.evidence_json(),
                ),
            ).fetchone()
            assert got is not None
            ids.append(got[0])
    conn.commit()
    return ids


def list_verification_results(
    conn: psycopg.Connection,
    task_id: str,
    *,
    commit_sha: str | None = None,
    limit: int = 200,
) -> list[VerificationResult]:
    clauses = ["task_id = %s"]
    params: list[object] = [task_id]
    if commit_sha is not None:
        clauses.append("commit_sha = %s")
        params.append(commit_sha)
    query = (
        f"select {_VERIFICATION_COLUMNS} from codeaxis.verification_results where "
        + " and ".join(clauses)
        + " order by created_at desc, id desc limit %s"
    )
    with conn.cursor(row_factory=class_row(VerificationResult)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


# --- context packages -------------------------------------------------------


def next_package_version(conn: psycopg.Connection, task_id: str) -> int:
    """The version number the next assembly for this task should use. Read
    here rather than computed by the caller so two assemblies racing for the
    same number collide on the unique `(task_id, version)` index instead of
    silently overwriting one another's history."""
    row = conn.execute(
        "select coalesce(max(version), 0) + 1 from codeaxis.context_packages where task_id = %s",
        (task_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def create_context_package(
    conn: psycopg.Connection, package: ContextPackage
) -> ContextPackage:
    """Record one assembly. Never an update: a package handed to an agent must
    stay exactly as it was handed over, so a changed task yields a new version
    (unique on `(task_id, version)`)."""
    with conn.cursor(row_factory=class_row(ContextPackage)) as cur:
        row = cur.execute(
            f"""
            insert into codeaxis.context_packages (
                task_id, repo_id, commit_sha, version, status, token_budget,
                token_count, manifest, retrieval, unresolved, analyzer, analyzer_version
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
            returning {_PACKAGE_COLUMNS}
            """,
            (
                package.task_id,
                package.repo_id,
                package.commit_sha,
                package.version,
                package.status,
                package.token_budget,
                package.token_count,
                json.dumps(package.manifest),
                json.dumps(package.retrieval),
                json.dumps(package.unresolved),
                package.analyzer,
                package.analyzer_version,
            ),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


def get_context_package(
    conn: psycopg.Connection, package_id: str
) -> ContextPackage | None:
    with conn.cursor(row_factory=class_row(ContextPackage)) as cur:
        return cur.execute(
            f"select {_PACKAGE_COLUMNS} from codeaxis.context_packages where id = %s",
            (package_id,),
        ).fetchone()


def get_latest_context_package(
    conn: psycopg.Connection, task_id: str, *, commit_sha: str | None = None
) -> ContextPackage | None:
    """The highest-versioned package for a task, optionally restricted to one
    snapshot — "the newest package that is actually about this commit" is the
    question the export and MCP paths ask."""
    clauses = ["task_id = %s"]
    params: list[object] = [task_id]
    if commit_sha is not None:
        clauses.append("commit_sha = %s")
        params.append(commit_sha)
    query = (
        f"select {_PACKAGE_COLUMNS} from codeaxis.context_packages where "
        + " and ".join(clauses)
        + " order by version desc limit 1"
    )
    with conn.cursor(row_factory=class_row(ContextPackage)) as cur:
        return cur.execute(query, params).fetchone()  # pyright: ignore[reportArgumentType]


def list_context_packages(
    conn: psycopg.Connection, task_id: str, *, limit: int = 50
) -> list[ContextPackage]:
    with conn.cursor(row_factory=class_row(ContextPackage)) as cur:
        return cur.execute(
            f"""
            select {_PACKAGE_COLUMNS} from codeaxis.context_packages
            where task_id = %s order by version desc limit %s
            """,
            (task_id, _capped(limit)),
        ).fetchall()


def set_context_package_status(
    conn: psycopg.Connection, package_id: str, status: str
) -> None:
    """The one permitted mutation: marking a package `stale` (newer snapshot
    or newer requirements) or `invalid`. Its content is never edited."""
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.context_packages set status = %s where id = %s",
            (status, package_id),
        )
    conn.commit()


# --- api tokens (per-repo, read-only MCP access) ----------------------------

# Prefix on every issued token, so a leaked string is recognizable as a
# CodeAxis MCP token in a log or a bug report and can be revoked on sight.
TOKEN_PREFIX = "cxa_"
# Characters of the issued token kept as the non-secret lookup handle. Long
# enough to be unique in practice (and unique-constrained in the schema),
# short enough to leave the secret unguessable from what is displayed.
PREFIX_LEN = 12
# A sha256 hex digest of the right length, used as the comparison operand when
# no candidate row exists, so verification takes the same path (and roughly
# the same time) for an unknown prefix as for a wrong secret.
_DUMMY_HASH = "0" * 64


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_api_token(
    conn: psycopg.Connection,
    project_id: str,
    repo_id: str,
    name: str,
    *,
    created_by: str | None = None,
    expires_at: datetime | None = None,
) -> IssuedApiToken:
    """Mint a read-only MCP token for one repo.

    The plaintext token is returned here and nowhere else: only its sha256
    hash is stored, and the token is never logged or sent to a model. A caller
    that loses it must mint a new one — which is the point (Part 0.3's secrets
    rule applies to our own credentials).
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with conn.cursor(row_factory=class_row(ApiToken)) as cur:
        row = cur.execute(
            f"""
            insert into codeaxis.api_tokens
              (project_id, repo_id, name, token_prefix, token_hash, created_by, expires_at)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning {_API_TOKEN_COLUMNS}
            """,
            (
                project_id,
                repo_id,
                name,
                token[:PREFIX_LEN],
                _hash_token(token),
                created_by,
                expires_at,
            ),
        ).fetchone()
    conn.commit()
    assert row is not None
    return IssuedApiToken(token=token, record=row)


def verify_api_token(
    conn: psycopg.Connection, repo_id: str, token: str
) -> ApiToken | None:
    """Resolve a presented token to its row, or None.

    The lookup is by the token's non-secret prefix; the secret itself is
    compared with `hmac.compare_digest`, never with an SQL equality on the
    hash, so a near-miss cannot be distinguished by how long the query took.
    An unknown prefix still runs one comparison against a dummy digest for the
    same reason.

    Returns None for a revoked token, an expired one, or one issued for a
    different repo — a token is scoped to exactly one repo, so presenting it
    elsewhere is a failure, not a narrower success.
    """
    presented = _hash_token(token)
    with conn.cursor(row_factory=class_row(ApiToken)) as cur:
        row = cur.execute(
            f"""
            select {_API_TOKEN_COLUMNS} from codeaxis.api_tokens
            where token_prefix = %s and repo_id = %s
              and revoked_at is null
              and (expires_at is null or expires_at > now())
            """,
            (token[:PREFIX_LEN], repo_id),
        ).fetchone()
    stored = row.token_hash if row is not None else _DUMMY_HASH
    if not hmac.compare_digest(stored, presented) or row is None:
        return None
    return row


def touch_api_token(conn: psycopg.Connection, token_id: str) -> None:
    """Record that a token was just used. A write, so it needs the worker
    connection: the read-only api role deliberately cannot update it."""
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.api_tokens set last_used_at = now() where id = %s",
            (token_id,),
        )
    conn.commit()


def revoke_api_token(conn: psycopg.Connection, token_id: str) -> bool:
    """Revoke a token. Returns False if it does not exist or was already
    revoked. A timestamp, not a delete: whatever logged the token's use keeps
    a resolvable id."""
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.api_tokens set revoked_at = now() "
            "where id = %s and revoked_at is null",
            (token_id,),
        )
        changed = cur.rowcount
    conn.commit()
    return changed == 1


def list_api_tokens(
    conn: psycopg.Connection, repo_id: str, *, include_revoked: bool = False
) -> list[ApiToken]:
    """Tokens for one repo. Hashes come back on the model but are excluded
    from serialization (`ApiToken.token_hash`), so a route cannot leak them by
    returning the model."""
    query = f"select {_API_TOKEN_COLUMNS} from codeaxis.api_tokens where repo_id = %s"
    if not include_revoked:
        query += " and revoked_at is null"
    query += " order by created_at desc, id"
    with conn.cursor(row_factory=class_row(ApiToken)) as cur:
        return cur.execute(query, (repo_id,)).fetchall()  # pyright: ignore[reportArgumentType]
