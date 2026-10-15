"""HTTP surface for Step 1: stage a repo, preview what a scan would include,
confirm to start the scan, poll its status, read the graph. Reads go through
the read-only `codeaxis_api` role (api_connection); the write paths —
registering a repo and running its scan — delegate to codeaxis_worker, which
is the only thing that writes to the Repo Model.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from codeaxis_contracts import IngestSource, Repo
from codeaxis_graph import (
    api_connection,
    get_blob,
    get_node,
    get_repo,
    get_root_node,
    list_children,
    list_pass_runs,
    neighborhood,
)
from codeaxis_graph.reads import MAX_GRAPH_DEPTH
from codeaxis_worker import (
    IngestError,
    MaterializedCheckout,
    PreviewEntry,
    preview_repo,
    run_scan_pass,
    stage_repo,
    stage_zip_repo,
)
from codeaxis_worker.ingest import ZIP_LIMITS

app = FastAPI(title="CodeAxis API")

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
def stage(source: IngestSource) -> StageRepoResponse:
    repo, checkout = stage_repo(source)
    return _stage_response(repo, checkout)


@app.post("/repos/upload", response_model=StageRepoResponse)
async def upload(file: UploadFile) -> StageRepoResponse:
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
        repo, checkout = await run_in_threadpool(stage_zip_repo, scratch)
        return await run_in_threadpool(_stage_response, repo, checkout)
    finally:
        scratch.unlink(missing_ok=True)


class ConfirmRequest(BaseModel):
    excluded_paths: list[str] = []


class ConfirmResponse(BaseModel):
    status: str


@app.post("/repos/{repo_id}/confirm", response_model=ConfirmResponse)
def confirm(
    repo_id: str, req: ConfirmRequest, background_tasks: BackgroundTasks
) -> ConfirmResponse:
    """Start the scan with the user's exclusions. Both the checkout path and
    the commit_sha are read from the repo row written at stage time, never
    taken from the request — a client must not be able to point the scanner
    (or its post-scan cleanup) at an arbitrary folder, nor label the graph
    with a SHA the server never resolved."""
    _require_uuid(repo_id)
    with api_connection() as conn:
        repo = get_repo(conn, repo_id)
        if repo is None or repo.local_path is None or repo.staged_commit_sha is None:
            raise HTTPException(404, "repo not found")
        if list_pass_runs(conn, repo_id):
            raise HTTPException(409, "a scan has already been started for this repo")

    background_tasks.add_task(
        run_scan_pass,
        repo_id,
        repo.staged_commit_sha,
        repo.local_path,
        frozenset(req.excluded_paths),
    )
    return ConfirmResponse(status="scan_started")


def _require_uuid(repo_id: str) -> None:
    try:
        uuid.UUID(repo_id)
    except ValueError:
        raise HTTPException(404, "repo not found") from None


# ------------------------------------------------------------- reads


@app.get("/repos/{repo_id}/status")
def repo_status(repo_id: str) -> dict:
    with api_connection() as conn:
        runs = list_pass_runs(conn, repo_id)
    return {"runs": [run.model_dump() for run in runs]}


@app.get("/repos/{repo_id}/graph")
def repo_graph(
    repo_id: str,
    commit_sha: str,
    root: int | None = None,
    depth: int = 1,
    kinds: str | None = None,
    status: str = "verified",
) -> dict:
    kind_list = kinds.split(",") if kinds else None

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
def symbol_mechanics(repo_id: str, sid: int, commit_sha: str) -> dict:
    """Mechanics-only: raw facts and provenance for one node, no narration.
    docs/03-architecture.md. Narrated lenses arrive in a later build step."""
    with api_connection() as conn:
        node = get_node(conn, repo_id, commit_sha, sid)
    if node is None:
        raise HTTPException(404, "symbol not found at this commit")
    return node.model_dump()


# ------------------------------------------------------------- explorer + source


@app.get("/repos/{repo_id}/tree-root")
def tree_root(repo_id: str, commit_sha: str) -> dict:
    """The checkout-root `directory` node — the Explorer's starting point."""
    _require_uuid(repo_id)
    with api_connection() as conn:
        root = get_root_node(conn, repo_id, commit_sha)
    if root is None:
        raise HTTPException(404, "repo has no graph at this commit yet")
    return root.model_dump()


@app.get("/repos/{repo_id}/nodes/{node_id}/children")
def node_children(repo_id: str, node_id: int, commit_sha: str) -> dict:
    """One level of the tree: nodes reached by a `contains` edge. The Explorer
    calls this lazily as directories and files are expanded."""
    _require_uuid(repo_id)
    with api_connection() as conn:
        children = list_children(conn, repo_id, commit_sha, node_id)
    return {"children": [child.model_dump() for child in children]}


@app.get("/repos/{repo_id}/files/{path:path}")
def file_content(repo_id: str, path: str, commit_sha: str) -> dict:
    """Pinned source text for one file at the analyzed commit."""
    _require_uuid(repo_id)
    with api_connection() as conn:
        blob = get_blob(conn, repo_id, commit_sha, path)
    if blob is None:
        raise HTTPException(
            404,
            "source not available for this file at this commit: it was too large, "
            "unreadable or binary when scanned, or has not been scanned yet",
        )
    return {"path": blob.path, "content": blob.content, "content_hash": blob.content_hash}
