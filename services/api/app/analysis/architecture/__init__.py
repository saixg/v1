"""Pass 1c: architecture analysis -- cluster the import/call graph into
candidate components (Leiden) and classify files into layers (rules-only).
Source in (nodes/edges already written by the scan + relationship passes),
nodes and edges with provenance out. No DB writes happen anywhere except
`write_architecture_pass`; `build_graph`/`cluster_graph`/`classify_layer` are
pure. See build-plan.md Part 4.C.
"""

from .cluster import build_graph, cluster_graph
from .layers import classify_layer
from .writer import ANALYZER, ANALYZER_VERSION, write_architecture_pass

__all__ = [
    "ANALYZER",
    "ANALYZER_VERSION",
    "build_graph",
    "classify_layer",
    "cluster_graph",
    "write_architecture_pass",
]
