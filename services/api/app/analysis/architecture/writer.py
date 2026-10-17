"""Pass 1c driver: cluster the graph, classify layers, and write both to
the Repo Model -- `cluster` nodes + `belongs_to` edges for cluster
membership, and `attrs["layer"]`/`attrs["layer_origin"]` merged onto
existing file nodes. Runs inside the same job/connection as the scan pass
(see jobs.py's `_run_scan_pass`) rather than as its own `PassRun`: one scan
job already produces one coherent graph for a commit, and this pass only
reads rows that pass already wrote -- it doesn't need its own checkout or
its own pass_run row. If a future need arises to re-run architecture
analysis independently of a full rescan, this can be split out then.
"""

from __future__ import annotations

import psycopg

from app.knowledge import insert_edges, insert_nodes, update_node_attrs
from app.models import Edge, Node

from .cluster import build_graph, cluster_graph
from .layers import classify_layer

ANALYZER = "architecture"
ANALYZER_VERSION = "0.1.0"


def write_architecture_pass(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> None:
    """Cluster the import/call graph into candidate components and classify
    file layers, writing both to the graph. Clustering is skipped (zero
    cluster nodes written) when there's no real relationship structure to
    cluster -- see `cluster_graph`'s empty-graph handling -- rather than
    raising; a genuine DB error still propagates like the rest of the scan
    pass."""
    graph = build_graph(conn, repo_id, commit_sha)
    membership = cluster_graph(graph)

    if membership:
        members_by_cluster: dict[int, list[int]] = {}
        for node_id, label in membership.items():
            members_by_cluster.setdefault(label, []).append(node_id)

        cluster_nodes = [
            Node(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="cluster",
                name=f"cluster-{label}",
                attrs={"origin": "inferred", "member_count": len(members)},
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="inferred",
            )
            for label, members in sorted(members_by_cluster.items())
        ]
        cluster_ids = insert_nodes(conn, cluster_nodes)
        cluster_id_by_label = {
            label: cluster_id
            for label, cluster_id in zip(sorted(members_by_cluster), cluster_ids, strict=True)
        }

        belongs_to_edges = [
            Edge(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="belongs_to",
                src_id=cluster_id_by_label[label],
                dst_id=member_id,
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                # The clustering algorithm did in fact assign this membership
                # -- that's a verified fact about the algorithm's output.
                # Whether the resulting *architectural* grouping is correct
                # is a separate question, carried by attrs["origin"] below,
                # not by this field.
                status="verified",
                attrs={"origin": "inferred"},
            )
            for label, members in members_by_cluster.items()
            for member_id in members
        ]
        if belongs_to_edges:
            insert_edges(conn, belongs_to_edges)

    with conn.cursor() as cur:
        file_rows = cur.execute(
            """
            select id, path from codeaxis.nodes
            where repo_id = %s and commit_sha = %s and kind = 'file' and path is not null
            """,
            (repo_id, commit_sha),
        ).fetchall()

    for node_id, path in file_rows:
        layer = classify_layer(path)
        if layer is None:
            continue
        update_node_attrs(conn, node_id, {"layer": layer, "layer_origin": "parsed"})
