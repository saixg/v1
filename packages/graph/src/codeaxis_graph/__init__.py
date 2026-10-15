"""Repo Model: Supabase migrations and the typed query layer. The only module that knows SQL."""

from codeaxis_graph.db import api_connection, worker_connection
from codeaxis_graph.pass_runs import finish_pass_run, list_pass_runs, start_pass_run
from codeaxis_graph.reads import (
    get_blob,
    get_node,
    get_root_node,
    list_children,
    list_nodes,
    neighborhood,
)
from codeaxis_graph.repos import create_repo, get_repo
from codeaxis_graph.writes import insert_edges, insert_nodes, upsert_blobs, upsert_coverage

__version__ = "0.1.0"

__all__ = [
    "api_connection",
    "worker_connection",
    "create_repo",
    "get_repo",
    "start_pass_run",
    "finish_pass_run",
    "list_pass_runs",
    "insert_nodes",
    "insert_edges",
    "upsert_coverage",
    "upsert_blobs",
    "get_blob",
    "get_node",
    "get_root_node",
    "list_nodes",
    "list_children",
    "neighborhood",
]
