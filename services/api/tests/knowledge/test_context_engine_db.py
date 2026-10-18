"""The Agent Context Engine tables against the live Supabase database
(build-plan.md Part 4.F, Part 6): a round trip through every table in
`app.knowledge.context`, snapshot scoping, cascade delete with the repo, the
check constraints that keep severity/origin/status honest, and the MCP token
verify/revoke path.

Skipped when WORKER_DATABASE_URL isn't set, same convention as test_reads.py.
Every assertion is scoped to ids this test created — never a whole-table
count — because the suite shares one live database and a concurrent run must
not be able to change an assertion's outcome.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest
from dotenv import load_dotenv

from app.knowledge import (
    api_connection,
    confirm_constraint,
    create_api_token,
    create_context_package,
    create_project,
    create_repo,
    create_task,
    delete_repo,
    get_context_package,
    get_latest_context_package,
    get_task,
    insert_constraints,
    insert_decisions,
    insert_nodes,
    insert_verification_results,
    list_api_tokens,
    list_constraints,
    list_context_items,
    list_context_packages,
    list_decisions,
    list_requirements,
    list_task_components,
    list_task_snapshots,
    list_tasks,
    list_verification_results,
    next_package_version,
    record_task_snapshot,
    reground_task,
    replace_context_items,
    replace_detected_constraints,
    replace_open_decisions,
    replace_requirements,
    replace_task_components,
    replace_task_constraints,
    resolve_decision,
    revoke_api_token,
    set_task_status,
    verify_api_token,
    worker_connection,
)
from app.models import (
    Citation,
    Constraint,
    ContextEvidence,
    ContextItem,
    ContextPackage,
    Decision,
    Node,
    Repo,
    Requirement,
    Task,
    TaskComponent,
    TaskSnapshot,
    VerificationResult,
)

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)

ANALYZER = "test.context_engine"
VERSION = "0.0.0-test"


@dataclass(frozen=True)
class Fixture:
    user: str
    project_id: str
    repo_id: str
    sha: str


def _sha() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def ctx(make_user) -> Iterator[Fixture]:
    """A fresh user, project, repo and snapshot sha. Teardown is `make_user`'s:
    deleting the profile removes the project and repo, and every table in this
    migration cascades from the repo — which `test_cascade_delete_with_repo`
    asserts rather than assumes."""
    user = make_user()
    with worker_connection() as conn:
        project = create_project(conn, "context engine test", user)
        repo = create_repo(
            conn, Repo(project_id=project.id, remote_url="https://example.com/octo/ctx.git")
        )
    assert repo.id is not None
    yield Fixture(user=user, project_id=project.id, repo_id=repo.id, sha=_sha())


def evidence(path: str = "app/auth.py") -> ContextEvidence:
    return ContextEvidence(
        spans=[Citation(path=path, line_start=1, line_end=4, symbol="verify_token")],
        notes="written from the span above",
    )


def a_task(f: Fixture, *, sha: str | None = None, title: str = "Add rate limiting") -> Task:
    return Task(
        project_id=f.project_id,
        repo_id=f.repo_id,
        commit_sha=sha or f.sha,
        title=title,
        intent="Limit /chat to 10 requests per minute per user.",
        created_by=f.user,
    )


def a_requirement(f: Fixture, task_id: str, *, sha: str | None = None, **over) -> Requirement:
    fields: dict = {
        "task_id": task_id,
        "repo_id": f.repo_id,
        "commit_sha": sha or f.sha,
        "kind": "functional",
        "statement": "Reject the 11th request in a minute with HTTP 429.",
        "priority": "must",
        "origin": "inferred",
        "evidence": evidence(),
        "analyzer": ANALYZER,
        "analyzer_version": VERSION,
    }
    fields.update(over)
    return Requirement(**fields)


def a_constraint(f: Fixture, *, sha: str | None = None, **over) -> Constraint:
    fields: dict = {
        "repo_id": f.repo_id,
        "commit_sha": sha or f.sha,
        "kind": "auth_boundary",
        "scope": "services/api/app/auth.py",
        "statement": "Every repo-scoped route checks project membership first.",
        "severity": "hard",
        "source": "auto_detected",
        "origin": "parsed",
        "evidence": evidence(),
        "analyzer": ANALYZER,
        "analyzer_version": VERSION,
    }
    fields.update(over)
    return Constraint(**fields)


def a_decision(f: Fixture, task_id: str, *, sha: str | None = None, **over) -> Decision:
    fields: dict = {
        "task_id": task_id,
        "repo_id": f.repo_id,
        "commit_sha": sha or f.sha,
        "question": "Per-user or per-IP limiting?",
        "origin": "inferred",
        "evidence": evidence(),
        "analyzer": ANALYZER,
        "analyzer_version": VERSION,
    }
    fields.update(over)
    return Decision(**fields)


def an_item(f: Fixture, task_id: str, *, sha: str | None = None, **over) -> ContextItem:
    fields: dict = {
        "task_id": task_id,
        "repo_id": f.repo_id,
        "commit_sha": sha or f.sha,
        "kind": "code",
        "content": "def verify_token(...):",
        "origin": "parsed",
        "evidence": evidence(),
        "token_count": 12,
        "analyzer": ANALYZER,
        "analyzer_version": VERSION,
    }
    fields.update(over)
    return ContextItem(**fields)


def a_package(f: Fixture, task_id: str, *, sha: str | None = None, **over) -> ContextPackage:
    fields: dict = {
        "task_id": task_id,
        "repo_id": f.repo_id,
        "commit_sha": sha or f.sha,
        "version": 1,
        "token_budget": 8000,
        "token_count": 120,
        "manifest": {"sections": ["constraint", "code"], "dropped_for_budget": 2},
        "retrieval": {"fts": {"ran": True}},
        "unresolved": [{"question": "Per-user or per-IP?"}],
        "analyzer": ANALYZER,
        "analyzer_version": VERSION,
    }
    fields.update(over)
    return ContextPackage(**fields)


# --- tasks ------------------------------------------------------------------


def test_task_round_trip_and_active_grounding(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None
        assert (task.project_id, task.repo_id, task.commit_sha) == (
            ctx.project_id,
            ctx.repo_id,
            ctx.sha,
        )
        assert task.status == "draft" and task.created_by == ctx.user

        fetched = get_task(conn, task.id)
        assert fetched is not None and fetched.title == "Add rate limiting"
        assert fetched.intent is not None and "10 requests" in fetched.intent

        # create_task writes the matching `active` grounding row in the same
        # transaction, so "which snapshot is this task about" has one answer.
        grounding = list_task_snapshots(conn, task.id)
        assert [(g.commit_sha, g.status, g.reason) for g in grounding] == [
            (ctx.sha, "active", None)
        ]

        listed = list_tasks(conn, ctx.project_id)
        assert [t.id for t in listed] == [task.id]
        assert list_tasks(conn, ctx.project_id, repo_id=ctx.repo_id, commit_sha=ctx.sha)
        assert list_tasks(conn, ctx.project_id, status="ready") == []

        set_task_status(conn, task.id, "blocked")
        blocked = get_task(conn, task.id)
        assert blocked is not None and blocked.status == "blocked"


def test_reground_marks_the_old_grounding_stale(ctx: Fixture):
    newer = _sha()
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None
        active = reground_task(conn, task.id, newer, "newer commit analyzed")
        assert (active.commit_sha, active.status) == (newer, "active")

        moved = get_task(conn, task.id)
        assert moved is not None and moved.commit_sha == newer

        rows = {g.commit_sha: (g.status, g.reason) for g in list_task_snapshots(conn, task.id)}
        assert rows[newer] == ("active", None)
        assert rows[ctx.sha] == ("stale", "newer commit analyzed")

        # An observer can mark a grounding stale without moving the task.
        record_task_snapshot(
            conn,
            TaskSnapshot(
                task_id=task.id,
                repo_id=ctx.repo_id,
                commit_sha=ctx.sha,
                status="superseded",
                reason="branch force-pushed",
            ),
        )
        rows = {g.commit_sha: g.status for g in list_task_snapshots(conn, task.id)}
        assert rows[ctx.sha] == "superseded" and rows[newer] == "active"


# --- requirements / constraints / decisions ---------------------------------


def test_requirements_round_trip_and_replace(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None
        ids = replace_requirements(
            conn,
            task.id,
            [
                a_requirement(ctx, task.id, ordinal=0),
                a_requirement(
                    ctx,
                    task.id,
                    kind="open_question",
                    statement="Which limiter backend is available?",
                    status="ambiguous",
                    question="No cache/redis dependency found in the manifests.",
                    priority=None,
                    ordinal=1,
                ),
            ],
        )
        assert len(ids) == 2

        rows = list_requirements(conn, task.id)
        assert [r.kind for r in rows] == ["functional", "open_question"]
        ambiguous = next(r for r in rows if r.status == "ambiguous")
        assert ambiguous.question is not None and "No cache" in ambiguous.question
        functional = next(r for r in rows if r.kind == "functional")
        assert functional.priority == "must" and functional.origin == "inferred"
        # Evidence survives the jsonb round trip as typed citations.
        assert functional.evidence.spans[0].path == "app/auth.py"
        assert functional.evidence.spans[0].line_end == 4
        assert list_requirements(conn, task.id, kind="functional") == [functional]
        assert list_requirements(conn, task.id, commit_sha=_sha()) == []

        # Replace means replace: the previous set is gone, not merged.
        replace_requirements(conn, task.id, [a_requirement(ctx, task.id, kind="objective")])
        assert [r.kind for r in list_requirements(conn, task.id)] == ["objective"]


def test_constraint_ledger_round_trip(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None

        detected = replace_detected_constraints(
            conn, ctx.repo_id, ctx.sha, [a_constraint(ctx), a_constraint(ctx, kind="db_schema")]
        )
        stated = insert_constraints(
            conn,
            [
                a_constraint(
                    ctx,
                    task_id=task.id,
                    kind="do_not_touch",
                    scope="services/api/app/auth.py",
                    statement="Do not change the JWT verifier.",
                    severity="soft",
                    source="user_stated",
                    origin="configured",
                    analyzer=None,
                    analyzer_version=None,
                )
            ],
        )
        assert len(detected) == 2 and len(stated) == 1

        rows = list_constraints(conn, ctx.repo_id, ctx.sha, task_id=task.id)
        assert {r.id for r in rows} == {*detected, *stated}
        # Hard constraints sort first: if anything ever cuts, it cuts soft ones.
        assert rows[0].severity == "hard" and rows[-1].severity == "soft"
        user_row = next(r for r in rows if r.source == "user_stated")
        assert (user_row.origin, user_row.analyzer) == ("configured", None)
        assert user_row.confirmed is False

        # Task-scoped view excludes the repo-level detections.
        own = list_constraints(
            conn, ctx.repo_id, ctx.sha, task_id=task.id, include_repo_level=False
        )
        assert [r.id for r in own] == stated
        assert [r.id for r in list_constraints(conn, ctx.repo_id, ctx.sha, severity="soft")] == (
            stated
        )

        updated = confirm_constraint(conn, stated[0], ctx.user)
        assert updated is not None and updated.confirmed is True
        assert updated.confirmed_by == ctx.user and updated.confirmed_at is not None
        assert [r.id for r in list_constraints(
            conn, ctx.repo_id, ctx.sha, task_id=task.id, confirmed_only=True
        )] == stated

        # A detection re-run must not delete what a human typed or confirmed.
        replace_detected_constraints(conn, ctx.repo_id, ctx.sha, [a_constraint(ctx)])
        after = list_constraints(conn, ctx.repo_id, ctx.sha, task_id=task.id)
        assert stated[0] in {r.id for r in after}
        assert sum(1 for r in after if r.source == "auto_detected") == 1

        # Replacing the task's own entries leaves the repo-level ledger alone.
        replace_task_constraints(conn, task.id, [])
        remaining = list_constraints(conn, ctx.repo_id, ctx.sha, task_id=task.id)
        assert all(r.task_id is None for r in remaining) and len(remaining) == 1


def test_decisions_keep_answered_rows(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None
        ids = insert_decisions(
            conn, [a_decision(ctx, task.id), a_decision(ctx, task.id, question="Which window?")]
        )
        assert len(ids) == 2
        assert all(d.status == "open" and d.decision is None for d in list_decisions(conn, task.id))

        answered = resolve_decision(conn, ids[0], "Per user id.", ctx.user, rationale="auth exists")
        assert answered is not None
        assert (answered.status, answered.locked) == ("decided", True)
        assert answered.decision == "Per user id." and answered.decided_at is not None
        assert answered.decided_by == ctx.user and answered.rationale == "auth exists"

        # Re-extraction may only restate what is still open (Part 4.G.9).
        replace_open_decisions(conn, task.id, [a_decision(ctx, task.id, question="Burst allowed?")])
        rows = {d.question: d.status for d in list_decisions(conn, task.id)}
        assert rows == {
            "Per-user or per-IP limiting?": "decided",
            "Burst allowed?": "open",
        }
        assert [d.question for d in list_decisions(conn, task.id, status="open")] == [
            "Burst allowed?"
        ]


# --- context items, components, packages, verification ----------------------


def test_context_items_and_packages_round_trip(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None

        assert next_package_version(conn, task.id) == 1
        package = create_context_package(conn, a_package(ctx, task.id))
        assert package.id is not None
        assert package.status == "assembled" and package.token_budget == 8000
        assert package.manifest["dropped_for_budget"] == 2
        assert package.unresolved[0]["question"] == "Per-user or per-IP?"
        assert next_package_version(conn, task.id) == 2

        item_ids = replace_context_items(
            conn,
            task.id,
            [
                an_item(ctx, task.id, ordinal=0),
                # A user-typed constraint legitimately carries no span.
                an_item(
                    ctx,
                    task.id,
                    kind="constraint",
                    content="Do not change the JWT verifier.",
                    origin="configured",
                    evidence=ContextEvidence(),
                    ordinal=1,
                ),
            ],
            package_id=package.id,
        )
        assert len(item_ids) == 2

        items = list_context_items(conn, task.id, package_id=package.id)
        assert [i.kind for i in items] == ["code", "constraint"]
        assert all(i.package_id == package.id for i in items)
        assert items[0].evidence.spans[0].symbol == "verify_token"
        assert items[1].evidence.spans == []
        assert list_context_items(conn, task.id, kind="code") == [items[0]]
        assert list_context_items(conn, task.id, commit_sha=_sha()) == []

        # A second version must not delete the rows version 1 was rendered
        # from: a package already handed to an agent stays reconstructable.
        v2 = create_context_package(
            conn, a_package(ctx, task.id, version=next_package_version(conn, task.id))
        )
        assert v2.id is not None
        replace_context_items(conn, task.id, [an_item(ctx, task.id)], package_id=v2.id)
        assert len(list_context_items(conn, task.id, package_id=package.id)) == 2
        assert len(list_context_items(conn, task.id, package_id=v2.id)) == 1

        latest = get_latest_context_package(conn, task.id)
        assert latest is not None and latest.version == 2
        assert get_latest_context_package(conn, task.id, commit_sha=_sha()) is None
        assert [p.version for p in list_context_packages(conn, task.id)] == [2, 1]
        got = get_context_package(conn, package.id)
        assert got is not None and got.version == 1


def test_task_components_and_verification_results(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None
        node_ids = insert_nodes(
            conn,
            [
                Node(
                    repo_id=ctx.repo_id,
                    commit_sha=ctx.sha,
                    kind="cluster",
                    name="auth",
                    analyzer=ANALYZER,
                    analyzer_version=VERSION,
                    status="verified",
                )
            ],
        )
        replace_task_components(
            conn,
            task.id,
            [
                TaskComponent(
                    task_id=task.id,
                    node_id=node_ids[0],
                    repo_id=ctx.repo_id,
                    commit_sha=ctx.sha,
                    relevance="primary",
                    rationale="the limiter hooks into the auth dependency",
                    origin="inferred",
                    confidence=0.8,
                    evidence=evidence(),
                    analyzer=ANALYZER,
                    analyzer_version=VERSION,
                )
            ],
        )
        components = list_task_components(conn, task.id)
        assert [c.node_id for c in components] == node_ids
        assert components[0].relevance == "primary"
        # `confidence` is a Postgres `real` (float4), same as nodes/conventions,
        # so it comes back float32-rounded — compared approximately, not exactly.
        assert components[0].confidence == pytest.approx(0.8, abs=1e-6)
        assert list_task_components(conn, task.id, relevance="impacted") == []
        assert list_task_components(conn, task.id, commit_sha=_sha()) == []

        req_ids = replace_requirements(
            conn, task.id, [a_requirement(ctx, task.id, kind="acceptance_criteria")]
        )
        result_ids = insert_verification_results(
            conn,
            [
                VerificationResult(
                    task_id=task.id,
                    repo_id=ctx.repo_id,
                    commit_sha=ctx.sha,
                    requirement_id=req_ids[0],
                    criterion="Reject the 11th request with 429.",
                    outcome="not_run",
                    source="user",
                    evidence=evidence(),
                )
            ],
        )
        assert len(result_ids) == 1
        results = list_verification_results(conn, task.id)
        assert [r.id for r in results] == result_ids
        assert results[0].outcome == "not_run" and results[0].source == "user"
        assert results[0].requirement_id == req_ids[0]
        assert list_verification_results(conn, task.id, commit_sha=_sha()) == []

        # Re-extraction replaces requirement rows; the verification history
        # survives with its criterion text and a null requirement_id.
        replace_requirements(conn, task.id, [])
        survived = list_verification_results(conn, task.id)
        assert [r.id for r in survived] == result_ids
        assert survived[0].requirement_id is None
        assert survived[0].criterion == "Reject the 11th request with 429."


# --- snapshot isolation -----------------------------------------------------


def test_rows_never_leak_across_snapshots(ctx: Fixture):
    """One repo, two commits. Every snapshot-scoped read must answer only for
    the commit it was asked about (Part 5: results from different versions can
    never mix)."""
    other = _sha()
    with worker_connection() as conn:
        task_a = create_task(conn, a_task(ctx, title="at commit A"))
        task_b = create_task(conn, a_task(ctx, sha=other, title="at commit B"))
        assert task_a.id is not None and task_b.id is not None

        replace_requirements(conn, task_a.id, [a_requirement(ctx, task_a.id, statement="A only")])
        replace_requirements(
            conn, task_b.id, [a_requirement(ctx, task_b.id, sha=other, statement="B only")]
        )
        replace_detected_constraints(
            conn, ctx.repo_id, ctx.sha, [a_constraint(ctx, statement="A rule")]
        )
        replace_detected_constraints(
            conn, ctx.repo_id, other, [a_constraint(ctx, sha=other, statement="B rule")]
        )

        assert [t.id for t in list_tasks(conn, ctx.project_id, commit_sha=ctx.sha)] == [task_a.id]
        assert [t.id for t in list_tasks(conn, ctx.project_id, commit_sha=other)] == [task_b.id]

        assert [r.statement for r in list_requirements(conn, task_a.id, commit_sha=ctx.sha)] == [
            "A only"
        ]
        assert list_requirements(conn, task_a.id, commit_sha=other) == []

        assert [c.statement for c in list_constraints(conn, ctx.repo_id, ctx.sha)] == ["A rule"]
        assert [c.statement for c in list_constraints(conn, ctx.repo_id, other)] == ["B rule"]

        # Replacing one snapshot's detected ledger leaves the other's intact.
        replace_detected_constraints(conn, ctx.repo_id, ctx.sha, [])
        assert list_constraints(conn, ctx.repo_id, ctx.sha) == []
        assert [c.statement for c in list_constraints(conn, ctx.repo_id, other)] == ["B rule"]


# --- check constraints ------------------------------------------------------
#
# These bypass the Pydantic models on purpose: the database is the last line,
# and a future writer that does not go through app.models must still be unable
# to store a severity or an origin outside the closed set.


def _raw_insert(sql: str, params: tuple) -> None:
    with worker_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)  # pyright: ignore[reportArgumentType]
        conn.commit()


_CONSTRAINT_INSERT = """
    insert into codeaxis.constraints
      (repo_id, commit_sha, kind, scope, statement, severity, source, origin,
       analyzer, analyzer_version)
    values (%s, %s, 'other', 'repo', 'x', %s, %s, %s, 'a', 'v')
"""


@pytest.mark.parametrize(
    ("severity", "source", "origin"),
    [
        ("critical", "auto_detected", "parsed"),  # severity outside hard|soft
        ("hard", "auto_detected", "guessed"),  # origin outside the Part 5 tags
        ("hard", "typed_by_a_user", "parsed"),  # source outside the closed set
        ("hard", "user_stated", "inferred"),  # user-stated rows are 'configured'
    ],
)
def test_constraint_check_constraints_reject_bad_values(
    ctx: Fixture, severity: str, source: str, origin: str
):
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(_CONSTRAINT_INSERT, (ctx.repo_id, ctx.sha, severity, source, origin))


def test_auto_detected_constraint_must_name_its_analyzer(ctx: Fixture):
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(
            """
            insert into codeaxis.constraints
              (repo_id, commit_sha, kind, scope, statement, severity, source, origin)
            values (%s, %s, 'other', 'repo', 'x', 'hard', 'auto_detected', 'parsed')
            """,
            (ctx.repo_id, ctx.sha),
        )


def test_ambiguous_requirement_must_state_its_question(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
    assert task.id is not None
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(
            """
            insert into codeaxis.requirements
              (task_id, repo_id, commit_sha, kind, statement, status, origin,
               analyzer, analyzer_version)
            values (%s, %s, %s, 'functional', 'x', 'ambiguous', 'inferred', 'a', 'v')
            """,
            (task.id, ctx.repo_id, ctx.sha),
        )


def test_decided_decision_must_carry_a_decision(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
    assert task.id is not None
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(
            """
            insert into codeaxis.decisions
              (task_id, repo_id, commit_sha, question, status, origin,
               analyzer, analyzer_version)
            values (%s, %s, %s, 'q?', 'decided', 'inferred', 'a', 'v')
            """,
            (task.id, ctx.repo_id, ctx.sha),
        )


def test_only_one_active_grounding_per_task(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
    assert task.id is not None
    with pytest.raises(psycopg.errors.UniqueViolation):
        _raw_insert(
            """
            insert into codeaxis.task_snapshots (task_id, repo_id, commit_sha, status)
            values (%s, %s, %s, 'active')
            """,
            (task.id, ctx.repo_id, _sha()),
        )


def test_context_item_kind_is_a_closed_set(ctx: Fixture):
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
    assert task.id is not None
    with pytest.raises(psycopg.errors.CheckViolation):
        _raw_insert(
            """
            insert into codeaxis.context_items
              (task_id, repo_id, commit_sha, kind, content, origin, analyzer, analyzer_version)
            values (%s, %s, %s, 'freeform_prose', 'x', 'inferred', 'a', 'v')
            """,
            (task.id, ctx.repo_id, ctx.sha),
        )


def test_model_validators_reject_the_same_shapes(ctx: Fixture):
    """The Pydantic side of the same invariants, so a bad record fails before
    it reaches the database rather than as an opaque driver error."""
    with pytest.raises(ValueError, match="question"):
        a_requirement(ctx, str(uuid.uuid4()), status="unresolved")
    with pytest.raises(ValueError, match="analyzer"):
        a_constraint(ctx, analyzer=None, analyzer_version=None)
    with pytest.raises(ValueError, match="configured"):
        a_constraint(ctx, source="user_stated", origin="inferred", analyzer=None,
                     analyzer_version=None)
    with pytest.raises(ValueError, match="decided_at"):
        a_decision(ctx, str(uuid.uuid4()), status="decided", decision="yes")
    with pytest.raises(ValueError, match="locked"):
        a_decision(ctx, str(uuid.uuid4()), locked=True)
    with pytest.raises(ValueError, match="span"):
        an_item(ctx, str(uuid.uuid4()), kind="code", evidence=ContextEvidence())
    with pytest.raises(ValueError, match="reason"):
        TaskSnapshot(
            task_id=str(uuid.uuid4()), repo_id=ctx.repo_id, commit_sha=ctx.sha, status="stale"
        )


# --- MCP tokens -------------------------------------------------------------


def test_api_token_create_verify_revoke(ctx: Fixture):
    with worker_connection() as conn:
        issued = create_api_token(
            conn, ctx.project_id, ctx.repo_id, "claude-code", created_by=ctx.user
        )
        assert issued.token.startswith("cxa_") and len(issued.token) > 20
        assert issued.record.id is not None
        assert issued.record.scope == "read" and issued.record.revoked_at is None
        # Only the hash is stored, and it is never the token.
        assert issued.record.token_hash != issued.token
        assert "token_hash" not in issued.record.model_dump()

        found = verify_api_token(conn, ctx.repo_id, issued.token)
        assert found is not None and found.id == issued.record.id

        # Wrong secret, wrong repo, and a truncated token all fail.
        assert verify_api_token(conn, ctx.repo_id, issued.token + "x") is None
        assert verify_api_token(conn, ctx.repo_id, "cxa_" + "y" * 40) is None
        assert verify_api_token(conn, str(uuid.uuid4()), issued.token) is None

        assert [t.id for t in list_api_tokens(conn, ctx.repo_id)] == [issued.record.id]

        assert revoke_api_token(conn, issued.record.id) is True
        assert revoke_api_token(conn, issued.record.id) is False  # already revoked
        assert verify_api_token(conn, ctx.repo_id, issued.token) is None
        assert list_api_tokens(conn, ctx.repo_id) == []
        revoked = list_api_tokens(conn, ctx.repo_id, include_revoked=True)
        assert [t.id for t in revoked] == [issued.record.id]
        assert revoked[0].revoked_at is not None


# --- role grants ------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("API_DATABASE_URL"), reason="API_DATABASE_URL not set"
)
def test_read_only_api_role_can_select_the_new_tables(ctx: Fixture):
    """`scripts/setup_db_roles.py` set `alter default privileges`, and the
    migration grants explicitly as well. This asserts the result rather than
    the assumption: the read role must be able to select every new table, and
    `codeaxis_api` is not BYPASSRLS, so its `*_api_read` policies must exist."""
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None
        replace_requirements(conn, task.id, [a_requirement(ctx, task.id)])
        insert_constraints(conn, [a_constraint(ctx, task_id=task.id)])
        insert_decisions(conn, [a_decision(ctx, task.id)])
        package = create_context_package(conn, a_package(ctx, task.id))
        assert package.id is not None
        replace_context_items(conn, task.id, [an_item(ctx, task.id)], package_id=package.id)
        insert_verification_results(
            conn,
            [
                VerificationResult(
                    task_id=task.id,
                    repo_id=ctx.repo_id,
                    commit_sha=ctx.sha,
                    criterion="c",
                    outcome="not_run",
                    source="user",
                )
            ],
        )
        issued = create_api_token(conn, ctx.project_id, ctx.repo_id, "api-role-read")

    with api_connection() as conn:
        assert get_task(conn, task.id) is not None
        assert len(list_requirements(conn, task.id)) == 1
        assert len(list_constraints(conn, ctx.repo_id, ctx.sha, task_id=task.id)) == 1
        assert len(list_decisions(conn, task.id)) == 1
        assert len(list_context_items(conn, task.id)) == 1
        assert len(list_verification_results(conn, task.id)) == 1
        assert len(list_task_snapshots(conn, task.id)) == 1
        assert get_latest_context_package(conn, task.id) is not None
        # Token verification is a read path: the MCP server runs on this role.
        assert verify_api_token(conn, ctx.repo_id, issued.token) is not None

        # And it is read-only: the api role must not be able to write.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("delete from codeaxis.tasks where id = %s", (task.id,))
        conn.rollback()


# --- cascade delete ---------------------------------------------------------


def test_cascade_delete_with_repo(ctx: Fixture):
    """Deleting the repo removes every context-engine row derived from it —
    Part 9's "full delete (rows + vectors)" has to include these tables, and
    `delete_repo` does not name them, so the FK cascade is what makes it
    true."""
    with worker_connection() as conn:
        task = create_task(conn, a_task(ctx))
        assert task.id is not None
        replace_requirements(conn, task.id, [a_requirement(ctx, task.id)])
        insert_constraints(conn, [a_constraint(ctx, task_id=task.id), a_constraint(ctx)])
        insert_decisions(conn, [a_decision(ctx, task.id)])
        node_ids = insert_nodes(
            conn,
            [
                Node(
                    repo_id=ctx.repo_id,
                    commit_sha=ctx.sha,
                    kind="cluster",
                    name="auth",
                    analyzer=ANALYZER,
                    analyzer_version=VERSION,
                    status="verified",
                )
            ],
        )
        replace_task_components(
            conn,
            task.id,
            [
                TaskComponent(
                    task_id=task.id,
                    node_id=node_ids[0],
                    repo_id=ctx.repo_id,
                    commit_sha=ctx.sha,
                    relevance="primary",
                    origin="parsed",
                    analyzer=ANALYZER,
                    analyzer_version=VERSION,
                )
            ],
        )
        package = create_context_package(conn, a_package(ctx, task.id))
        assert package.id is not None
        replace_context_items(conn, task.id, [an_item(ctx, task.id)], package_id=package.id)
        insert_verification_results(
            conn,
            [
                VerificationResult(
                    task_id=task.id,
                    repo_id=ctx.repo_id,
                    commit_sha=ctx.sha,
                    criterion="c",
                    outcome="not_run",
                    source="user",
                )
            ],
        )
        create_api_token(conn, ctx.project_id, ctx.repo_id, "to-be-cascaded")

        delete_repo(conn, ctx.repo_id)

        assert get_task(conn, task.id) is None
        assert list_tasks(conn, ctx.project_id) == []
        # Counted by the ids this test created, never a whole-table count.
        for table, column, value in (
            ("requirements", "task_id", task.id),
            ("constraints", "repo_id", ctx.repo_id),
            ("decisions", "task_id", task.id),
            ("context_items", "task_id", task.id),
            ("task_components", "task_id", task.id),
            ("task_snapshots", "task_id", task.id),
            ("verification_results", "task_id", task.id),
            ("context_packages", "task_id", task.id),
            ("api_tokens", "repo_id", ctx.repo_id),
        ):
            row = conn.execute(
                f"select count(*) from codeaxis.{table} where {column} = %s",  # pyright: ignore[reportArgumentType]
                (value,),
            ).fetchone()
            assert row is not None and row[0] == 0, f"{table} rows survived the repo delete"
