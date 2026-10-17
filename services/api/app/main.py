"""HTTP surface: create/list projects, stage a repo under a project, preview
what a scan would include, confirm to start the scan, poll its job, read the
graph.

Every route except /health requires a verified Clerk JWT (app.auth), and
every /repos and /jobs route additionally requires membership in the owning
project — the app-layer gate of build-plan.md Part 2, and the only one that
applies: nothing in this app ever reads Postgres directly from the browser.
Reads go through the read-only `codeaxis_api` role (api_connection); the
write paths — registering a repo and running its scan — delegate to
app.analysis/app.repository, the only modules that write to the Repo Model.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.analysis.jobs import (
    discard_checkout_if_scratch,
    preview_repo,
    run_scan_job,
    stage_repo,
    stage_zip_repo,
)
from app.analysis.scan import PreviewEntry
from app.auth import (
    CurrentUser,
    RepoAccess,
    require_project_member,
    require_uuid,
)
from app.knowledge import (
    JobAlreadyExistsError,
    api_connection,
    create_job,
    create_project,
    delete_repo,
    get_active_job_for_repo,
    get_blob,
    get_job,
    get_membership_role,
    get_node,
    get_root_node,
    list_children,
    list_coverage,
    list_pass_runs,
    list_projects_for_user,
    neighborhood,
    unresolved_summary,
    worker_connection,
)
from app.knowledge.reads import MAX_GRAPH_DEPTH
from app.logging import RequestLoggingMiddleware, configure_logging
from app.models import IngestSource, Job, ProjectWithRole, Repo
from app.repository.ingest import ZIP_LIMITS, IngestError, MaterializedCheckout

configure_logging()

app = FastAPI(title="CodeAxis API")

app.add_middleware(RequestLoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(IngestError)
async def _ingest_error(_: Request, exc: IngestError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ------------------------------------------------------------- projects
#
# Minimal: create a project (caller becomes its owner) and list the caller's
# projects, so there is a project_id to register repos under.


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@app.post("/projects", response_model=ProjectWithRole, status_code=201)
def post_project(req: CreateProjectRequest, user: CurrentUser) -> ProjectWithRole:
    name = req.name.strip()
    if not name:
        raise HTTPException(422, "project name must not be blank")
    email = user.claims.get("email") if isinstance(user.claims.get("email"), str) else None
    with worker_connection() as conn:
        return create_project(conn, name, user.id, email)


@app.get("/projects")
def get_projects(user: CurrentUser) -> dict[str, list[ProjectWithRole]]:
    with api_connection() as conn:
        return {"projects": list_projects_for_user(conn, user.id)}


# ------------------------------------------------------------- jobs


@app.get("/jobs/{job_id}", response_model=Job)
def job_status(job_id: str, user: CurrentUser) -> Job:
    """Status of one platform job. 404 for an unknown id, 403 unless the
    caller is a member of the job's project."""
    require_uuid(job_id, "job")
    with api_connection() as conn:
        job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        role = get_membership_role(conn, job.project_id, user.id)
    if role is None:
        raise HTTPException(403, "not a member of this job's project")
    return job


# ------------------------------------------------------------- ingestion
#
# stage/upload materialize a checkout, register the repo, and return the
# exclusion preview. Nothing is scanned until confirm is called.


class PreviewEntryOut(BaseModel):
    path: str
    kind: str
    language: str | None
    size_bytes: int | None
    included: bool
    reason: str | None


class StageRepoResponse(BaseModel):
    id: str
    commit_sha: str
    local_path: str
    preview: list[PreviewEntryOut]


def _preview_out(entry: PreviewEntry) -> PreviewEntryOut:
    return PreviewEntryOut(
        path=entry.path,
        kind=entry.kind,
        language=entry.language,
        size_bytes=entry.size_bytes,
        included=entry.included,
        reason=entry.reason,
    )


def _stage_response(repo: Repo, checkout: MaterializedCheckout) -> StageRepoResponse:
    assert repo.id is not None
    preview = preview_repo(str(checkout.local_path))
    return StageRepoResponse(
        id=repo.id,
        commit_sha=checkout.commit_sha,
        local_path=str(checkout.local_path),
        preview=[_preview_out(e) for e in preview],
    )


@app.post("/repos/stage", response_model=StageRepoResponse)
def stage(source: IngestSource, project_id: str, user: CurrentUser) -> StageRepoResponse:
    """Register a repo under `project_id` (query parameter). Membership is
    checked before anything is fetched or written."""
    require_project_member(project_id, user)
    repo, checkout = stage_repo(source, project_id)
    return _stage_response(repo, checkout)


@app.post("/repos/upload", response_model=StageRepoResponse)
async def upload(file: UploadFile, project_id: str, user: CurrentUser) -> StageRepoResponse:
    await run_in_threadpool(require_project_member, project_id, user)
    # The upload itself is capped at the same size as the extracted archive
    # may reach; anything larger can't be a legitimate archive under the cap.
    fd, scratch_name = tempfile.mkstemp(prefix="codeaxis-upload-", suffix=".zip")
    scratch = Path(scratch_name)
    try:
        received = 0
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(65536):
                received += len(chunk)
                if received > ZIP_LIMITS.max_total_size_bytes:
                    raise HTTPException(
                        413, f"upload exceeds {ZIP_LIMITS.max_total_size_bytes} bytes"
                    )
                out.write(chunk)
        repo, checkout = await run_in_threadpool(stage_zip_repo, scratch, project_id)
        return await run_in_threadpool(_stage_response, repo, checkout)
    finally:
        scratch.unlink(missing_ok=True)


class ConfirmRequest(BaseModel):
    excluded_paths: list[str] = []


class ConfirmResponse(BaseModel):
    status: str
    job_id: str


@app.post("/repos/{repo_id}/confirm", response_model=ConfirmResponse)
def confirm(
    repo: RepoAccess, req: ConfirmRequest, background_tasks: BackgroundTasks
) -> ConfirmResponse:
    """Start the scan with the user's exclusions and return the job tracking
    it (GET /jobs/{job_id}). Both the checkout path and the commit_sha are
    read from the repo row written at stage time, never taken from the
    request — a client must not be able to point the scanner (or its
    post-scan cleanup) at an arbitrary folder, nor label the graph with a SHA
    the server never resolved."""
    assert repo.id is not None and repo.project_id is not None
    if repo.local_path is None or repo.staged_commit_sha is None:
        raise HTTPException(404, "repo not found")
    with api_connection() as conn:
        if list_pass_runs(conn, repo.id):
            raise HTTPException(409, "a scan has already been started for this repo")
    try:
        with worker_connection() as conn:
            # The unique (repo_id) where kind='scan' index makes this the
            # atomic once-only gate; the pass_run check above is a fast path.
            job = create_job(conn, repo.project_id, "scan", repo_id=repo.id)
    except JobAlreadyExistsError:
        raise HTTPException(409, "a scan has already been started for this repo") from None

    background_tasks.add_task(
        run_scan_job,
        job.id,
        repo.id,
        repo.staged_commit_sha,
        repo.local_path,
        frozenset(req.excluded_paths),
    )
    return ConfirmResponse(status="scan_started", job_id=job.id)


@app.delete("/repos/{repo_id}", status_code=204)
def delete_repo_route(repo: RepoAccess) -> Response:
    """Permanently delete a repo and everything analyzed from it. 409 while
    a scan is still queued or running for it — deleting out from under a
    scan would leave half-written rows, and the job runner would go on
    writing to a repo that no longer exists."""
    assert repo.id is not None
    with api_connection() as conn:
        active_job = get_active_job_for_repo(conn, repo.id)
    if active_job is not None:
        raise HTTPException(
            409, "a scan is in progress for this repo; wait for it to finish before deleting"
        )
    with worker_connection() as conn:
        delete_repo(conn, repo.id)
    if repo.local_path is not None:
        discard_checkout_if_scratch(repo.local_path)
    return Response(status_code=204)


# ------------------------------------------------------------- reads


@app.get("/repos/{repo_id}/status")
def repo_status(repo: RepoAccess) -> dict:
    assert repo.id is not None
    with api_connection() as conn:
        runs = list_pass_runs(conn, repo.id)
    return {"runs": [run.model_dump() for run in runs]}


# Edge kinds the Component Inspector's relationship/coverage views render.
# Deliberately excludes "contains" (the Explorer's tree, a different lens)
# and any edge kinds future passes may add (e.g. "belongs_to" clustering
# edges) — this list is what build-plan.md Part 4.B calls the relationship
# extraction pass, not "every edge kind that exists".
RELATIONSHIP_EDGE_KINDS = ["imports", "calls", "extends", "implements", "tests"]


def _relationship_entries(edges: list, nodes: list, other_id) -> list[dict]:
    """Shapes one side (incoming or outgoing) of a relationships payload:
    each edge's kind/resolution_status/attrs plus the connected node's
    identity, so the frontend can render and link to it without a second
    round-trip per edge. `other_id(edge)` picks which endpoint is "the other
    node" (dst_id for outgoing, src_id for incoming); it may be None for an
    unresolved/external edge, in which case `node` is None — the UI must
    show that as a gap, never invent a destination."""
    by_id = {n.id: n for n in nodes}
    entries = []
    for edge in edges:
        other = by_id.get(other_id(edge))
        entries.append(
            {
                "kind": edge.kind,
                "resolution_status": edge.resolution_status,
                "attrs": edge.attrs,
                "node": (
                    {"id": other.id, "name": other.name, "path": other.path, "kind": other.kind}
                    if other is not None
                    else None
                ),
            }
        )
    return entries


@app.get("/repos/{repo_id}/coverage")
def repo_coverage(repo: RepoAccess, commit_sha: str) -> dict:
    """File/metric-level coverage counters plus the edge-level
    resolved/ambiguous/unresolved/external breakdown — build-plan.md Part
    10's "coverage/unresolved view"."""
    assert repo.id is not None
    with api_connection() as conn:
        coverage = list_coverage(conn, repo.id, commit_sha)
        unresolved = unresolved_summary(conn, repo.id, commit_sha)
    return {
        "coverage": [row.model_dump() for row in coverage],
        "unresolved": unresolved,
    }


@app.get("/repos/{repo_id}/graph")
def repo_graph(
    repo: RepoAccess,
    commit_sha: str,
    root: int | None = None,
    depth: int = 1,
    kinds: str | None = None,
    status: str = "verified",
) -> dict:
    kind_list = kinds.split(",") if kinds else None
    assert repo.id is not None
    repo_id = repo.id

    with api_connection() as conn:
        if root is None:
            root_node = get_root_node(conn, repo_id, commit_sha)
            if root_node is None or root_node.id is None:
                raise HTTPException(404, "repo has no graph at this commit yet")
            root_id = root_node.id
        else:
            root_id = root

        nodes, edges, truncated = neighborhood(
            conn, repo_id, commit_sha, root_id, depth=depth, kinds=kind_list, status=status
        )

    return {
        "commit_sha": commit_sha,
        "nodes": [node.model_dump() for node in nodes],
        "edges": [edge.model_dump() for edge in edges],
        "truncated": {"reason": "depth_limit", "depth_cap": MAX_GRAPH_DEPTH} if truncated else None,
    }


@app.get("/repos/{repo_id}/symbols/{sid}")
def symbol_mechanics(repo: RepoAccess, sid: int, commit_sha: str) -> dict:
    """Mechanics-only: raw facts and provenance for one node, no narration.
    docs/03-architecture.md. Narrated lenses arrive in a later build step.

    Additive `relationships.outgoing`/`.incoming`: the depth-1 imports/
    calls/extends/implements/tests edges touching this node in either
    direction, each carrying its own resolution_status so ambiguous and
    unresolved edges render distinguishably rather than disappearing."""
    assert repo.id is not None
    with api_connection() as conn:
        node = get_node(conn, repo.id, commit_sha, sid)
        if node is None:
            raise HTTPException(404, "symbol not found at this commit")
        out_nodes, out_edges, _ = neighborhood(
            conn, repo.id, commit_sha, sid, depth=1, kinds=RELATIONSHIP_EDGE_KINDS
        )
        in_nodes, in_edges, _ = neighborhood(
            conn,
            repo.id,
            commit_sha,
            sid,
            depth=1,
            kinds=RELATIONSHIP_EDGE_KINDS,
            direction="reverse",
        )
    payload = node.model_dump()
    payload["relationships"] = {
        "outgoing": _relationship_entries(out_edges, out_nodes, lambda e: e.dst_id),
        "incoming": _relationship_entries(in_edges, in_nodes, lambda e: e.src_id),
    }
    return payload


# ------------------------------------------------------------- explorer + source


@app.get("/repos/{repo_id}/tree-root")
def tree_root(repo: RepoAccess, commit_sha: str) -> dict:
    """The checkout-root `directory` node — the Explorer's starting point."""
    assert repo.id is not None
    with api_connection() as conn:
        root = get_root_node(conn, repo.id, commit_sha)
    if root is None:
        raise HTTPException(404, "repo has no graph at this commit yet")
    return root.model_dump()


@app.get("/repos/{repo_id}/nodes/{node_id}/children")
def node_children(repo: RepoAccess, node_id: int, commit_sha: str) -> dict:
    """One level of the tree: nodes reached by a `contains` edge. The Explorer
    calls this lazily as directories and files are expanded."""
    assert repo.id is not None
    with api_connection() as conn:
        children = list_children(conn, repo.id, commit_sha, node_id)
    return {"children": [child.model_dump() for child in children]}


@app.get("/repos/{repo_id}/files/{path:path}")
def file_content(repo: RepoAccess, path: str, commit_sha: str) -> dict:
    """Pinned source text for one file at the analyzed commit."""
    assert repo.id is not None
    with api_connection() as conn:
        blob = get_blob(conn, repo.id, commit_sha, path)
    if blob is None:
        raise HTTPException(
            404,
            "source not available for this file at this commit: it was too large, "
            "unreadable or binary when scanned, or has not been scanned yet",
        )
    return {"path": blob.path, "content": blob.content, "content_hash": blob.content_hash}
