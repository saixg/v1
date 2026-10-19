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
from typing import Annotated

import psycopg
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
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
from app.analysis.semantic.chat import STREAMING_GAP, narrate
from app.analysis.semantic.retrieve import (
    DEFAULT_LIMIT,
    DEFAULT_TOKEN_BUDGET,
    ModelServiceEmbedder,
    RetrievalQuery,
    RetrievalResult,
    retrieve,
)
from app.auth import (
    CurrentUser,
    RepoAccess,
    require_project_member,
    require_uuid,
)
from app.blueprint_api import router as blueprint_router
from app.context_api import router as context_router
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
    # Component Inspector explanations (build-plan.md Part 7).
    get_summary,
    get_summary_for_node,
    list_children,
    list_coverage,
    list_pass_runs,
    list_projects_for_user,
    neighborhood,
    unresolved_summary,
    worker_connection,
)
from app.knowledge.reads import MAX_GRAPH_DEPTH
from app.knowledge.retrieval import (
    create_chat_thread,
    get_chat_thread,
    insert_chat_message,
    list_chat_messages,
)
from app.llm import (
    CallContext,
    ModelService,
    ModelServiceError,
    StructuredOutputError,
    anthropic_model_service,
)
from app.logging import RequestLoggingMiddleware, configure_logging
from app.mcp_server import router as mcp_router
from app.models import ChatMessage, ChatThread, IngestSource, Job, ProjectWithRole, Repo
from app.repository.ingest import ZIP_LIMITS, IngestError, MaterializedCheckout

configure_logging()

app = FastAPI(title="CodeAxis API")

app.add_middleware(RequestLoggingMiddleware)

# Agent Context Engine (tasks, ledger, packages, tokens) and its read-only MCP
# endpoint. Each module authorizes in app code; see their docstrings.
app.include_router(context_router)
app.include_router(mcp_router)

# Virtual Development Environment (blueprints). Same app-layer authorization
# contract; see app/blueprint_api.py's docstring.
app.include_router(blueprint_router)

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


# ------------------------------------------------- search + grounded chat
#
# build-plan.md Part 4.E / Part 8. Paths follow this app's existing `/repos/
# {repo_id}/...` convention rather than Part 8's literal `/repositories/...`:
# every route already shipped uses `/repos`, and one endpoint spelled
# differently would be a worse contradiction than the shorthand.
#
# Both routes take the `RepoAccess` dependency (401 without a valid Clerk
# token, 404 unknown repo, 403 non-member) and a required `commit_sha`, the
# same way every other read route in this file does.


def get_model_service() -> ModelService:
    """The ModelService the chat route narrates with. A FastAPI dependency so
    tests override it with a scripted provider — no API key is used or needed
    anywhere in the test suite."""
    return anthropic_model_service()


def _query_embedder() -> ModelServiceEmbedder:
    """The vector stage's embedder. Built from the same ModelService; when
    `embed` is unwired or unkeyed, the stage degrades to skipped and FTS +
    exact-symbol + graph expansion answer on their own."""
    return ModelServiceEmbedder(anthropic_model_service())


def _retrieval_query(
    q: str,
    limit: int,
    token_budget: int,
    hops: int,
    vector: bool,
    path_prefix: list[str] | None,
    kind: list[str] | None,
) -> RetrievalQuery:
    return RetrievalQuery(
        text=q,
        path_prefixes=tuple(path_prefix or ()),
        kinds=tuple(kind or ()),
        limit=limit,
        token_budget=token_budget,
        hops=hops,
        use_vector=vector,
    )


def _semantic_index_required() -> HTTPException:
    return HTTPException(
        503,
        "the semantic index is not available for this deployment: the chunks/chat "
        "tables from supabase/migrations/20261019000000_semantic.sql are not applied",
    )


def _budget_payload(result: RetrievalResult) -> dict:
    return {
        "token_budget": result.token_budget,
        "tokens_used": result.tokens_used,
        "dropped_for_budget": result.dropped_for_budget,
        "fused_candidates": result.fused_candidates,
    }


def _result_payload(result: RetrievalResult, *, include_content: bool) -> list[dict]:
    items = []
    for item in result.items:
        chunk = item.chunk
        row: dict = {
            "chunk_id": chunk.id,
            "node_id": chunk.node_id,
            "path": chunk.path,
            "symbol": chunk.symbol,
            "kind": chunk.kind,
            "line_start": chunk.line_start,
            "line_end": chunk.line_end,
            "token_count": chunk.token_count,
            "score": item.score,
            "stages": item.stages,
        }
        if include_content:
            row["content"] = chunk.content
        items.append(row)
    return items


@app.get("/repos/{repo_id}/search")
def repo_search(
    repo: RepoAccess,
    commit_sha: str,
    q: str,
    limit: int = DEFAULT_LIMIT,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    hops: int = 1,
    vector: bool = True,
    path_prefix: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
) -> dict:
    """Hybrid retrieval over the chunk index (build-plan.md Part 4.E).

    Returns budget-fitted spans with file+line citations, plus one entry per
    pipeline stage saying whether it ran or was skipped and why — a skipped
    vector stage (no embeddings yet, no API key) is a reported fact, not a
    silent absence of results."""
    assert repo.id is not None
    query = _retrieval_query(q, limit, token_budget, hops, vector, path_prefix, kind)
    try:
        with api_connection() as conn:
            result = retrieve(conn, repo.id, commit_sha, query, embedder=_query_embedder())
    except psycopg.errors.UndefinedTable:
        raise _semantic_index_required() from None
    return {
        "commit_sha": commit_sha,
        "query": q,
        "results": _result_payload(result, include_content=True),
        "citations": [c.model_dump(exclude_none=True) for c in result.citations()],
        "stages": [s.as_dict() for s in result.stages],
        "budget": _budget_payload(result),
    }


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # Continue an existing thread, or omit to start one.
    thread_id: str | None = None
    token_budget: int = DEFAULT_TOKEN_BUDGET
    limit: int = DEFAULT_LIMIT
    hops: int = 1
    vector: bool = True
    path_prefix: list[str] = []
    kind: list[str] = []


def _chat_thread(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    thread_id: str | None,
    user_id: str,
) -> ChatThread | None:
    """Resolve a caller-supplied thread id, or None to start a new thread.

    A thread is scoped to one repo, one commit and one user: continuing
    someone else's thread, or a thread from another snapshot, is a 404 rather
    than a 403 — the caller has no business knowing it exists."""
    if thread_id is None:
        return None
    require_uuid(thread_id, "thread")
    thread = get_chat_thread(conn, thread_id)
    if (
        thread is None
        or thread.repo_id != repo_id
        or thread.user_id != user_id
        or thread.commit_sha != commit_sha
    ):
        raise HTTPException(404, "thread not found")
    return thread


@app.post("/repos/{repo_id}/chat")
def repo_chat(
    repo: RepoAccess,
    req: ChatRequest,
    commit_sha: str,
    user: CurrentUser,
    service: Annotated[ModelService, Depends(get_model_service)],
) -> dict:
    """Grounded chat with validated citations (build-plan.md Part 8).

    Retrieval is deterministic; the model only narrates the spans it is given,
    framed as untrusted data. Every path, symbol and line range it cites is
    checked against this snapshot **before** the answer is persisted or
    returned: a citation that does not resolve returns 422 with the reason per
    citation and stores no assistant message. Non-streaming — see
    `app.analysis.semantic.chat.STREAMING_GAP`."""
    assert repo.id is not None and repo.project_id is not None
    repo_id = repo.id

    query = _retrieval_query(
        req.message,
        req.limit,
        req.token_budget,
        req.hops,
        req.vector,
        req.path_prefix,
        req.kind,
    )
    try:
        with api_connection() as conn:
            thread = _chat_thread(conn, repo_id, commit_sha, req.thread_id, user.id)
            result = retrieve(conn, repo_id, commit_sha, query, embedder=_query_embedder())
    except psycopg.errors.UndefinedTable:
        raise _semantic_index_required() from None

    with worker_connection() as conn:
        if thread is None:
            thread = create_chat_thread(conn, repo_id, commit_sha, user.id, req.message[:200])
        insert_chat_message(
            conn, ChatMessage(thread_id=thread.id, role="user", content=req.message)
        )

    base = {
        "thread_id": thread.id,
        "commit_sha": commit_sha,
        "stages": [s.as_dict() for s in result.stages],
        "budget": _budget_payload(result),
        "evidence": _result_payload(result, include_content=False),
        "streaming": STREAMING_GAP,
    }

    if not result.items:
        # No evidence means no model call: there is nothing to ground an
        # answer in, and generating anyway is exactly the failure mode the
        # citation validator exists to catch. Said plainly instead.
        return {
            **base,
            "status": "no_evidence",
            "message_id": None,
            "answer": None,
            "claims": [],
            "citations": [],
            "unsupported": (
                "No indexed code matched this question at this commit, so there is "
                "nothing to answer from. The repository may not be chunked yet."
            ),
        }

    try:
        with api_connection() as conn:
            grounded = narrate(
                conn,
                service,
                repo_id=repo_id,
                commit_sha=commit_sha,
                question=req.message,
                retrieval=result,
                context=CallContext(project_id=repo.project_id, repo_id=repo_id),
            )
    except StructuredOutputError as exc:
        raise HTTPException(
            502,
            {"error": "model_output_invalid", "detail": str(exc), "thread_id": thread.id},
        ) from None
    except ModelServiceError as exc:
        raise HTTPException(
            502,
            {"error": "model_call_failed", "detail": str(exc), "thread_id": thread.id},
        ) from None

    if not grounded.report.ok:
        # Phase 3's exit criterion, enforced: the answer is discarded whole.
        # Nothing is persisted, nothing is rephrased, and every rejected
        # citation is returned with the reason it failed.
        raise HTTPException(
            422,
            {
                "error": "citation_check_failed",
                "thread_id": thread.id,
                "checked": grounded.report.checked,
                "unsupported_claims": grounded.report.unsupported_claim_count,
                "rejected": [r.as_dict() for r in grounded.report.rejected],
                "uncited_claim_indices": list(grounded.report.uncited_claim_indices),
                "detail": (
                    "the model cited code that does not exist in this snapshot; "
                    "the answer was discarded rather than reworded"
                ),
            },
        )

    answer_text = grounded.answer_text()
    with worker_connection() as conn:
        stored = insert_chat_message(
            conn,
            ChatMessage(
                thread_id=thread.id,
                role="assistant",
                content=answer_text,
                citations=grounded.citations,
                model="synth",
            ),
        )

    return {
        **base,
        "status": "ok",
        "message_id": stored.id,
        "answer": answer_text,
        "claims": [
            {
                "text": claim.text,
                "citations": [c.model_dump(exclude_none=True) for c in claim.citations],
            }
            for claim in grounded.answer.claims
        ],
        "citations": grounded.citation_payload(),
        "unsupported": grounded.answer.unsupported,
        "citation_check": {"checked": grounded.report.checked, "rejected": 0},
    }


@app.get("/repos/{repo_id}/chat/{thread_id}")
def repo_chat_thread(repo: RepoAccess, thread_id: str, commit_sha: str, user: CurrentUser) -> dict:
    """One thread's persisted messages, oldest first, with their stored
    citations. Part 4.F's "context rebuilt after session end" starts here:
    the thread is the record, not the client's memory."""
    assert repo.id is not None
    try:
        with api_connection() as conn:
            thread = _chat_thread(conn, repo.id, commit_sha, thread_id, user.id)
            assert thread is not None
            messages = list_chat_messages(conn, thread.id)
    except psycopg.errors.UndefinedTable:
        raise _semantic_index_required() from None
    return {
        "thread_id": thread.id,
        "commit_sha": thread.commit_sha,
        "title": thread.title,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "citations": [c.model_dump(exclude_none=True) for c in m.citations],
                "model": m.model,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


# ----------------------------------------------- Inspector explanation
#
# Part 7's "Purpose (grounded explanation, with citations)" field. These
# routes **serve stored summaries only**: generation belongs to the analysis
# pass (Pass 2b), so a snapshot that has not been summarized returns 404 with
# the reason rather than quietly producing prose on request. A summary is
# always `origin='inferred'` -- model prose about facts the analyzers
# established -- and ships with the evidence it was written from so every
# statement stays clickable back to source (Part 5).


def _explanation_payload(summary) -> dict:
    evidence = summary.evidence
    return {
        "level": summary.level,
        "scope_key": summary.scope_key,
        "node_id": summary.node_id,
        "summary": summary.summary,
        # Never presented as parsed fact: the UI keys its "AI observation"
        # treatment off this field (Part 4.C).
        "origin": summary.origin,
        "model": summary.model,
        "analyzer": summary.analyzer,
        "analyzer_version": summary.analyzer_version,
        "created_at": summary.created_at.isoformat() if summary.created_at else None,
        # The file+line spans behind the prose, in the same shape every other
        # cited surface uses.
        "citations": [c.model_dump(exclude_none=True) for c in evidence.spans],
        "evidence": {
            "input": evidence.input,
            "chunk_ids": evidence.chunk_ids,
            "node_ids": evidence.node_ids,
            "children": evidence.children,
            "truncated": evidence.truncated,
        },
    }


_NO_EXPLANATION = (
    "no stored explanation for this scope at this commit: the summary pass has "
    "not run, was skipped (no model provider configured), or did not reach this "
    "scope within its budget. Explanations are never generated on request."
)


@app.get("/repos/{repo_id}/nodes/{node_id}/explanation")
def node_explanation(repo: RepoAccess, node_id: int, commit_sha: str) -> dict:
    """The stored grounded explanation for one node -- a symbol, a file, or a
    cluster (candidate component). 404 when none was written."""
    assert repo.id is not None
    try:
        with api_connection() as conn:
            summary = get_summary_for_node(conn, repo.id, commit_sha, node_id)
    except psycopg.errors.UndefinedTable:
        raise _semantic_index_required() from None
    if summary is None:
        raise HTTPException(404, _NO_EXPLANATION)
    return _explanation_payload(summary)


@app.get("/repos/{repo_id}/explanation")
def scope_explanation(
    repo: RepoAccess, commit_sha: str, level: str, scope_key: str = ""
) -> dict:
    """The stored explanation for a scope addressed by level + key: a file
    path, a directory path, a component key, or `''` for the whole repo."""
    assert repo.id is not None
    if level not in ("symbol", "file", "directory", "component", "repo"):
        raise HTTPException(422, "level must be one of: symbol, file, directory, component, repo")
    try:
        with api_connection() as conn:
            summary = get_summary(conn, repo.id, commit_sha, level, scope_key)
    except psycopg.errors.UndefinedTable:
        raise _semantic_index_required() from None
    if summary is None:
        raise HTTPException(404, _NO_EXPLANATION)
    return _explanation_payload(summary)
