"""codeaxis.chunks (read) / codeaxis.summaries / codeaxis.conventions.

The semantic layer's slice of the Repo Model. `app.knowledge` stays the only
module that knows SQL, so the summary and convention passes in
`app.analysis.semantic` go through here rather than issuing their own queries.

Chunks themselves are read through `app.knowledge.chunks.list_chunks`: the
summary pass consumes the *table*, never the chunker's Python API, so the two
passes are coupled only by the schema the migration defines.
"""

from __future__ import annotations

import json

import psycopg
from psycopg.rows import DictRow, class_row, dict_row

from app.models import Convention, Summary, SummaryEvidence

_SUMMARY_COLUMNS = """
    id, repo_id, commit_sha, level, node_id, scope_key, summary, origin,
    evidence, model, analyzer, analyzer_version, created_at
"""

_CONVENTION_COLUMNS = """
    id, repo_id, commit_sha, category, rule, origin, confidence::float8 as confidence,
    evidence, analyzer, analyzer_version, created_at
"""


# --- summaries --------------------------------------------------------------


def upsert_summaries(conn: psycopg.Connection, rows: list[Summary]) -> None:
    """Write summaries, replacing any existing row for the same
    (repo, commit, level, scope_key) — re-running the pass for a commit
    refreshes its prose rather than accumulating duplicates."""
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                insert into codeaxis.summaries (
                    repo_id, commit_sha, level, node_id, scope_key, summary,
                    origin, evidence, model, analyzer, analyzer_version
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                on conflict (repo_id, commit_sha, level, scope_key)
                do update set node_id = excluded.node_id, summary = excluded.summary,
                              origin = excluded.origin, evidence = excluded.evidence,
                              model = excluded.model, analyzer = excluded.analyzer,
                              analyzer_version = excluded.analyzer_version
                """,
                (
                    row.repo_id,
                    row.commit_sha,
                    row.level,
                    row.node_id,
                    row.scope_key,
                    row.summary,
                    row.origin,
                    row.evidence.evidence_json(),
                    row.model,
                    row.analyzer,
                    row.analyzer_version,
                ),
            )
    conn.commit()


def _summary_rows(rows: list[DictRow]) -> list[Summary]:
    out: list[Summary] = []
    for row in rows:
        data = dict(row)
        evidence = data.get("evidence") or {}
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        data["evidence"] = SummaryEvidence.model_validate(
            evidence if evidence else {"input": "chunks"}
        )
        out.append(Summary.model_validate(data))
    return out


def list_summaries(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, level: str | None = None
) -> list[Summary]:
    clauses = ["repo_id = %s", "commit_sha = %s"]
    params: list[object] = [repo_id, commit_sha]
    if level is not None:
        clauses.append("level = %s")
        params.append(level)
    query = (
        f"select {_SUMMARY_COLUMNS} from codeaxis.summaries where "
        + " and ".join(clauses)
        + " order by level, scope_key"
    )
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(query, params).fetchall()  # pyright: ignore[reportArgumentType]
    return _summary_rows(rows)


def get_summary(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, level: str, scope_key: str
) -> Summary | None:
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            select {_SUMMARY_COLUMNS} from codeaxis.summaries
            where repo_id = %s and commit_sha = %s and level = %s and scope_key = %s
            """,
            (repo_id, commit_sha, level, scope_key),
        ).fetchall()
    summaries = _summary_rows(rows)
    return summaries[0] if summaries else None


def get_summary_for_node(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, node_id: int
) -> Summary | None:
    """The stored summary about one graph node (a symbol, a file, or a
    cluster/component). Never generates one: generation belongs to the
    analysis pass, and a missing row is an honest "not summarized"."""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            f"""
            select {_SUMMARY_COLUMNS} from codeaxis.summaries
            where repo_id = %s and commit_sha = %s and node_id = %s
            order by level limit 1
            """,
            (repo_id, commit_sha, node_id),
        ).fetchall()
    summaries = _summary_rows(rows)
    return summaries[0] if summaries else None


# --- conventions ------------------------------------------------------------


def upsert_conventions(conn: psycopg.Connection, rows: list[Convention]) -> None:
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                insert into codeaxis.conventions (
                    repo_id, commit_sha, category, rule, origin, confidence,
                    evidence, analyzer, analyzer_version
                )
                values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                on conflict (repo_id, commit_sha, category, rule)
                do update set origin = excluded.origin, confidence = excluded.confidence,
                              evidence = excluded.evidence, analyzer = excluded.analyzer,
                              analyzer_version = excluded.analyzer_version
                """,
                (
                    row.repo_id,
                    row.commit_sha,
                    row.category,
                    row.rule,
                    row.origin,
                    row.confidence,
                    json.dumps(row.evidence),
                    row.analyzer,
                    row.analyzer_version,
                ),
            )
    conn.commit()


def list_conventions(
    conn: psycopg.Connection, repo_id: str, commit_sha: str
) -> list[Convention]:
    with conn.cursor(row_factory=class_row(Convention)) as cur:
        return cur.execute(
            f"""
            select {_CONVENTION_COLUMNS} from codeaxis.conventions
            where repo_id = %s and commit_sha = %s
            order by category, rule
            """,
            (repo_id, commit_sha),
        ).fetchall()
