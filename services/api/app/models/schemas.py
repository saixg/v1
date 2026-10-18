"""Shapes analyzers emit: nodes and edges carrying provenance, and coverage
counters. See docs/01-evidence-model.md and docs/04-graph-schema.md.

Pure data — no database access, no I/O. `app.knowledge` is the only module
that knows SQL; these models are what crosses the boundary into it.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field, model_validator

# repos.id is a Postgres uuid; accept either a str or a UUID object (psycopg
# rows return UUID) and normalize to str everywhere it's used as a foreign key.
RepoId = Annotated[str, BeforeValidator(lambda v: str(v) if isinstance(v, UUID) else v)]

AnalysisStatus = Literal["verified", "inferred", "unknown", "unanalyzed"]

# How a stored record came to exist (build-plan.md Part 4.C / Part 5):
#   parsed     — read out of source by a deterministic analyzer
#   configured — read out of an explicit config/rule file in the repo
#   inferred   — a model generalized or narrated it
# Distinct from `status` (the analyzer's verification confidence) and from
# `resolution_status` (how an edge's destination resolved). This is the
# authoritative origin tag; it lives in a real column on nodes, edges,
# summaries and conventions, never only in `attrs`.
OriginTag = Literal["parsed", "configured", "inferred"]

NodeKind = Literal[
    "repository",
    "directory",
    "file",
    "symbol",
    "route",
    "model",
    "test",
    "external_service",
    "config_value",
    "cluster",
    "dependency",
]

EdgeKind = Literal[
    "imports",
    "defines",
    "calls",
    "implements",
    "extends",
    "routes_to",
    "reads",
    "writes",
    "flows_to",
    "tests",
    "calls_external",
    "belongs_to",
    "changed_with",
    "contains",
]

# How an edge's destination resolved against the graph. Distinct from
# `status` (Provenance), which is the analyzer's own verification confidence
# and already has a live consumer (knowledge/reads.py::neighborhood's
# `status = %s` filter) that must keep working unchanged for `contains`
# edges, which never set this field.
ResolutionStatus = Literal["resolved", "ambiguous", "unresolved", "external"]


class PassRunStatus(str, Enum):  # noqa: UP042 — str mixin kept for psycopg param binding
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Provenance(BaseModel):
    """Required on every node and every edge. docs/01-evidence-model.md."""

    analyzer: str
    analyzer_version: str
    status: AnalysisStatus
    source_file: str | None = None
    confidence: float | None = None
    reason: str | None = None
    # Part 4.C: every node and edge states where it came from. Defaults to
    # `parsed` because every writer that predates the origin column is a
    # deterministic analyzer reading source; writers that are not must set
    # it explicitly.
    origin: OriginTag = "parsed"

    @model_validator(mode="after")
    def _reason_required_for_unknown(self) -> Provenance:
        if self.status == "unknown" and not self.reason:
            raise ValueError("reason is required when status is 'unknown'")
        return self


class Repo(BaseModel):
    id: RepoId | None = None  # uuid, assigned by the database on insert
    # Owning project (codeaxis.projects); required by the database. Access to
    # the repo and its graph is decided by membership in this project.
    project_id: RepoId | None = None
    remote_url: str | None = None
    local_path: str | None = None
    default_branch: str | None = None
    staged_commit_sha: str | None = None


ProjectRole = Literal["owner", "member"]


class Project(BaseModel):
    id: RepoId
    name: str
    owner_id: RepoId
    created_at: datetime


class ProjectWithRole(Project):
    """A project as seen by one member: includes that member's role."""

    role: ProjectRole


class JobStatus(str, Enum):  # noqa: UP042 — str mixin kept for psycopg param binding
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


JobKind = Literal["scan"]


class Job(BaseModel):
    """codeaxis.jobs: one long-running request a user started. A `scan` job
    drives exactly one scan pass_run for its repo."""

    id: RepoId
    project_id: RepoId
    repo_id: RepoId | None = None
    kind: JobKind
    status: JobStatus
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class GithubSource(BaseModel):
    """Remote git repository. `ref` may be a branch, tag, or commit SHA; the
    pinned `commit_sha` is resolved from the actual checkout, never trusted
    from the caller."""

    kind: Literal["github"] = "github"
    remote_url: str
    ref: str | None = None


class LocalSource(BaseModel):
    """A folder already on disk. There is no git history requirement, so the
    caller supplies the commit_sha the graph rows are keyed by."""

    kind: Literal["local"] = "local"
    local_path: str
    commit_sha: str


# ZIP uploads are multipart, not JSON, so they have their own endpoint and
# are deliberately not part of this union.
IngestSource = Annotated[GithubSource | LocalSource, Field(discriminator="kind")]


class PassRun(BaseModel):
    id: int | None = None
    repo_id: RepoId
    commit_sha: str
    analyzer: str
    analyzer_version: str
    status: PassRunStatus
    error: str | None = None


class Node(Provenance):
    id: int | None = None  # bigserial, assigned by the database on insert
    repo_id: RepoId
    commit_sha: str
    kind: NodeKind
    name: str
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    col_start: int | None = None
    col_end: int | None = None
    attrs: dict = {}
    embedding: list[float] | None = None


class Edge(Provenance):
    id: int | None = None
    repo_id: RepoId
    commit_sha: str
    kind: EdgeKind
    src_id: int
    dst_id: int | None = None  # null together with status='unknown'
    line_start: int | None = None
    line_end: int | None = None
    col_start: int | None = None
    col_end: int | None = None
    resolution_status: ResolutionStatus | None = None
    attrs: dict = {}

    @model_validator(mode="after")
    def _unknown_edge_has_no_destination(self) -> Edge:
        if self.status == "unknown" and self.dst_id is not None:
            raise ValueError("an 'unknown' edge must have dst_id = null")
        return self


class Coverage(BaseModel):
    repo_id: RepoId
    commit_sha: str
    area: str
    metric: str
    resolved: int
    total: int
    blockers: dict[str, int] = {}


class Blob(BaseModel):
    """Raw text of one scanned file at one commit — the pinned source that
    "show the code" reads. Written by the scan pass, never by narration."""

    repo_id: RepoId
    commit_sha: str
    path: str
    content: str
    content_hash: str


# --- Model layer (build-plan.md Part 3) -------------------------------------

ModelRole = Literal["bulk", "synth", "design", "vision", "embed", "rerank"]

LlmOperation = Literal["structured", "text", "embed", "rerank"]

# succeeded         — response received and (for structured) passed schema validation
# validation_failed — response received but did not validate against the caller's schema
# refused           — provider returned stop_reason="refusal"
# truncated         — provider stopped at max_tokens; output is incomplete
# error             — the request itself failed (network, 4xx/5xx after SDK retries)
LlmCallStatus = Literal["succeeded", "validation_failed", "refused", "truncated", "error"]


class LlmCall(BaseModel):
    """One provider request made by app.llm — usage and cost, never content.

    Prompts and completions are deliberately not stored: repository text is
    untrusted and may carry secrets, which must never reach logs (Part 0.3).
    One row per provider attempt, so a retry shows up as two rows.
    """

    id: int | None = None  # bigserial, assigned by the database on insert
    role: ModelRole
    provider: str
    model: str  # what the routing table requested
    served_model: str | None = None  # what the provider reports actually ran
    operation: LlmOperation
    attempt: int = Field(default=1, ge=1)
    status: LlmCallStatus
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)  # None = model not in the price table
    latency_ms: int = Field(ge=0)
    stop_reason: str | None = None
    request_id: str | None = None
    schema_name: str | None = None
    error: str | None = None
    # Attribution (all nullable uuids; RepoId is the shared uuid->str normalizer).
    project_id: RepoId | None = None
    repo_id: RepoId | None = None
    job_id: RepoId | None = None


# --- Semantic layer (build-plan.md Part 4.D) ---------------------------------

# How a chunk's span was chosen:
#   symbol       — exactly one symbol node's line range
#   symbol_group — several adjacent, individually-trivial symbols merged
#   module       — file lines no kept symbol covers (imports, module-level
#                  code, and whole files that have no symbols at all)
ChunkKind = Literal["symbol", "symbol_group", "module"]


class Chunk(BaseModel):
    """codeaxis.chunks: one AST-aware retrieval unit, pinned to a snapshot.

    Content is the *stored* (already secret-redacted) source text, never the
    bytes on disk. `content_hash` is the sha256 of this chunk's own content
    and is the incremental re-index key (build-plan.md Part 4.D): an unchanged
    chunk at a new commit reuses the embedding computed for an earlier one.
    `tsv` is a generated column and is deliberately absent here — it is
    never written by application code.
    """

    id: int | None = None  # bigserial, assigned by the database on insert
    repo_id: RepoId
    commit_sha: str
    # The symbol node this chunk was cut from, when there is exactly one
    # (or, for a symbol_group, the first of the merged symbols). None for
    # `module` chunks: no single node owns those lines.
    node_id: int | None = None
    path: str
    symbol: str | None = None
    kind: ChunkKind
    line_start: int
    line_end: int
    content: str
    content_hash: str
    token_count: int = Field(ge=0)
    part_index: int = Field(default=0, ge=0)
    part_count: int = Field(default=1, ge=1)
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedded_at: datetime | None = None
    analyzer: str
    analyzer_version: str

    @model_validator(mode="after")
    def _part_index_within_count(self) -> Chunk:
        if self.part_index >= self.part_count:
            raise ValueError("part_index must be < part_count")
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


# --- Semantic layer: retrieval, citations, chat (Part 4.E, Part 6, Part 8)


class ScoredChunk(Chunk):
    """A chunk as one retrieval stage ranked it. `score` is that stage's own
    scale (ts_rank_cd, cosine similarity, a fixed exact-match weight) and is
    never compared across stages — reciprocal-rank fusion uses ranks, not
    scores, precisely because these are not commensurable."""

    score: float = 0.0


class Citation(BaseModel):
    """A file+line reference. Every claim the narrator makes carries at least
    one of these, and every one is checked against the snapshot before the
    answer is returned or persisted (build-plan.md Part 9, "every cited
    path/symbol/line must exist")."""

    # Deliberately unconstrained. A nonsense citation (empty path, line 0,
    # an inverted range) must be *reported* by the citation validator with a
    # reason the caller can show, not rejected as a schema violation that
    # surfaces only as "the model failed". Validity is the validator's job.
    path: str
    line_start: int
    line_end: int
    symbol: str | None = None
    node_id: int | None = None
    chunk_id: int | None = None


ChatRole = Literal["user", "assistant"]


class ChatThread(BaseModel):
    """codeaxis.chat_threads: one grounded-chat conversation, pinned to one
    repo at one commit and owned by one user."""

    id: RepoId
    repo_id: RepoId
    commit_sha: str
    user_id: str
    title: str | None = None
    created_at: datetime


class ChatMessage(BaseModel):
    """codeaxis.chat_messages. An assistant row exists only if every citation
    in it resolved against the snapshot."""

    id: int | None = None
    thread_id: RepoId
    role: ChatRole
    content: str
    citations: list[Citation] = []
    model: str | None = None
    llm_call_id: int | None = None
    created_at: datetime | None = None

    def citations_json(self) -> str:
        """The `citations` jsonb payload, for the insert in app.knowledge."""
        return json.dumps([c.model_dump(exclude_none=True) for c in self.citations])


# --- Semantic layer: hierarchical summaries + conventions (Part 4.D) --------

# Strictly bottom-up. A parent level summarizes its children's *summaries*,
# never raw code again (build-plan.md Part 3, "Patterns") — that is both the
# point of the hierarchy and the cost control.
SummaryLevel = Literal["symbol", "file", "directory", "component", "repo"]


class SummaryEvidence(BaseModel):
    """What one summary was written from, named precisely enough that the UI
    can link every statement back to source (Part 5).

    Produced by the analyzer from what it actually sent to the model — never
    taken from the model's own output, which is prose about facts, not a
    source of facts (Part 0.3).
    """

    # "chunks" for the symbol level, "child_summaries" for every level above.
    input: Literal["chunks", "child_summaries"]
    chunk_ids: list[int] = []
    node_ids: list[int] = []
    # File+line spans of the material behind this summary.
    spans: list[Citation] = []
    # (level, scope_key) of each child summary rolled up into this one.
    children: list[dict[str, str]] = []
    # True when the children/chunks had to be capped to stay inside the
    # pass's model-call and prompt budgets — an admitted gap, not a silent one.
    truncated: bool = False

    def evidence_json(self) -> str:
        """The `evidence` jsonb payload, for the insert in app.knowledge.
        Named `evidence_json` rather than `json` so it does not shadow
        pydantic's own (deprecated) BaseModel.json."""
        return json.dumps(
            {
                "input": self.input,
                "chunk_ids": self.chunk_ids,
                "node_ids": self.node_ids,
                "spans": [s.model_dump(exclude_none=True) for s in self.spans],
                "children": self.children,
                "truncated": self.truncated,
            }
        )


class Summary(BaseModel):
    """codeaxis.summaries: one model-written paragraph about facts the
    analyzers established. Always `origin='inferred'` — it is prose, and the
    UI must never present it as parsed fact."""

    id: int | None = None
    repo_id: RepoId
    commit_sha: str
    level: SummaryLevel
    # The graph node this summary is about, when one node owns it (a symbol,
    # a file, a cluster). None for directory and repo levels.
    node_id: int | None = None
    # Path, directory path, component key, or '' for the repo level.
    scope_key: str
    summary: str = Field(min_length=1)
    origin: OriginTag = "inferred"
    evidence: SummaryEvidence
    model: str | None = None
    analyzer: str
    analyzer_version: str
    created_at: datetime | None = None


ConventionCategory = Literal["naming", "structure", "testing", "lint", "ci", "other"]


class Convention(BaseModel):
    """codeaxis.conventions: one house rule this repository actually follows.

    `origin` is set honestly per row: `configured` when the rule was read out
    of a real config file, `parsed` when it was counted from code, `inferred`
    only when a model generalized a pattern (Part 4.D).
    """

    id: int | None = None
    repo_id: RepoId
    commit_sha: str
    category: ConventionCategory
    rule: str = Field(min_length=1)
    origin: OriginTag
    # For a `parsed` row: the share of candidates that follow the rule.
    # None when the notion does not apply (a configured rule is not a vote).
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: dict = {}
    analyzer: str
    analyzer_version: str
    created_at: datetime | None = None
