"""HTTP surface of the Virtual Development Environment (build-plan.md Part
4.G, Part 8).

Authorization is entirely app-layer, same as the rest of this API: the
database does not scope rows by user (the Clerk migration dropped RLS
membership), so every route resolves its own project membership.
Repo-scoped routes use `RepoAccess`; every `/blueprints/{id}` route goes
through `blueprint_access`, which loads the blueprint and then checks the
caller's membership in *its* project (404 unknown, 403 non-member) before
anything else about it is read.

Two things this module will not do:

* **It does not decide anything about a design.** Generation, validation,
  the file-structure proposal, regeneration and export live in
  `app.blueprint`; these routes authorize, parse, call, and serialize.
* **It never presents a design as fact.** A blueprint is a proposal about
  code that does not exist yet. Every payload carries the version's own
  `status` and `validation_errors`, and an `invalid` or superseded version is
  returned as such rather than quietly withheld or smoothed over (Part
  4.G.5). No route implies the program was run.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.auth import CurrentUser, RepoAccess, require_uuid
from app.blueprint import (
    InvalidBlueprintError,
    build_document,
    export_blueprint_context,
    generate_blueprint,
    modules_of,
    regenerate_blueprint,
    validate_and_record,
)
from app.context import EXPORT_FILENAMES, EXPORT_FORMATS, render_export
from app.knowledge import (
    api_connection,
    create_blueprint,
    create_job,
    get_active_job_for_repo,
    get_blueprint,
    get_component,
    get_current_version,
    get_layout,
    get_membership_role,
    get_task,
    get_version,
    list_blueprints,
    list_context_items,
    list_proposed_files,
    list_versions,
    set_blueprint_title,
    set_job_status,
    set_layout,
    update_component,
    worker_connection,
)
from app.knowledge.context import MAX_LIST_LIMIT, list_tasks
from app.knowledge.context_edits import is_scanned_commit
from app.knowledge.jobs import JobAlreadyExistsError
from app.llm import (
    CallContext,
    ModelService,
    ModelServiceError,
    StructuredOutputError,
    model_service,
)
from app.models import (
    BLUEPRINT_SCHEMA_VERSION,
    KIND_TO_MAPPING,
    Blueprint,
    BlueprintComponentKind,
    BlueprintDocument,
    BlueprintMapping,
    BlueprintVersion,
    Job,
    JobKind,
    JobStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter()

STATIC_ANALYSIS_NOTICE = (
    "Derived from static analysis of the repository at the snapshot commit named here. "
    "Nothing was observed by running the program, its tests or its build."
)

# A blueprint is a proposal, and saying so once in the payload is cheaper than
# hoping every surface remembers to.
PROPOSAL_NOTICE = (
    "A proposed design for code that does not exist yet. Components marked "
    "'existing' or 'modified' name a real entity in the snapshot; 'proposed' "
    "components do not exist in the repository."
)

MAX_TITLE_CHARS = 300


def get_blueprint_model_service() -> ModelService:
    """The ModelService design generation goes through. A module-level
    function rather than a FastAPI dependency because the generation pass runs
    in a background task, where the request's dependency scope is already
    gone; tests monkeypatch this name."""
    return model_service()


# ------------------------------------------------------------- dependencies


def blueprint_access(blueprint_id: str, user: CurrentUser) -> Blueprint:
    """Dependency for every `/blueprints/{id}` route: 401 without a token, 404
    for an unknown blueprint, 403 unless the caller is a member of its
    project. The membership check precedes any further read."""
    require_uuid(blueprint_id, "blueprint")
    with api_connection() as conn:
        blueprint = get_blueprint(conn, blueprint_id)
        if blueprint is None:
            raise HTTPException(404, "blueprint not found")
        role = get_membership_role(conn, blueprint.project_id, user.id)
    if role is None:
        raise HTTPException(403, "not a member of this blueprint's project")
    return blueprint


BlueprintAccess = Annotated[Blueprint, Depends(blueprint_access)]


# ----------------------------------------------------------------- assembly


def _document_or_404(version_id: str) -> BlueprintDocument:
    """One version as the canonical document.

    Assembly lives in `app.blueprint.document`, not here: it also decides
    which validation errors are advisory rather than blocking, and which open
    decisions and questions ride along as `unresolved`. Duplicating that in
    the route would give the HTTP surface its own slowly-diverging idea of
    what is unresolved.
    """
    with api_connection() as conn:
        try:
            return build_document(conn, version_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc


def _serialize_document(document: BlueprintDocument) -> dict:
    """The document plus the two things Part 6's canonical JSON names but the
    model does not store: `data_flows` (a filtered view of `connections`) and
    `modules` (a grouping by `module_key`). Both derived here so a client
    never has to re-derive them and risk disagreeing with the server."""
    payload = document.model_dump(mode="json")
    payload["data_flows"] = [c.model_dump(mode="json") for c in document.data_flows]
    payload["modules"] = modules_of(document.components)
    payload["notice"] = STATIC_ANALYSIS_NOTICE
    payload["proposal_notice"] = PROPOSAL_NOTICE
    return payload


def _current_or_404(blueprint: Blueprint) -> BlueprintVersion:
    assert blueprint.id is not None
    with api_connection() as conn:
        version = get_current_version(conn, blueprint.id)
    if version is None:
        raise HTTPException(
            404,
            "this blueprint has no version yet — nothing has been generated for it",
        )
    return version


# -------------------------------------------------------------------- tasks
# Listing a repo's tasks belongs to the context engine, but no route exposed
# it, which left the Build tab unable to offer a task to design against
# without remembering ids client-side. One read, here, rather than a
# client-side workaround.


@router.get("/repos/{repo_id}/tasks")
def list_tasks_route(
    repo: RepoAccess,
    commit_sha: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 100,
) -> list[dict]:
    assert repo.id is not None and repo.project_id is not None
    with api_connection() as conn:
        # `project_id` is positional and required by design: a task list that
        # could span projects would be a route waiting to leak one.
        tasks = list_tasks(
            conn, repo.project_id, repo_id=repo.id, commit_sha=commit_sha, limit=limit
        )
    return [t.model_dump(mode="json") for t in tasks]


# --------------------------------------------------------------- blueprints


class CreateBlueprintRequest(BaseModel):
    task_id: str
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    # Optional: defaults to the task's own snapshot, which is the only
    # snapshot its requirements and grounding are about.
    commit_sha: str | None = None


@router.post("/repos/{repo_id}/blueprints", status_code=201)
def create_blueprint_route(
    repo: RepoAccess, req: CreateBlueprintRequest, user: CurrentUser
) -> dict:
    """Start a design effort against one task.

    The snapshot comes from the task unless the caller names one explicitly,
    and either way it must be a commit this repo has actually scanned: a
    design grounded in a snapshot the graph does not hold could not have its
    'existing' components checked against anything (Part 4.G.5).
    """
    assert repo.id is not None and repo.project_id is not None
    require_uuid(req.task_id, "task")
    title = req.title.strip()
    if not title:
        raise HTTPException(422, "title must not be blank")

    with api_connection() as conn:
        task = get_task(conn, req.task_id)
        if task is None or task.repo_id != repo.id:
            raise HTTPException(404, "task not found in this repo")
        commit_sha = req.commit_sha or task.commit_sha
        if not is_scanned_commit(conn, repo.id, commit_sha):
            raise HTTPException(404, "no completed scan for that commit")
        if req.commit_sha is not None and req.commit_sha != task.commit_sha:
            # Allowed, but it means the task's requirements were extracted
            # against a different snapshot than the design is grounded in.
            # Surfaced rather than silently accepted.
            logger.info(
                "blueprint commit %s differs from task commit %s",
                req.commit_sha,
                task.commit_sha,
            )

    with worker_connection() as conn:
        blueprint = create_blueprint(
            conn,
            Blueprint(
                project_id=repo.project_id,
                repo_id=repo.id,
                commit_sha=commit_sha,
                task_id=req.task_id,
                title=title,
                created_by=user.id,
            ),
        )
    payload = blueprint.model_dump(mode="json")
    payload["task_commit_sha"] = task.commit_sha
    return payload


@router.get("/repos/{repo_id}/blueprints")
def list_blueprints_route(
    repo: RepoAccess,
    commit_sha: Annotated[str | None, Query()] = None,
    task_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 100,
) -> list[dict]:
    assert repo.id is not None
    if task_id is not None:
        require_uuid(task_id, "task")
    with api_connection() as conn:
        rows = list_blueprints(
            conn, repo_id=repo.id, commit_sha=commit_sha, task_id=task_id, limit=limit
        )
    return [r.model_dump(mode="json") for r in rows]


@router.get("/blueprints/schema-version")
def schema_version_route() -> dict:
    """What contract this server writes and will accept. A client holding a
    different one should say so rather than guess at the difference.

    Declared before `/blueprints/{blueprint_id}`: FastAPI matches in
    declaration order, so the parameterized route would otherwise swallow
    this path and reject "schema-version" as a malformed id.
    """
    return {"schema_version": BLUEPRINT_SCHEMA_VERSION}


@router.get("/blueprints/{blueprint_id}")
def get_blueprint_route(blueprint: BlueprintAccess) -> dict:
    """The current version, assembled. 404 when nothing has been generated
    yet — an empty design is not a design, and returning one would let a
    client show a blank canvas as though it were a proposal."""
    version = _current_or_404(blueprint)
    assert version.id is not None
    return _serialize_document(_document_or_404(version.id))


class UpdateBlueprintRequest(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)


@router.patch("/blueprints/{blueprint_id}")
def update_blueprint_route(blueprint: BlueprintAccess, req: UpdateBlueprintRequest) -> dict:
    """Rename a design. The title is the only field a caller may change here:
    the snapshot and the task are what the design is *about*, and repointing
    either would silently invalidate every mapping in it."""
    assert blueprint.id is not None
    title = req.title.strip()
    if not title:
        raise HTTPException(422, "title must not be blank")
    with worker_connection() as conn:
        set_blueprint_title(conn, blueprint.id, title)
    with api_connection() as conn:
        updated = get_blueprint(conn, blueprint.id)
    assert updated is not None
    return updated.model_dump(mode="json")


# ----------------------------------------------------------------- versions


@router.get("/blueprints/{blueprint_id}/versions")
def list_versions_route(
    blueprint: BlueprintAccess,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 100,
) -> list[dict]:
    """Newest first. Nothing is pruned: the history is the audit trail Part
    4.G.9 asks for, including the versions that failed validation."""
    assert blueprint.id is not None
    with api_connection() as conn:
        versions = list_versions(conn, blueprint.id, limit=limit)
    return [
        {
            **v.model_dump(mode="json"),
            "is_current": v.id == blueprint.current_version_id,
        }
        for v in versions
    ]


@router.get("/blueprints/{blueprint_id}/versions/{version_id}")
def get_version_route(blueprint: BlueprintAccess, version_id: str) -> dict:
    """One specific version, assembled — including a superseded or invalid
    one. Reading an old version is how an edit is reviewed, so it is served
    with its own status rather than hidden."""
    require_uuid(version_id, "version")
    with api_connection() as conn:
        version = get_version(conn, version_id)
    if version is None or version.blueprint_id != blueprint.id:
        raise HTTPException(404, "version not found for this blueprint")
    assert version.id is not None
    payload = _serialize_document(_document_or_404(version.id))
    payload["is_current"] = version.id == blueprint.current_version_id
    return payload


# --------------------------------------------------------------- components


class UpdateComponentRequest(BaseModel):
    """A partial edit. `kind` and `mapping` are not independent — the pair is
    fixed by the taxonomy — so a request naming one may name the other only
    if it is the matching half."""

    name: str | None = Field(default=None, min_length=1, max_length=300)
    kind: BlueprintComponentKind | None = None
    mapping: BlueprintMapping | None = None
    linked_component_id: int | None = None
    module_key: str | None = None
    responsibility: str | None = None
    locked: bool | None = None


@router.patch("/blueprints/{blueprint_id}/components/{component_id}")
def update_component_route(
    blueprint: BlueprintAccess, component_id: str, req: UpdateComponentRequest
) -> dict:
    """Edit one component.

    Three rules this enforces rather than trusting the caller with:

    * `kind` and `mapping` move together (existing+reuse, modified+extend,
      proposed+new). Naming only `kind` derives the mapping; naming a
      contradictory pair is a 422, not a row the database would reject later
      with a constraint error the UI cannot explain.
    * An `existing`/`modified` component must name a real entity, so a kind
      change into either requires a `linked_component_id` — and that id must
      exist at *this blueprint's* snapshot.
    * Any edit marks the row `edited` (or `locked`), so a later regeneration
      can tell human intent from generated output (Part 4.G.9).
    """
    require_uuid(component_id, "component")
    with api_connection() as conn:
        component = get_component(conn, component_id)
    if component is None:
        raise HTTPException(404, "component not found")
    version_ids = _version_ids(blueprint)
    if component.blueprint_version_id not in version_ids:
        raise HTTPException(404, "component not found for this blueprint")

    fields = req.model_dump(exclude_unset=True)
    if not fields:
        return component.model_dump(mode="json")

    kind = fields.get("kind", component.kind)
    if "kind" in fields or "mapping" in fields:
        expected = KIND_TO_MAPPING[kind]
        mapping = fields.get("mapping", expected)
        if mapping != expected:
            raise HTTPException(
                422,
                f"kind={kind!r} pairs with mapping={expected!r}, not {mapping!r}",
            )
        fields["kind"] = kind
        fields["mapping"] = expected

    link = fields.get("linked_component_id", component.linked_component_id)
    if kind != "proposed":
        if link is None:
            raise HTTPException(
                422,
                f"a {kind!r} component must name the repo entity it maps to "
                "(linked_component_id)",
            )
        _require_real_node(blueprint, link)
    elif "kind" in fields and kind == "proposed":
        # A component that no longer claims to exist must not keep a link to
        # something that does.
        fields["linked_component_id"] = None

    if fields.get("locked") is True:
        fields["generation_status"] = "locked"
    elif fields.get("locked") is False:
        fields["generation_status"] = "edited"
    else:
        fields.setdefault("generation_status", "edited")
        if component.generation_status == "locked" and fields["generation_status"] != "locked":
            raise HTTPException(
                409,
                "this component is locked; unlock it before editing (locked: false)",
            )

    with worker_connection() as conn:
        updated = update_component(conn, component_id, fields)
    if updated is None:
        raise HTTPException(404, "component not found")
    return updated.model_dump(mode="json")


def _version_ids(blueprint: Blueprint) -> set[str]:
    assert blueprint.id is not None
    with api_connection() as conn:
        versions = list_versions(conn, blueprint.id, limit=MAX_LIST_LIMIT)
    return {v.id for v in versions if v.id is not None}


def _require_real_node(blueprint: Blueprint, node_id: int) -> None:
    """A mapping must point at a node that exists *at this blueprint's own
    snapshot*. A node id from another commit is the cross-snapshot mix Part 5
    forbids, and accepting it would make a design look grounded when it is
    not."""
    from app.knowledge import existing_node_ids

    with api_connection() as conn:
        found = existing_node_ids(conn, blueprint.repo_id, blueprint.commit_sha, [node_id])
    if node_id not in found:
        raise HTTPException(
            422,
            f"node {node_id} does not exist at this blueprint's snapshot "
            f"({blueprint.commit_sha})",
        )


# ----------------------------------------------------- generation (async)
# Part 8: anything long returns a job id and is polled. A design pass is one
# or more model calls, so it goes through the same `codeaxis.jobs` record a
# scan does and the client polls GET /jobs/{id} — the flow the import screen
# already uses.


def _start_design_job(blueprint: Blueprint, kind: JobKind) -> Job:
    """Claim a job slot for a design pass, or explain why it can't run.

    The repo-wide active-job check is deliberate: a scan rewrites the very
    graph a design's `existing` components are checked against, so letting a
    generation run beside one would ground the design in a snapshot that is
    being replaced underneath it.
    """
    assert blueprint.id is not None
    with api_connection() as conn:
        active = get_active_job_for_repo(conn, blueprint.repo_id)
    if active is not None:
        raise HTTPException(
            409,
            f"this repo already has a {active.kind} job {active.status}; wait for it to finish",
        )
    with worker_connection() as conn:
        try:
            return create_job(conn, blueprint.project_id, kind, repo_id=blueprint.repo_id)
        except JobAlreadyExistsError as exc:
            raise HTTPException(409, "this repo already has a job in flight") from exc


def _run_generate(blueprint_id: str, job_id: str, user_id: str, deep: bool) -> None:
    """Background body of a generation. Records the outcome on the job:
    a pass that produced an `invalid` version still *succeeded* — it did its
    work and the verdict is on the version, which is not the same thing as
    the job failing."""
    service = get_blueprint_model_service()
    try:
        with worker_connection() as conn:
            generate_blueprint(
                conn,
                service,
                blueprint_id,
                deep=deep,
                created_by=user_id,
                context=CallContext(project_id=None, repo_id=None),
            )
        with worker_connection() as conn:
            set_job_status(conn, job_id, JobStatus.SUCCEEDED)
    except Exception as exc:  # noqa: BLE001 - the job row is the error channel
        logger.exception("blueprint generation failed for %s", blueprint_id)
        with worker_connection() as conn:
            set_job_status(conn, job_id, JobStatus.FAILED, str(exc))


class GenerateRequest(BaseModel):
    # Part 3's "deep plan" mode: escalates the `design` role to the larger
    # model. Off by default because it costs more per call.
    deep: bool = False


@router.post("/blueprints/{blueprint_id}/generate", status_code=202)
def generate_route(
    blueprint: BlueprintAccess,
    req: GenerateRequest,
    user: CurrentUser,
    background: BackgroundTasks,
) -> dict:
    assert blueprint.id is not None
    job = _start_design_job(blueprint, "blueprint_generate")
    assert job.id is not None
    with worker_connection() as conn:
        set_job_status(conn, job.id, JobStatus.RUNNING)
    background.add_task(_run_generate, blueprint.id, job.id, user.id, req.deep)
    return {"job_id": job.id, "blueprint_id": blueprint.id}


class RegenerateRequest(BaseModel):
    """`component_keys` names where to start; the pass expands that to the
    affected neighborhood itself. Empty means the whole design."""

    component_keys: list[str] = []
    instruction: str | None = Field(default=None, max_length=4000)
    deep: bool = False


@router.post("/blueprints/{blueprint_id}/regenerate")
def regenerate_route(
    blueprint: BlueprintAccess, req: RegenerateRequest, user: CurrentUser
) -> dict:
    """Regenerate part of a design.

    Synchronous, unlike `generate`: a regeneration can be *rejected* before
    any model call — a locked entity in the affected neighborhood is a hard
    block (Part 4.G.9) — and a client needs that answer in the response it
    asked for, not discovered later by polling a job that says "failed".
    """
    assert blueprint.id is not None
    service = get_blueprint_model_service()
    try:
        with worker_connection() as conn:
            result = regenerate_blueprint(
                conn,
                service,
                blueprint.id,
                req.component_keys,
                instruction=req.instruction,
                deep=req.deep,
                created_by=user.id,
                context=CallContext(project_id=None, repo_id=None),
            )
    except (ModelServiceError, StructuredOutputError) as exc:
        raise HTTPException(502, f"the design model call failed: {exc}") from exc

    if result.rejected:
        # Nothing was written. The reasons are the point of the response.
        raise HTTPException(
            422,
            {
                "error": "regeneration_rejected",
                "message": (
                    "This regeneration would have altered a locked part of the design, "
                    "so nothing was changed."
                ),
                "rejected": [e.model_dump(mode="json") for e in result.rejected],
            },
        )
    assert result.version is not None
    return {
        "version": result.version.model_dump(mode="json"),
        "promoted": result.promoted,
        "advisories": [e.model_dump(mode="json") for e in result.advisories],
        "unresolved": result.unresolved,
        "dropped": [asdict(d) for d in result.dropped],
        # What was actually rewritten vs. carried over unchanged — the
        # distinction Part 4.G.9's partial regeneration exists to make.
        "regenerated_keys": sorted(result.neighborhood.regenerate)
        if result.neighborhood is not None
        else [],
        "frozen_keys": sorted(result.neighborhood.frozen)
        if result.neighborhood is not None
        else [],
    }


# --------------------------------------------------------------- validation


@router.post("/blueprints/{blueprint_id}/validate")
def validate_route(blueprint: BlueprintAccess) -> dict:
    """Re-run validation on the current version and record the verdict.

    Writes only `status` and `validation_errors` — validating a design does
    not change what it proposes, so this is not a new version (Part 4.G.5).
    A failing run is a 200 carrying `invalid` plus its reasons, not an HTTP
    error: the request succeeded, and the blueprint is the thing with the
    problem.
    """
    version = _current_or_404(blueprint)
    assert version.id is not None
    with worker_connection() as conn:
        outcome = validate_and_record(conn, version.id)
        updated = get_version(conn, version.id)
    assert updated is not None
    return {
        **updated.model_dump(mode="json"),
        "blocking": [e.model_dump(mode="json") for e in outcome.blocking],
    }


# ----------------------------------------------------------- file structure


@router.get("/blueprints/{blueprint_id}/file-structure")
def file_structure_route(blueprint: BlueprintAccess) -> list[dict]:
    """The current version's proposed files (Part 4.G.7). Read from what the
    generation pass stored — this route does not propose anything, so a
    blueprint whose generation never got to files returns an empty list
    rather than inventing a plausible tree."""
    version = _current_or_404(blueprint)
    assert version.id is not None
    with api_connection() as conn:
        files = list_proposed_files(conn, version.id, limit=MAX_LIST_LIMIT)
    return [f.model_dump(mode="json") for f in files]


# ------------------------------------------------------------------- export


class ExportRequest(BaseModel):
    format: str = "claude_md"
    token_budget: int = Field(default=12_000, ge=500, le=60_000)
    # An invalid design can still be exported, but only on purpose: the
    # package then carries a limitation item saying so.
    allow_invalid: bool = False


@router.post("/blueprints/{blueprint_id}/export")
def export_route(blueprint: BlueprintAccess, req: ExportRequest) -> dict:
    """Export the design as a context package (Part 4.G.8) and render it.

    Reuses the context engine's own assembly and renderer — there is no
    blueprint-specific export format, deliberately, so an agent receives the
    same shape whatever produced it.
    """
    if req.format not in EXPORT_FORMATS:
        raise HTTPException(422, f"unknown format {req.format!r}; expected one of {EXPORT_FORMATS}")
    version = _current_or_404(blueprint)
    assert version.id is not None and blueprint.id is not None

    try:
        with worker_connection() as conn:
            built = export_blueprint_context(
                conn,
                version.id,
                token_budget=req.token_budget,
                allow_invalid=req.allow_invalid,
            )
    except InvalidBlueprintError as exc:
        raise HTTPException(
            409,
            {
                "error": "blueprint_invalid",
                "message": str(exc),
                "hint": "re-validate and fix it, or export again with allow_invalid: true",
            },
        ) from exc

    with api_connection() as conn:
        items = list_context_items(conn, built.package.task_id, package_id=built.package.id)
    markdown = render_export(built.package, items, req.format)  # pyright: ignore[reportArgumentType]
    return {
        "filename": EXPORT_FILENAMES[req.format],
        "markdown": markdown,
        "package_status": built.package.status,
        "token_count": built.package.token_count,
        "blueprint_version": version.version,
    }


# ------------------------------------------------------------------- layout


@router.get("/blueprints/{blueprint_id}/layout")
def get_layout_route(blueprint: BlueprintAccess) -> dict:
    version = _current_or_404(blueprint)
    assert version.id is not None
    with api_connection() as conn:
        row = get_layout(conn, version.id)
    return {
        "blueprint_version_id": version.id,
        "layout": row.layout if row is not None else {},
    }


class SetLayoutRequest(BaseModel):
    layout: dict


@router.put("/blueprints/{blueprint_id}/layout", status_code=204)
def set_layout_route(blueprint: BlueprintAccess, req: SetLayoutRequest) -> Response:
    """Persist positions for the current version.

    Deliberately not a new version: arranging the diagram changes nothing
    about what is being built (Part 4.G.6). Layout is keyed by component and
    connection `key`, so it survives a regeneration of the rows it positions.
    """
    version = _current_or_404(blueprint)
    assert version.id is not None
    with worker_connection() as conn:
        set_layout(conn, version.id, req.layout)
    return Response(status_code=204)
