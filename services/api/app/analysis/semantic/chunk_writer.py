"""Pass 2a driver: chunk every scanned file, persist the chunks, and embed
the ones whose vectors are not already paid for.

(Module name `chunk_writer` rather than `writer` because two sub-areas share
this package: `writer.py` is the Pass 2b summaries/conventions driver.)

Runs inside the scan job's connection (see `jobs.py::_run_scan_pass`), after
the architecture pass, because it reads only rows those passes already wrote
plus the blobs the scan stored -- it needs no checkout of its own. Unlike the
architecture pass it *does* take its own `pass_run` row: it is the first pass
that can make an external API call, so its analyzer identity, timing and
failure state have to be separable from Pass 1's.

Two failure policies, deliberately different:

- **Chunking** is deterministic local work. If it fails, that is a bug, and
  it fails the pass loudly.
- **Embedding** is an external, optional stage (Part 4.D: "embeddings are a
  pluggable stage"). With no `VOYAGE_API_KEY`, or when the API errors, the
  chunks are still stored and still keyword-searchable, the gap is recorded
  in `codeaxis.coverage` as a blocker on the `chunks_embedded` metric, and
  the pass completes. The gap is visible, not hidden -- but it does not throw
  away a good graph over a missing vector.
"""

from __future__ import annotations

import logging

import psycopg

from app.knowledge import (
    count_chunks,
    find_reusable_embeddings,
    finish_pass_run,
    get_blob,
    insert_chunks,
    list_nodes,
    set_chunk_embeddings,
    start_pass_run,
    upsert_coverage,
)
from app.llm import (
    CallContext,
    ModelService,
    ModelServiceError,
    embedding_enabled,
    embedding_model_service,
)
from app.llm.routing import DEFAULT_ROUTES
from app.models import Chunk, Coverage, PassRun, PassRunStatus

from .chunk import ANALYZER, ANALYZER_VERSION, FileSource, SymbolSpan, plan_chunks

logger = logging.getLogger(__name__)


def collect_sources(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> list[FileSource]:
    """One `FileSource` per scanned file that has stored text.

    Reuses the symbol nodes the scan pass already parsed rather than
    re-parsing the checkout (which is gone by now anyway), and reads the text
    from `codeaxis.blobs`. A file with no blob -- binary, oversized,
    unreadable -- yields no chunks; that absence is what the `files_chunked`
    coverage row counts.
    """
    files = list_nodes(conn, repo_id, commit_sha, kind="file")
    symbols_by_path: dict[str, list[SymbolSpan]] = {}
    for node in list_nodes(conn, repo_id, commit_sha, kind="symbol"):
        if node.path is None or node.line_start is None or node.line_end is None:
            continue
        symbols_by_path.setdefault(node.path, []).append(
            SymbolSpan(
                node_id=node.id,
                name=node.name,
                line_start=node.line_start,
                line_end=node.line_end,
            )
        )

    sources: list[FileSource] = []
    for node in files:
        if node.path is None:
            continue
        blob = get_blob(conn, repo_id, commit_sha, node.path)
        if blob is None:
            continue
        sources.append(
            FileSource(
                path=node.path,
                content=blob.content,
                symbols=tuple(symbols_by_path.get(node.path, ())),
            )
        )
    return sources


def _attach_reused_embeddings(conn: psycopg.Connection, repo_id: str, chunks: list[Chunk]) -> int:
    """Copy vectors this repo already holds for identical content onto the
    planned chunks (build-plan.md Part 4.D's incremental re-index key).
    Returns how many were reused."""
    reusable = find_reusable_embeddings(
        conn, repo_id, sorted({chunk.content_hash for chunk in chunks})
    )
    if not reusable:
        return 0
    reused = 0
    for chunk in chunks:
        hit = reusable.get(chunk.content_hash)
        if hit is None:
            continue
        vector, model = hit
        chunk.embedding, chunk.embedding_model = vector, model
        reused += 1
    return reused


def _embed_missing(
    conn: psycopg.Connection,
    repo_id: str,
    chunks: list[Chunk],
    chunk_ids: list[int],
    service: ModelService,
) -> int:
    """Embed the chunks that have no vector yet, batched by ModelService into
    as few provider requests as the route's caps allow. Returns how many
    vectors were written."""
    pending = [
        (chunk_id, chunk)
        for chunk_id, chunk in zip(chunk_ids, chunks, strict=True)
        if chunk.embedding is None
    ]
    if not pending:
        return 0
    vectors = service.embed(
        [chunk.content for _id, chunk in pending],
        "embed",
        input_type="document",
        context=CallContext(repo_id=repo_id),
    )
    if len(vectors) != len(pending):
        # ModelService already guarantees this; checking again here means a
        # future provider change cannot quietly mis-align vectors to chunks.
        raise ModelServiceError(f"embed returned {len(vectors)} vectors for {len(pending)} chunks")
    return set_chunk_embeddings(
        conn,
        [(chunk_id, vector) for (chunk_id, _chunk), vector in zip(pending, vectors, strict=True)],
        embedding_model=DEFAULT_ROUTES["embed"].model,
    )


def write_chunk_pass(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    service: ModelService | None = None,
) -> None:
    """Chunk + embed one snapshot. `service` is injectable so tests never
    touch the network; in production it is the Voyage-backed ModelService."""
    run = start_pass_run(
        conn,
        PassRun(
            repo_id=repo_id,
            commit_sha=commit_sha,
            analyzer=ANALYZER,
            analyzer_version=ANALYZER_VERSION,
            status=PassRunStatus.RUNNING,
        ),
    )
    assert run.id is not None
    try:
        sources = collect_sources(conn, repo_id, commit_sha)
        chunks = plan_chunks(sources, repo_id, commit_sha)
        reused = _attach_reused_embeddings(conn, repo_id, chunks) if chunks else 0
        chunk_ids = insert_chunks(conn, chunks) if chunks else []
    except Exception as exc:
        finish_pass_run(conn, run.id, PassRunStatus.FAILED, error=str(exc))
        raise

    # --- embedding: optional, and its absence is recorded, not raised -------
    blockers: dict[str, int] = {}
    if chunks:
        if service is None and not embedding_enabled():
            blockers["embedding_not_configured"] = sum(
                1 for chunk in chunks if chunk.embedding is None
            )
        else:
            try:
                _embed_missing(
                    conn, repo_id, chunks, chunk_ids, service or embedding_model_service()
                )
            except ModelServiceError:
                # Includes EmbeddingNotConfiguredError (a provider that cannot
                # authenticate) and every transport/shape failure. Logged with
                # its traceback and counted; never silently absorbed.
                logger.exception("embedding failed for repo %s at %s", repo_id, commit_sha)
                blockers["embedding_failed"] = sum(
                    1 for chunk in chunks if chunk.embedding is None
                )

    total_chunks, embedded_chunks = count_chunks(conn, repo_id, commit_sha)
    upsert_coverage(
        conn,
        [
            Coverage(
                repo_id=repo_id,
                commit_sha=commit_sha,
                area="/",
                metric="files_chunked",
                resolved=len({chunk.path for chunk in chunks}),
                total=len(sources),
                blockers={},
            ),
            Coverage(
                repo_id=repo_id,
                commit_sha=commit_sha,
                area="/",
                metric="chunks_embedded",
                resolved=embedded_chunks,
                total=total_chunks,
                blockers=blockers,
            ),
            Coverage(
                repo_id=repo_id,
                commit_sha=commit_sha,
                area="/",
                metric="chunk_embeddings_reused",
                resolved=reused,
                total=total_chunks,
                blockers={},
            ),
        ],
    )
    finish_pass_run(conn, run.id, PassRunStatus.SUCCEEDED)
