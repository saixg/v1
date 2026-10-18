"""Pass 2b (part 1): hierarchical summaries, strictly bottom-up.

    symbol -> file -> directory -> component -> repo

**A parent summarizes its children's summaries, never raw code again.** Only
the symbol level ever sees source text (and only the chunk text the chunking
pass already stored, which is the secret-redacted copy -- see
`app.analysis.scan.secrets`). Every level above it is handed prose written at
the level below. That is the point of the hierarchy (build-plan.md Part 3,
"Patterns") and it is also the cost control: a 5,000-symbol repo costs ~5,000
cheap `bulk` calls plus a few hundred roll-ups, not 5,000 expensive ones.

Role routing (Part 3): symbol and file summaries use `bulk` (Haiku) because
there are many of them; directory summaries use `bulk` for the same reason;
component and repo summaries use `synth` (Sonnet) because there are few and
they are read first. Roles are addressed by name, never models.

Fan-out is hard-bounded by `SummaryBudget` -- a per-level cap *and* a total
model-call ceiling. Over-cap candidates are not summarized and the shortfall
is written to `codeaxis.coverage` as an Understanding-Gap counter, so a
missing summary is a visible gap rather than a silent one.

Nothing here is a source of fact. The model receives material the analyzers
produced and returns prose; the `evidence` stored next to that prose is built
from what this module *sent*, never parsed out of what came back.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

import psycopg
from pydantic import BaseModel, Field

from app.knowledge import list_chunks, upsert_coverage, upsert_summaries
from app.llm import (
    DEFAULT_ROUTES,
    CallContext,
    ModelService,
    ModelServiceError,
)
from app.models import (
    Chunk,
    Citation,
    Coverage,
    ModelRole,
    Summary,
    SummaryEvidence,
    SummaryLevel,
)

from .prompts import (
    COMPONENT_SYSTEM,
    DIRECTORY_SYSTEM,
    FILE_SYSTEM,
    REPO_SYSTEM,
    SYMBOL_SYSTEM,
    material_prompt,
)

logger = logging.getLogger(__name__)

ANALYZER = "semantic.summaries"
ANALYZER_VERSION = "0.1.0"

# Spans carried in one summary's evidence. A roll-up can descend from
# thousands of lines; the UI needs enough to link back, not all of them.
_MAX_SPANS = 50


class SummaryDraft(BaseModel):
    """The only thing a model is allowed to return here: prose.

    Deliberately *not* a place for citations, paths or symbol names. Evidence
    is assembled by the analyzer from the material it sent (Part 0.3: an LLM
    is never the source of a path, symbol or relationship).
    """

    summary: str = Field(min_length=1, max_length=1500)


@dataclass(frozen=True)
class SummaryBudget:
    """Hard caps on model fan-out for one repo snapshot.

    Defaults are deliberately modest: a first analysis of a large repository
    must not be able to spend an unbounded amount. Raising them is a config
    change, and the shortfall is always recorded in coverage either way.
    """

    max_symbol: int = 300
    max_file: int = 150
    max_directory: int = 50
    max_component: int = 25
    # Ceiling across every level, checked before each call.
    max_total_calls: int = 400
    # Child summaries (or chunks) placed in one prompt.
    max_children_per_prompt: int = 40
    # Characters of chunk text placed in one symbol prompt.
    max_chunk_chars: int = 8000
    # Consecutive provider failures after which the pass stops rather than
    # hammering a broken provider once per candidate.
    max_consecutive_failures: int = 3


@dataclass
class LevelStats:
    candidates: int = 0
    written: int = 0
    failed: int = 0
    capped: int = 0


@dataclass
class SummaryStats:
    calls: int = 0
    by_level: dict[str, LevelStats] = field(default_factory=dict)
    aborted_reason: str | None = None

    def level(self, name: str) -> LevelStats:
        return self.by_level.setdefault(name, LevelStats())


class _BudgetExhausted(Exception):
    """The total model-call ceiling was reached. Not an error: a stop."""


class _ProviderUnhealthy(Exception):
    """Too many consecutive provider failures; stop and fail visibly."""


def _routed_model(role: ModelRole) -> str | None:
    """The model the default routing table sends this role to, recorded on
    the row for traceability. `llm_calls` holds the authoritative per-attempt
    record including what the provider actually served."""
    route = DEFAULT_ROUTES.get(role)
    return None if route is None else route.model


def _parent_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _ancestors(path: str) -> list[str]:
    """Every directory containing `path`, deepest first, ending at ''."""
    out: list[str] = []
    current = _parent_dir(path)
    while True:
        out.append(current)
        if current == "":
            return out
        current = _parent_dir(current)


def _depth(dir_path: str) -> int:
    return 0 if dir_path == "" else dir_path.count("/") + 1


# --- symbol-level grouping ---------------------------------------------------


@dataclass
class _ChunkGroup:
    """All parts of one chunked symbol (or module block): the unit a symbol
    summary is written about."""

    path: str
    symbol: str | None
    line_start: int
    line_end: int
    node_id: int | None
    chunks: list[Chunk]

    @property
    def scope_key(self) -> str:
        name = self.symbol or "module"
        return f"{self.path}#{name}@{self.line_start}-{self.line_end}"

    @property
    def token_count(self) -> int:
        return sum(c.token_count for c in self.chunks)


def group_chunks(chunks: list[Chunk]) -> list[_ChunkGroup]:
    """Group chunks into one group per symbol span, parts in order."""
    buckets: dict[tuple[str, str | None, int, int], _ChunkGroup] = {}
    for chunk in chunks:
        key = (chunk.path, chunk.symbol, chunk.line_start, chunk.line_end)
        group = buckets.get(key)
        if group is None:
            group = _ChunkGroup(
                path=chunk.path,
                symbol=chunk.symbol,
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                node_id=chunk.node_id,
                chunks=[],
            )
            buckets[key] = group
        group.chunks.append(chunk)
    for group in buckets.values():
        group.chunks.sort(key=lambda c: (c.part_index, c.id or 0))
    return list(buckets.values())


# --- the driver --------------------------------------------------------------


class _Summarizer:
    def __init__(
        self,
        conn: psycopg.Connection,
        repo_id: str,
        commit_sha: str,
        service: ModelService,
        budget: SummaryBudget,
        context: CallContext | None,
    ) -> None:
        self._conn = conn
        self._repo_id = repo_id
        self._commit_sha = commit_sha
        self._service = service
        self._budget = budget
        self._context = context
        self.stats = SummaryStats()
        self._consecutive_failures = 0
        # level -> scope_key -> Summary, for the next level up to read.
        self._written: dict[str, dict[str, Summary]] = defaultdict(dict)
        # symbol scope_key -> owning file path. Kept explicitly rather than
        # re-parsed out of the scope_key, which would mis-split a path that
        # happens to contain '#'.
        self._symbol_path: dict[str, str] = {}

    # -- one model call --

    def _summarize(
        self,
        *,
        level: SummaryLevel,
        role: ModelRole,
        system: str,
        blocks: list[str],
        scope_key: str,
        node_id: int | None,
        evidence: SummaryEvidence,
    ) -> Summary | None:
        stats = self.stats.level(level)
        if self.stats.calls >= self._budget.max_total_calls:
            raise _BudgetExhausted
        prompt = material_prompt(system, blocks)
        self.stats.calls += 1
        try:
            draft = self._service.generate_structured(
                SummaryDraft, prompt, role, context=self._context
            )
        except ModelServiceError as exc:
            # generate_structured already applied the one-retry-then-fail
            # rule and recorded every attempt in llm_calls. One candidate
            # failing is a gap, not a pass failure -- but a run of them means
            # the provider is unhealthy and we stop instead of fanning out.
            stats.failed += 1
            self._consecutive_failures += 1
            logger.warning("%s summary failed for %r: %s", level, scope_key, exc)
            if self._consecutive_failures >= self._budget.max_consecutive_failures:
                raise _ProviderUnhealthy(str(exc)) from exc
            return None
        self._consecutive_failures = 0
        summary = Summary(
            repo_id=self._repo_id,
            commit_sha=self._commit_sha,
            level=level,
            node_id=node_id,
            scope_key=scope_key,
            summary=draft.summary.strip(),
            origin="inferred",
            evidence=evidence,
            model=_routed_model(role),
            analyzer=ANALYZER,
            analyzer_version=ANALYZER_VERSION,
        )
        stats.written += 1
        self._written[level][scope_key] = summary
        return summary

    # -- levels --

    def symbols(self, groups: list[_ChunkGroup]) -> list[Summary]:
        stats = self.stats.level("symbol")
        stats.candidates = len(groups)
        # Deterministic and value-ordered: biggest symbols first, ties broken
        # by location so two runs over the same snapshot pick the same set.
        ordered = sorted(groups, key=lambda g: (-g.token_count, g.path, g.line_start))
        selected = ordered[: self._budget.max_symbol]
        stats.capped = len(ordered) - len(selected)

        out: list[Summary] = []
        for group in selected:
            text = "\n".join(c.content for c in group.chunks)
            truncated = len(text) > self._budget.max_chunk_chars
            if truncated:
                text = text[: self._budget.max_chunk_chars]
            block = (
                f"File: {group.path}\n"
                f"Symbol: {group.symbol or '(module-level code)'}\n"
                f"Lines {group.line_start}-{group.line_end}\n"
                f"Source:\n{text}"
            )
            evidence = SummaryEvidence(
                input="chunks",
                chunk_ids=[c.id for c in group.chunks if c.id is not None],
                node_ids=[group.node_id] if group.node_id is not None else [],
                spans=[
                    Citation(
                        path=group.path,
                        line_start=group.line_start,
                        line_end=group.line_end,
                        symbol=group.symbol,
                        node_id=group.node_id,
                        chunk_id=group.chunks[0].id,
                    )
                ],
                truncated=truncated,
            )
            summary = self._summarize(
                level="symbol",
                role="bulk",
                system=SYMBOL_SYSTEM,
                blocks=[block],
                scope_key=group.scope_key,
                node_id=group.node_id,
                evidence=evidence,
            )
            if summary is not None:
                self._symbol_path[summary.scope_key] = group.path
                out.append(summary)
        return out

    def _rollup_evidence(
        self, children: list[Summary], level_names: list[str], truncated: bool
    ) -> SummaryEvidence:
        spans: list[Citation] = []
        chunk_ids: list[int] = []
        node_ids: list[int] = []
        for child in children:
            spans.extend(child.evidence.spans)
            chunk_ids.extend(child.evidence.chunk_ids)
            if child.node_id is not None:
                node_ids.append(child.node_id)
        return SummaryEvidence(
            input="child_summaries",
            chunk_ids=sorted(set(chunk_ids))[:_MAX_SPANS],
            node_ids=sorted(set(node_ids))[:_MAX_SPANS],
            spans=spans[:_MAX_SPANS],
            children=[
                {"level": lvl, "scope_key": child.scope_key}
                for lvl, child in zip(level_names, children, strict=True)
            ],
            truncated=truncated or len(spans) > _MAX_SPANS,
        )

    def _rollup(
        self,
        *,
        level: SummaryLevel,
        role: ModelRole,
        system: str,
        scope_key: str,
        node_id: int | None,
        header: str,
        children: list[Summary],
        file_nodes: dict[str, int] | None = None,
    ) -> Summary | None:
        """One parent summary written from `children`'s summaries only."""
        capped = children[: self._budget.max_children_per_prompt]
        blocks = [header]
        level_names: list[str] = []
        for child in capped:
            label = {
                "symbol": "Symbol",
                "file": "File",
                "directory": "Directory",
                "component": "Component",
            }.get(child.level, child.level.title())
            blocks.append(f"{label} {child.scope_key}:\n{child.summary}")
            level_names.append(child.level)
        evidence = self._rollup_evidence(capped, level_names, len(capped) < len(children))
        if node_id is None and file_nodes is not None:
            node_id = file_nodes.get(scope_key)
        return self._summarize(
            level=level,
            role=role,
            system=system,
            blocks=blocks,
            scope_key=scope_key,
            node_id=node_id,
            evidence=evidence,
        )

    def files(self, file_node_ids: dict[str, int]) -> None:
        by_path: dict[str, list[Summary]] = defaultdict(list)
        for summary in self._written["symbol"].values():
            path = self._symbol_path.get(summary.scope_key)
            if path is not None:
                by_path[path].append(summary)

        stats = self.stats.level("file")
        stats.candidates = len(by_path)
        ordered = sorted(by_path.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        selected = ordered[: self._budget.max_file]
        stats.capped = len(ordered) - len(selected)

        for path, children in selected:
            children.sort(key=lambda s: s.evidence.spans[0].line_start if s.evidence.spans else 0)
            self._rollup(
                level="file",
                role="bulk",
                system=FILE_SYSTEM,
                scope_key=path,
                node_id=file_node_ids.get(path),
                header=f"File: {path}",
                children=children,
            )

    def directories(self) -> None:
        """Summarize directories bottom-up over a *selected* set.

        The set is chosen shallow-first (the root and its immediate children
        matter most to a reader), then summarized deepest-first so every
        parent has its children's prose available. A file or sub-directory
        whose own directory did not make the cut rolls up into the nearest
        selected ancestor instead of disappearing.
        """
        file_summaries = self._written["file"]
        if not file_summaries:
            return

        dirs_with_files: set[str] = set()
        for path in file_summaries:
            dirs_with_files.update(_ancestors(path))

        stats = self.stats.level("directory")
        stats.candidates = len(dirs_with_files)
        selected = sorted(dirs_with_files, key=lambda d: (_depth(d), d))[
            : self._budget.max_directory
        ]
        stats.capped = len(dirs_with_files) - len(selected)
        selected_set = set(selected)

        def nearest_selected(path: str) -> str | None:
            for ancestor in _ancestors(path):
                if ancestor in selected_set:
                    return ancestor
            return None

        file_children: dict[str, list[Summary]] = defaultdict(list)
        for path, summary in sorted(file_summaries.items()):
            owner = nearest_selected(path)
            if owner is not None:
                file_children[owner].append(summary)

        dir_children: dict[str, list[str]] = defaultdict(list)
        for d in selected:
            if d == "":
                continue
            owner = nearest_selected(d)
            if owner is not None and owner != d:
                dir_children[owner].append(d)

        for d in sorted(selected, key=lambda d: (-_depth(d), d)):
            children: list[Summary] = list(file_children.get(d, []))
            children.extend(
                self._written["directory"][sub]
                for sub in sorted(dir_children.get(d, []))
                if sub in self._written["directory"]
            )
            if not children:
                continue
            self._rollup(
                level="directory",
                role="bulk",
                system=DIRECTORY_SYSTEM,
                scope_key=d,
                node_id=None,
                header=f"Directory: {d or '(repository root)'}",
                children=children,
            )

    def components(self, clusters: list[tuple[int, str, list[str]]]) -> None:
        """`clusters` is (cluster node id, cluster name, member file paths)."""
        file_summaries = self._written["file"]
        candidates = [
            (cid, name, [p for p in paths if p in file_summaries])
            for cid, name, paths in clusters
        ]
        candidates = [c for c in candidates if c[2]]

        stats = self.stats.level("component")
        stats.candidates = len(candidates)
        ordered = sorted(candidates, key=lambda c: (-len(c[2]), c[1]))
        selected = ordered[: self._budget.max_component]
        stats.capped = len(ordered) - len(selected)

        for cluster_id, name, paths in selected:
            self._rollup(
                level="component",
                role="synth",
                system=COMPONENT_SYSTEM,
                scope_key=name,
                node_id=cluster_id,
                header=f"Component: {name} ({len(paths)} member files)",
                children=[file_summaries[p] for p in sorted(paths)],
            )

    def repo(self) -> None:
        children: list[Summary] = sorted(
            self._written["component"].values(), key=lambda s: s.scope_key
        )
        if not children:
            # No components were summarized: fall back to the directory level,
            # which is still a child level -- never back to raw code.
            children = sorted(
                self._written["directory"].values(),
                key=lambda s: (_depth(s.scope_key), s.scope_key),
            )
        if not children:
            children = sorted(self._written["file"].values(), key=lambda s: s.scope_key)
        if not children:
            return
        stats = self.stats.level("repo")
        stats.candidates = 1
        self._rollup(
            level="repo",
            role="synth",
            system=REPO_SYSTEM,
            scope_key="",
            node_id=None,
            header="Repository overview, assembled from the summaries below.",
            children=children,
        )

    # -- persistence --

    def flush(self) -> None:
        rows = [s for level in self._written.values() for s in level.values()]
        if rows:
            upsert_summaries(self._conn, rows)

    def write_coverage(self) -> None:
        rows: list[Coverage] = []
        for level, stats in self.stats.by_level.items():
            blockers: dict[str, int] = {}
            if stats.capped:
                blockers["budget_cap"] = stats.capped
            if stats.failed:
                blockers["model_failed"] = stats.failed
            rows.append(
                Coverage(
                    repo_id=self._repo_id,
                    commit_sha=self._commit_sha,
                    area="/",
                    metric=f"summaries_{level}",
                    resolved=stats.written,
                    total=stats.candidates,
                    blockers=blockers,
                )
            )
        if rows:
            upsert_coverage(self._conn, rows)


def _file_node_ids(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> dict[str, int]:
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            select path, id from codeaxis.nodes
            where repo_id = %s and commit_sha = %s and kind = 'file' and path is not null
            """,
            (repo_id, commit_sha),
        ).fetchall()
    return {path: node_id for path, node_id in rows}


def _clusters(
    conn: psycopg.Connection, repo_id: str, commit_sha: str
) -> list[tuple[int, str, list[str]]]:
    """Candidate components the architecture pass wrote: (id, name, member
    file paths). Members are reached through `belongs_to` edges; a member
    that is a symbol contributes its file's path."""
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            select c.id, c.name, m.path
            from codeaxis.nodes c
            join codeaxis.edges e
              on e.repo_id = c.repo_id and e.commit_sha = c.commit_sha
             and e.kind = 'belongs_to' and e.src_id = c.id
            join codeaxis.nodes m on m.id = e.dst_id
            where c.repo_id = %s and c.commit_sha = %s and c.kind = 'cluster'
              and m.path is not null
            """,
            (repo_id, commit_sha),
        ).fetchall()
    grouped: dict[int, tuple[str, set[str]]] = {}
    for cluster_id, name, path in rows:
        entry = grouped.setdefault(cluster_id, (name, set()))
        entry[1].add(path)
    return [(cid, name, sorted(paths)) for cid, (name, paths) in sorted(grouped.items())]


def write_summary_pass(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    service: ModelService,
    *,
    budget: SummaryBudget | None = None,
    context: CallContext | None = None,
) -> SummaryStats:
    """Build and persist the summary hierarchy for one repo snapshot.

    Returns the per-level stats. Raises only on a genuine failure (DB error,
    or a provider that failed repeatedly in a row); an empty chunk table, an
    exhausted budget, or individual candidates the model could not describe
    all end as recorded gaps, not exceptions.
    """
    budget = budget or SummaryBudget()
    chunks = list_chunks(conn, repo_id, commit_sha)
    summarizer = _Summarizer(conn, repo_id, commit_sha, service, budget, context)

    if not chunks:
        # No chunks at this commit: the chunking pass wrote none (or has not
        # run). There is nothing to narrate and nothing is claimed.
        summarizer.stats.aborted_reason = "no_chunks"
        summarizer.write_coverage()
        return summarizer.stats

    file_node_ids = _file_node_ids(conn, repo_id, commit_sha)
    clusters = _clusters(conn, repo_id, commit_sha)

    try:
        summarizer.symbols(group_chunks(chunks))
        summarizer.files(file_node_ids)
        summarizer.directories()
        summarizer.components(clusters)
        summarizer.repo()
    except _BudgetExhausted:
        summarizer.stats.aborted_reason = "budget_exhausted"
        logger.warning(
            "summary pass hit its %d-call ceiling for repo %s", budget.max_total_calls, repo_id
        )
    except _ProviderUnhealthy as exc:
        # Persist what already validated, then fail visibly: the caller
        # records the pass_run as failed.
        summarizer.flush()
        summarizer.write_coverage()
        raise ModelServiceError(
            f"summary pass stopped after {budget.max_consecutive_failures} consecutive "
            f"model failures: {exc}"
        ) from exc

    summarizer.flush()
    summarizer.write_coverage()
    return summarizer.stats
