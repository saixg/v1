"""Pass 1: tree-sitter scan. Source in, nodes and edges with provenance out.
No DB, no network, no exec."""

from codeaxis_scan.limits import DEFAULT_LIMITS, ScanLimits
from codeaxis_scan.preview import PreviewEntry, list_included_files
from codeaxis_scan.scanner import (
    ANALYZER,
    ANALYZER_VERSION,
    ScannedNode,
    ScanStats,
    scan_repo,
    walk,
)

__version__ = "0.1.0"

__all__ = [
    "ANALYZER",
    "ANALYZER_VERSION",
    "DEFAULT_LIMITS",
    "PreviewEntry",
    "ScanLimits",
    "ScanStats",
    "ScannedNode",
    "list_included_files",
    "scan_repo",
    "walk",
]
