"""Analyzer output contracts: nodes, edges and coverage with required
provenance, plus the ingestion/repo/pass-run request-response shapes. Pure
data — no database access, no I/O. `app.knowledge` is the only module that
knows SQL; these models are what crosses the boundary into it.
"""

from app.models.schemas import (
    AnalysisStatus,
    Blob,
    Coverage,
    Edge,
    EdgeKind,
    GithubSource,
    IngestSource,
    Job,
    JobKind,
    JobStatus,
    LlmCall,
    LlmCallStatus,
    LlmOperation,
    LocalSource,
    ModelRole,
    Node,
    NodeKind,
    PassRun,
    PassRunStatus,
    Project,
    ProjectRole,
    ProjectWithRole,
    Repo,
    ResolutionStatus,
)

__all__ = [
    "AnalysisStatus",
    "Blob",
    "Coverage",
    "Edge",
    "EdgeKind",
    "GithubSource",
    "IngestSource",
    "Job",
    "JobKind",
    "JobStatus",
    "LlmCall",
    "LlmCallStatus",
    "LlmOperation",
    "LocalSource",
    "ModelRole",
    "Node",
    "NodeKind",
    "PassRun",
    "PassRunStatus",
    "Project",
    "ProjectRole",
    "ProjectWithRole",
    "Repo",
    "ResolutionStatus",
]
