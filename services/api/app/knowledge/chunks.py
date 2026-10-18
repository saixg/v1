"""codeaxis.chunks: AST-aware retrieval units, one row per chunk per snapshot.

Writers are called by `app.analysis.semantic`; readers by the retrieval layer
and the FastAPI read routes. Two things about this table are load-bearing:

- **`tsv` is a generated column.** It is never named in an INSERT or UPDATE
  here. Postgres recomputes it from `symbol`, `path` and `content`, which is
  what makes keyword search impossible to leave stale.
- **`content_hash` is the incremental re-index key** (build-plan.md Part 4.D).
  It is the sha256 of the chunk's own content, so an unchanged chunk at a new
  commit can inherit the vector that was already paid for
  (`find_reusable_embeddings`) instead of being re-embedded.

`embedding` is an `extensions.vector(1024)` column. psycopg has no native
adapter for it without the pgvector package, so vectors cross this boundary
as the textual `[a,b,c]` literal pgvector itself accepts and emits -- see
`_vector_literal` / `_parse_vector`. That keeps the dependency list shorter
and the SQL explicit, at the cost of a round of text formatting.
"""

from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from app.models import Chunk

_CHUNK_COLUMNS = """
    id, repo_id, commit_sha, node_id, path, symbol, kind,
    line_start, line_end, content, content_hash, token_count,
    part_index, part_count, embedding::text as embedding,
    embedding_model, embedded_at, analyzer, analyzer_version
"""


def _vector_literal(vector: list[float]) -> str:
    """The pgvector input format: `[0.1,0.2,...]`."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _parse_vector(raw: object) -> list[float] | None:
    """Inverse of `_vector_literal`, tolerant of a NULL column."""
    if raw is None:
        return None
    if isinstance(raw, list):  # a future pgvector adapter would hand us this
        return [float(value) for value in raw]
    text = str(raw).strip()
    if not text or text in ("[]", "null"):
        return None
    return [float(part) for part in text.strip("[]").split(",")]


def _to_chunk(row: dict) -> Chunk:
    return Chunk.model_validate({**row, "embedding": _parse_vector(row.get("embedding"))})


# --- writers ------------------------------------------------------------------


def insert_chunks(conn: psycopg.Connection, chunks: list[Chunk]) -> list[int]:
    """Insert chunks, returning their assigned ids in the same order.

    Idempotent per snapshot: the unique key
    `(repo_id, commit_sha, path, line_start, line_end, part_index)` means a
    re-run of the same pass updates its own rows instead of duplicating them.
    """
    ids: list[int] = []
    with conn.cursor() as cur:
        for chunk in chunks:
            row = cur.execute(
                """
                insert into codeaxis.chunks (
                    repo_id, commit_sha, node_id, path, symbol, kind,
                    line_start, line_end, content, content_hash, token_count,
                    part_index, part_count, embedding, embedding_model, embedded_at,
                    analyzer, analyzer_version
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::extensions.vector, %s, %s, %s, %s)
                on conflict (repo_id, commit_sha, path, line_start, line_end, part_index)
                do update set
                    node_id = excluded.node_id,
                    symbol = excluded.symbol,
                    kind = excluded.kind,
                    content = excluded.content,
                    content_hash = excluded.content_hash,
                    token_count = excluded.token_count,
                    part_count = excluded.part_count,
                    embedding = excluded.embedding,
                    embedding_model = excluded.embedding_model,
                    embedded_at = excluded.embedded_at,
                    analyzer = excluded.analyzer,
                    analyzer_version = excluded.analyzer_version
                returning id
                """,
                (
                    chunk.repo_id,
                    chunk.commit_sha,
                    chunk.node_id,
                    chunk.path,
                    chunk.symbol,
                    chunk.kind,
                    chunk.line_start,
                    chunk.line_end,
                    chunk.content,
                    chunk.content_hash,
                    chunk.token_count,
                    chunk.part_index,
                    chunk.part_count,
                    None if chunk.embedding is None else _vector_literal(chunk.embedding),
                    chunk.embedding_model,
                    chunk.embedded_at,
                    chunk.analyzer,
                    chunk.analyzer_version,
                ),
            ).fetchone()
            assert row is not None
            ids.append(row[0])
    conn.commit()
    return ids


def set_chunk_embeddings(
    conn: psycopg.Connection,
    rows: list[tuple[int, list[float]]],
    *,
    embedding_model: str,
    embedded_at: datetime | None = None,
) -> int:
    """Attach vectors to already-inserted chunks, returning the row count.

    `embedded_at` defaults to the database's `now()`. The model name is stored
    with every vector so a later model change is detectable instead of
    producing a silently mixed index.
    """
    updated = 0
    with conn.cursor() as cur:
        for chunk_id, vector in rows:
            cur.execute(
                """
                update codeaxis.chunks
                set embedding = %s::extensions.vector,
                    embedding_model = %s,
                    embedded_at = coalesce(%s, now())
                where id = %s
                """,
                (_vector_literal(vector), embedding_model, embedded_at, chunk_id),
            )
            updated += cur.rowcount
    conn.commit()
    return updated


# --- readers ------------------------------------------------------------------


def find_reusable_embeddings(
    conn: psycopg.Connection, repo_id: str, content_hashes: list[str]
) -> dict[str, tuple[list[float], str]]:
    """The incremental re-index lookup (build-plan.md Part 4.D).

    For each requested content hash that this repo has *already* embedded at
    any commit, return `(vector, embedding_model)`. Scoped to `repo_id`, which
    is exactly what the `chunks_content_hash_idx` index covers: a vector is
    never shared across repositories, so one tenant's content can never pull a
    row written for another's. Deliberately not scoped to a commit -- reuse
    across commits is the entire point.
    """
    if not content_hashes:
        return {}
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            select distinct on (content_hash)
                   content_hash, embedding::text, embedding_model
            from codeaxis.chunks
            where repo_id = %s
              and content_hash = any(%s)
              and embedding is not null
              and embedding_model is not null
            order by content_hash, embedded_at desc nulls last
            """,
            (repo_id, content_hashes),
        ).fetchall()
    out: dict[str, tuple[list[float], str]] = {}
    for content_hash, raw_vector, model in rows:
        vector = _parse_vector(raw_vector)
        if vector is not None:
            out[content_hash] = (vector, model)
    return out


def list_chunks(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, path: str | None = None
) -> list[Chunk]:
    """Every chunk for one snapshot, optionally one file, in reading order."""
    with conn.cursor(row_factory=dict_row) as cur:
        if path is None:
            rows = cur.execute(
                f"""
                select {_CHUNK_COLUMNS} from codeaxis.chunks
                where repo_id = %s and commit_sha = %s
                order by path, line_start, part_index
                """,
                (repo_id, commit_sha),
            ).fetchall()
        else:
            rows = cur.execute(
                f"""
                select {_CHUNK_COLUMNS} from codeaxis.chunks
                where repo_id = %s and commit_sha = %s and path = %s
                order by line_start, part_index
                """,
                (repo_id, commit_sha, path),
            ).fetchall()
    return [_to_chunk(row) for row in rows]


def get_chunk(conn: psycopg.Connection, chunk_id: int) -> Chunk | None:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            f"select {_CHUNK_COLUMNS} from codeaxis.chunks where id = %s", (chunk_id,)
        ).fetchone()
    return None if row is None else _to_chunk(row)


def search_chunks_fts(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    query: str,
    limit: int = 20,
) -> list[tuple[Chunk, float]]:
    """Keyword search over one snapshot, highest `ts_rank` first.

    `websearch_to_tsquery` is used rather than `to_tsquery` because the query
    string comes from a user and must never be able to raise a syntax error or
    be read as operator syntax. Returns `(chunk, rank)` pairs; the rank is
    Postgres's, not a model's.
    """
    if not query.strip():
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            select {_CHUNK_COLUMNS},
                   ts_rank(tsv, websearch_to_tsquery('english', %s))::float8 as rank
            from codeaxis.chunks
            where repo_id = %s and commit_sha = %s
              and tsv @@ websearch_to_tsquery('english', %s)
            order by rank desc, path, line_start
            limit %s
            """,
            (query, repo_id, commit_sha, query, limit),
        ).fetchall()
    return [(_to_chunk(row), float(row["rank"])) for row in rows]


def count_chunks(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> tuple[int, int]:
    """`(total, embedded)` for one snapshot -- the numbers the Understanding
    Gap counters report for this pass."""
    with conn.cursor() as cur:
        row = cur.execute(
            """
            select count(*), count(embedding)
            from codeaxis.chunks where repo_id = %s and commit_sha = %s
            """,
            (repo_id, commit_sha),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])
