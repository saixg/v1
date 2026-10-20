"""HTTP surface of the Agent Context Engine (build-plan.md Part 4.F, Part 8).

Authorization is entirely app-layer: the database does not scope rows by user
(the Clerk migration dropped RLS membership), so every route here resolves its
own project membership. Repo-scoped routes use `RepoAccess`; every
`/tasks/{id}` route goes through `task_access`, which loads the task and then
checks the caller's membership in *the task's* project (404 unknown, 403
non-member) before anything else about the task is read.

Nothing here invents structure: extraction goes through `ModelService`, and
the stored requirements/constraints/packages are read back from the Repo Model.
Everything a client is told about code comes from static analysis; no route
implies the program was run.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.analysis.semantic.retrieve import ModelServiceEmbedder
from app.auth import CurrentUser, RepoAccess, require_project_member, require_uuid
from app.context import (
    EXPORT_FILENAMES,
    EXPORT_FORMATS,
    IntakeError,
    IntakeLimits,
    Upload,
    assemble_context_package,
    current_package,
    ground_task,
    prepare_intake,
    rebuild_task_context,
    reground_to_commit,
    render_export,
)
from app.context import extract_requirements as run_extraction
from app.context.intake import DEFAULT_LIMITS
from app.knowledge import (
    api_connection,
    confirm_constraint,
    create_api_token,
    create_task,
    get_membership_role,
    get_task,
    list_api_tokens,
    list_constraints,
    list_context_items,
    list_context_packages,
    resolve_decision,
    revoke_api_token,
    set_context_package_status,
    worker_connection,
)
from app.knowledge.context import (
    MAX_LIST_LIMIT,
    insert_constraints,
    list_decisions,
)
from app.knowledge.context_edits import (
    delete_user_constraint,
    get_constraint,
    is_scanned_commit,
    latest_scanned_commit,
    update_user_constraint,
)
from app.llm import (
    CallContext,
    ModelService,
    ModelServiceError,
    StructuredOutputError,
    completion_model_service,
)
from app.models import Constraint, ContextItem, ContextPackage, Task

logger = logging.getLogger(__name__)

router = APIRouter()

STATIC_ANALYSIS_NOTICE = (
    "Derived from static analysis of the repository at the snapshot commit named here. "
    "Nothing was observed by running the program, its tests or its build."
)

# Request-size ceilings. The per-document byte cap comes from IntakeLimits so
# the route and the extractor can never disagree about what "too big" is.
MAX_UPLOAD_FILES = 10
MAX_TITLE_CHARS = 300
MAX_INTENT_CHARS = 20_000
MIN_PACKAGE_BUDGET = 500
MAX_PACKAGE_BUDGET = 60_000
DEFAULT_PACKAGE_BUDGET = 12_000


def get_context_model_service() -> ModelService:
    """The ModelService requirement extraction and embedding go through. A
    dependency so tests substitute a scripted provider; no key is read in tests.
    (Separate from `app.main.get_model_service` to avoid a main <-> router
    import cycle.)"""
    return completion_model_service()


ContextModel = Annotated[ModelService, Depends(get_context_model_service)]


# ------------------------------------------------------------- authorization


def task_access(task_id: str, user: CurrentUser) -> Task:
    """Dependency for every `/tasks/{id}` route: 401 without a token, 404 for
    an unknown task, 403 unless the caller is a member of the task's project.
    The membership check precedes any further read of the task."""
    require_uuid(task_id, "task")
    with api_connection() as conn:
        task = get_task(conn, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        role = get_membership_role(conn, task.project_id, user.id)
    if role is None:
        raise HTTPException(403, "not a member of this task's project")
    return task


TaskAccess = Annotated[Task, Depends(task_access)]


def _require_open_task(task: Task) -> None:
    if task.status == "archived":
        raise HTTPException(409, "task is archived")


def _serialize_task(task: Task) -> dict:
    return task.model_dump(mode="json")


def _stale_all_packages(conn: psycopg.Connection, task_id: str) -> list[str]:
    """Mark every non-stale package of a task stale. Used after edits that the
    created_at/confirmed_at change-detection in the package module cannot see
    (a patched, deleted or un-confirmed constraint; a resolved decision): a
    package built before them must not be served as current."""
    marked: list[str] = []
    for package in list_context_packages(conn, task_id, limit=MAX_LIST_LIMIT):
        if package.id is not None and package.status != "stale":
            set_context_package_status(conn, package.id, "stale")
            marked.append(package.id)
    return marked


# ------------------------------------------------------------------- tasks


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    intent: str = Field(default="", max_length=MAX_INTENT_CHARS)
    # Defaults to the repo's newest scanned commit; if given it must have one.
    commit_sha: str | None = None


@router.post("/repos/{repo_id}/tasks", status_code=201)
def create_task_route(repo: RepoAccess, req: CreateTaskRequest, user: CurrentUser) -> dict:
    assert repo.id is not None and repo.project_id is not None
    title = req.title.strip()
    if not title:
        raise HTTPException(422, "title must not be blank")
    with api_connection() as conn:
        if req.commit_sha is None:
            commit_sha = latest_scanned_commit(conn, repo.id)
            if commit_sha is None:
                raise HTTPException(409, "this repo has no completed scan to ground a task in")
        else:
            commit_sha = req.commit_sha
            if not is_scanned_commit(conn, repo.id, commit_sha):
                raise HTTPException(404, "no completed scan for that commit")
    with worker_connection() as conn:
        task = create_task(
            conn,
            Task(
                project_id=repo.project_id,
                repo_id=repo.id,
                commit_sha=commit_sha,
                title=title,
                # Verbatim: extraction can be re-run and audited against it.
                intent=req.intent or None,
                created_by=user.id,
            ),
        )
    return _serialize_task(task)


def _task_text(task: Task) -> str:
    return f"{task.title}\n\n{task.intent}" if task.intent else task.title


async def _read_uploads(
    files: list[UploadFile], *, allow_images: bool, limits: IntakeLimits = DEFAULT_LIMITS
) -> list[Upload]:
    """Read uploads under the size caps. Reads at most cap+1 bytes per file so
    an oversized upload is rejected without buffering all of it."""
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(413, f"at most {MAX_UPLOAD_FILES} files per request")
    uploads: list[Upload] = []
    for f in files:
        cap = limits.max_image_bytes if _is_image_name(f) else limits.max_document_bytes
        data = await f.read(cap + 1)
        if len(data) > cap:
            raise HTTPException(413, f"{f.filename or 'upload'} exceeds {cap} bytes")
        upload = Upload(name=f.filename or "upload", data=data, content_type=f.content_type)
        if _is_image_name(f) and not allow_images:
            # Sending an image to a vision model is a deliberate act (it leaves
            # the process, and may carry text the secret scanner cannot read).
            raise HTTPException(
                400, "images are only processed when include_images=true is set explicitly"
            )
        uploads.append(upload)
    return uploads


def _is_image_name(f: UploadFile) -> bool:
    name = (f.filename or "").lower()
    return (f.content_type or "").lower().startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp")
    )


@router.post("/tasks/{task_id}/documents")
async def check_documents(
    task: TaskAccess,
    files: Annotated[list[UploadFile], File()],
    include_images: Annotated[bool, Form()] = False,
) -> dict:
    """Validate documents for a task without extracting or storing anything:
    runs the same deterministic intake (type/size/zip-bomb caps, text
    extraction, secret redaction) extraction will use and reports what it
    found. Uploaded bytes are not persisted; send them again with the extract
    call. No model is called."""
    _require_open_task(task)
    uploads = await _read_uploads(files, allow_images=include_images)
    try:
        intake = await run_in_threadpool(prepare_intake, _task_text(task), uploads)
    except IntakeError as exc:
        raise HTTPException(400, str(exc)) from None
    return {
        "task_id": task.id,
        "stored": False,
        "documents": [
            {"name": d.name, "chunks": len(d.chunks), "secrets_redacted": d.secrets_redacted}
            for d in intake.documents
        ],
        "images": [{"chunk_id": i.chunk_id, "name": i.name} for i in intake.images],
        "secrets_redacted": intake.secrets_redacted,
        "unreadable": intake.unreadable,
    }


def _dropped_payload(d) -> dict:
    return {"key": d.key, "kind": d.kind, "reason": d.reason, "detail": d.detail}


@router.post("/tasks/{task_id}/requirements/extract")
async def extract_route(
    task: TaskAccess,
    service: ContextModel,
    files: Annotated[list[UploadFile] | None, File()] = None,
    include_images: Annotated[bool, Form()] = False,
    check_contradictions: Annotated[bool, Form()] = True,
) -> dict:
    """Extract requirements from the task text (plus any uploaded documents)
    through ModelService, validate every statement against the input it cites,
    and persist the result. Runs inline; the response is the finished state.
    Model failures are 502 and leave the task's previous requirements as they
    were."""
    _require_open_task(task)
    assert task.id is not None
    uploads = await _read_uploads(files or [], allow_images=include_images)

    def _run() -> dict:
        intake = prepare_intake(_task_text(task), uploads)
        with worker_connection() as conn:
            result = run_extraction(
                conn,
                service,
                task,
                intake,
                check_contradictions=check_contradictions,
                context=CallContext(project_id=task.project_id, repo_id=task.repo_id),
            )
        outcome = result.outcome
        return {
            "task_id": task.id,
            "task_status": result.task_status,
            "requirements": [r.model_dump(mode="json") for r in result.requirements],
            "dropped": [_dropped_payload(d) for d in outcome.dropped],
            "constraint_conflicts": [asdict(c) for c in outcome.conflicts],
            "contradictions": [asdict(c) for c in outcome.contradictions],
            "contradiction_check": outcome.contradiction_check,
            "secrets_redacted": result.secrets_redacted,
            "unreadable": result.unreadable,
        }

    try:
        return await run_in_threadpool(_run)
    except IntakeError as exc:
        raise HTTPException(400, str(exc)) from None
    except StructuredOutputError as exc:
        raise HTTPException(
            502, {"error": "model_output_invalid", "detail": str(exc), "task_id": task.id}
        ) from None
    except ModelServiceError as exc:
        raise HTTPException(
            502, {"error": "model_call_failed", "detail": str(exc), "task_id": task.id}
        ) from None


def _freshness_payload(f) -> dict:
    p = f.package
    return {
        "package_id": p.id,
        "version": p.version,
        "commit_sha": p.commit_sha,
        "status": p.status,
        "state": "current" if f.current else "stale",
        "reason": f.reason,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/tasks/{task_id}")
def get_task_route(task: TaskAccess) -> dict:
    """Full task state, rebuilt from the database: requirements, constraints,
    decisions, grounding history and every package with its freshness."""
    assert task.id is not None
    with api_connection() as conn:
        state = rebuild_task_context(conn, task.id)
    if state is None:
        raise HTTPException(404, "task not found")
    return {
        "analysis": "static",
        "notice": STATIC_ANALYSIS_NOTICE,
        "task": _serialize_task(state.task),
        "active_snapshot": (
            state.active_snapshot.model_dump(mode="json") if state.active_snapshot else None
        ),
        "snapshots": [s.model_dump(mode="json") for s in state.snapshots],
        "requirements": [r.model_dump(mode="json") for r in state.requirements],
        "constraints": [c.model_dump(mode="json") for c in state.constraints],
        "components": [c.model_dump(mode="json") for c in state.components],
        "decisions": [d.model_dump(mode="json") for d in state.decisions],
        "verification_results": [v.model_dump(mode="json") for v in state.verification_results],
        "packages": [_freshness_payload(f) for f in state.packages],
        "latest_package": _freshness_payload(state.packages[0]) if state.packages else None,
        "current_package_id": state.current_package.id if state.current_package else None,
        "pinned_elsewhere": [asdict(p) for p in state.pinned_elsewhere],
        "problems": state.problems,
    }


# ------------------------------------------------------------- constraints


class ConstraintIn(BaseModel):
    kind: Literal[
        "public_api",
        "db_schema",
        "auth_boundary",
        "ci_lint",
        "do_not_touch",
        "dependency",
        "performance",
        "other",
    ]
    scope: str = Field(min_length=1, max_length=500)
    statement: str = Field(min_length=1, max_length=4000)
    severity: Literal["hard", "soft"]
    rationale: str | None = Field(default=None, max_length=4000)


class ConstraintPatch(BaseModel):
    kind: (
        Literal[
            "public_api",
            "db_schema",
            "auth_boundary",
            "ci_lint",
            "do_not_touch",
            "dependency",
            "performance",
            "other",
        ]
        | None
    ) = None
    scope: str | None = Field(default=None, min_length=1, max_length=500)
    statement: str | None = Field(default=None, min_length=1, max_length=4000)
    severity: Literal["hard", "soft"] | None = None
    rationale: str | None = Field(default=None, max_length=4000)


class ConfirmRequest(BaseModel):
    confirmed: bool = True


@router.get("/tasks/{task_id}/constraints")
def list_task_constraints(task: TaskAccess) -> dict:
    assert task.id is not None
    with api_connection() as conn:
        rows = list_constraints(conn, task.repo_id, task.commit_sha, task_id=task.id)
    return {
        "commit_sha": task.commit_sha,
        "constraints": [c.model_dump(mode="json") for c in rows],
    }


@router.post("/tasks/{task_id}/constraints", status_code=201)
def create_task_constraint(task: TaskAccess, req: ConstraintIn) -> dict:
    _require_open_task(task)
    assert task.id is not None
    scope, statement = req.scope.strip(), req.statement.strip()
    if not scope or not statement:
        raise HTTPException(422, "scope and statement must not be blank")
    row = Constraint(
        task_id=task.id,
        repo_id=task.repo_id,
        commit_sha=task.commit_sha,
        kind=req.kind,
        scope=scope,
        statement=statement,
        severity=req.severity,
        # A person wrote this rule down: `configured`, never `inferred`, and no
        # analyzer is named because none produced it.
        source="user_stated",
        origin="configured",
        rationale=req.rationale,
    )
    with worker_connection() as conn:
        (constraint_id,) = insert_constraints(conn, [row])
        stored = get_constraint(conn, constraint_id)
    assert stored is not None
    return stored.model_dump(mode="json")


def _task_constraint(conn: psycopg.Connection, task: Task, constraint_id: str) -> Constraint:
    """A constraint this task may act on: its own, or a repo-level detection
    for its repo. Anything else is a 404 — a constraint id from another
    task or repo must not be confirmable through this one."""
    require_uuid(constraint_id, "constraint")
    row = get_constraint(conn, constraint_id)
    if (
        row is None
        or row.repo_id != task.repo_id
        or row.task_id not in (None, task.id)
    ):
        raise HTTPException(404, "constraint not found")
    return row


@router.patch("/tasks/{task_id}/constraints/{constraint_id}")
def patch_task_constraint(task: TaskAccess, constraint_id: str, req: ConstraintPatch) -> dict:
    _require_open_task(task)
    assert task.id is not None
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "scope" in fields:
        fields["scope"] = str(fields["scope"]).strip()
    if "statement" in fields:
        fields["statement"] = str(fields["statement"]).strip()
    if any(v == "" for v in fields.values()):
        raise HTTPException(422, "scope and statement must not be blank")
    with worker_connection() as conn:
        existing = _task_constraint(conn, task, constraint_id)
        if existing.source != "user_stated" or existing.task_id != task.id:
            raise HTTPException(
                409, "only this task's user-stated constraints can be edited; auto-detected "
                "entries can be confirmed but not rewritten"
            )
        row = update_user_constraint(conn, constraint_id, task.id, fields)
        if row is None:
            raise HTTPException(404, "constraint not found")
        _stale_all_packages(conn, task.id)
    return row.model_dump(mode="json")


@router.delete("/tasks/{task_id}/constraints/{constraint_id}", status_code=204)
def delete_task_constraint(task: TaskAccess, constraint_id: str) -> Response:
    _require_open_task(task)
    assert task.id is not None
    with worker_connection() as conn:
        existing = _task_constraint(conn, task, constraint_id)
        if existing.source != "user_stated" or existing.task_id != task.id:
            raise HTTPException(409, "only this task's user-stated constraints can be deleted")
        if not delete_user_constraint(conn, constraint_id, task.id):
            raise HTTPException(404, "constraint not found")
        _stale_all_packages(conn, task.id)
    return Response(status_code=204)


@router.post("/tasks/{task_id}/constraints/{constraint_id}/confirm")
def confirm_task_constraint(
    task: TaskAccess, constraint_id: str, user: CurrentUser, req: ConfirmRequest | None = None
) -> dict:
    assert task.id is not None
    confirmed = True if req is None else req.confirmed
    with worker_connection() as conn:
        _task_constraint(conn, task, constraint_id)
        row = confirm_constraint(conn, constraint_id, user.id, confirmed=confirmed)
        if row is None:
            raise HTTPException(404, "constraint not found")
        # Un-confirming clears confirmed_at, which change detection cannot see.
        _stale_all_packages(conn, task.id)
    return row.model_dump(mode="json")


@router.get("/repos/{repo_id}/constraints")
def repo_constraints(repo: RepoAccess, commit_sha: str | None = None) -> dict:
    """The repo-wide ledger at one snapshot: auto-detected and repo-level
    entries only. Task-level entries stay on their task."""
    assert repo.id is not None
    with api_connection() as conn:
        sha = commit_sha or latest_scanned_commit(conn, repo.id)
        if sha is None:
            raise HTTPException(409, "this repo has no completed scan")
        rows = list_constraints(conn, repo.id, sha, limit=MAX_LIST_LIMIT)
    return {
        "analysis": "static",
        "commit_sha": sha,
        "constraints": [c.model_dump(mode="json") for c in rows if c.task_id is None],
    }


# --------------------------------------------------------------- decisions


class ResolveDecisionRequest(BaseModel):
    decision: str = Field(min_length=1, max_length=4000)
    rationale: str | None = Field(default=None, max_length=4000)
    # Locked by default: an answered decision is what later passes read and
    # must not rewrite.
    locked: bool = True


@router.post("/tasks/{task_id}/decisions/{decision_id}/resolve")
def resolve_decision_route(
    task: TaskAccess, decision_id: str, req: ResolveDecisionRequest, user: CurrentUser
) -> dict:
    _require_open_task(task)
    assert task.id is not None
    require_uuid(decision_id, "decision")
    text = req.decision.strip()
    if not text:
        raise HTTPException(422, "decision must not be blank")
    with worker_connection() as conn:
        # Scoped to this task's own decisions: an id from another task is a 404.
        existing = next(
            (d for d in list_decisions(conn, task.id, limit=MAX_LIST_LIMIT) if d.id == decision_id),
            None,
        )
        if existing is None:
            raise HTTPException(404, "decision not found")
        if existing.locked:
            raise HTTPException(409, "this decision is already resolved and locked")
        row = resolve_decision(
            conn, decision_id, text, user.id, rationale=req.rationale, locked=req.locked
        )
        if row is None:
            raise HTTPException(404, "decision not found")
        _stale_all_packages(conn, task.id)
    return row.model_dump(mode="json")


# ----------------------------------------------------------------- context


class AssembleRequest(BaseModel):
    token_budget: int = Field(
        default=DEFAULT_PACKAGE_BUDGET, ge=MIN_PACKAGE_BUDGET, le=MAX_PACKAGE_BUDGET
    )
    # Re-run deterministic grounding even if the task already has components.
    reground_components: bool = False


def _package_payload(
    package: ContextPackage, items: list[ContextItem], *, current: bool, reason: str | None
) -> dict:
    return {
        "analysis": "static",
        "notice": STATIC_ANALYSIS_NOTICE,
        "package": package.model_dump(mode="json"),
        # Stale means: do not hand this to an agent. It is still returned (and
        # labelled) so the UI can show what changed instead of a blank.
        "freshness": {
            "state": "current" if current else "stale",
            "current": current,
            "reason": reason,
        },
        "items": [i.model_dump(mode="json") for i in items],
    }


@router.post("/tasks/{task_id}/context", status_code=201)
def assemble_route(
    task: TaskAccess, service: ContextModel, req: AssembleRequest | None = None
) -> dict:
    """Ground the task in the graph (deterministic) and assemble a new,
    budgeted context package version. Constraints are never truncated: a
    budget that cannot hold them yields a package marked `invalid`."""
    _require_open_task(task)
    assert task.id is not None
    req = req or AssembleRequest()
    try:
        with worker_connection() as conn:
            ground_task(conn, task.id, write=True)
            assembled = assemble_context_package(
                conn,
                task.id,
                token_budget=req.token_budget,
                embedder=ModelServiceEmbedder(service),
                reground_components=False,
            )
    except psycopg.errors.UndefinedTable:
        raise HTTPException(
            503, "the semantic index tables are not applied in this deployment"
        ) from None
    package = assembled.package
    return _package_payload(
        package,
        assembled.items,
        current=package.status == "assembled",
        reason=None if package.status == "assembled" else f"package is {package.status}",
    )


@router.get("/tasks/{task_id}/context")
def get_context_route(task: TaskAccess) -> dict:
    assert task.id is not None
    with api_connection() as conn:
        fresh = current_package(conn, task.id)
        if fresh is None or fresh.package.id is None:
            raise HTTPException(404, "no context package has been assembled for this task")
        items = list_context_items(conn, task.id, package_id=fresh.package.id)
    return _package_payload(fresh.package, items, current=fresh.current, reason=fresh.reason)


@router.get("/tasks/{task_id}/export")
def export_route(
    task: TaskAccess,
    format: Annotated[str, Query()] = "context_md",
    allow_stale: bool = False,
) -> Response:
    """Render the task's latest package as markdown. A stale or invalid package
    is refused (409) unless `allow_stale=true`, in which case the export
    carries a STALE/INVALID banner."""
    if format not in EXPORT_FORMATS:
        raise HTTPException(422, f"format must be one of: {', '.join(EXPORT_FORMATS)}")
    assert task.id is not None
    with api_connection() as conn:
        fresh = current_package(conn, task.id)
        if fresh is None or fresh.package.id is None:
            raise HTTPException(404, "no context package has been assembled for this task")
        package = fresh.package
        usable = fresh.current and package.status == "assembled"
        if not usable and not allow_stale:
            raise HTTPException(
                409,
                {
                    "error": "package_not_current",
                    "package_status": package.status,
                    "reason": fresh.reason or f"package is {package.status}",
                    "detail": "regenerate the context package, or pass allow_stale=true",
                },
            )
        items = list_context_items(conn, task.id, package_id=package.id)
    if not fresh.current and package.status == "assembled":
        # Stale by input change but still marked 'assembled' in storage: label
        # it for rendering so the document says so.
        package = package.model_copy(update={"status": "stale"})
    body = render_export(package, items, format)  # pyright: ignore[reportArgumentType]
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{EXPORT_FILENAMES[format]}"',
            "X-CodeAxis-Package-Status": package.status,
            "X-CodeAxis-Snapshot": package.commit_sha,
        },
    )


class RegroundRequest(BaseModel):
    commit_sha: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/tasks/{task_id}/reground")
def reground_route(task: TaskAccess, req: RegroundRequest) -> dict:
    """Move the task to another scanned commit. Packages built elsewhere become
    stale; records still pinned to the old commit are listed, not migrated."""
    _require_open_task(task)
    assert task.id is not None
    if not req.reason.strip():
        raise HTTPException(422, "a reason is required")
    with api_connection() as conn:
        if not is_scanned_commit(conn, task.repo_id, req.commit_sha):
            raise HTTPException(404, "no completed scan for that commit")
    with worker_connection() as conn:
        report = reground_to_commit(conn, task.id, req.commit_sha, req.reason.strip())
    return {
        "previous_commit_sha": report.previous_commit_sha,
        "snapshot": report.snapshot.model_dump(mode="json"),
        "stale_package_ids": report.stale_package_ids,
        "needs_rederivation": [asdict(p) for p in report.needs_rederivation],
    }


# -------------------------------------------------------------- API tokens


def _owner_only(repo_id: str, project_id: str | None, user: CurrentUser) -> None:
    if project_id is None:
        raise HTTPException(403, "repo has no project")
    role = require_project_member(project_id, user)
    if role != "owner":
        raise HTTPException(403, "only a project owner can manage API tokens")


class CreateTokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


@router.post("/repos/{repo_id}/api-tokens", status_code=201)
def create_token_route(
    repo: RepoAccess, req: CreateTokenRequest, user: CurrentUser
) -> dict:
    assert repo.id is not None and repo.project_id is not None
    _owner_only(repo.id, repo.project_id, user)
    name = req.name.strip()
    if not name:
        raise HTTPException(422, "name must not be blank")
    expires = None
    if req.expires_in_days is not None:
        expires = datetime.now(UTC) + timedelta(days=req.expires_in_days)
    with worker_connection() as conn:
        issued = create_api_token(
            conn, repo.project_id, repo.id, name, created_by=user.id, expires_at=expires
        )
    # The one response that ever contains the plaintext. It is deliberately
    # not logged anywhere (the request logger records method/path only).
    return {
        "token": issued.token,
        "shown_once": True,
        "record": issued.record.model_dump(mode="json"),
        "mcp_url_path": f"/mcp/{repo.id}",
    }


@router.get("/repos/{repo_id}/api-tokens")
def list_tokens_route(repo: RepoAccess, user: CurrentUser, include_revoked: bool = False) -> dict:
    assert repo.id is not None
    _owner_only(repo.id, repo.project_id, user)
    with api_connection() as conn:
        rows = list_api_tokens(conn, repo.id, include_revoked=include_revoked)
    return {"tokens": [t.model_dump(mode="json") for t in rows]}


@router.delete("/repos/{repo_id}/api-tokens/{token_id}", status_code=204)
def revoke_token_route(repo: RepoAccess, token_id: str, user: CurrentUser) -> Response:
    assert repo.id is not None
    _owner_only(repo.id, repo.project_id, user)
    require_uuid(token_id, "token")
    with worker_connection() as conn:
        # Scoped to this repo's tokens: another repo's token id is a 404.
        if not any(t.id == token_id for t in list_api_tokens(conn, repo.id)):
            raise HTTPException(404, "token not found")
        revoke_api_token(conn, token_id)
    return Response(status_code=204)
