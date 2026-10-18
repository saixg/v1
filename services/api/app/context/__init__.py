"""Agent Context Engine (build-plan.md Part 4.F, Phase 4).

Task B (intake, extraction, validation, constraint ledger) and Task C (package
assembly, grounding, export, re-grounding) are exported below.
"""

from __future__ import annotations

from .export import EXPORT_FILENAMES, EXPORT_FORMATS, render_export
from .extract import (
    ExtractionOutcome,
    ExtractionResult,
    RequirementExtraction,
    extract_against,
    extract_requirements,
)
from .grounding import GroundingResult, ground_task
from .intake import (
    Intake,
    IntakeError,
    IntakeLimits,
    Upload,
    prepare_intake,
)
from .ledger import detect_constraints, load_ledger_snapshot, write_ledger_pass
from .package import (
    AssembledPackage,
    PackageFreshness,
    assemble_context_package,
    check_package,
    current_package,
    mark_stale_if_snapshot_moved,
)
from .reground import RegroundReport, TaskState, rebuild_task_context, reground_to_commit

__all__ = [
    "AssembledPackage",
    "EXPORT_FILENAMES",
    "EXPORT_FORMATS",
    "GroundingResult",
    "PackageFreshness",
    "RegroundReport",
    "TaskState",
    "assemble_context_package",
    "check_package",
    "current_package",
    "ground_task",
    "mark_stale_if_snapshot_moved",
    "rebuild_task_context",
    "render_export",
    "reground_to_commit",
    "ExtractionOutcome",
    "ExtractionResult",
    "Intake",
    "IntakeError",
    "IntakeLimits",
    "RequirementExtraction",
    "Upload",
    "detect_constraints",
    "extract_against",
    "extract_requirements",
    "load_ledger_snapshot",
    "prepare_intake",
    "write_ledger_pass",
]
