"""cluster_graph against hand-built synthetic NetworkX graphs -- no DB. Two
clearly-separate clusters of import/call edges must end up with different
cluster labels, and the empty/trivial graph case must not crash."""

from __future__ import annotations

import networkx as nx

from app.analysis.architecture.cluster import cluster_graph


def test_two_separate_clusters_get_different_labels() -> None:
    graph = nx.DiGraph()
    # Cluster A: a tight group of files calling/importing each other.
    graph.add_edge(1, 2, weight=1.0)
    graph.add_edge(2, 3, weight=1.0)
    graph.add_edge(3, 1, weight=1.0)
    # Cluster B: a separate tight group, no edges to cluster A at all.
    graph.add_edge(10, 11, weight=1.0)
    graph.add_edge(11, 12, weight=1.0)
    graph.add_edge(12, 10, weight=1.0)

    membership = cluster_graph(graph)

    assert set(membership) == {1, 2, 3, 10, 11, 12}
    labels_a = {membership[n] for n in (1, 2, 3)}
    labels_b = {membership[n] for n in (10, 11, 12)}
    assert len(labels_a) == 1, "cluster A members should share one label"
    assert len(labels_b) == 1, "cluster B members should share one label"
    assert labels_a != labels_b, "the two disconnected groups must get different labels"


def test_empty_graph_does_not_crash() -> None:
    assert cluster_graph(nx.DiGraph()) == {}


def test_graph_with_nodes_but_no_edges_does_not_crash() -> None:
    graph = nx.DiGraph()
    graph.add_node(1)
    graph.add_node(2)
    assert cluster_graph(graph) == {}


def test_single_edge_graph_clusters_together() -> None:
    graph = nx.DiGraph()
    graph.add_edge(1, 2, weight=1.0)
    membership = cluster_graph(graph)
    assert set(membership) == {1, 2}
    assert membership[1] == membership[2]
