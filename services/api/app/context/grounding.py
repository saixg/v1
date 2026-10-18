"""Repository grounding for one task (build-plan.md Part 4.F.4): which existing
graph entities the task is about, written as `codeaxis.task_components` rows.

Deterministic end to end. No model call decides what a task touches (Part
0.3: "an LLM must never be the source of a file path, symbol, or
relationship that is stored as fact"). Three sources, in descending order of
how much they are allowed to claim:

1. **Literal mentions -> `origin='parsed'`.** A path or an identifier that
   appears verbatim in the task text and resolves to exactly one node in the
   snapshot. Paths are matched against the snapshot's `file`/`directory`
   nodes; identifiers go through the existing exact-symbol retrieval stage
   (`app.knowledge.retrieval.exact_symbol_chunks`), case-sensitive only — a
   case-folded hit is a guess, not a literal.
2. **Reverse dependencies -> `relevance='impacted'`.** Files that import,
   call, extend or implement a primary file, found with the shared BFS
   (`app.knowledge.reads.neighborhood`, reverse direction). A `resolved` edge
   is a parsed fact; an `ambiguous` (name-only) edge yields an `inferred` row
   with a confidence below every literal match, because the edge itself is a
   guess (Part 4.B: ambiguous is never silently promoted).
3. **Retrieval-ranked guesses -> `origin='inferred'`, with confidence.** The
   hybrid retrieval pipeline's top files that nothing literal pointed at.
   Their confidence is capped below a literal match so a guess can never
   outrank a fact when a reader sorts by it.

Every row references a node id read back from the snapshot in this call. A
mention that does not resolve (no such path, or more than one candidate) is
returned as a gap — never "corrected" into the nearest real path. That is the
whole point of `LiteralMatches.unmatched_paths` / `ambiguous_paths`: a gap is
shown, not guessed (Part 0.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg

from app.analysis.semantic.retrieve import (
    RetrievalQuery,
    StageOutcome,
    identifier_candidates,
    retrieve,
)
from app.knowledge import (
    exact_symbol_chunks,
    get_task,
    list_nodes,
    list_requirements,
    neighborhood,
    replace_task_components,
)
from app.models import (
    Citation,
    ComponentRelevance,
    ContextEvidence,
    Node,
    OriginTag,
    Task,
    TaskComponent,
)

ANALYZER = "context.grounding"
ANALYZER_VERSION = "0.1.0"

# Edge kinds whose source *depends on* their destination, so a change to the
# destination can break the source. `tests` is deliberately absent: a test
# that breaks is the test doing its job, and tests get their own package
# section; `contains`/`belongs_to` are structure, not dependency.
IMPACT_EDGE_KINDS = ["imports", "calls", "extends", "implements"]

# How many retrieval-ranked files may be added as guesses. Small on purpose:
# each one is an unverified claim that the task is about that file.
MAX_RETRIEVAL_GUESSES = 5
# The highest confidence a retrieval guess may carry. Literal matches carry
# 1.0 (or 1/k when a symbol is defined in k files); a ranked guess is a
# relative fusion score, which is not a probability, so it is scaled into
# [0, GUESS_CONFIDENCE_CEILING] rather than reported raw.
GUESS_CONFIDENCE_CEILING = 0.5
# An `impacted` row reached through a name-only (`ambiguous`) edge.
AMBIGUOUS_EDGE_CONFIDENCE = 0.4
# Caps on the graph walk per task, so a task that names a hub file cannot
# turn grounding into a whole-repo traversal. Hitting a cap is reported.
MAX_PRIMARY_FILES_FOR_IMPACT = 12
MAX_SYMBOLS_PER_FILE_FOR_IMPACT = 40
MAX_IMPACTED = 25

# Path-shaped tokens: `a/b/c.py`, `c.py`, `a/b/`. Deliberately conservative:
# anything that does not look like a path is left to the identifier stage.
_PATH_RE = re.compile(
    r"(?<![\w/.\-])((?:[\w.\-]+/)+[\w.\-]*|[\w\-]+(?:\.[\w\-]+)*\.[A-Za-z0-9]{1,8})(?![\w/])"
)

_RELEVANCE_RANK: dict[str, int] = {"primary": 0, "supporting": 1, "impacted": 2}


@dataclass
class LiteralMatches:
    """What the task text names literally, resolved against the snapshot.

    `files` maps a resolved file path to the evidence of how it was named;
    the three gap lists are the honest remainder (Part 0.3)."""

    files: dict[str, _Hit] = field(default_factory=dict)
    directories: dict[str, _Hit] = field(default_factory=dict)
    # Path-shaped tokens (containing a `/`) that match no node in the snapshot.
    unmatched_paths: list[str] = field(default_factory=list)
    # A token whose suffix matches more than one file: which one is meant is
    # a question for the user, not for us.
    ambiguous_paths: dict[str, list[str]] = field(default_factory=dict)
    # An identifier defined (as a chunked symbol) in more than one file.
    ambiguous_symbols: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class _Hit:
    node: Node
    # The literal tokens from the task text that resolved here.
    tokens: list[str] = field(default_factory=list)
    spans: list[Citation] = field(default_factory=list)
    symbol_node_ids: list[int] = field(default_factory=list)
    # 1.0 for a unique match; 1/k when an identifier is defined in k files.
    confidence: float = 1.0


@dataclass(frozen=True)
class GroundingResult:
    components: list[TaskComponent]
    literals: LiteralMatches
    # Stage outcomes of the retrieval run behind the inferred guesses, so a
    # caller can tell "no guess was needed" from "retrieval could not run".
    retrieval_stages: list[StageOutcome]
    # Caps that were hit, as human-readable notes. Empty when none were.
    truncated: list[str]


def task_text(task: Task, requirement_statements: list[str]) -> str:
    """The text grounding reads: title, raw intent, and the extracted
    requirement statements. All untrusted data — it is only ever matched
    against the snapshot, never interpreted."""
    parts = [task.title, task.intent or "", *requirement_statements]
    return "\n".join(p for p in parts if p)


def _clean_path_token(token: str) -> str:
    token = token.strip().strip("`'\"()[]{}<>,;:")
    while token.startswith("./"):
        token = token[2:]
    return token.rstrip(".")


def path_candidates(text: str) -> list[str]:
    """Path-shaped tokens in first-appearance order, de-duplicated."""
    out: list[str] = []
    for match in _PATH_RE.finditer(text):
        token = _clean_path_token(match.group(1))
        if token and token not in out:
            out.append(token)
    return out


def literal_matches(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, text: str
) -> LiteralMatches:
    """Resolve every literal path and identifier in `text` against one
    snapshot. Read-only, so the package assembler can call it for its
    open-question gaps without re-writing `task_components`."""
    out = LiteralMatches()
    files = {n.path: n for n in list_nodes(conn, repo_id, commit_sha, kind="file") if n.path}
    dirs = {
        n.path.rstrip("/"): n
        for n in list_nodes(conn, repo_id, commit_sha, kind="directory")
        if n.path
    }

    for token in path_candidates(text):
        stripped = token.rstrip("/")
        if token in files:
            out.files.setdefault(token, _Hit(node=files[token])).tokens.append(token)
            continue
        if stripped and stripped in dirs:
            out.directories.setdefault(stripped, _Hit(node=dirs[stripped])).tokens.append(token)
            continue
        # A bare `orders.py` (or `store/orders.py`) names a file only if
        # exactly one path ends with it on a segment boundary.
        suffix = "/" + stripped
        candidates = sorted(p for p in files if p.endswith(suffix))
        if len(candidates) == 1:
            out.files.setdefault(candidates[0], _Hit(node=files[candidates[0]])).tokens.append(
                token
            )
        elif len(candidates) > 1:
            out.ambiguous_paths[token] = candidates
        elif "/" in token:
            # Only slash-bearing tokens are reported: `e.g.` or `v1.2` look
            # like a filename to the regex and are not a user naming a path.
            out.unmatched_paths.append(token)

    identifiers = identifier_candidates(text, limit=24)
    if identifiers:
        hits = exact_symbol_chunks(conn, repo_id, commit_sha, identifiers, limit=200)
        by_symbol: dict[str, dict[str, list[Citation]]] = {}
        node_ids: dict[tuple[str, str], list[int]] = {}
        for chunk in hits:
            # Case-sensitive only (score 1.0): a folded match is not literal.
            if chunk.symbol is None or chunk.symbol not in identifiers or chunk.score < 1.0:
                continue
            by_symbol.setdefault(chunk.symbol, {}).setdefault(chunk.path, []).append(
                Citation(
                    path=chunk.path,
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    symbol=chunk.symbol,
                    node_id=chunk.node_id,
                    chunk_id=chunk.id,
                )
            )
            if chunk.node_id is not None:
                node_ids.setdefault((chunk.symbol, chunk.path), []).append(chunk.node_id)
        for symbol, per_path in by_symbol.items():
            paths = sorted(p for p in per_path if p in files)
            if len(paths) > 1:
                out.ambiguous_symbols[symbol] = paths
            for path in paths:
                hit = out.files.setdefault(path, _Hit(node=files[path]))
                hit.tokens.append(symbol)
                hit.spans.extend(per_path[path])
                hit.symbol_node_ids.extend(node_ids.get((symbol, path), []))
                hit.confidence = min(hit.confidence, 1.0 / len(paths))
    return out


def _impacted(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    primary: list[Node],
    already: set[int],
    truncated: list[str],
) -> dict[int, tuple[Node, list[tuple[str, str, int | None, str]]]]:
    """Files with a dependency edge *into* a primary file (or into a symbol
    declared in it). Returns `{file node id: (node, [(edge kind,
    resolution_status, line, target path)])}`."""
    if len(primary) > MAX_PRIMARY_FILES_FOR_IMPACT:
        truncated.append(
            f"impact walk limited to the first {MAX_PRIMARY_FILES_FOR_IMPACT} of "
            f"{len(primary)} primary files"
        )
    found: dict[int, tuple[Node, list[tuple[str, str, int | None, str]]]] = {}
    for file_node in primary[:MAX_PRIMARY_FILES_FOR_IMPACT]:
        assert file_node.id is not None and file_node.path is not None
        roots = [file_node.id]
        symbols = list_nodes(conn, repo_id, commit_sha, kind="symbol", path=file_node.path)
        if len(symbols) > MAX_SYMBOLS_PER_FILE_FOR_IMPACT:
            truncated.append(
                f"impact walk for {file_node.path} limited to "
                f"{MAX_SYMBOLS_PER_FILE_FOR_IMPACT} of {len(symbols)} symbols"
            )
        roots.extend(s.id for s in symbols[:MAX_SYMBOLS_PER_FILE_FOR_IMPACT] if s.id is not None)
        for root in roots:
            nodes, edges, _trunc = neighborhood(
                conn,
                repo_id,
                commit_sha,
                root,
                depth=1,
                kinds=IMPACT_EDGE_KINDS,
                direction="reverse",
            )
            by_id = {n.id: n for n in nodes}
            for edge in edges:
                src = by_id.get(edge.src_id)
                # Relationship edges originate at file nodes; anything else
                # is not a file we can name, so it is not reported as one.
                if src is None or src.kind != "file" or src.id is None or src.id in already:
                    continue
                if edge.resolution_status not in ("resolved", "ambiguous"):
                    continue
                entry = found.setdefault(src.id, (src, []))
                entry[1].append(
                    (edge.kind, edge.resolution_status, edge.line_start, file_node.path)
                )
    if len(found) > MAX_IMPACTED:
        truncated.append(f"impacted files limited to {MAX_IMPACTED} of {len(found)}")
        found = dict(sorted(found.items())[:MAX_IMPACTED])
    return found


def ground_task(
    conn: psycopg.Connection,
    task_id: str,
    *,
    write: bool = True,
    use_retrieval: bool = True,
) -> GroundingResult:
    """Compute (and by default persist) a task's `task_components` at the
    task's active snapshot.

    `write=True` needs a worker connection; the replacement is wholesale,
    like every other derived set in `app.knowledge.context` — a file the new
    grounding no longer names is a file nothing supports any more."""
    task = get_task(conn, task_id)
    if task is None or task.id is None:
        raise LookupError(f"task {task_id} not found")
    repo_id, sha = task.repo_id, task.commit_sha
    statements = [
        r.statement
        for r in list_requirements(conn, task.id, commit_sha=sha)
        if r.status != "rejected"
    ]
    text = task_text(task, statements)
    literals = literal_matches(conn, repo_id, sha, text)
    truncated: list[str] = []
    rows: dict[int, TaskComponent] = {}

    def add(
        node: Node,
        relevance: ComponentRelevance,
        origin: OriginTag,
        rationale: str,
        evidence: ContextEvidence,
        confidence: float | None,
    ) -> None:
        assert node.id is not None
        existing = rows.get(node.id)
        if (
            existing is not None
            and _RELEVANCE_RANK[existing.relevance] <= _RELEVANCE_RANK[relevance]
        ):
            return
        rows[node.id] = TaskComponent(
            task_id=task.id,  # pyright: ignore[reportArgumentType]
            node_id=node.id,
            repo_id=repo_id,
            commit_sha=sha,
            relevance=relevance,
            rationale=rationale,
            origin=origin,
            confidence=confidence,
            evidence=evidence,
            analyzer=ANALYZER,
            analyzer_version=ANALYZER_VERSION,
        )

    # -- 1. literal mentions -------------------------------------------------
    for _path, hit in sorted(literals.files.items()):
        unique = hit.confidence >= 1.0
        tokens = ", ".join(dict.fromkeys(hit.tokens))
        add(
            hit.node,
            "primary" if unique else "supporting",
            "parsed",
            (
                f"named literally in the task text ({tokens})"
                if unique
                else f"defines {tokens}, which the task names but which is also defined "
                "elsewhere; which definition is meant is ambiguous"
            ),
            ContextEvidence(
                spans=hit.spans,
                node_ids=[hit.node.id, *hit.symbol_node_ids],  # pyright: ignore[reportArgumentType]
                chunk_ids=[s.chunk_id for s in hit.spans if s.chunk_id is not None],
                documents=[{"source": "text", "ref": "task", "match": t} for t in hit.tokens],
            ),
            hit.confidence,
        )
    for _path, hit in sorted(literals.directories.items()):
        add(
            hit.node,
            "primary",
            "parsed",
            f"directory named literally in the task text ({', '.join(hit.tokens)})",
            ContextEvidence(
                node_ids=[hit.node.id],  # pyright: ignore[reportArgumentType]
                documents=[{"source": "text", "ref": "task", "match": t} for t in hit.tokens],
            ),
            1.0,
        )

    # -- 2. retrieval-ranked guesses -----------------------------------------
    stages: list[StageOutcome] = []
    if use_retrieval and text.strip():
        result = retrieve(
            conn, repo_id, sha, RetrievalQuery(text=text, limit=30, token_budget=60000)
        )
        stages = result.stages
        files = {n.path: n for n in list_nodes(conn, repo_id, sha, kind="file") if n.path}
        literal_paths = set(literals.files)
        top = result.items[0].score if result.items else 0.0
        guessed: list[str] = []
        for item in result.items:
            path = item.chunk.path
            if path in literal_paths or path in guessed or path not in files:
                continue
            guessed.append(path)
            confidence = round(GUESS_CONFIDENCE_CEILING * (item.score / top), 3) if top else 0.0
            add(
                files[path],
                "supporting",
                "inferred",
                f"ranked by hybrid retrieval ({'+'.join(item.stages)}); not named by the task",
                ContextEvidence(
                    spans=[item.citation()],
                    node_ids=[files[path].id],  # pyright: ignore[reportArgumentType]
                    chunk_ids=[item.chunk.id] if item.chunk.id is not None else [],
                    notes=f"fused rank score {item.score:.5f}",
                ),
                max(0.0, min(confidence, GUESS_CONFIDENCE_CEILING)),
            )
            if len(guessed) >= MAX_RETRIEVAL_GUESSES:
                break

    # -- 3. impacted: reverse dependencies of the primary files --------------
    primary = [
        literals.files[p].node
        for p in sorted(literals.files)
        if literals.files[p].confidence >= 1.0
    ]
    for node, via in _impacted(conn, repo_id, sha, primary, set(rows), truncated).values():
        resolved = any(status == "resolved" for _k, status, _l, _t in via)
        spans = [
            Citation(path=node.path or "", line_start=line, line_end=line)
            for _k, _s, line, _t in via
            if line is not None and node.path
        ]
        targets = sorted({t for _k, _s, _l, t in via})
        kinds = sorted({k for k, _s, _l, _t in via})
        add(
            node,
            "impacted",
            "parsed" if resolved else "inferred",
            f"{'/'.join(kinds)} edge(s) into {', '.join(targets)}"
            + ("" if resolved else " (name-only match: ambiguous)"),
            ContextEvidence(spans=spans[:10], node_ids=[node.id]),  # pyright: ignore[reportArgumentType]
            None if resolved else AMBIGUOUS_EDGE_CONFIDENCE,
        )

    components = sorted(rows.values(), key=lambda r: (_RELEVANCE_RANK[r.relevance], r.node_id))
    if write:
        replace_task_components(conn, task.id, components)
    return GroundingResult(
        components=components,
        literals=literals,
        retrieval_stages=stages,
        truncated=truncated,
    )
