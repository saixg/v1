"""Read queries against the Repo Model. Used by the FastAPI routes via the
read-only codeaxis_api role — see docs/04-graph-schema.md.
"""

from __future__ import annotations

from typing import Literal

import psycopg
from psycopg.rows import class_row

from app.models import Blob, Coverage, Edge, Node

# Depth cap for /graph traversal. docs/10-api.md: "Unbounded traversal
# requests are rejected, not silently truncated" — the cap itself is the
# rejection mechanism; callers above this just get told it was applied.
MAX_GRAPH_DEPTH = 5

_NODE_COLUMNS = """
    id, repo_id, commit_sha, kind, name, path, line_start, line_end,
    col_start, col_end, attrs, analyzer, analyzer_version, status,
    source_file, confidence, reason, origin
"""

_NODE_COLUMNS_PREFIXED = """
    n.id, n.repo_id, n.commit_sha, n.kind, n.name, n.path, n.line_start, n.line_end,
    n.col_start, n.col_end, n.attrs, n.analyzer, n.analyzer_version, n.status,
    n.source_file, n.confidence, n.reason, n.origin
"""

_EDGE_COLUMNS = """
    id, repo_id, commit_sha, kind, src_id, dst_id, line_start, line_end,
    col_start, col_end, analyzer, analyzer_version, status, source_file,
    confidence, reason, resolution_status, attrs, origin
"""


def get_node(conn: psycopg.Connection, repo_id: str, commit_sha: str, node_id: int) -> Node | None:
    with conn.cursor(row_factory=class_row(Node)) as cur:
        return cur.execute(
            f"""
            select {_NODE_COLUMNS} from codeaxis.nodes
            where repo_id = %s and commit_sha = %s and id = %s
            """,
            (repo_id, commit_sha, node_id),
        ).fetchone()


def list_nodes(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    kind: str | None = None,
    path: str | None = None,
) -> list[Node]:
    clauses = ["repo_id = %s", "commit_sha = %s"]
    params: list[object] = [repo_id, commit_sha]
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    if path is not None:
        clauses.append("path = %s")
        params.append(path)
    # Clauses are built from a fixed internal whitelist, never from caller
    # input directly — only %s-bound params below carry caller-supplied
    # values. pyright wants a LiteralString here; the dynamic WHERE is safe.
    query = f"select {_NODE_COLUMNS} from codeaxis.nodes where {' and '.join(clauses)} order by line_start nulls first, name"  # noqa: E501
    with conn.cursor(row_factory=class_row(Node)) as cur:
        return cur.execute(query, params).fetchall()  # pyright: ignore[reportArgumentType]


def list_children(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, parent_id: int
) -> list[Node]:
    """Nodes reached from `parent_id` by a `contains` edge — the file tree
    and a file's symbol list are both this query."""
    with conn.cursor(row_factory=class_row(Node)) as cur:
        return cur.execute(
            f"""
            select {_NODE_COLUMNS_PREFIXED}
            from codeaxis.nodes n
            join codeaxis.edges e on e.dst_id = n.id
            where e.repo_id = %s and e.commit_sha = %s and e.kind = 'contains' and e.src_id = %s
            order by n.line_start nulls first, n.name
            """,
            (repo_id, commit_sha, parent_id),
        ).fetchall()


def get_root_node(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> Node | None:
    """The `directory` node for the checkout root (`path = ''`)."""
    roots = list_nodes(conn, repo_id, commit_sha, kind="directory", path="")
    return roots[0] if roots else None


def neighborhood(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    root_id: int,
    depth: int = 1,
    kinds: list[str] | None = None,
    status: str = "verified",
    direction: Literal["forward", "reverse"] = "forward",
) -> tuple[list[Node], list[Edge], bool]:
    """BFS from `root_id` over edges of the given kinds (default: all),
    filtered to `status`. `direction="forward"` walks `src_id -> dst_id`
    (what `root_id` points at); `"reverse"` walks `dst_id -> src_id` (what
    points at `root_id`). Returns (nodes, edges, truncated)."""
    capped_depth = min(depth, MAX_GRAPH_DEPTH)
    truncated = depth > MAX_GRAPH_DEPTH

    # Forward matches the frontier against src_id and extends via dst_id;
    # reverse is the mirror image. Same BFS shape either way.
    match_col, extend_col = ("src_id", "dst_id") if direction == "forward" else ("dst_id", "src_id")

    visited_ids = {root_id}
    frontier = [root_id]
    edges_out: list[Edge] = []

    with conn.cursor(row_factory=class_row(Edge)) as cur:
        for _ in range(capped_depth):
            if not frontier:
                break
            clauses = [
                "repo_id = %s",
                "commit_sha = %s",
                "status = %s",
                f"{match_col} = any(%s)",
            ]
            params: list[object] = [repo_id, commit_sha, status, frontier]
            if kinds:
                clauses.append("kind = any(%s)")
                params.append(kinds)
            edge_query = f"select {_EDGE_COLUMNS} from codeaxis.edges where {' and '.join(clauses)}"  # noqa: E501
            rows = cur.execute(edge_query, params).fetchall()  # pyright: ignore[reportArgumentType]
            next_frontier: list[int] = []
            for edge in rows:
                edges_out.append(edge)
                extend_id = edge.dst_id if direction == "forward" else edge.src_id
                if extend_id is not None and extend_id not in visited_ids:
                    visited_ids.add(extend_id)
                    next_frontier.append(extend_id)
            frontier = next_frontier
        if frontier:
            # The depth cap was reached with nodes left unexpanded — only
            # truncated if any of them actually has further children.
            clauses = [
                "repo_id = %s",
                "commit_sha = %s",
                "status = %s",
                f"{match_col} = any(%s)",
            ]
            params = [repo_id, commit_sha, status, frontier]
            if kinds:
                clauses.append("kind = any(%s)")
                params.append(kinds)
            peek_query = f"select 1 from codeaxis.edges where {' and '.join(clauses)} limit 1"
            truncated = (
                cur.execute(peek_query, params).fetchone()  # pyright: ignore[reportArgumentType]
                is not None
            )

    with conn.cursor(row_factory=class_row(Node)) as cur:
        nodes_out = cur.execute(
            f"""
            select {_NODE_COLUMNS} from codeaxis.nodes
            where repo_id = %s and commit_sha = %s and id = any(%s)
            order by line_start nulls first, name
            """,
            (repo_id, commit_sha, list(visited_ids)),
        ).fetchall()

    return nodes_out, edges_out, truncated


def list_coverage(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> list[Coverage]:
    """All `codeaxis.coverage` rows for one repo at one commit — the
    file/metric-level counters the scan and later passes write."""
    with conn.cursor(row_factory=class_row(Coverage)) as cur:
        return cur.execute(
            """
            select repo_id, commit_sha, area, metric, resolved, total, blockers
            from codeaxis.coverage
            where repo_id = %s and commit_sha = %s
            """,
            (repo_id, commit_sha),
        ).fetchall()


def unresolved_summary(
    conn: psycopg.Connection, repo_id: str, commit_sha: str
) -> list[dict[str, object]]:
    """Edge-level breakdown: how many edges of each `kind` landed in each
    `resolution_status`, for one repo at one commit. Distinct from
    `list_coverage`'s file-level counters — this is "how much of the graph's
    relationships are still unresolved/ambiguous", visible rather than
    hidden (build-plan.md Part 10's "unresolved/ambiguous visible")."""
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            select kind, resolution_status, count(*) as count
            from codeaxis.edges
            where repo_id = %s and commit_sha = %s
            group by kind, resolution_status
            order by kind, resolution_status
            """,
            (repo_id, commit_sha),
        ).fetchall()
    return [
        {"kind": kind, "resolution_status": resolution_status, "count": count}
        for kind, resolution_status, count in rows
    ]


def get_blob(conn: psycopg.Connection, repo_id: str, commit_sha: str, path: str) -> Blob | None:
    """Pinned source text for one file, or None if the scan stored none
    (oversized, unreadable, binary, or not scanned at this commit)."""
    with conn.cursor(row_factory=class_row(Blob)) as cur:
        return cur.execute(
            """
            select repo_id, commit_sha, path, content, content_hash from codeaxis.blobs
            where repo_id = %s and commit_sha = %s and path = %s
            """,
            (repo_id, commit_sha, path),
        ).fetchone()
