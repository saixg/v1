"""Build the import/call graph for one repo snapshot and cluster it with
Leiden (build-plan.md Part 4.C: "Cluster the import/call graph (Leiden,
seeded by directory structure) -> candidate components").

Weighting choice (documented here because build-plan.md doesn't specify a
mechanism): "seeded by directory structure" is implemented as a *bias*, not
a hard constraint. Every real `imports`/`calls` edge (resolution_status in
`resolved`/`ambiguous` -- see relationships.py's resolution discipline) gets
weight 1.0. For every pair of `file` nodes that share the same parent
directory and are *not* already connected by a real edge in either
direction, we add a low-weight (0.3) synthetic edge in both directions. This
nudges Leiden's modularity optimization toward keeping directory-siblings
together without directory membership being the only signal, and without
overriding real import/call structure when it disagrees with the directory
layout. It is not a literal Leiden API parameter -- Leiden itself only sees
one weighted graph.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

import igraph
import leidenalg
import networkx as nx
import psycopg

# Synthetic directory-co-membership edge weight; real relationship edges are 1.0.
_DIRECTORY_BIAS_WEIGHT = 0.3
_REAL_EDGE_WEIGHT = 1.0


def _parent_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def build_graph(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> nx.DiGraph:
    """Read `file`/`symbol` nodes and `imports`/`calls` edges (resolved or
    ambiguous only -- `unresolved`/`external` edges don't represent real
    internal repo structure) for this repo+commit, and build a directed,
    weighted graph with directory-co-membership bias edges added."""
    graph: nx.DiGraph = nx.DiGraph()

    with conn.cursor() as cur:
        node_rows = cur.execute(
            """
            select id, path from codeaxis.nodes
            where repo_id = %s and commit_sha = %s and kind in ('file', 'symbol')
            """,
            (repo_id, commit_sha),
        ).fetchall()
    file_paths: dict[int, str] = {}
    for node_id, path in node_rows:
        graph.add_node(node_id)
        if path is not None:
            file_paths[node_id] = path

    with conn.cursor() as cur:
        edge_rows = cur.execute(
            """
            select src_id, dst_id from codeaxis.edges
            where repo_id = %s and commit_sha = %s
              and kind in ('imports', 'calls')
              and resolution_status in ('resolved', 'ambiguous')
              and dst_id is not null
            """,
            (repo_id, commit_sha),
        ).fetchall()
    for src_id, dst_id in edge_rows:
        if src_id not in graph or dst_id not in graph:
            # Defensive: an edge whose endpoint wasn't in our node query
            # (shouldn't happen for imports/calls, which only ever point at
            # file/symbol nodes, but a future edge kind might).
            continue
        if graph.has_edge(src_id, dst_id):
            # Multiple relationship edges between the same pair (e.g. two
            # calls) just reinforce the real connection, not accumulate.
            graph[src_id][dst_id]["weight"] = _REAL_EDGE_WEIGHT
        else:
            graph.add_edge(src_id, dst_id, weight=_REAL_EDGE_WEIGHT)

    # Directory co-membership bias, keyed off `file` node paths only.
    by_dir: dict[str, list[int]] = defaultdict(list)
    for node_id, path in file_paths.items():
        by_dir[_parent_dir(path)].append(node_id)

    for siblings in by_dir.values():
        for a, b in combinations(sorted(siblings), 2):
            if graph.has_edge(a, b) or graph.has_edge(b, a):
                continue  # a real edge already connects them
            graph.add_edge(a, b, weight=_DIRECTORY_BIAS_WEIGHT)
            graph.add_edge(b, a, weight=_DIRECTORY_BIAS_WEIGHT)

    return graph


def cluster_graph(graph: nx.DiGraph) -> dict[int, int]:
    """Partition `graph` with Leiden, weighted by edge `weight`. Returns a
    mapping from original node id to an integer cluster label.

    Empty/trivial-graph handling: a graph with no edges at all (no real
    relationships and no directory bias -- e.g. a one-file repo, or a repo
    where every import/call turned out external/unresolved) produces *no*
    clusters: we return `{}` rather than forcing every isolated node into
    its own singleton "cluster", which wouldn't represent any real
    structure. A graph with at least one edge is clustered normally, which
    may still include singleton clusters for nodes Leiden couldn't connect
    to anything -- that's a legitimate algorithmic result, not a special
    case.
    """
    if graph.number_of_nodes() == 0 or graph.number_of_edges() == 0:
        return {}

    # igraph.Graph.from_networkx (igraph >= 0.10 / python-igraph) converts
    # directly, carrying node identity in the `_nx_name` vertex attribute and
    # edge attributes (including `weight`) over unchanged. Verified against
    # the installed igraph 1.0.0 / leidenalg 0.12.0.
    ig_graph = igraph.Graph.from_networkx(graph)

    # Collapse to undirected before clustering: component membership is
    # about co-membership, not call/import direction, and empirically (on
    # the installed igraph 1.0.0 / leidenalg 0.12.0) RBConfigurationVertexPartition
    # on a *directed* graph leaves even a single connecting edge as two
    # singleton communities -- a tie the optimizer breaks toward splitting --
    # which defeats the point of clustering a sparse import/call graph.
    # `combine_edges="sum"` merges a reciprocal pair's weights into one
    # undirected edge rather than leaving two parallel ones; a directory-bias
    # pair (added in both directions in `build_graph`, each 0.3) collapses to
    # 0.6, still well below a real edge's 1.0 either way.
    ig_graph = ig_graph.as_undirected(mode="collapse", combine_edges="sum")

    # RBConfigurationVertexPartition is leidenalg's modularity-style
    # objective -- the standard choice for Leiden community detection.
    partition = leidenalg.find_partition(
        ig_graph, leidenalg.RBConfigurationVertexPartition, weights="weight"
    )

    return {
        ig_graph.vs[i]["_nx_name"]: label
        for i, label in enumerate(partition.membership)
    }
