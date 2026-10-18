"""Read queries for hybrid retrieval and grounded chat, plus the two chat
tables' writes (build-plan.md Part 4.E, Part 8).

Same rules as `reads.py`: this is the only module that knows SQL, every
caller-supplied value is a bound `%s` parameter, and dynamic WHERE fragments
are assembled from a fixed internal whitelist only.

Read paths use the read-only `codeaxis_api` role (`api_connection`); the two
chat writes need `worker_connection`, the same split every other write in
this app already uses.

Nothing here ranks across stages or assembles a prompt — that is
`app.analysis.semantic.retrieve`'s job. Each function is one retrieval stage's
query, returning chunks in that stage's own order.
"""

from __future__ import annotations

from collections.abc import Sequence

import psycopg
from psycopg.rows import class_row

from app.models import ChatMessage, ChatThread, ScoredChunk

# Every column a retrieved chunk needs to be rendered and cited. Deliberately
# excludes `embedding` and `tsv`: a 1024-float vector per row is pure weight on
# a path that only ever needs text + span, and `tsv` is a generated column no
# application code reads. `Chunk.embedding` therefore comes back None on a
# retrieval read — absence of the vector here means "not selected", never
# "not embedded"; `app.knowledge.chunks.count_chunks()` answers that question.
_CHUNK_COLUMNS = """
    c.id, c.repo_id, c.commit_sha, c.node_id, c.path, c.symbol, c.kind,
    c.line_start, c.line_end, c.content, c.content_hash, c.token_count,
    c.part_index, c.part_count, c.embedding_model, c.embedded_at,
    c.analyzer, c.analyzer_version
"""

# Overlap with `app.knowledge.chunks`, stated rather than left to be
# discovered: that module owns the chunk table's writers plus whole-snapshot
# reads (`list_chunks`, `count_chunks`, `find_reusable_embeddings`) and an
# unfiltered `search_chunks_fts`. This module owns the *retrieval stages*,
# which differ in three ways that matter: path/metadata filters are pushed
# down into every query (Part 4.E's filter stage), the 1024-float `embedding`
# column is never selected (pure weight on a text+span path), and rows come
# back as `ScoredChunk` so fusion can rank them. `count_chunks` is reused
# as-is rather than reimplemented.

# Hard ceiling on how many chunks any single stage may return. A stage that
# wants more is a bug: the token budget would throw them away anyway, and
# Part 4.E's rule is "never send the whole repo".
MAX_STAGE_LIMIT = 200


def _capped(limit: int) -> int:
    return max(1, min(limit, MAX_STAGE_LIMIT))


# `path_prefixes` and `kinds` are the "path & metadata filters" stage of Part
# 4.E. They are applied inside every other stage's query rather than as a
# standalone pass, so a filtered search never ranks a chunk it would then
# discard. starts_with() is used instead of LIKE so a caller-supplied prefix
# containing % or _ is matched literally.
_PATH_FILTER = "exists (select 1 from unnest(%s::text[]) p where starts_with(c.path, p))"


def _filters(
    path_prefixes: Sequence[str] | None, kinds: Sequence[str] | None
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if path_prefixes:
        clauses.append(_PATH_FILTER)
        params.append(list(path_prefixes))
    if kinds:
        clauses.append("c.kind = any(%s::text[])")
        params.append(list(kinds))
    return clauses, params


def exact_symbol_chunks(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    symbols: Sequence[str],
    *,
    limit: int = 20,
    path_prefixes: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
) -> list[ScoredChunk]:
    """Chunks whose `symbol` is exactly one of `symbols` — the first stage of
    Part 4.E. Case-sensitive matches rank above case-insensitive ones, because
    an identifier that matches exactly is a stronger signal than one that only
    matches when folded."""
    if not symbols:
        return []
    names = list(symbols)
    folded = [s.lower() for s in names]
    clauses, params = _filters(path_prefixes, kinds)
    where = ("and " + " and ".join(clauses)) if clauses else ""
    query = f"""
        select {_CHUNK_COLUMNS},
               case when c.symbol = any(%s::text[]) then 1.0 else 0.5 end as score
        from codeaxis.chunks c
        where c.repo_id = %s and c.commit_sha = %s
          and (c.symbol = any(%s::text[]) or lower(c.symbol) = any(%s::text[]))
          {where}
        order by score desc, c.path, c.line_start
        limit %s
    """
    with conn.cursor(row_factory=class_row(ScoredChunk)) as cur:
        return cur.execute(
            query,  # pyright: ignore[reportArgumentType]
            [names, repo_id, commit_sha, names, folded, *params, _capped(limit)],
        ).fetchall()


def fts_chunks(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    text: str,
    *,
    limit: int = 20,
    path_prefixes: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    relaxed: bool = False,
) -> list[ScoredChunk]:
    """Keyword stage: the `chunks.tsv` GIN index, ranked by `ts_rank_cd`.

    `websearch_to_tsquery` is used rather than `to_tsquery` so an arbitrary
    natural-language question can never be a syntax error — an unparseable
    query degrades to no matches, not a 500.

    `relaxed=True` rewrites that query's `&` operators to `|`, so a chunk
    matching *any* term qualifies instead of all of them. This exists because
    the eval grader measured it: `websearch_to_tsquery` ANDs every term, and a
    question like "where is main defined?" asks for `main & defin`, which a
    file containing `def main():` does not satisfy — recall@5 was 0.33 across
    the three fixtures with the strict query alone. The rewrite is done on
    Postgres's own parsed `tsquery`, not on the user's string, so phrase
    (`<->`) and negation (`!`) operators survive intact and nothing
    caller-supplied is ever concatenated into SQL. Ranking still orders the
    looser match set, which is what keeps precision: `ts_rank_cd` puts chunks
    hitting more terms first."""
    if not text.strip():
        return []
    clauses, params = _filters(path_prefixes, kinds)
    where = ("and " + " and ".join(clauses)) if clauses else ""
    query = f"""
        with q as (
            select case
                     when %s then replace(
                       websearch_to_tsquery('english', %s)::text, '&', '|'
                     )::tsquery
                     else websearch_to_tsquery('english', %s)
                   end as tsq
        )
        select {_CHUNK_COLUMNS},
               ts_rank_cd(c.tsv, q.tsq)::float8 as score
        from codeaxis.chunks c, q
        where c.repo_id = %s and c.commit_sha = %s
          and c.tsv @@ q.tsq
          {where}
        order by score desc, c.path, c.line_start
        limit %s
    """
    with conn.cursor(row_factory=class_row(ScoredChunk)) as cur:
        return cur.execute(
            query,  # pyright: ignore[reportArgumentType]
            [relaxed, text, text, repo_id, commit_sha, *params, _capped(limit)],
        ).fetchall()


def vector_chunks(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    embedding: Sequence[float],
    *,
    limit: int = 20,
    path_prefixes: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
) -> list[ScoredChunk]:
    """Nearest neighbours by cosine distance over `chunks.embedding` (the HNSW
    index the migration creates). `embedding IS NULL` rows are excluded, so a
    partially-embedded snapshot returns what it has instead of failing."""
    if not embedding:
        return []
    # pgvector's text input format; psycopg has no adapter registered for the
    # extension type, so the literal is cast explicitly.
    literal = "[" + ",".join(repr(float(v)) for v in embedding) + "]"
    clauses, params = _filters(path_prefixes, kinds)
    where = ("and " + " and ".join(clauses)) if clauses else ""
    query = f"""
        select {_CHUNK_COLUMNS},
               (1 - (c.embedding <=> %s::extensions.vector))::float8 as score
        from codeaxis.chunks c
        where c.repo_id = %s and c.commit_sha = %s
          and c.embedding is not null
          {where}
        order by c.embedding <=> %s::extensions.vector
        limit %s
    """
    with conn.cursor(row_factory=class_row(ScoredChunk)) as cur:
        return cur.execute(
            query,  # pyright: ignore[reportArgumentType]
            [literal, repo_id, commit_sha, *params, literal, _capped(limit)],
        ).fetchall()


def chunks_for_graph_neighbors(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    node_ids: Sequence[int],
    paths: Sequence[str],
    *,
    limit: int = 40,
    path_prefixes: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
) -> list[ScoredChunk]:
    """Chunks belonging to graph-expansion neighbours, addressed both ways.

    A chunk is tied to the graph by `node_id` when the chunker had a symbol
    node for it, but the relationship edges this app extracts attach to *file*
    nodes (see `app/analysis/scan/relationships.py`), so a neighbour is often
    only reachable by its path. Matching on either is what makes graph
    expansion actually return code rather than nothing."""
    if not node_ids and not paths:
        return []
    clauses, params = _filters(path_prefixes, kinds)
    where = ("and " + " and ".join(clauses)) if clauses else ""
    query = f"""
        select {_CHUNK_COLUMNS},
               case when c.node_id = any(%s::bigint[]) then 1.0 else 0.5 end as score
        from codeaxis.chunks c
        where c.repo_id = %s and c.commit_sha = %s
          and (c.node_id = any(%s::bigint[]) or c.path = any(%s::text[]))
          {where}
        order by score desc, c.path, c.line_start
        limit %s
    """
    ids = list(node_ids)
    with conn.cursor(row_factory=class_row(ScoredChunk)) as cur:
        return cur.execute(
            query,  # pyright: ignore[reportArgumentType]
            [ids, repo_id, commit_sha, ids, list(paths), *params, _capped(limit)],
        ).fetchall()


# ------------------------------------------------------- citation checking
#
# These two queries are what makes a citation checkable. They read the
# deterministic analyzer output (codeaxis.blobs, codeaxis.nodes) rather than
# codeaxis.chunks: a model must be held to what is actually in the snapshot,
# not to what happened to be retrieved for this one question.


def file_line_counts(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, paths: Sequence[str]
) -> dict[str, int]:
    """`{path: line count}` for the paths that exist in this snapshot.

    A path absent from the result does not exist at this commit — which is
    exactly the citation failure the validator must catch. Line counts come
    from the stored blob text, the same bytes the file viewer serves, so a
    cited line range is checked against the real file length."""
    if not paths:
        return {}
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            select path,
                   case
                     when content = '' then 0
                     else length(content) - length(replace(content, E'\\n', '')) + 1
                   end as line_count
            from codeaxis.blobs
            where repo_id = %s and commit_sha = %s and path = any(%s::text[])
            """,
            (repo_id, commit_sha, list(paths)),
        ).fetchall()
    return {path: count for path, count in rows}


def symbols_by_path(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, paths: Sequence[str]
) -> dict[str, set[str]]:
    """`{path: {symbol names declared in it}}` from `codeaxis.nodes`.

    Includes the file node's own name so a citation naming the file itself as
    its symbol still resolves."""
    if not paths:
        return {}
    out: dict[str, set[str]] = {p: set() for p in paths}
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            select path, name from codeaxis.nodes
            where repo_id = %s and commit_sha = %s and path = any(%s::text[])
            """,
            (repo_id, commit_sha, list(paths)),
        ).fetchall()
    for path, name in rows:
        out.setdefault(path, set()).add(name)
    return out


# ------------------------------------------------------------------- chat


_THREAD_COLUMNS = "id, repo_id, commit_sha, user_id, title, created_at"
_MESSAGE_COLUMNS = "id, thread_id, role, content, citations, model, llm_call_id, created_at"


def create_chat_thread(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, user_id: str, title: str | None
) -> ChatThread:
    with conn.cursor(row_factory=class_row(ChatThread)) as cur:
        row = cur.execute(
            f"""
            insert into codeaxis.chat_threads (repo_id, commit_sha, user_id, title)
            values (%s, %s, %s, %s)
            returning {_THREAD_COLUMNS}
            """,
            (repo_id, commit_sha, user_id, title),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


def get_chat_thread(conn: psycopg.Connection, thread_id: str) -> ChatThread | None:
    with conn.cursor(row_factory=class_row(ChatThread)) as cur:
        return cur.execute(
            f"select {_THREAD_COLUMNS} from codeaxis.chat_threads where id = %s",
            (thread_id,),
        ).fetchone()


def insert_chat_message(conn: psycopg.Connection, message: ChatMessage) -> ChatMessage:
    """Append one message. Assistant messages reach here only after their
    citations have been validated against the snapshot — the migration's own
    comment on `chat_messages.citations` states that contract, and
    `app.analysis.semantic.chat` is where it is enforced."""
    with conn.cursor(row_factory=class_row(ChatMessage)) as cur:
        row = cur.execute(
            f"""
            insert into codeaxis.chat_messages
              (thread_id, role, content, citations, model, llm_call_id)
            values (%s, %s, %s, %s::jsonb, %s, %s)
            returning {_MESSAGE_COLUMNS}
            """,
            (
                message.thread_id,
                message.role,
                message.content,
                message.citations_json(),
                message.model,
                message.llm_call_id,
            ),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


def list_chat_messages(
    conn: psycopg.Connection, thread_id: str, *, limit: int = 100
) -> list[ChatMessage]:
    with conn.cursor(row_factory=class_row(ChatMessage)) as cur:
        return cur.execute(
            f"""
            select {_MESSAGE_COLUMNS} from codeaxis.chat_messages
            where thread_id = %s
            order by created_at, id
            limit %s
            """,
            (thread_id, max(1, min(limit, 500))),
        ).fetchall()
