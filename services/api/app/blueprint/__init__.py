"""Virtual Development Environment (build-plan.md Part 1.3, Part 4.G; Phase 5).

The engine behind a blueprint: generation, validation, the file-structure
proposal, partial regeneration, the canonical document and export to a
context package. The tables and their query layer live in
`supabase/migrations/20261021000000_blueprint.sql` and
`app.knowledge.blueprints`; nothing in this package writes SQL.

The division of labour, which is the project's governing rule applied to
design work: the repository slice and the task's records are read
deterministically, the model only arranges them into typed rows, and
`validate` is what decides whether the result may be served. A design that
fails validation is stored with its reasons, never repaired.
"""

from __future__ import annotations

from .document import build_current_document, build_document, modules_of
from .export import (
    InvalidBlueprintError,
    blueprint_candidates,
    component_item,
    export_blueprint_context,
)
from .files import FileContext, FileOutcome, load_file_context, propose_files, resolve_proposal
from .generate import (
    BlueprintDraft,
    DraftOutcome,
    Dropped,
    GenerationResult,
    RepoSlice,
    TaskInputs,
    generate_blueprint,
    load_repo_slice,
    load_task_inputs,
    persist_version,
    propose_design,
    resolve_draft,
)
from .regenerate import (
    Neighborhood,
    RegenerationResult,
    affected_neighborhood,
    carry_component,
    check_locks,
    regenerate_blueprint,
)
from .validate import (
    BLOCKING_CODES,
    KNOWN_SCHEMA_VERSIONS,
    ValidationOutcome,
    VersionRows,
    check_constraints,
    check_grounding,
    check_structure,
    load_version_rows,
    validate_and_record,
    validate_rows,
)

__all__ = [
    # validation (Part 4.G.5)
    "BLOCKING_CODES",
    "KNOWN_SCHEMA_VERSIONS",
    "ValidationOutcome",
    "VersionRows",
    "check_constraints",
    "check_grounding",
    "check_structure",
    "load_version_rows",
    "validate_and_record",
    "validate_rows",
    # generation (Part 4.G.1-4)
    "BlueprintDraft",
    "DraftOutcome",
    "Dropped",
    "GenerationResult",
    "RepoSlice",
    "TaskInputs",
    "generate_blueprint",
    "load_repo_slice",
    "load_task_inputs",
    "persist_version",
    "propose_design",
    "resolve_draft",
    # file structure (Part 4.G.7)
    "FileContext",
    "FileOutcome",
    "load_file_context",
    "propose_files",
    "resolve_proposal",
    # partial regeneration (Part 4.G.9)
    "Neighborhood",
    "RegenerationResult",
    "affected_neighborhood",
    "carry_component",
    "check_locks",
    "regenerate_blueprint",
    # canonical document (Part 6) and export (Part 4.G.8)
    "InvalidBlueprintError",
    "blueprint_candidates",
    "build_current_document",
    "build_document",
    "component_item",
    "export_blueprint_context",
    "modules_of",
]
