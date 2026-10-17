"""Pass 1: tree-sitter scan. Source in, nodes and edges with provenance out.
No DB, no network, no exec."""

from app.analysis.scan.entrypoints import annotate_entry_points
from app.analysis.scan.history import collect_history
from app.analysis.scan.limits import DEFAULT_LIMITS, ScanLimits
from app.analysis.scan.manifests import extract_manifest_dependencies
from app.analysis.scan.preview import PreviewEntry, list_included_files
from app.analysis.scan.relationships import extract_relationships
from app.analysis.scan.scanner import (
    ANALYZER,
    ANALYZER_VERSION,
    FULL_TIER_LANGUAGES,
    LANGUAGE_BY_EXT,
    ScannedNode,
    ScanStats,
    scan_repo,
    walk,
)

__all__ = [
    "ANALYZER",
    "ANALYZER_VERSION",
    "DEFAULT_LIMITS",
    "FULL_TIER_LANGUAGES",
    "LANGUAGE_BY_EXT",
    "PreviewEntry",
    "ScanLimits",
    "ScanStats",
    "ScannedNode",
    "annotate_entry_points",
    "collect_history",
    "extract_manifest_dependencies",
    "extract_relationships",
    "list_included_files",
    "scan_repo",
    "walk",
]
