"""Context-package assembly (build-plan.md Part 4.F.5): the budgeted, ordered,
evidence-carrying bundle a coding agent is handed for one task.

**Deterministic.** No model call happens here and no model decides what goes
in a package (Part 0.3). Every item is a row some analyzer, extractor or human
already wrote — a requirement, a ledger constraint, a chunk retrieved by the
hybrid pipeline, a graph edge, a convention, a coverage counter — re-shaped
into a `ContextItem` that keeps that row's origin and evidence.

**Order is the spec's, exactly:**

    task intent -> constraints -> requirements -> architecture slice
      -> relevant code/evidence -> patterns/conventions -> relevant tests
      -> known limitations -> unresolved questions

**Constraints are never truncated.** Task intent and the constraint ledger
form the fixed head of the package and are always included in full, hard
before soft. If they alone exceed the token budget, nothing is dropped from
them: the package records `constraints_exceed_budget` in `unresolved` and is
stored with status `invalid` (the migration's own reading of Part 4.F.5:
"the honest outcome is a package marked invalid with the reason"). The tail
then gets a budget of zero, and every tail item it could not hold is counted.

**The budget applies to the tail only**, split by a per-section allocation.
Sections are filled in spec order; whatever a section does not use rolls over
to the next one, so an empty architecture slice does not waste its share.
Every item that did not fit is recorded in `manifest` — per section, by a
content-free reference — never silently lost (the same discipline as
`retrieve.assemble`'s `dropped_for_budget`).

**Pinned to the task's active snapshot.** Everything is read at
`task.commit_sha`; rows derived at another commit are listed in `unresolved`
as needing re-derivation rather than mixed in (Part 5: "results from
different versions can never mix"). A package whose commit is no longer the
task's is `stale` and is never served as current — see
`mark_stale_if_snapshot_moved` and `check_package`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import psycopg

from app.analysis.semantic.citations import DbSnapshotIndex, check_citation
from app.analysis.semantic.retrieve import (
    EXPANSION_EDGE_KINDS,
    FusedChunk,
    QueryEmbedder,
    RetrievalQuery,
    chunk_tokens,
    identifier_candidates,
    retrieve,
)
from app.context.export import scrub_secrets
from app.context.grounding import (
    LiteralMatches,
    ground_task,
    literal_matches,
    task_text,
)
from app.knowledge import (
    create_context_package,
    file_line_counts,
    get_task,
    list_constraints,
    list_context_packages,
    list_conventions,
    list_coverage,
    list_decisions,
    list_nodes,
    list_requirements,
    list_task_components,
    neighborhood,
    next_package_version,
    replace_context_items,
    set_context_package_status,
    unresolved_summary,
)
from app.knowledge.context import MAX_LIST_LIMIT
from app.llm.tokens import estimate_tokens
from app.models import (
    Citation,
    Constraint,
    ContextEvidence,
    ContextItem,
    ContextItemKind,
    ContextPackage,
    ContextPackageStatus,
    Node,
    OriginTag,
    Requirement,
    Task,
    TaskComponent,
)

ANALYZER = "context.package"
ANALYZER_VERSION = "0.1.0"

DEFAULT_PACKAGE_BUDGET = 12000
# The retrieval pipeline's own ceiling (`retrieve.MAX_TOKEN_BUDGET`); a larger
# package budget is accepted, the code section just cannot use more than this.
MAX_PACKAGE_BUDGET = 200000

SECTION_ORDER: tuple[ContextItemKind, ...] = (
    "task_intent",
    "constraint",
    "requirement",
    "architecture",
    "code",
    "convention",
    "test",
    "limitation",
    "open_question",
)
# Never truncated, never budgeted.
FIXED_SECTIONS: tuple[ContextItemKind, ...] = ("task_intent", "constraint")
TAIL_SECTIONS: tuple[ContextItemKind, ...] = tuple(
    k for k in SECTION_ORDER if k not in FIXED_SECTIONS
)

# Share of the tail budget each section starts with. Code gets the most
# because it is what an agent cannot reconstruct on its own; requirements
# come first in fill order, so in practice they are rarely cut. Starting
# values, not tuned: the with/without-context eval (Part 9) is what should
# move them.
DEFAULT_ALLOCATION: dict[str, float] = {
    "requirement": 0.15,
    "architecture": 0.10,
    "code": 0.45,
    "convention": 0.06,
    "test": 0.08,
    "limitation": 0.09,
    "open_question": 0.07,
}

_KIND_ORDER = {
    "objective": 0,
    "functional": 1,
    "technical": 2,
    "architectural": 3,
    "existing_behavior": 4,
    "acceptance_criteria": 5,
}
_ORIGIN_ORDER = {"configured": 0, "parsed": 1, "inferred": 2}
_RELEVANCE_ORDER = {"primary": 0, "supporting": 1, "impacted": 2}

# Caps on the deterministic graph reads behind the architecture, test and
# limitation sections. Hitting one is reported in `unresolved`.
MAX_GROUNDED_FILES = 20
MAX_EDGES_LISTED_PER_FILE = 12
RETRIEVAL_LIMIT = 40
MAX_SEED_SYMBOLS = 48


# ------------------------------------------------------------- data shapes


@dataclass
class Candidate:
    """One item before budgeting. `ref` is a short, content-free handle for
    the manifest (an id, or `path:start-end`), so recording what was dropped
    never copies repository text into a second place."""

    item: ContextItem
    ref: str


@dataclass
class PackageInputs:
    """Everything assembly read from the database, already shaped into
    candidates in priority order per section. Pure data: `plan_package`
    budgets it without touching the database."""

    task: Task
    sections: dict[str, list[Candidate]]
    retrieval: dict = field(default_factory=dict)
    # Candidates the retrieval pipeline itself dropped for its budget/limit
    # before they ever became candidates. Counted into the code section.
    retrieval_dropped: int = 0
    unresolved: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class PackagePlan:
    items: list[ContextItem]
    manifest: dict
    unresolved: list[dict]
    status: ContextPackageStatus
    token_count: int


@dataclass(frozen=True)
class AssembledPackage:
    package: ContextPackage
    items: list[ContextItem]


@dataclass(frozen=True)
class PackageFreshness:
    """Whether a stored package may be served as the task's current one.
    `reason` says why not, when it may not."""

    package: ContextPackage
    current: bool
    reason: str | None = None


# ------------------------------------------------------------- the budget


def _tokens(item: ContextItem) -> int:
    return item.token_count


def plan_package(
    inputs: PackageInputs,
    token_budget: int,
    allocation: Mapping[str, float] | None = None,
) -> PackagePlan:
    """Order, budget and number the candidates. Pure: no I/O.

    The fixed head (intent + constraints) is always taken whole. The tail is
    `max(0, budget - head)`, split by `allocation`; a section takes its
    candidates in order while they fit, skips (and records) the ones that do
    not, and passes its unused tokens to the next section."""
    shares = dict(allocation or DEFAULT_ALLOCATION)
    total_share = sum(shares.get(k, 0.0) for k in TAIL_SECTIONS) or 1.0
    unresolved = list(inputs.unresolved)
    status: ContextPackageStatus = "assembled"

    items: list[ContextItem] = []
    sections_manifest: list[dict] = []
    head_tokens = 0
    for kind in FIXED_SECTIONS:
        cands = inputs.sections.get(kind, [])
        used = sum(_tokens(c.item) for c in cands)
        head_tokens += used
        items.extend(c.item for c in cands)
        sections_manifest.append(
            {
                "kind": kind,
                "truncatable": False,
                "allocated": None,
                "used": used,
                "included": len(cands),
                "dropped": 0,
                "dropped_items": [],
            }
        )

    if head_tokens > token_budget:
        constraints = inputs.sections.get("constraint", [])
        unresolved.append(
            {
                "kind": "constraints_exceed_budget",
                "detail": (
                    "task intent and constraints alone need more tokens than the budget; "
                    "none were truncated, and the rest of the package received no budget"
                ),
                "fixed_tokens": head_tokens,
                "token_budget": token_budget,
                "constraint_count": len(constraints),
                "hard_constraint_count": sum(1 for c in constraints if _severity(c.item) == "hard"),
            }
        )
        status = "invalid"

    tail = max(0, token_budget - head_tokens)
    carry = 0
    tail_used = 0
    for index, kind in enumerate(TAIL_SECTIONS):
        last = index == len(TAIL_SECTIONS) - 1
        base = math.floor(tail * shares.get(kind, 0.0) / total_share)
        allocated = (tail - tail_used) if last else base + carry
        used = 0
        included = 0
        dropped: list[dict] = []
        for cand in inputs.sections.get(kind, []):
            cost = _tokens(cand.item)
            if used + cost <= allocated:
                items.append(cand.item)
                used += cost
                included += 1
            else:
                dropped.append({"ref": cand.ref, "tokens": cost})
        entry: dict = {
            "kind": kind,
            "truncatable": True,
            "allocated": allocated,
            "used": used,
            "included": included,
            "dropped": len(dropped),
            "dropped_items": dropped,
        }
        if kind == "code":
            entry["dropped_by_retrieval"] = inputs.retrieval_dropped
        sections_manifest.append(entry)
        carry = allocated - used
        tail_used += used

    for ordinal, item in enumerate(items):
        item.ordinal = ordinal

    total_dropped = sum(s["dropped"] for s in sections_manifest) + inputs.retrieval_dropped
    manifest = {
        "order": list(SECTION_ORDER),
        "budget": {
            "token_budget": token_budget,
            "fixed_tokens": head_tokens,
            "tail_budget": tail,
            "tail_used": tail_used,
            "allocation": {k: shares.get(k, 0.0) for k in TAIL_SECTIONS},
        },
        "sections": sections_manifest,
        "dropped_total": total_dropped,
    }
    return PackagePlan(
        items=items,
        manifest=manifest,
        unresolved=unresolved,
        status=status,
        token_count=head_tokens + tail_used,
    )


def _severity(item: ContextItem) -> str | None:
    for doc in item.evidence.documents:
        if doc.get("source") == "constraint_ledger":
            return doc.get("severity")
    return None


# ------------------------------------------------------------- candidates


def _item(
    task: Task,
    kind: ContextItemKind,
    content: str,
    origin: OriginTag,
    evidence: ContextEvidence | None = None,
    *,
    tokens: int | None = None,
) -> ContextItem:
    # Defence in depth: chunk text was redacted at scan time, but intent,
    # requirement and constraint statements are user text that never passed
    # the scanner, and a package is served over the API and MCP as well as
    # exported. Same detector as ingestion.
    content = scrub_secrets(content)
    return ContextItem(
        task_id=task.id,  # pyright: ignore[reportArgumentType]
        repo_id=task.repo_id,
        commit_sha=task.commit_sha,
        kind=kind,
        content=content,
        origin=origin,
        evidence=evidence or ContextEvidence(),
        token_count=tokens if tokens is not None else estimate_tokens(content),
        analyzer=ANALYZER,
        analyzer_version=ANALYZER_VERSION,
    )


def _str_docs(raw: Mapping[str, object]) -> dict[str, str]:
    """`ContextEvidence.documents` holds str->str; stringify a free-form ref
    without dropping keys."""
    return {str(k): "" if v is None else str(v) for k, v in raw.items()}


def _span_ref(span: Citation) -> str:
    return f"{span.path}:{span.line_start}-{span.line_end}"


def intent_candidates(task: Task) -> list[Candidate]:
    content = task.title if not task.intent else f"{task.title}\n\n{task.intent}"
    # The user's verbatim statement of the task. Recorded as `configured`
    # for the same reason a user-stated constraint is: an explicit human
    # statement about this repository, not a parse and not a model's output.
    return [
        Candidate(
            _item(
                task,
                "task_intent",
                content,
                "configured",
                ContextEvidence(
                    documents=[{"source": "text", "ref": "task.intent", "task_id": str(task.id)}]
                ),
            ),
            ref=f"task:{task.id}",
        )
    ]


def constraint_candidates(task: Task, constraints: Sequence[Constraint]) -> list[Candidate]:
    """Hard before soft, then confirmed before unconfirmed; the ledger's own
    order is kept within that (stable sort)."""
    ordered = sorted(constraints, key=lambda c: (c.severity != "hard", not c.confirmed))
    out: list[Candidate] = []
    for c in ordered:
        ledger = _str_docs(
            {
                "source": "constraint_ledger",
                "ref": c.id,
                "kind": c.kind,
                "scope": c.scope,
                "severity": c.severity,
                "confirmed": "true" if c.confirmed else "false",
                "constraint_source": c.source,
            }
        )
        out.append(
            Candidate(
                _item(
                    task,
                    "constraint",
                    c.statement,
                    c.origin,
                    ContextEvidence(
                        spans=c.evidence.spans,
                        node_ids=c.evidence.node_ids,
                        chunk_ids=c.evidence.chunk_ids,
                        documents=[ledger, *c.evidence.documents],
                        notes=c.rationale,
                        truncated=c.evidence.truncated,
                    ),
                ),
                ref=f"constraint:{c.id}",
            )
        )
    return out


def _requirement_doc(r: Requirement) -> dict[str, str]:
    return _str_docs(
        {
            "source": "requirement",
            "ref": r.id,
            "kind": r.kind,
            "status": r.status,
            "priority": r.priority,
        }
    )


def requirement_candidates(task: Task, requirements: Sequence[Requirement]) -> list[Candidate]:
    rows = [r for r in requirements if r.status != "rejected" and r.kind != "open_question"]
    rows.sort(key=lambda r: (_KIND_ORDER.get(r.kind, 9), r.priority != "must", r.ordinal))
    out: list[Candidate] = []
    for r in rows:
        docs = [_requirement_doc(r)]
        if r.source_ref:
            docs.append(_str_docs(r.source_ref))
        out.append(
            Candidate(
                _item(
                    task,
                    "requirement",
                    r.statement,
                    r.origin,
                    ContextEvidence(
                        spans=r.evidence.spans,
                        node_ids=r.evidence.node_ids,
                        chunk_ids=r.evidence.chunk_ids,
                        documents=[*docs, *r.evidence.documents],
                        notes=r.question,
                    ),
                ),
                ref=f"requirement:{r.id}",
            )
        )
    return out


def code_candidates(
    task: Task, items: Sequence[FusedChunk], file_relevance: Mapping[str, str]
) -> list[Candidate]:
    """Retrieved chunks, chunks of the task's own files first (primary, then
    supporting), fusion order kept within each group (stable sort)."""
    ordered = sorted(
        items, key=lambda f: _RELEVANCE_ORDER.get(file_relevance.get(f.chunk.path, ""), 3)
    )
    out: list[Candidate] = []
    for fused in ordered:
        span = fused.citation()
        chunk = fused.chunk
        out.append(
            Candidate(
                _item(
                    task,
                    "code",
                    chunk.content,
                    "parsed",
                    ContextEvidence(
                        spans=[span],
                        node_ids=[chunk.node_id] if chunk.node_id is not None else [],
                        chunk_ids=[chunk.id] if chunk.id is not None else [],
                        notes="retrieved by " + "+".join(fused.stages),
                    ),
                    tokens=chunk_tokens(chunk),
                ),
                ref=_span_ref(span),
            )
        )
    return out


def convention_candidates(task: Task, conventions: Iterable) -> list[Candidate]:
    rows = sorted(conventions, key=lambda c: (_ORIGIN_ORDER.get(c.origin, 9), c.category, c.rule))
    out: list[Candidate] = []
    for conv in rows:
        docs = [
            _str_docs(
                {
                    "source": "convention",
                    "ref": conv.id,
                    "category": conv.category,
                    "confidence": conv.confidence,
                }
            )
        ]
        cfg = conv.evidence.get("source") if isinstance(conv.evidence, dict) else None
        if isinstance(cfg, str) and cfg:
            docs.append({"source": "repo_doc", "ref": cfg})
        out.append(
            Candidate(
                _item(task, "convention", conv.rule, conv.origin, ContextEvidence(documents=docs)),
                ref=f"convention:{conv.id}",
            )
        )
    return out


# ------------------------------------------------- graph-derived sections


@dataclass
class _Grounding:
    """The task's grounded files at the active snapshot, read from
    `task_components` (never from client state)."""

    components: list[TaskComponent]
    files: dict[int, Node]  # file node id -> node, for primary/supporting rows
    impacted: dict[int, Node]
    relevance: dict[str, str]  # path -> relevance


def _load_grounding(
    components: Sequence[TaskComponent], files_by_id: Mapping[int, Node]
) -> _Grounding:
    files: dict[int, Node] = {}
    impacted: dict[int, Node] = {}
    relevance: dict[str, str] = {}
    for row in sorted(components, key=lambda r: (_RELEVANCE_ORDER[r.relevance], r.node_id)):
        node = files_by_id.get(row.node_id)
        if node is None or node.path is None:
            continue  # a directory row, or a node that is not a file
        if row.relevance == "impacted":
            impacted[row.node_id] = node
        else:
            files[row.node_id] = node
        relevance.setdefault(node.path, row.relevance)
    return _Grounding(list(components), files, impacted, relevance)


class _ClusterIndex:
    """node id -> its cluster node, via reverse `belongs_to` edges and the
    shared BFS. Cached, because the architecture slice asks about the same
    neighbours repeatedly."""

    def __init__(self, conn: psycopg.Connection, repo_id: str, commit_sha: str) -> None:
        self._conn, self._repo, self._sha = conn, repo_id, commit_sha
        self._cache: dict[int, Node | None] = {}

    def of(self, node_id: int) -> Node | None:
        if node_id not in self._cache:
            nodes, _edges, _t = neighborhood(
                self._conn,
                self._repo,
                self._sha,
                node_id,
                depth=1,
                kinds=["belongs_to"],
                direction="reverse",
            )
            clusters = sorted(
                (n for n in nodes if n.kind == "cluster" and n.id is not None),
                key=lambda n: n.id or 0,
            )
            self._cache[node_id] = clusters[0] if clusters else None
        return self._cache[node_id]


def architecture_candidates(
    conn: psycopg.Connection, task: Task, grounding: _Grounding
) -> list[Candidate]:
    """One item per component cluster that contains a grounded file: the
    cluster (an `inferred`, algorithmic grouping — its origin column says so
    and the item keeps it), the task's files in it with their layer, and the
    neighbouring clusters reached by one hop of the dependency edges."""
    index = _ClusterIndex(conn, task.repo_id, task.commit_sha)
    by_cluster: dict[int, tuple[Node, list[Node]]] = {}
    unclustered: list[Node] = []
    grounded = list(grounding.files.values())[:MAX_GROUNDED_FILES]
    for node in grounded:
        assert node.id is not None
        cluster = index.of(node.id)
        if cluster is None or cluster.id is None:
            unclustered.append(node)
            continue
        by_cluster.setdefault(cluster.id, (cluster, []))[1].append(node)

    def layer(n: Node) -> str:
        value = n.attrs.get("layer")
        if not value:
            return "layer unclassified"
        return f"layer {value} ({n.attrs.get('layer_origin', 'parsed')})"

    out: list[Candidate] = []

    # Clusters holding a primary file first, then by id: deterministic.
    def cluster_rank(item: tuple[int, tuple[Node, list[Node]]]) -> tuple[int, int]:
        best = min(
            _RELEVANCE_ORDER.get(grounding.relevance.get(f.path or "", ""), 3) for f in item[1][1]
        )
        return best, item[0]

    for cluster_id, (cluster, members) in sorted(by_cluster.items(), key=cluster_rank):
        depends_on: dict[str, set[str]] = {}
        depended_by: dict[str, set[str]] = {}
        neighbor_ids: list[int] = [cluster_id]
        for member in members:
            assert member.id is not None
            for direction, sink in (("forward", depends_on), ("reverse", depended_by)):
                nodes, edges, _t = neighborhood(
                    conn,
                    task.repo_id,
                    task.commit_sha,
                    member.id,
                    depth=1,
                    kinds=EXPANSION_EDGE_KINDS,
                    direction=direction,  # pyright: ignore[reportArgumentType]
                )
                for n in nodes:
                    if n.id is None or n.id == member.id:
                        continue
                    other = index.of(n.id)
                    if other is None or other.id is None or other.id == cluster_id:
                        continue
                    kinds = {
                        e.kind
                        for e in edges
                        if (e.dst_id == n.id if direction == "forward" else e.src_id == n.id)
                    }
                    sink.setdefault(other.name, set()).update(kinds)
                    neighbor_ids.append(other.id)
        lines = [
            f"Component {cluster.name} — algorithmic grouping (origin {cluster.origin}), "
            f"{cluster.attrs.get('member_count', '?')} members.",
            "Task files in this component:",
        ]
        for m in members:
            lines.append(f"- {m.path} [{grounding.relevance.get(m.path or '', '?')}; {layer(m)}]")
        if depends_on:
            lines.append(
                "Depends on: "
                + "; ".join(f"{k} via {'/'.join(sorted(v))}" for k, v in sorted(depends_on.items()))
            )
        if depended_by:
            lines.append(
                "Depended on by: "
                + "; ".join(
                    f"{k} via {'/'.join(sorted(v))}" for k, v in sorted(depended_by.items())
                )
            )
        out.append(
            Candidate(
                _item(
                    task,
                    "architecture",
                    "\n".join(lines),
                    cluster.origin,
                    ContextEvidence(
                        node_ids=sorted(
                            {*neighbor_ids, *(m.id for m in members if m.id is not None)}
                        ),
                        notes="cluster membership via belongs_to; neighbours via one hop of "
                        + "/".join(EXPANSION_EDGE_KINDS),
                    ),
                ),
                ref=f"cluster:{cluster_id}",
            )
        )
    if unclustered:
        lines = ["Task files not assigned to any component cluster:"]
        lines.extend(f"- {n.path} [{layer(n)}]" for n in unclustered)
        out.append(
            Candidate(
                _item(
                    task,
                    "architecture",
                    "\n".join(lines),
                    "parsed",
                    ContextEvidence(
                        node_ids=[n.id for n in unclustered if n.id is not None],
                        notes="no belongs_to edge: clustering found no relationship structure",
                    ),
                ),
                ref="cluster:none",
            )
        )
    if grounding.impacted:
        lines = ["Files that depend on the task's files (may break):"]
        lines.extend(f"- {n.path} [{layer(n)}]" for n in grounding.impacted.values())
        out.append(
            Candidate(
                _item(
                    task,
                    "architecture",
                    "\n".join(lines),
                    "parsed",
                    ContextEvidence(
                        node_ids=[n.id for n in grounding.impacted.values() if n.id is not None],
                        notes="task_components rows with relevance=impacted",
                    ),
                ),
                ref="impacted",
            )
        )
    return out


def relevant_test_candidates(
    conn: psycopg.Connection,
    task: Task,
    grounding: _Grounding,
    files_by_id: Mapping[int, Node],
) -> tuple[list[Candidate], list[Candidate]]:
    """`tests` edges touching the grounded files: tests *of* a grounded file
    (reverse), and the target of a grounded file that is itself a test
    (forward). Returns `(test candidates, limitation candidates)` — a test
    file with no stored blob cannot be cited by line, so it becomes a stated
    limitation instead of an uncited test item."""
    found: dict[tuple[int, int], tuple[str, str, OriginTag]] = {}
    for node in list(grounding.files.values())[:MAX_GROUNDED_FILES]:
        assert node.id is not None
        for direction in ("reverse", "forward"):
            _nodes, edges, _t = neighborhood(
                conn,
                task.repo_id,
                task.commit_sha,
                node.id,
                depth=1,
                kinds=["tests"],
                direction=direction,  # pyright: ignore[reportArgumentType]
            )
            for e in edges:
                if e.dst_id is None:
                    continue
                src, dst = files_by_id.get(e.src_id), files_by_id.get(e.dst_id)
                if src is None or dst is None or not src.path or not dst.path:
                    continue
                found[(e.src_id, e.dst_id)] = (
                    e.resolution_status or "unknown",
                    str(e.attrs.get("evidence", "")),
                    e.origin,
                )
    paths = sorted({files_by_id[s].path or "" for s, _d in found})
    lines = file_line_counts(conn, task.repo_id, task.commit_sha, paths)
    tests: list[Candidate] = []
    limits: list[Candidate] = []

    def rank(key: tuple[int, int]) -> tuple[int, str]:
        status = found[key][0]
        return (0 if status == "resolved" else 1, files_by_id[key[0]].path or "")

    for key in sorted(found, key=rank):
        status, ev, origin = found[key]
        src, dst = files_by_id[key[0]], files_by_id[key[1]]
        assert src.path is not None and dst.path is not None
        statement = (
            f"{src.path} tests {dst.path} (association {status}"
            + (f"; evidence {ev}" if ev else "")
            + ")"
        )
        count = lines.get(src.path)
        if not count:
            limits.append(
                Candidate(
                    _item(
                        task,
                        "limitation",
                        statement + " — test file has no stored source, so it cannot be cited",
                        "parsed",
                        ContextEvidence(node_ids=[key[0], key[1]]),
                    ),
                    ref=f"test:{src.path}",
                )
            )
            continue
        span = Citation(path=src.path, line_start=1, line_end=count, node_id=key[0])
        tests.append(
            Candidate(
                _item(
                    task,
                    "test",
                    statement,
                    origin,
                    ContextEvidence(spans=[span], node_ids=[key[0], key[1]]),
                ),
                ref=_span_ref(span),
            )
        )
    return tests, limits


def limitation_candidates(
    conn: psycopg.Connection,
    task: Task,
    grounding: _Grounding,
) -> list[Candidate]:
    """What the analyzers could not establish, near the task and repo-wide
    (Part 0.3: unresolved is shown, not hidden)."""
    out: list[Candidate] = []
    kinds = [*EXPANSION_EDGE_KINDS]
    for node in list(grounding.files.values())[:MAX_GROUNDED_FILES]:
        assert node.id is not None and node.path is not None
        _nodes, edges, _t = neighborhood(
            conn, task.repo_id, task.commit_sha, node.id, depth=1, kinds=kinds, direction="forward"
        )
        gaps = sorted(
            (
                e
                for e in edges
                if e.src_id == node.id and e.resolution_status in ("unresolved", "ambiguous")
            ),
            key=lambda e: (e.line_start or 0, e.kind, e.id or 0),
        )
        if not gaps:
            continue
        shown = gaps[:MAX_EDGES_LISTED_PER_FILE]
        lines = [f"Relationships from {node.path} the analyzer could not resolve with certainty:"]
        for e in shown:
            name = e.attrs.get("callee_name") or e.attrs.get("raw") or e.attrs.get("name") or "?"
            where = f"line {e.line_start}" if e.line_start else "line unknown"
            why = f" ({e.reason})" if e.reason else ""
            lines.append(f"- {where}: {e.kind} {name} is {e.resolution_status}{why}")
        if len(gaps) > len(shown):
            lines.append(f"- ... and {len(gaps) - len(shown)} more")
        spans = [
            Citation(path=node.path, line_start=e.line_start, line_end=e.line_start)
            for e in shown
            if e.line_start
        ]
        out.append(
            Candidate(
                _item(
                    task,
                    "limitation",
                    "\n".join(lines),
                    "parsed",
                    ContextEvidence(spans=spans, node_ids=[node.id]),
                ),
                ref=f"edges:{node.path}",
            )
        )

    for cov in sorted(
        list_coverage(conn, task.repo_id, task.commit_sha), key=lambda c: (c.area, c.metric)
    ):
        if cov.resolved >= cov.total and not cov.blockers:
            continue
        blockers = ", ".join(f"{k}={v}" for k, v in sorted(cov.blockers.items()))
        out.append(
            Candidate(
                _item(
                    task,
                    "limitation",
                    f"Coverage {cov.metric} at {cov.area}: {cov.resolved}/{cov.total}"
                    + (f" (blockers: {blockers})" if blockers else ""),
                    "parsed",
                    ContextEvidence(notes="codeaxis.coverage"),
                ),
                ref=f"coverage:{cov.area}:{cov.metric}",
            )
        )

    gaps = [
        row
        for row in unresolved_summary(conn, task.repo_id, task.commit_sha)
        if row["resolution_status"] in ("unresolved", "ambiguous")
    ]
    if gaps:
        out.append(
            Candidate(
                _item(
                    task,
                    "limitation",
                    "Repo-wide relationships not resolved with certainty: "
                    + ", ".join(f"{r['kind']} {r['resolution_status']} {r['count']}" for r in gaps),
                    "parsed",
                    ContextEvidence(notes="edge resolution_status breakdown"),
                ),
                ref="edges:repo",
            )
        )
    return out


def open_question_candidates(
    task: Task,
    requirements: Sequence[Requirement],
    decisions: Iterable,
    literals: LiteralMatches,
) -> tuple[list[Candidate], list[dict]]:
    """Questions carried into the package rather than answered by guessing
    (Part 4.F.3). Returns `(candidates, unresolved entries)`."""
    out: list[Candidate] = []
    unresolved: list[dict] = []
    for r in sorted(requirements, key=lambda r: (r.kind != "open_question", r.ordinal)):
        if r.status == "rejected":
            continue
        if r.kind == "open_question":
            text = r.statement
        elif r.status in ("ambiguous", "unresolved") and r.question:
            text = r.question
        else:
            continue
        out.append(
            Candidate(
                _item(
                    task,
                    "open_question",
                    text,
                    r.origin,
                    ContextEvidence(documents=[_requirement_doc(r)], spans=r.evidence.spans),
                ),
                ref=f"requirement:{r.id}",
            )
        )
        unresolved.append(
            {
                "kind": "open_question" if r.kind == "open_question" else f"{r.status}_requirement",
                "ref": f"requirement:{r.id}",
            }
        )
    for d in decisions:
        if d.status != "open":
            continue
        out.append(
            Candidate(
                _item(
                    task,
                    "open_question",
                    d.question,
                    d.origin,
                    ContextEvidence(documents=[{"source": "decision", "ref": str(d.id)}]),
                ),
                ref=f"decision:{d.id}",
            )
        )
        unresolved.append({"kind": "open_decision", "ref": f"decision:{d.id}"})

    sha = task.commit_sha
    for token in literals.unmatched_paths:
        out.append(
            Candidate(
                _item(
                    task,
                    "open_question",
                    f"The task names {token}, which is not a file or directory at commit {sha}.",
                    "parsed",
                    ContextEvidence(documents=[{"source": "text", "ref": "task", "match": token}]),
                ),
                ref=f"path:{token}",
            )
        )
        unresolved.append({"kind": "unmatched_path", "ref": token})
    for token, candidates in sorted(literals.ambiguous_paths.items()):
        out.append(
            Candidate(
                _item(
                    task,
                    "open_question",
                    f"The task names {token}, which matches {len(candidates)} files: "
                    + ", ".join(candidates),
                    "parsed",
                    ContextEvidence(documents=[{"source": "text", "ref": "task", "match": token}]),
                ),
                ref=f"path:{token}",
            )
        )
        unresolved.append({"kind": "ambiguous_path", "ref": token, "candidates": candidates})
    for symbol, paths in sorted(literals.ambiguous_symbols.items()):
        out.append(
            Candidate(
                _item(
                    task,
                    "open_question",
                    f"The task names {symbol}, which is defined in {len(paths)} files: "
                    + ", ".join(paths),
                    "parsed",
                    ContextEvidence(documents=[{"source": "text", "ref": "task", "match": symbol}]),
                ),
                ref=f"symbol:{symbol}",
            )
        )
        unresolved.append({"kind": "ambiguous_symbol", "ref": symbol, "candidates": paths})
    return out, unresolved


# ------------------------------------------------------------- gathering


def _drop_uncitable(
    conn: psycopg.Connection, task: Task, sections: dict[str, list[Candidate]]
) -> list[dict]:
    """Defensive: every span on a `code`/`test` item is re-checked against
    the snapshot with the same validator chat answers go through. A span
    that does not resolve removes its item (and is reported) — an agent must
    never be handed a citation to a line that is not there."""
    paths = {
        s.path
        for kind in ("code", "test")
        for c in sections.get(kind, [])
        for s in c.item.evidence.spans
    }
    if not paths:
        return []
    index = DbSnapshotIndex(conn, task.repo_id, task.commit_sha, paths)
    rejected: list[dict] = []
    for kind in ("code", "test"):
        kept: list[Candidate] = []
        for c in sections.get(kind, []):
            failures = [f for s in c.item.evidence.spans if (f := check_citation(index, s))]
            if failures:
                rejected.append(
                    {"kind": "uncitable_item_removed", "ref": c.ref, "reason": failures[0].reason}
                )
            else:
                kept.append(c)
        sections[kind] = kept
    return rejected


def gather_inputs(
    conn: psycopg.Connection,
    task: Task,
    *,
    token_budget: int,
    embedder: QueryEmbedder | None = None,
    literals: LiteralMatches | None = None,
) -> PackageInputs:
    """Read every section's candidates for `task` at its active snapshot.
    Read-only: safe on `api_connection` (assembly's writes happen after)."""
    assert task.id is not None
    repo_id, sha = task.repo_id, task.commit_sha
    unresolved: list[dict] = []
    sections: dict[str, list[Candidate]] = {k: [] for k in SECTION_ORDER}

    sections["task_intent"] = intent_candidates(task)

    constraints = list_constraints(conn, repo_id, sha, task_id=task.id, limit=MAX_LIST_LIMIT)
    if len(constraints) >= MAX_LIST_LIMIT:
        # The read cap was hit, so the ledger may hold more. Constraints may
        # not be silently truncated, so this is a stated gap.
        unresolved.append(
            {"kind": "constraint_read_capped", "detail": f"ledger read capped at {MAX_LIST_LIMIT}"}
        )
    sections["constraint"] = constraint_candidates(task, constraints)

    all_requirements = list_requirements(conn, task.id, limit=MAX_LIST_LIMIT)
    requirements = [r for r in all_requirements if r.commit_sha == sha]
    for r in all_requirements:
        if r.commit_sha != sha and r.status != "rejected":
            unresolved.append(
                {
                    "kind": "requirement_pinned_to_older_commit",
                    "ref": f"requirement:{r.id}",
                    "commit_sha": r.commit_sha,
                }
            )
    sections["requirement"] = requirement_candidates(task, requirements)

    files = list_nodes(conn, repo_id, sha, kind="file")
    files_by_id = {n.id: n for n in files if n.id is not None}
    grounding = _load_grounding(
        list_task_components(conn, task.id, commit_sha=sha, limit=MAX_LIST_LIMIT), files_by_id
    )
    if not grounding.files:
        unresolved.append(
            {
                "kind": "no_grounded_components",
                "detail": "no file in this snapshot is linked to the task; "
                "code evidence comes from retrieval over the task text only",
            }
        )
    if len(grounding.files) > MAX_GROUNDED_FILES:
        unresolved.append(
            {
                "kind": "grounding_capped",
                "detail": f"graph sections read the first {MAX_GROUNDED_FILES} of "
                f"{len(grounding.files)} grounded files",
            }
        )

    sections["architecture"] = architecture_candidates(conn, task, grounding)

    # -- code: Pipeline E, seeded by the requirements text + the task's files
    text = task_text(task, [r.statement for r in requirements if r.status != "rejected"])
    seeds: list[str] = list(identifier_candidates(text, limit=24))
    for node in list(grounding.files.values())[:MAX_GROUNDED_FILES]:
        for sym in list_nodes(conn, repo_id, sha, kind="symbol", path=node.path):
            if sym.name not in seeds:
                seeds.append(sym.name)
    seeds = seeds[:MAX_SEED_SYMBOLS]
    query = RetrievalQuery(
        text=text,
        symbols=tuple(seeds),
        limit=RETRIEVAL_LIMIT,
        token_budget=max(1, min(token_budget, MAX_PACKAGE_BUDGET)),
        hops=1,
    )
    result = retrieve(conn, repo_id, sha, query, embedder=embedder)
    sections["code"] = code_candidates(task, result.items, grounding.relevance)
    retrieval = {
        "pipeline": "app.analysis.semantic.retrieve",
        "seed_symbols": len(seeds),
        "grounded_files": len(grounding.files),
        "stages": [s.as_dict() for s in result.stages],
        "token_budget": result.token_budget,
        "tokens_used": result.tokens_used,
        "fused_candidates": result.fused_candidates,
        "dropped_for_budget": result.dropped_for_budget,
    }
    if not result.items:
        unresolved.append(
            {"kind": "no_code_evidence", "detail": "retrieval returned no chunk for this task"}
        )

    sections["convention"] = convention_candidates(task, list_conventions(conn, repo_id, sha))

    tests, untestable = relevant_test_candidates(conn, task, grounding, files_by_id)
    sections["test"] = tests
    sections["limitation"] = [*limitation_candidates(conn, task, grounding), *untestable]

    if literals is None:
        literals = literal_matches(conn, repo_id, sha, text)
    questions, q_unresolved = open_question_candidates(
        task, requirements, list_decisions(conn, task.id, status="open"), literals
    )
    sections["open_question"] = questions
    unresolved.extend(q_unresolved)

    unresolved.extend(_drop_uncitable(conn, task, sections))
    return PackageInputs(
        task=task,
        sections=sections,
        retrieval=retrieval,
        retrieval_dropped=result.dropped_for_budget,
        unresolved=unresolved,
    )


# ------------------------------------------------------------- assembly


def assemble_context_package(
    conn: psycopg.Connection,
    task_id: str,
    *,
    token_budget: int = DEFAULT_PACKAGE_BUDGET,
    embedder: QueryEmbedder | None = None,
    allocation: Mapping[str, float] | None = None,
    reground_components: bool = False,
    extra_sections: Mapping[str, Sequence[Candidate]] | None = None,
    extra_unresolved: Sequence[dict] = (),
) -> AssembledPackage:
    """Assemble, persist and return a new package version for `task_id`.
    Needs `worker_connection`.

    `extra_sections` appends already-built candidates to named sections after
    the deterministic gather and before budgeting, so a caller with its own
    stored records to contribute — `app.blueprint.export` hands over a
    blueprint's per-component implementation context — gets this module's
    ordering, budgeting and never-truncate-constraints rule rather than a
    parallel assembler. Items arrive at the end of their section, so they are
    the first to be cut if that section runs out of budget.

    Grounding: the task's stored `task_components` at its active commit are
    used as-is; if there are none (or `reground_components=True`), the
    deterministic grounding pass (`app.context.grounding.ground_task`) runs
    first and writes them. Older packages pinned to another commit are
    marked `stale` before the new one is written."""
    if token_budget <= 0:
        raise ValueError("token_budget must be positive")
    task = get_task(conn, task_id)
    if task is None or task.id is None:
        raise LookupError(f"task {task_id} not found")

    mark_stale_if_snapshot_moved(conn, task.id)

    literals: LiteralMatches | None = None
    existing = list_task_components(conn, task.id, commit_sha=task.commit_sha, limit=1)
    if reground_components or not existing:
        literals = ground_task(conn, task.id, write=True).literals

    inputs = gather_inputs(
        conn, task, token_budget=token_budget, embedder=embedder, literals=literals
    )
    for kind, candidates in (extra_sections or {}).items():
        if kind not in SECTION_ORDER:
            raise ValueError(f"unknown context section {kind!r}; expected one of {SECTION_ORDER}")
        inputs.sections.setdefault(kind, []).extend(candidates)
    inputs.unresolved.extend(extra_unresolved)
    plan = plan_package(inputs, token_budget, allocation)

    package = create_context_package(
        conn,
        ContextPackage(
            task_id=task.id,
            repo_id=task.repo_id,
            commit_sha=task.commit_sha,
            version=next_package_version(conn, task.id),
            status=plan.status,
            token_budget=token_budget,
            token_count=plan.token_count,
            manifest=plan.manifest,
            retrieval=inputs.retrieval,
            unresolved=plan.unresolved,
            analyzer=ANALYZER,
            analyzer_version=ANALYZER_VERSION,
        ),
    )
    assert package.id is not None
    try:
        ids = replace_context_items(conn, task.id, plan.items, package_id=package.id)
    except Exception:
        # The package row exists but its content does not: it must not be
        # served as if it were complete.
        conn.rollback()
        set_context_package_status(conn, package.id, "invalid")
        raise
    for item, item_id in zip(plan.items, ids, strict=True):
        item.id = item_id
        item.package_id = package.id
    return AssembledPackage(package=package, items=plan.items)


# ------------------------------------------------------------- staleness


def _latest_input_change(
    requirements: Iterable[Requirement], constraints: Iterable[Constraint]
) -> datetime | None:
    stamps: list[datetime] = []
    for r in requirements:
        if r.created_at is not None:
            stamps.append(r.created_at)
    for c in constraints:
        stamps.extend(t for t in (c.created_at, c.confirmed_at) if t is not None)
    return max(stamps) if stamps else None


def package_staleness_reason(
    package: ContextPackage, task: Task, latest_input_change: datetime | None = None
) -> str | None:
    """Why `package` may not be served as `task`'s current package, or None.
    Pure. A package is stale when its snapshot is not the task's active one,
    or when a requirement/constraint it was built from changed after it."""
    if package.status == "stale":
        return "marked stale"
    if package.commit_sha != task.commit_sha:
        return (
            f"snapshot moved: package was assembled at {package.commit_sha}, "
            f"task is now grounded at {task.commit_sha}"
        )
    if (
        latest_input_change is not None
        and package.created_at is not None
        and latest_input_change > package.created_at
    ):
        return "requirements or constraints changed after this package was assembled"
    return None


def check_package(
    conn: psycopg.Connection, package: ContextPackage, task: Task | None = None
) -> PackageFreshness:
    """Read-only freshness check (safe on `api_connection`)."""
    task = task or get_task(conn, package.task_id)
    if task is None or task.id is None:
        return PackageFreshness(package, False, "task no longer exists")
    latest = _latest_input_change(
        list_requirements(conn, task.id, commit_sha=task.commit_sha, limit=MAX_LIST_LIMIT),
        list_constraints(
            conn, task.repo_id, task.commit_sha, task_id=task.id, limit=MAX_LIST_LIMIT
        ),
    )
    reason = package_staleness_reason(package, task, latest)
    return PackageFreshness(package, reason is None, reason)


def current_package(conn: psycopg.Connection, task_id: str) -> PackageFreshness | None:
    """The task's newest package and whether it may be served as current.
    Never returns a package from another commit as current."""
    task = get_task(conn, task_id)
    if task is None:
        return None
    packages = list_context_packages(conn, task_id, limit=1)
    if not packages:
        return None
    return check_package(conn, packages[0], task)


def mark_stale_if_snapshot_moved(
    conn: psycopg.Connection, task_id: str, *, include_input_changes: bool = True
) -> list[str]:
    """Mark every package of `task_id` that is no longer about the task's
    active snapshot `stale` (and, by default, every package whose
    requirements/constraints changed after it was assembled). Returns the ids
    newly marked. Needs `worker_connection`. Content is never edited — the
    status is the one permitted mutation (`set_context_package_status`)."""
    task = get_task(conn, task_id)
    if task is None or task.id is None:
        return []
    latest = None
    if include_input_changes:
        latest = _latest_input_change(
            list_requirements(conn, task.id, commit_sha=task.commit_sha, limit=MAX_LIST_LIMIT),
            list_constraints(
                conn, task.repo_id, task.commit_sha, task_id=task.id, limit=MAX_LIST_LIMIT
            ),
        )
    marked: list[str] = []
    for package in list_context_packages(conn, task.id, limit=MAX_LIST_LIMIT):
        if package.status == "stale" or package.id is None:
            continue
        if package_staleness_reason(package, task, latest) is not None:
            set_context_package_status(conn, package.id, "stale")
            marked.append(package.id)
    return marked
