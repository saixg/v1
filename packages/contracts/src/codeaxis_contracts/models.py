"""Shapes analyzers emit: nodes and edges carrying provenance, and coverage
counters. See docs/01-evidence-model.md and docs/04-graph-schema.md.

Pure data — no database access, no I/O. `packages/graph` is the only module
that knows SQL; these models are what crosses the boundary into it.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field, model_validator

# repos.id is a Postgres uuid; accept either a str or a UUID object (psycopg
# rows return UUID) and normalize to str everywhere it's used as a foreign key.
RepoId = Annotated[str, BeforeValidator(lambda v: str(v) if isinstance(v, UUID) else v)]

AnalysisStatus = Literal["verified", "inferred", "unknown", "unanalyzed"]

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

    @model_validator(mode="after")
    def _reason_required_for_unknown(self) -> Provenance:
        if self.status == "unknown" and not self.reason:
            raise ValueError("reason is required when status is 'unknown'")
        return self


class Repo(BaseModel):
    id: RepoId | None = None  # uuid, assigned by the database on insert
    remote_url: str | None = None
    local_path: str | None = None
    default_branch: str | None = None


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
