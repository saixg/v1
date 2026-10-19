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


JobKind = Literal["scan", "blueprint_generate", "blueprint_regenerate"]


class Job(BaseModel):
    """codeaxis.jobs: one long-running request a user started. A `scan` job
    drives exactly one scan pass_run for its repo; the two blueprint kinds
    drive one model-backed design pass each (Part 8: anything long returns a
    job id and is polled)."""

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


# --- Agent Context Engine (build-plan.md Part 4.F) --------------------------

# The snapshot key in this codebase is the `(repo_id, commit_sha)` pair, not a
# `snapshot_id` uuid (there is no `repository_snapshots` table — see the
# comment block at the top of
# supabase/migrations/20261020000000_context_engine.sql). Every model below
# therefore carries both, including models that also carry `task_id` whose
# task already names a snapshot: scoping to a commit must be expressible
# without a join, so rows analyzed at one commit can never answer a query
# about another.
#
# Untrusted data: `Task.intent`, `Requirement.statement`,
# `Constraint.statement` and `ContextItem.content` hold user prose, document
# text, or repository text. None of it is ever an instruction (Part 0.3).


class ContextEvidence(BaseModel):
    """What one context-engine record was derived from, named precisely enough
    that the UI can link it back to source (Part 5).

    Assembled by the pass from what it actually read — never taken from a
    model's own output, which is prose about facts and not a source of facts
    (Part 0.3). Same contract as `SummaryEvidence`, with document refs added
    because Part 4.F.1 ingests PDFs, DOCX and images alongside code.
    """

    # File+line spans in the snapshot. `Citation` is reused rather than
    # re-declared so the citation validator that already checks chat answers
    # checks these too.
    spans: list[Citation] = []
    node_ids: list[int] = []
    chunk_ids: list[int] = []
    # Non-code inputs: {"source": "pdf"|"docx"|"image"|"text"|"repo_doc",
    # "ref": "<filename>", "page": "3", "region": "..."} — page and region
    # refs are kept so a statement extracted from a document stays traceable
    # to the place in that document (Part 4.F.1).
    documents: list[dict[str, str]] = []
    # Why this record exists, when the spans alone do not say it.
    notes: str | None = None
    # True when the inputs had to be capped to stay inside a prompt or token
    # budget — an admitted gap, not a silent one.
    truncated: bool = False

    def evidence_json(self) -> str:
        """The `evidence` jsonb payload, for the insert in app.knowledge.
        Named `evidence_json` for the same reason `SummaryEvidence`'s is: not
        to shadow pydantic's own (deprecated) `BaseModel.json`."""
        return json.dumps(
            {
                "spans": [s.model_dump(exclude_none=True) for s in self.spans],
                "node_ids": self.node_ids,
                "chunk_ids": self.chunk_ids,
                "documents": self.documents,
                "notes": self.notes,
                "truncated": self.truncated,
            }
        )


# draft/extracting/ready/blocked/archived. `blocked` is load-bearing: Part
# 4.F.3 forbids inventing a missing requirement, so a contradiction or a gap
# stops the task until a human resolves it rather than being papered over.
TaskStatus = Literal["draft", "extracting", "ready", "blocked", "archived"]


class Task(BaseModel):
    """codeaxis.tasks: one thing a developer wants built or changed, grounded
    against exactly one snapshot of one repo."""

    id: RepoId | None = None  # uuid, assigned by the database on insert
    project_id: RepoId
    repo_id: RepoId
    commit_sha: str
    title: str = Field(min_length=1, max_length=300)
    # Raw input, kept verbatim so extraction can be re-run and audited
    # against what the user actually wrote. Untrusted data.
    intent: str | None = None
    status: TaskStatus = "draft"
    created_by: str | None = None  # Clerk user id (profiles.id is text)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# The seven buckets of Part 4.F.2's extraction schema. `open_question` is one
# of them, not an error state: a gap is stored as a question.
RequirementKind = Literal[
    "objective",
    "functional",
    "technical",
    "architectural",
    "existing_behavior",
    "acceptance_criteria",
    "open_question",
]

# `ambiguous` and `unresolved` are valid stored states (Part 0.3). Both
# require a `question` saying what is missing.
RequirementStatus = Literal["open", "ambiguous", "unresolved", "satisfied", "rejected"]

# None is not "could": an unstated priority is unknown, not low.
RequirementPriority = Literal["must", "should", "could"]


class Requirement(BaseModel):
    """codeaxis.requirements: one extracted statement, with its own origin,
    evidence and status so it can be confirmed or contradicted individually."""

    id: RepoId | None = None
    task_id: RepoId
    repo_id: RepoId
    commit_sha: str
    kind: RequirementKind
    statement: str = Field(min_length=1)
    priority: RequirementPriority | None = None
    status: RequirementStatus = "open"
    # What exactly is missing or contradictory. Required whenever the status
    # is not a clean one, so an ambiguity is never a bare flag.
    question: str | None = None
    origin: OriginTag
    # Where in the input this came from: {"source": ..., "page": ..., ...}.
    # Kept as a free dict because intake formats differ; `ContextEvidence`
    # covers the code side.
    source_ref: dict = {}
    evidence: ContextEvidence = ContextEvidence()
    ordinal: int = Field(default=0, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    model: str | None = None
    analyzer: str
    analyzer_version: str
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _unclean_status_states_why(self) -> Requirement:
        if self.status in ("ambiguous", "unresolved") and not self.question:
            raise ValueError("an ambiguous/unresolved requirement must state its question")
        return self


# What the constraint is about (Part 4.F.8's own list: public API, DB schema,
# auth boundaries, CI/lint rules, "do not touch" paths).
ConstraintKind = Literal[
    "public_api",
    "db_schema",
    "auth_boundary",
    "ci_lint",
    "do_not_touch",
    "dependency",
    "performance",
    "other",
]

# hard — violating it makes the work wrong; soft — a tradeoff with a reason.
ConstraintSeverity = Literal["hard", "soft"]

# Who put it in the ledger. Kept separate from `origin` so the human/analyzer
# distinction is queryable without overloading Part 5's enum.
ConstraintSource = Literal["user_stated", "auto_detected"]


class Constraint(BaseModel):
    """codeaxis.constraints: one entry in the Constraint Ledger (Part 4.F.8).

    Constraints are the one section of a context package that is never
    truncated (Part 4.F.5), which is why they are their own record with their
    own severity rather than a `Requirement` kind.

    `task_id` is optional: auto-detected repo constraints belong to the repo
    at a commit and are reused by every task (and by the MCP
    `get_constraints` tool, which is per-repo).
    """

    id: RepoId | None = None
    task_id: RepoId | None = None
    repo_id: RepoId
    commit_sha: str
    kind: ConstraintKind
    # What it applies to: a path or glob, a component key, a symbol, or
    # 'repo'. Free text because the addressable things differ per kind.
    scope: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    severity: ConstraintSeverity
    source: ConstraintSource
    # A user-stated constraint is recorded as `configured`: it is an explicit
    # rule stated for this repository, and emphatically not `inferred` — no
    # model produced it.
    origin: OriginTag
    # A human has accepted this entry. Auto-detected entries start False: an
    # unconfirmed detection is shown as a detection, never as an agreed rule.
    confirmed: bool = False
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    rationale: str | None = None
    evidence: ContextEvidence = ContextEvidence()
    # None for a user-stated constraint: no analyzer produced it, and writing
    # a fake analyzer name would corrupt provenance.
    analyzer: str | None = None
    analyzer_version: str | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _provenance_matches_source(self) -> Constraint:
        if self.source == "auto_detected" and not (self.analyzer and self.analyzer_version):
            raise ValueError("an auto-detected constraint must name its analyzer and version")
        if self.source == "user_stated" and self.origin != "configured":
            raise ValueError("a user-stated constraint has origin='configured', not a model tag")
        if self.confirmed and self.confirmed_at is None:
            raise ValueError("a confirmed constraint must record confirmed_at")
        return self


# `open` with no decision is the normal resting state of an unresolved
# question — storing it is the point (Part 4.F.6).
DecisionStatus = Literal["open", "decided", "superseded"]


class Decision(BaseModel):
    """codeaxis.decisions: a decision taken on a task, or an open question
    still waiting for one. An approved decision is `locked`: later passes read
    it, never rewrite it (Part 4.G.9)."""

    id: RepoId | None = None
    task_id: RepoId
    repo_id: RepoId
    commit_sha: str
    question: str = Field(min_length=1)
    decision: str | None = None
    status: DecisionStatus = "open"
    locked: bool = False
    decided_by: str | None = None
    decided_at: datetime | None = None
    rationale: str | None = None
    supersedes_id: RepoId | None = None
    origin: OriginTag
    evidence: ContextEvidence = ContextEvidence()
    analyzer: str
    analyzer_version: str
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _decided_rows_are_complete(self) -> Decision:
        if self.status == "decided" and not (self.decision and self.decided_at):
            raise ValueError("a decided row must carry both decision and decided_at")
        if self.locked and self.status != "decided":
            raise ValueError("only a decided row can be locked")
        return self


# The nine sections of Part 4.F.5's package order. The order itself belongs to
# the assembler, not to this enum, so changing presentation never requires a
# schema change.
ContextItemKind = Literal[
    "task_intent",
    "constraint",
    "requirement",
    "architecture",
    "code",
    "convention",
    "test",
    "limitation",
    "open_question",
]


class ContextItem(BaseModel):
    """codeaxis.context_items. Part 6 names the columns
    `(id, task_id, kind, content, origin, snapshot_id, evidence jsonb)`;
    `snapshot_id` is realized here as `(repo_id, commit_sha)`, the snapshot key
    this repository actually uses.

    One row per assembled piece of context rather than one rendered blob, so
    every line of a delivered package still points at its source and carries
    its own origin tag.
    """

    id: RepoId | None = None
    task_id: RepoId
    # The package this item was assembled into. None while the item exists but
    # no package has been built from it: extraction and assembly are separate
    # passes.
    package_id: RepoId | None = None
    repo_id: RepoId
    commit_sha: str
    kind: ContextItemKind
    content: str
    origin: OriginTag
    evidence: ContextEvidence = ContextEvidence()
    token_count: int = Field(default=0, ge=0)
    ordinal: int = Field(default=0, ge=0)
    analyzer: str
    analyzer_version: str
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _code_items_are_citable(self) -> ContextItem:
        # A source excerpt handed to an agent without a file+line span is
        # unusable and uncitable, so these two kinds must carry one. The other
        # kinds legitimately have none (a user-typed constraint, an open
        # question), and demanding a span there would push callers to invent
        # one — exactly what Part 0.3 forbids.
        if self.kind in ("code", "test") and not self.evidence.spans:
            raise ValueError("a 'code'/'test' context item must carry at least one span")
        return self


# primary    — the work happens here
# supporting — needed to understand or call into the primary component
# impacted   — may break; impact analysis found a path to it
ComponentRelevance = Literal["primary", "supporting", "impacted"]


class TaskComponent(BaseModel):
    """codeaxis.task_components: a task's link to an existing graph entity.
    Points at `codeaxis.nodes` (components are `kind='cluster'` nodes here), so
    a task can never reference a component absent from the snapshot."""

    task_id: RepoId
    node_id: int
    repo_id: RepoId
    commit_sha: str
    relevance: ComponentRelevance
    rationale: str | None = None
    origin: OriginTag
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: ContextEvidence = ContextEvidence()
    analyzer: str
    analyzer_version: str
    created_at: datetime | None = None


TaskSnapshotStatus = Literal["active", "stale", "superseded"]


class TaskSnapshot(BaseModel):
    """codeaxis.task_snapshots: the audit trail of which snapshots a task has
    been grounded against (Part 4.F.6). Exactly one row per task is `active`;
    every other row says why it stopped being."""

    id: int | None = None  # bigserial
    task_id: RepoId
    repo_id: RepoId
    commit_sha: str
    status: TaskSnapshotStatus
    reason: str | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _inactive_rows_state_why(self) -> TaskSnapshot:
        if self.status != "active" and not self.reason:
            raise ValueError("a stale/superseded grounding must record the reason")
        return self


# `not_run` is the honest default: CodeAxis never executes repository code
# (Part 0.3 / Part 9), so no outcome here is produced by running anything.
VerificationOutcome = Literal["passed", "failed", "inconclusive", "not_run"]

# Who reported it. There is deliberately no 'codeaxis_execution' value.
VerificationSource = Literal["user", "analyzer", "external"]


class VerificationResult(BaseModel):
    """codeaxis.verification_results: an outcome *reported in* for one of a
    task's acceptance criteria — by a human, by one of our static passes, or
    by an external CI system. Never the product of executing repo code."""

    id: int | None = None  # bigserial
    task_id: RepoId
    repo_id: RepoId
    commit_sha: str
    # The requirement being verified, when it is one of ours. `criterion`
    # keeps the human-readable statement so the history survives a
    # re-extraction that replaces requirement rows.
    requirement_id: RepoId | None = None
    criterion: str = Field(min_length=1)
    outcome: VerificationOutcome
    detail: str | None = None
    source: VerificationSource
    evidence: ContextEvidence = ContextEvidence()
    created_at: datetime | None = None


# `invalid` is reachable and stored: Part 4.F.5 forbids truncating
# constraints, so a budget that cannot hold them yields a package marked
# invalid with the reason, never a quietly shortened one.
ContextPackageStatus = Literal["assembled", "stale", "invalid"]


class ContextPackage(BaseModel):
    """codeaxis.context_packages: the budget decision record for one assembly.

    Not a second copy of the content — that lives in `ContextItem` rows, so a
    package can be re-rendered (API, CLAUDE.md, AGENTS.md) from rows that each
    still carry their own evidence and snapshot. Assembly never mutates a
    package; a new version is created instead, so an agent run can always be
    traced to the exact package it was given.
    """

    id: RepoId | None = None
    task_id: RepoId
    repo_id: RepoId
    commit_sha: str
    version: int = Field(default=1, ge=1)
    status: ContextPackageStatus = "assembled"
    token_budget: int = Field(gt=0)
    token_count: int = Field(default=0, ge=0)
    # Which sections were included, in what order, and what was dropped for
    # budget — so the UI can say "dropped 14 spans for budget" instead of
    # silently shipping less context.
    manifest: dict = {}
    # Per-stage retrieval outcomes (ran/skipped + reason) from Pipeline E.
    retrieval: dict = {}
    # Gaps, contradictions and open questions carried into the package rather
    # than resolved by guessing (Part 4.F.3).
    unresolved: list[dict] = []
    analyzer: str
    analyzer_version: str
    created_at: datetime | None = None


class ApiToken(BaseModel):
    """codeaxis.api_tokens: one per-repo, read-only MCP token (Part 4.F.7).

    Holds the sha256 **hash** of the token, never the token. `token_hash` is
    excluded from serialization so a route that returns this model cannot leak
    it by accident; `app.knowledge.context` reads the attribute directly.
    """

    id: RepoId | None = None
    project_id: RepoId
    repo_id: RepoId
    name: str = Field(min_length=1, max_length=200)
    # Non-secret display/lookup handle (e.g. 'cxa_7f3b'); narrows verification
    # to one candidate row so the secret comparison can be constant-time in
    # Python rather than an SQL equality on secret material.
    token_prefix: str = Field(min_length=1)
    token_hash: str = Field(min_length=64, max_length=64, exclude=True, repr=False)
    # V1 MCP is read-only, in the schema as well as in code.
    scope: Literal["read"] = "read"
    created_by: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    # Revocation is a timestamp, not a delete: a revoked token's id stays
    # referenceable by whatever logged its use.
    revoked_at: datetime | None = None


class IssuedApiToken(BaseModel):
    """The one and only time the plaintext token exists outside the caller's
    hands: the response to creating it. Never stored, never logged, never sent
    to a model (Part 0.3)."""

    token: str
    record: ApiToken


# ----------------------------------------------------------------------------
# Virtual Development Environment (Part 4.G, Part 6 "Virtual env", Phase 5).
#
# A deliberately separate vocabulary from `NodeKind`/`EdgeKind`: those name
# facts an analyzer established about code that exists, these name a design a
# model proposed for code that does not. Part 13 decision #4 locks this
# taxonomy; `supabase/migrations/20261021000000_blueprint.sql` carries the
# full reasoning.
# ----------------------------------------------------------------------------

# The contract `packages/schemas/blueprint.v1.json` describes. An unknown
# value is rejected at read time rather than coerced (Part 4.G.5).
BLUEPRINT_SCHEMA_VERSION = "1.0.0"

# What a component is relative to the repo as it stands.
BlueprintComponentKind = Literal["existing", "proposed", "modified"]
# What the design does to it. Paired with `kind`, never independent of it:
# existing+reuse, modified+extend, proposed+new (enforced in SQL and below).
BlueprintMapping = Literal["reuse", "extend", "new"]
KIND_TO_MAPPING: dict[str, str] = {
    "existing": "reuse",
    "modified": "extend",
    "proposed": "new",
}

# Replaces `OriginTag` for blueprint rows: none of parsed/configured/inferred
# honestly describes "a model authored this design", and `locked` is a state
# the regeneration pass must be able to refuse to overwrite (Part 4.G.9).
BlueprintGenerationStatus = Literal["generated", "edited", "locked"]

BlueprintVersionStatus = Literal["draft", "validated", "invalid", "approved"]
BlueprintVersionReason = Literal["generated", "regenerated", "edited", "validated"]

# `dependency` is the only kind a cycle is invalid in: control flow, data
# flow and events legitimately loop (retries, polling, pub/sub).
BlueprintConnectionKind = Literal["control_flow", "data_flow", "dependency", "event"]
ProposedFileStatus = Literal["new", "modified", "unchanged"]


class BlueprintInterface(BaseModel):
    """One entry point a component exposes. Not a node in the graph: an
    interface has no identity outside the component that declares it."""

    name: str = Field(min_length=1)
    kind: Literal["api", "event", "function"]
    # Free text: the signature's shape differs per language and per kind, and
    # a parsed signature here would imply a precision the generator does not
    # have for code that does not exist yet.
    signature: str | None = None


class ValidationError(BaseModel):
    """One reason a blueprint is invalid. Stored, not recomputed: Part 4.G.5
    requires an invalid blueprint to be rejected *with reasons*, and a reason
    that only existed during the failing run cannot be shown to anyone."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    # Which row it is about, when it is about one.
    entity_kind: Literal["component", "connection", "proposed_file", "blueprint"] | None = None
    entity_key: str | None = None


class Blueprint(BaseModel):
    """codeaxis.blueprints: one design effort, grounded against one snapshot
    of one repo and hanging off one Context Engine task.

    `task_id` is required (Part 13 decision #4, sub-decision C): Part 4.G.1
    begins from extracted objective/requirements/constraints, which are
    Pillar 2's records, so a blueprint references `codeaxis.requirements` /
    `constraints` / `decisions` instead of copying them.
    """

    id: RepoId | None = None
    project_id: RepoId
    repo_id: RepoId
    commit_sha: str
    task_id: RepoId
    title: str = Field(min_length=1, max_length=300)
    current_version_id: RepoId | None = None
    created_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BlueprintVersion(BaseModel):
    """codeaxis.blueprint_versions: one immutable snapshot of a design.

    Append-only in the same way `ContextPackage` is: an edit, a regeneration
    or a validation outcome creates a new version rather than mutating one,
    so an exported context package can always be traced to the exact design
    it came from (Part 4.G.9).
    """

    id: RepoId | None = None
    blueprint_id: RepoId
    version: int = Field(ge=1)
    schema_version: str = BLUEPRINT_SCHEMA_VERSION
    status: BlueprintVersionStatus = "draft"
    validation_errors: list[ValidationError] = []
    origin_reason: BlueprintVersionReason = "generated"
    analyzer: str
    analyzer_version: str
    created_by: str | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _invalid_states_its_reasons(self) -> BlueprintVersion:
        if self.status == "invalid" and not self.validation_errors:
            raise ValueError("an invalid blueprint version must carry its validation errors")
        if self.status == "validated" and self.validation_errors:
            raise ValueError("a validated blueprint version cannot carry validation errors")
        return self


class BlueprintComponent(BaseModel):
    """codeaxis.blueprint_components: one capability in the design.

    `key` is the stable identity across versions — connections and proposed
    files reference it by `key`, not by `id`, because a new version
    re-inserts rows and Part 4.G.9 requires untouched parts of the design to
    survive a partial regeneration unchanged.
    """

    id: RepoId | None = None
    blueprint_version_id: RepoId | None = None
    key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    kind: BlueprintComponentKind
    mapping: BlueprintMapping
    # The real `cluster`-kind node this maps to (Part 4.G.3, Part 11:
    # "existing components map to real entities"). Finer-grained targets —
    # the exact file or symbol to extend — ride in `evidence`.
    linked_component_id: int | None = None
    repo_id: RepoId
    commit_sha: str
    module_key: str | None = None
    responsibility: str | None = None
    interfaces: list[BlueprintInterface] = []
    confidence: float | None = Field(default=None, ge=0, le=1)
    # Reused unchanged from the context engine so the existing citation
    # validator checks these spans too, rather than a parallel shape.
    evidence: ContextEvidence = ContextEvidence()
    generation_status: BlueprintGenerationStatus = "generated"
    locked: bool = False
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _taxonomy_invariants(self) -> BlueprintComponent:
        if KIND_TO_MAPPING[self.kind] != self.mapping:
            raise ValueError(
                f"kind={self.kind!r} pairs with mapping={KIND_TO_MAPPING[self.kind]!r}, "
                f"not {self.mapping!r}"
            )
        if self.kind != "proposed" and self.linked_component_id is None:
            raise ValueError(
                f"a {self.kind!r} component must name the repo entity it maps to "
                "(linked_component_id)"
            )
        if self.locked != (self.generation_status == "locked"):
            raise ValueError("`locked` and generation_status='locked' are one fact, not two")
        return self


class BlueprintConnection(BaseModel):
    """codeaxis.blueprint_connections: one relationship between two
    components, named by their stable keys.

    The canonical JSON's `data_flows` is this filtered to
    `kind == 'data_flow'` at read time — one stored edge, so a data flow can
    never drift from the connection it describes.
    """

    id: RepoId | None = None
    blueprint_version_id: RepoId | None = None
    key: str = Field(min_length=1, max_length=200)
    kind: BlueprintConnectionKind
    source_key: str = Field(min_length=1)
    target_key: str = Field(min_length=1)
    # {protocol, trigger, description}
    semantics: dict = {}
    # Only meaningful on a data flow.
    data_shape: dict | None = None
    locked: bool = False
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _endpoints_and_shape(self) -> BlueprintConnection:
        if self.source_key == self.target_key:
            raise ValueError("a connection must join two different components")
        if self.kind != "data_flow" and self.data_shape is not None:
            raise ValueError("data_shape belongs to a data_flow connection")
        return self


class ProposedFile(BaseModel):
    """codeaxis.proposed_files: one file the design would add or change
    (Part 4.G.7). Every file states a purpose; `conventions_refs` names the
    `codeaxis.conventions` rows that justified its path and shape, and an
    empty list is shown as an uncited placement rather than hidden."""

    id: RepoId | None = None
    blueprint_version_id: RepoId | None = None
    path: str = Field(min_length=1)
    status: ProposedFileStatus
    purpose: str = Field(min_length=1)
    implements_component_key: str | None = None
    conventions_refs: list[int] = []
    locked: bool = False
    created_at: datetime | None = None


class BlueprintLayout(BaseModel):
    """codeaxis.blueprint_layouts: where the boxes sit (Part 4.G.6).

    Stored apart from semantics and keyed by component/connection `key`, so
    dragging a box neither creates a new version of the design nor breaks
    when the rows it positions are regenerated.
    """

    blueprint_version_id: RepoId
    layout: dict = {}
    updated_at: datetime | None = None


class BlueprintDocument(BaseModel):
    """The canonical blueprint JSON (Part 6), assembled for one version.

    This is a *read* shape: the tables are the source of truth, and this is
    what the API serves and what an export renders from. `data_flows` is
    derived here rather than stored.
    """

    schema_version: str = BLUEPRINT_SCHEMA_VERSION
    blueprint: Blueprint
    version: BlueprintVersion
    components: list[BlueprintComponent] = []
    connections: list[BlueprintConnection] = []
    proposed_files: list[ProposedFile] = []
    layout: dict = {}
    # Referenced, never copied: the context engine owns these records, and
    # duplicating them here would give a design its own divergent idea of
    # what the requirements are.
    requirement_ids: list[str] = []
    constraint_ids: list[str] = []
    # Open decisions carried rather than guessed (Part 4.G.5, Part 0.3).
    unresolved: list[dict] = []

    @property
    def data_flows(self) -> list[BlueprintConnection]:
        """Part 6's `data_flows`: a view over `connections`, not a second
        collection."""
        return [c for c in self.connections if c.kind == "data_flow"]
