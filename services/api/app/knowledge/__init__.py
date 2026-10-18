"""Repo Model: the typed query layer. The only module that knows SQL."""

from app.knowledge.chunks import (
    count_chunks,
    find_reusable_embeddings,
    get_chunk,
    insert_chunks,
    list_chunks,
    search_chunks_fts,
    set_chunk_embeddings,
)
from app.knowledge.db import api_connection, worker_connection
from app.knowledge.jobs import (
    JobAlreadyExistsError,
    create_job,
    get_active_job_for_repo,
    get_job,
    set_job_status,
)
from app.knowledge.llm_calls import get_llm_call, insert_llm_call
from app.knowledge.pass_runs import finish_pass_run, list_pass_runs, start_pass_run
from app.knowledge.projects import (
    create_project,
    ensure_profile,
    get_membership_role,
    list_projects_for_user,
)
from app.knowledge.reads import (
    get_blob,
    get_node,
    get_root_node,
    list_children,
    list_coverage,
    list_nodes,
    neighborhood,
    unresolved_summary,
)
from app.knowledge.repos import create_repo, delete_repo, get_repo
from app.knowledge.retrieval import (
    chunks_for_graph_neighbors,
    create_chat_thread,
    exact_symbol_chunks,
    file_line_counts,
    fts_chunks,
    get_chat_thread,
    insert_chat_message,
    list_chat_messages,
    symbols_by_path,
    vector_chunks,
)
from app.knowledge.semantic import (
    get_summary,
    get_summary_for_node,
    list_conventions,
    list_summaries,
    upsert_conventions,
    upsert_summaries,
)
from app.knowledge.writes import (
    insert_edges,
    insert_nodes,
    update_node_attrs,
    upsert_blobs,
    upsert_coverage,
)

__all__ = [
    "api_connection",
    "worker_connection",
    "create_repo",
    "get_repo",
    "delete_repo",
    "create_project",
    "ensure_profile",
    "list_projects_for_user",
    "get_membership_role",
    "JobAlreadyExistsError",
    "create_job",
    "get_job",
    "get_active_job_for_repo",
    "set_job_status",
    "start_pass_run",
    "finish_pass_run",
    "list_pass_runs",
    "insert_nodes",
    "insert_edges",
    "update_node_attrs",
    "upsert_coverage",
    "upsert_blobs",
    "get_blob",
    "get_node",
    "get_root_node",
    "list_nodes",
    "list_children",
    "list_coverage",
    "neighborhood",
    "unresolved_summary",
    "insert_llm_call",
    "get_llm_call",
    # --- semantic layer: codeaxis.chunks (build-plan.md Part 4.D) ---
    "insert_chunks",
    "set_chunk_embeddings",
    "find_reusable_embeddings",
    "list_chunks",
    "get_chunk",
    "search_chunks_fts",
    "count_chunks",
    # --- semantic layer: summaries + conventions (build-plan.md Part 4.D) ---
    "upsert_summaries",
    "list_summaries",
    "get_summary",
    "get_summary_for_node",
    "upsert_conventions",
    "list_conventions",
    # --- semantic layer: hybrid retrieval stages + chat (Part 4.E / Part 8) ---
    "exact_symbol_chunks",
    "fts_chunks",
    "vector_chunks",
    "chunks_for_graph_neighbors",
    "file_line_counts",
    "symbols_by_path",
    "create_chat_thread",
    "get_chat_thread",
    "insert_chat_message",
    "list_chat_messages",
]
