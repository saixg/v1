"""Hybrid retrieval (build-plan.md Part 4.E), in the order the spec states:

    exact symbol/identifier
      -> path & metadata filters
      -> FTS keyword (the chunks.tsv GIN index)
      -> vector (when enabled)
      -> graph expansion (callers/callees/importers, 1-2 hops)
      -> reciprocal-rank fusion
      -> rerank (optional)
      -> token-budgeted assembly with file+line citations

Four properties this module is built around:

**The filter stage is not a pass.** Path and metadata filters are pushed down
into every other stage's SQL instead of running as their own step, so a
filtered search never spends a rank slot on a chunk it would then discard. The
stage is still reported, so a caller can see it applied.

**Fusion is by rank, never by score.** `ts_rank_cd`, cosine similarity and a
fixed exact-match weight are not on a common scale. Reciprocal-rank fusion
only needs the orderings, which is exactly why Part 4.E names it.

**The graph walk is the existing one.** Expansion calls
`app.knowledge.reads.neighborhood` (forward and reverse) — the same BFS the
Explorer and the Inspector read. There is no second graph walker.

**The budget is a hard constraint.** Assembly stops when the next chunk would
exceed the token budget and reports how many chunks it dropped. Nothing in
this module can ever return "the whole repo".

A stage that cannot run says so (`status="skipped"` plus a reason) rather than
returning empty and letting a caller read "no index" as "no matches". The
vector stage is the common case: with no `VOYAGE_API_KEY`, or before the
embedding backfill, FTS + exact-symbol + graph expansion carry the query on
their own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import psycopg

from app.knowledge.chunks import count_chunks
from app.knowledge.reads import neighborhood
from app.knowledge.retrieval import (
    chunks_for_graph_neighbors,
    exact_symbol_chunks,
    fts_chunks,
    vector_chunks,
)

# `estimate_tokens` is the shared ~4-chars-per-token heuristic
# (`app.llm.tokens`, a leaf module with no provider dependency). Assembly
# budgets with `chunks.token_count` where the chunker wrote one and falls back
# to this estimate only when it is zero.
from app.llm.tokens import estimate_tokens
from app.models import Citation, ScoredChunk

# Edge kinds graph expansion follows: callers/callees/importers, per Part 4.E.
# Same list main.py's relationship views use; `contains` (the file tree) and
# `belongs_to` (clustering) are different lenses, not relationships.
EXPANSION_EDGE_KINDS = ["imports", "calls", "extends", "implements", "tests"]

# RRF's damping constant. 60 is the value from the original Cormack et al.
# formulation and the one every later system quotes; it is not tuned here
# because nothing has been measured yet (Part 3: change a model or a parameter
# only when the eval harness shows a win).
RRF_K = 60

# Per-stage fusion weights. A chunk whose symbol *is* the identifier in the
# question is the strongest signal retrieval has, so exact-symbol outranks the
# ranked stages; graph expansion is a neighbour, relevant but second-hand, so
# it is damped. These are starting values, deliberately plain, and the
# eval grader is what should move them.
STAGE_WEIGHTS: dict[str, float] = {
    "exact_symbol": 2.0,
    "fts": 1.0,
    "vector": 1.0,
    "graph": 0.5,
}

DEFAULT_TOKEN_BUDGET = 8000
MAX_TOKEN_BUDGET = 60000
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_HOPS = 2
# How many top-ranked chunks seed the graph walk. Expanding from everything
# would make the walk the dominant stage instead of a refinement.
GRAPH_SEED_CHUNKS = 5

# Identifier candidates for the exact-symbol stage. Tokens shorter than three
# characters are dropped (too ambiguous) and ordinary English question words
# are not identifiers.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORD_TEXT = """
    the and for are but not you all any can her was one our out his has had how who why what
    when where which with this that these those from into over under about after before than
    then them they their there here does did done how's its itself your yours ours mine
    code file files function functions class classes method methods module modules repo
    repository does do done use used uses using show tell explain describe find where
    handle handled handles call called calls return returns work works why how
"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def identifier_candidates(text: str, *, limit: int = 12) -> list[str]:
    """Identifier-looking tokens in a question, in first-appearance order.

    A token survives if it is already code-shaped (contains `_`, or an inner
    capital, or is not a lowercase English word in the stop list). Deliberately
    simple and deterministic: this stage is a *filter* into exact lookup, and a
    false positive costs one index probe that matches nothing."""
    out: list[str] = []
    seen: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        code_shaped = "_" in token or any(ch.isupper() for ch in token[1:])
        if not code_shaped and token.lower() in _STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------- requests


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    path_prefixes: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    # Caller-supplied identifiers. When empty, they are extracted from `text`.
    symbols: tuple[str, ...] = ()
    limit: int = DEFAULT_LIMIT
    token_budget: int = DEFAULT_TOKEN_BUDGET
    hops: int = 1
    # False forces the vector stage skipped even where embeddings exist —
    # the switch an eval run needs to measure retrieval without them.
    use_vector: bool = True

    def normalized(self) -> RetrievalQuery:
        return RetrievalQuery(
            text=self.text,
            path_prefixes=self.path_prefixes,
            kinds=self.kinds,
            symbols=self.symbols,
            limit=max(1, min(self.limit, MAX_LIMIT)),
            token_budget=max(1, min(self.token_budget, MAX_TOKEN_BUDGET)),
            hops=max(0, min(self.hops, MAX_HOPS)),
            use_vector=self.use_vector,
        )


class QueryEmbedder:
    """What the vector stage needs from the model layer: one embedding for the
    query text, or None when embeddings are unavailable.

    Narrow on purpose. `ModelService.embed` is the real implementation (a
    sibling change wires voyage-code-3 behind it); this protocol keeps the
    retrieval pipeline from depending on the model layer at all, and makes
    "embeddings unavailable" an ordinary return value instead of an exception
    class retrieval has to know about."""

    def embed_query(self, text: str) -> Sequence[float] | None:  # pragma: no cover - interface
        raise NotImplementedError


class ModelServiceEmbedder(QueryEmbedder):
    """Adapter over `ModelService.embed`. Any failure — no API key, the stub
    `NotImplementedError`, a provider outage — degrades the vector stage to
    skipped. Retrieval still answers from FTS, exact symbols and the graph,
    which is the whole reason those stages run first."""

    def __init__(self, service: object) -> None:
        self._service = service

    def embed_query(self, text: str) -> Sequence[float] | None:
        embed = getattr(self._service, "embed", None)
        if embed is None:
            return None
        try:
            vectors = embed([text])
        except Exception:
            # Deliberately broad: every failure mode here has the same correct
            # response, and the reason is reported as the stage's skip reason
            # rather than raised at the caller.
            return None
        if not vectors:
            return None
        first = vectors[0]
        return first if first else None


# ---------------------------------------------------------------- results


@dataclass(frozen=True)
class StageOutcome:
    """What one retrieval stage did. `skipped` is a first-class result."""

    name: str
    status: str  # "ran" | "skipped"
    hits: int = 0
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "hits": self.hits,
            "reason": self.reason,
        }


@dataclass
class FusedChunk:
    chunk: ScoredChunk
    score: float
    stages: list[str] = field(default_factory=list)

    def citation(self) -> Citation:
        return Citation(
            path=self.chunk.path,
            line_start=self.chunk.line_start,
            line_end=self.chunk.line_end,
            symbol=self.chunk.symbol,
            node_id=self.chunk.node_id,
            chunk_id=self.chunk.id,
        )


@dataclass(frozen=True)
class RetrievalResult:
    """Budget-fitted evidence, in fused rank order, plus how it was produced."""

    items: list[FusedChunk]
    stages: list[StageOutcome]
    token_budget: int
    tokens_used: int
    dropped_for_budget: int
    # Candidates that survived fusion but not the budget are counted, never
    # silently lost: a caller can tell "nothing matched" from "too much did".
    fused_candidates: int

    def citations(self) -> list[Citation]:
        return [item.citation() for item in self.items]


# The rerank stage, as specified: present, pluggable, and not implemented.
# Part 3 is explicit that reranking "ships only if eval shows it's needed", so
# building one now would be shipping an unmeasured model call. The stage is
# reported so its absence is visible rather than tacit.
RERANK_OUTCOME = StageOutcome(
    name="rerank",
    status="skipped",
    reason=(
        "not implemented: reranking ships only after the eval harness shows "
        "fusion alone is insufficient (build-plan.md Part 3)"
    ),
)


def chunk_tokens(chunk: ScoredChunk) -> int:
    return chunk.token_count if chunk.token_count > 0 else estimate_tokens(chunk.content)


def reciprocal_rank_fusion(
    ranked_lists: Sequence[tuple[str, Sequence[ScoredChunk]]],
) -> list[FusedChunk]:
    """Fuse per-stage orderings into one. Score for chunk c is

        sum over stages s containing c of  weight(s) / (RRF_K + rank_s(c))

    with `rank` 1-based. Ties break on the number of stages that found the
    chunk, then on path and line, so the order is total and reproducible."""
    fused: dict[int, FusedChunk] = {}
    for stage, chunks in ranked_lists:
        weight = STAGE_WEIGHTS.get(stage, 1.0)
        for rank, chunk in enumerate(chunks, start=1):
            if chunk.id is None:
                continue
            entry = fused.get(chunk.id)
            if entry is None:
                entry = FusedChunk(chunk=chunk, score=0.0)
                fused[chunk.id] = entry
            entry.score += weight / (RRF_K + rank)
            if stage not in entry.stages:
                entry.stages.append(stage)
    return sorted(
        fused.values(),
        key=lambda e: (-e.score, -len(e.stages), e.chunk.path, e.chunk.line_start),
    )


def _graph_expansion(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    seeds: Sequence[FusedChunk],
    query: RetrievalQuery,
) -> tuple[list[ScoredChunk], StageOutcome]:
    """Walk 1-2 hops out from the top-ranked chunks and bring back their
    neighbours' chunks: callers and callees (`calls`), importers and imported
    (`imports`), plus `extends`/`implements`/`tests`.

    Both directions are walked — "what does this call" and "what calls this"
    are both context a question about a symbol needs. The walk itself is
    `app.knowledge.reads.neighborhood`, unchanged."""
    if query.hops == 0:
        return [], StageOutcome("graph", "skipped", reason="hops=0")
    if not seeds:
        return [], StageOutcome("graph", "skipped", reason="no seed chunks to expand from")

    # Relationship edges attach to file nodes (app/analysis/scan/
    # relationships.py), so the walk is seeded from each seed chunk's file
    # node; a chunk's own symbol node has no relationship edges of its own yet.
    seed_paths = []
    for seed in seeds[:GRAPH_SEED_CHUNKS]:
        if seed.chunk.path not in seed_paths:
            seed_paths.append(seed.chunk.path)

    from app.knowledge.reads import list_nodes  # local: avoids a cycle at import time

    seed_node_ids: list[int] = []
    for path in seed_paths:
        for node in list_nodes(conn, repo_id, commit_sha, kind="file", path=path):
            if node.id is not None:
                seed_node_ids.append(node.id)

    if not seed_node_ids:
        return [], StageOutcome(
            "graph", "skipped", reason="no file node in the graph for the top-ranked chunks"
        )

    neighbor_ids: set[int] = set()
    neighbor_paths: set[str] = set()
    for node_id in seed_node_ids:
        for direction in ("forward", "reverse"):
            nodes, _edges, _truncated = neighborhood(
                conn,
                repo_id,
                commit_sha,
                node_id,
                depth=query.hops,
                kinds=EXPANSION_EDGE_KINDS,
                direction=direction,  # pyright: ignore[reportArgumentType]
            )
            for node in nodes:
                if node.id is not None and node.id not in seed_node_ids:
                    neighbor_ids.add(node.id)
                if node.path:
                    neighbor_paths.add(node.path)
    neighbor_paths -= set(seed_paths)

    if not neighbor_ids and not neighbor_paths:
        return [], StageOutcome(
            "graph", "ran", 0, reason="the top-ranked chunks have no resolved relationships"
        )

    hits = chunks_for_graph_neighbors(
        conn,
        repo_id,
        commit_sha,
        sorted(neighbor_ids),
        sorted(neighbor_paths),
        limit=query.limit * 2,
        path_prefixes=query.path_prefixes or None,
        kinds=query.kinds or None,
    )
    return hits, StageOutcome("graph", "ran", len(hits))


def assemble(
    fused: Sequence[FusedChunk], *, token_budget: int, limit: int
) -> tuple[list[FusedChunk], int, int]:
    """Take fused candidates in order while they fit the budget.

    A chunk that does not fit is skipped and counted; the walk continues, so a
    single oversized chunk does not truncate everything behind it. Returns
    `(kept, tokens_used, dropped)`."""
    kept: list[FusedChunk] = []
    used = 0
    dropped = 0
    for item in fused:
        if len(kept) >= limit:
            dropped += 1
            continue
        cost = chunk_tokens(item.chunk)
        if used + cost > token_budget:
            dropped += 1
            continue
        kept.append(item)
        used += cost
    return kept, used, dropped


def retrieve(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    query: RetrievalQuery,
    *,
    embedder: QueryEmbedder | None = None,
) -> RetrievalResult:
    """Run the full Part 4.E pipeline and return budget-fitted evidence."""
    q = query.normalized()
    stages: list[StageOutcome] = []
    ranked: list[tuple[str, Sequence[ScoredChunk]]] = []

    total_chunks, embedded_chunks = count_chunks(conn, repo_id, commit_sha)

    # -- 1. exact symbol / identifier --------------------------------------
    symbols = list(q.symbols) or identifier_candidates(q.text)
    if symbols:
        hits = exact_symbol_chunks(
            conn,
            repo_id,
            commit_sha,
            symbols,
            limit=q.limit,
            path_prefixes=q.path_prefixes or None,
            kinds=q.kinds or None,
        )
        ranked.append(("exact_symbol", hits))
        stages.append(StageOutcome("exact_symbol", "ran", len(hits)))
    else:
        stages.append(
            StageOutcome(
                "exact_symbol", "skipped", reason="no identifier-shaped token in the query"
            )
        )

    # -- 2. path & metadata filters ----------------------------------------
    # Pushed down into every other stage's SQL rather than run as a pass of
    # its own; reported here so an applied filter is visible.
    if q.path_prefixes or q.kinds:
        stages.append(
            StageOutcome(
                "filters",
                "ran",
                reason=(
                    f"path_prefixes={list(q.path_prefixes)} kinds={list(q.kinds)}; "
                    "applied inside every other stage's query"
                ),
            )
        )
    else:
        stages.append(StageOutcome("filters", "skipped", reason="no path or metadata filter given"))

    # -- 3. FTS keyword ----------------------------------------------------
    if total_chunks == 0:
        stages.append(
            StageOutcome(
                "fts", "skipped", reason="no chunks indexed for this repo at this commit"
            )
        )
    else:
        # Strict first (all terms), then relaxed (any term) only if strict
        # found nothing. Precision when the question is precise; recall when
        # it is prose. The eval grader is what established that the strict
        # query alone is not enough — see `fts_chunks`'s docstring.
        hits = fts_chunks(
            conn,
            repo_id,
            commit_sha,
            q.text,
            limit=q.limit,
            path_prefixes=q.path_prefixes or None,
            kinds=q.kinds or None,
        )
        reason = None
        if not hits:
            hits = fts_chunks(
                conn,
                repo_id,
                commit_sha,
                q.text,
                limit=q.limit,
                path_prefixes=q.path_prefixes or None,
                kinds=q.kinds or None,
                relaxed=True,
            )
            reason = "no chunk matched every term; relaxed to any-term matching"
        ranked.append(("fts", hits))
        stages.append(StageOutcome("fts", "ran", len(hits), reason=reason))

    # -- 4. vector ---------------------------------------------------------
    stages.append(_vector_stage(conn, repo_id, commit_sha, q, embedder, embedded_chunks, ranked))

    # -- 5/6. graph expansion, then fusion ---------------------------------
    # Expansion is seeded from the fusion of the content stages, so the walk
    # starts from the best evidence found so far rather than from one stage's
    # opinion; its own hits are then fused in alongside them.
    pre_fused = reciprocal_rank_fusion(ranked)
    graph_hits, graph_stage = _graph_expansion(conn, repo_id, commit_sha, pre_fused, q)
    stages.append(graph_stage)
    if graph_hits:
        ranked.append(("graph", graph_hits))
    fused = reciprocal_rank_fusion(ranked) if graph_hits else pre_fused

    # -- 7. rerank (pluggable, not implemented) ----------------------------
    stages.append(RERANK_OUTCOME)

    # -- 8. token-budgeted assembly ----------------------------------------
    kept, used, dropped = assemble(fused, token_budget=q.token_budget, limit=q.limit)

    return RetrievalResult(
        items=kept,
        stages=stages,
        token_budget=q.token_budget,
        tokens_used=used,
        dropped_for_budget=dropped,
        fused_candidates=len(fused),
    )


def _vector_stage(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    q: RetrievalQuery,
    embedder: QueryEmbedder | None,
    embedded_chunks: int,
    ranked: list[tuple[str, Sequence[ScoredChunk]]],
) -> StageOutcome:
    """The vector stage, which is allowed not to happen.

    Four ways it degrades to skipped, each reported distinctly: the caller
    turned it off, no embedder was supplied, nothing in this snapshot is
    embedded yet, or the embedder could not produce a query vector (no
    VOYAGE_API_KEY being the common case). In none of them does retrieval
    fail — FTS, exact symbols and graph expansion are useful on their own,
    which is why they run first."""
    if not q.use_vector:
        return StageOutcome("vector", "skipped", reason="disabled for this request")
    if embedder is None:
        return StageOutcome("vector", "skipped", reason="no query embedder configured")
    if embedded_chunks == 0:
        return StageOutcome(
            "vector", "skipped", reason="no chunk in this snapshot has an embedding yet"
        )
    vector = embedder.embed_query(q.text)
    if not vector:
        return StageOutcome(
            "vector",
            "skipped",
            reason="the embedding model is unavailable (no API key, or the provider failed)",
        )
    hits = vector_chunks(
        conn,
        repo_id,
        commit_sha,
        vector,
        limit=q.limit,
        path_prefixes=q.path_prefixes or None,
        kinds=q.kinds or None,
    )
    ranked.append(("vector", hits))
    return StageOutcome("vector", "ran", len(hits))
