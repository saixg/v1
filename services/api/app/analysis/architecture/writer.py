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
                attrs={"member_count": len(members)},
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="inferred",
                # A cluster is an algorithmic grouping, not something read
                # out of source: the `origin` column says so. It used to be
                # duplicated in attrs["origin"]; the column is authoritative
                # now (build-plan.md Part 4.C / Part 5) and attrs no longer
                # carries a second copy.
                origin="inferred",
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
                # not by this field -- see the `origin` column below.
                status="verified",
                origin="inferred",
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
        # `layer_origin` is deliberately *not* folded into the node's
        # `origin` column and is not a duplicate of it. The column states
        # where the node record itself came from (a file node is `parsed`:
        # the scanner read it off disk). `layer_origin` states where this
        # one *attribute* came from -- Part 4.C's "every architectural
        # statement is tagged by origin" is per statement, and a single file
        # node can carry a parsed record with, say, an inferred layer once
        # the LLM fallback exists. It uses the same vocabulary
        # (parsed|configured|inferred) on purpose.
        update_node_attrs(conn, node_id, {"layer": layer, "layer_origin": "parsed"})
