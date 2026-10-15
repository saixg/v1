"""Bulk writers for analyzer output. Only services/worker should call these —
the one-way rule in docs/03-architecture.md.
"""

from __future__ import annotations

import json

import psycopg

from codeaxis_contracts import Blob, Coverage, Edge, Node


def insert_nodes(conn: psycopg.Connection, nodes: list[Node]) -> list[int]:
    """Insert nodes, returning their assigned ids in the same order."""
    ids: list[int] = []
    with conn.cursor() as cur:
        for node in nodes:
            row = cur.execute(
                """
                insert into codeaxis.nodes (
                    repo_id, commit_sha, kind, name, path,
                    line_start, line_end, col_start, col_end, attrs,
                    analyzer, analyzer_version, status, source_file, confidence, reason
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    node.repo_id,
                    node.commit_sha,
                    node.kind,
                    node.name,
                    node.path,
                    node.line_start,
                    node.line_end,
                    node.col_start,
                    node.col_end,
                    json.dumps(node.attrs),
                    node.analyzer,
                    node.analyzer_version,
                    node.status,
                    node.source_file,
                    node.confidence,
                    node.reason,
                ),
            ).fetchone()
            assert row is not None
            ids.append(row[0])
    conn.commit()
    return ids


def insert_edges(conn: psycopg.Connection, edges: list[Edge]) -> list[int]:
    """Insert edges, returning their assigned ids in the same order."""
    ids: list[int] = []
    with conn.cursor() as cur:
        for edge in edges:
            row = cur.execute(
                """
                insert into codeaxis.edges (
                    repo_id, commit_sha, kind, src_id, dst_id,
                    line_start, line_end, col_start, col_end,
                    analyzer, analyzer_version, status, source_file, confidence, reason
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    edge.repo_id,
                    edge.commit_sha,
                    edge.kind,
                    edge.src_id,
                    edge.dst_id,
                    edge.line_start,
                    edge.line_end,
                    edge.col_start,
                    edge.col_end,
                    edge.analyzer,
                    edge.analyzer_version,
                    edge.status,
                    edge.source_file,
                    edge.confidence,
                    edge.reason,
                ),
            ).fetchone()
            assert row is not None
            ids.append(row[0])
    conn.commit()
    return ids


def upsert_coverage(conn: psycopg.Connection, rows: list[Coverage]) -> None:
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                insert into codeaxis.coverage
                    (repo_id, commit_sha, area, metric, resolved, total, blockers)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (repo_id, commit_sha, area, metric)
                do update set resolved = excluded.resolved, total = excluded.total,
                              blockers = excluded.blockers
                """,
                (
                    row.repo_id,
                    row.commit_sha,
                    row.area,
                    row.metric,
                    row.resolved,
                    row.total,
                    json.dumps(row.blockers),
                ),
            )
    conn.commit()


def upsert_blobs(conn: psycopg.Connection, rows: list[Blob]) -> None:
    """Store each file's pinned source text, keyed by (repo, commit, path)."""
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                insert into codeaxis.blobs (repo_id, commit_sha, path, content, content_hash)
                values (%s, %s, %s, %s, %s)
                on conflict (repo_id, commit_sha, path)
                do update set content = excluded.content, content_hash = excluded.content_hash
                """,
                (row.repo_id, row.commit_sha, row.path, row.content, row.content_hash),
            )
    conn.commit()
