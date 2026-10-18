"""Hybrid retrieval (build-plan.md Part 4.E).

Two halves:

- Pure-logic tests for the parts that decide what evidence an answer gets:
  identifier extraction, reciprocal-rank fusion, and token-budgeted assembly.
  No database, no model, no API key.
- Live-DB tests for the SQL stages and the `/repos/{id}/search` route, which
  seed real `codeaxis.chunks` rows and then assert on what came back. They
  skip with a clear reason when the semantic migration is not applied, rather
  than failing on a missing table.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from app.analysis.semantic.retrieve import (
    RRF_K,
    STAGE_WEIGHTS,
    ModelServiceEmbedder,
    RetrievalQuery,
    assemble,
    estimate_tokens,
    identifier_candidates,
    reciprocal_rank_fusion,
    retrieve,
)
from app.knowledge import api_connection, worker_connection
from app.knowledge.chunks import count_chunks, insert_chunks
from app.knowledge.retrieval import (
    exact_symbol_chunks,
    file_line_counts,
    fts_chunks,
    symbols_by_path,
)
from app.models import Chunk, ScoredChunk

load_dotenv()


# ------------------------------------------------------------- pure logic


def test_identifier_candidates_keeps_code_shaped_tokens() -> None:
    found = identifier_candidates("How does run_scan_job handle a failed PassRun?")
    assert "run_scan_job" in found
    assert "PassRun" in found
    # Ordinary question words are not identifiers.
    assert "does" not in found
    assert "handle" not in found


def test_identifier_candidates_drops_short_tokens_and_dedupes() -> None:
    found = identifier_candidates("is a of retrieve retrieve")
    assert found == ["retrieve"]


def test_identifier_candidates_keeps_first_appearance_order() -> None:
    assert identifier_candidates("beta_one alpha_two beta_one") == ["beta_one", "alpha_two"]


def _chunk(chunk_id: int, path: str = "a.py", tokens: int = 10) -> ScoredChunk:
    return ScoredChunk(
        id=chunk_id,
        repo_id=str(uuid.uuid4()),
        commit_sha="sha",
        path=path,
        kind="symbol",
        line_start=1,
        line_end=2,
        content="x = 1",
        content_hash="h",
        token_count=tokens,
        analyzer="test",
        analyzer_version="1",
    )


def test_rrf_rewards_agreement_across_stages() -> None:
    a, b = _chunk(1), _chunk(2)
    # b is second in both stages; a is first in one and absent from the other.
    fused = reciprocal_rank_fusion([("fts", [a, b]), ("vector", [b])])
    by_id = {f.chunk.id: f for f in fused}
    expected_b = STAGE_WEIGHTS["fts"] / (RRF_K + 2) + STAGE_WEIGHTS["vector"] / (RRF_K + 1)
    assert by_id[2].score == pytest.approx(expected_b)
    assert by_id[1].score == pytest.approx(STAGE_WEIGHTS["fts"] / (RRF_K + 1))
    assert by_id[2].stages == ["fts", "vector"]


def test_rrf_weights_exact_symbol_above_keyword() -> None:
    a, b = _chunk(1), _chunk(2)
    fused = reciprocal_rank_fusion([("exact_symbol", [b]), ("fts", [a])])
    assert [f.chunk.id for f in fused] == [2, 1]


def test_rrf_is_deterministic_on_ties() -> None:
    a = _chunk(1, path="z.py")
    b = _chunk(2, path="a.py")
    first = reciprocal_rank_fusion([("fts", [a]), ("vector", [b])])
    second = reciprocal_rank_fusion([("vector", [b]), ("fts", [a])])
    assert [f.chunk.id for f in first] == [f.chunk.id for f in second]


def test_assembly_enforces_the_token_budget() -> None:
    fused = reciprocal_rank_fusion(
        [("fts", [_chunk(1, tokens=40), _chunk(2, tokens=40), _chunk(3, tokens=40)])]
    )
    kept, used, dropped = assemble(fused, token_budget=100, limit=10)
    assert len(kept) == 2
    assert used == 80
    assert dropped == 1


def test_assembly_skips_an_oversized_chunk_without_truncating_the_rest() -> None:
    fused = reciprocal_rank_fusion([("fts", [_chunk(1, tokens=500), _chunk(2, tokens=10)])])
    kept, used, dropped = assemble(fused, token_budget=100, limit=10)
    assert [k.chunk.id for k in kept] == [2]
    assert (used, dropped) == (10, 1)


def test_assembly_respects_the_result_limit() -> None:
    fused = reciprocal_rank_fusion([("fts", [_chunk(i, tokens=1) for i in range(1, 11)])])
    kept, _used, dropped = assemble(fused, token_budget=10_000, limit=3)
    assert len(kept) == 3
    assert dropped == 7


def test_assembly_falls_back_to_an_estimate_when_token_count_is_zero() -> None:
    chunk = _chunk(1, tokens=0)
    chunk.content = "x" * 400
    fused = reciprocal_rank_fusion([("fts", [chunk])])
    _kept, used, _dropped = assemble(fused, token_budget=10_000, limit=5)
    assert used == estimate_tokens(chunk.content) == 100


class _BrokenEmbed:
    """Stands in for `ModelService.embed` before an embedding provider is
    wired: the real one raises NotImplementedError today."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("no embedding provider")


class _EmptyEmbed:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return []


def test_embedder_degrades_to_none_instead_of_raising() -> None:
    assert ModelServiceEmbedder(_BrokenEmbed()).embed_query("q") is None
    assert ModelServiceEmbedder(_EmptyEmbed()).embed_query("q") is None
    assert ModelServiceEmbedder(object()).embed_query("q") is None


# --------------------------------------------------------------- live DB

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)


def _semantic_schema_present() -> bool:
    if not os.environ.get("WORKER_DATABASE_URL"):
        return False
    with worker_connection() as conn:
        row = conn.execute(
            "select 1 from information_schema.tables "
            "where table_schema = 'codeaxis' and table_name = 'chunks'"
        ).fetchone()
    return row is not None


needs_semantic_schema = pytest.mark.skipif(
    not _semantic_schema_present(),
    reason="supabase/migrations/20261019000000_semantic.sql is not applied to this database",
)

FIXTURE_FILES = {
    "svc/orders.py": (
        "from svc.store import save_order\n"
        "\n"
        "def place_order(cart):\n"
        '    """Validate a cart and persist it as an order."""\n'
        "    return save_order(cart)\n"
    ),
    "svc/store.py": (
        "def save_order(order):\n"
        '    """Write one order row to the database."""\n'
        "    return order\n"
    ),
    "web/ui.py": ("def render_button(label):\n    return f'<button>{label}</button>'\n"),
}

CHUNK_SPECS = [
    ("svc/orders.py", "place_order", 3, 5),
    ("svc/store.py", "save_order", 1, 3),
    ("web/ui.py", "render_button", 1, 2),
]


@pytest.fixture
def owner(make_user) -> tuple[str, str]:
    """A fresh user and a project they own. Returned together because the
    route tests need the user id to mint that user's bearer token."""
    from app.knowledge import create_project

    user_id = make_user()
    with worker_connection() as conn:
        project = create_project(conn, "retrieval test", user_id)
    return user_id, project.id


@pytest.fixture
def seeded_repo(owner: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """A repo at one commit with blobs, file nodes and chunks for three files.

    Chunk rows are written through `app.knowledge.chunks.insert_chunks` — the
    real writer against the real table — so these tests exercise the shipped
    SQL, not a hand-rolled approximation of it."""
    from app.knowledge import create_repo, insert_nodes, upsert_blobs
    from app.models import Blob, Node, Repo

    commit_sha = f"ret-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        repo = create_repo(conn, Repo(project_id=owner[1], local_path="/nonexistent"))
        assert repo.id is not None
        repo_id = repo.id

        upsert_blobs(
            conn,
            [
                Blob(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    path=path,
                    content=content,
                    content_hash=f"hash-{path}",
                )
                for path, content in FIXTURE_FILES.items()
            ],
        )
        file_ids = insert_nodes(
            conn,
            [
                Node(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="file",
                    name=path.rsplit("/", 1)[-1],
                    path=path,
                    analyzer="test",
                    analyzer_version="1",
                    status="verified",
                )
                for path in FIXTURE_FILES
            ],
        )
        symbol_ids = insert_nodes(
            conn,
            [
                Node(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="symbol",
                    name=symbol,
                    path=path,
                    line_start=start,
                    line_end=end,
                    analyzer="test",
                    analyzer_version="1",
                    status="verified",
                )
                for path, symbol, start, end in CHUNK_SPECS
            ],
        )
        by_symbol = dict(zip([spec[1] for spec in CHUNK_SPECS], symbol_ids, strict=True))
        assert len(file_ids) == 3

        insert_chunks(
            conn,
            [
                Chunk(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    node_id=by_symbol[symbol],
                    path=path,
                    symbol=symbol,
                    kind="symbol",
                    line_start=start,
                    line_end=end,
                    content="\n".join(
                        FIXTURE_FILES[path].split("\n")[start - 1 : end]
                    ),
                    content_hash=f"chunk-{path}-{symbol}",
                    token_count=20,
                    analyzer="test",
                    analyzer_version="1",
                )
                for path, symbol, start, end in CHUNK_SPECS
            ],
        )
    try:
        yield repo_id, commit_sha
    finally:
        with worker_connection() as conn, conn.cursor() as cur:
            for table in ("chunks", "blobs", "edges", "nodes", "coverage", "pass_runs"):
                cur.execute(f"delete from codeaxis.{table} where repo_id = %s", (repo_id,))
            cur.execute("delete from codeaxis.repos where id = %s", (repo_id,))
            conn.commit()


@needs_semantic_schema
def test_exact_symbol_stage_finds_the_named_symbol(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        hits = exact_symbol_chunks(conn, repo_id, commit_sha, ["save_order"])
    assert [h.path for h in hits] == ["svc/store.py"]


@needs_semantic_schema
def test_exact_symbol_stage_ranks_case_sensitive_matches_first(
    seeded_repo: tuple[str, str],
) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        hits = exact_symbol_chunks(conn, repo_id, commit_sha, ["SAVE_ORDER"])
    # Folded match still found, at the lower weight.
    assert [h.symbol for h in hits] == ["save_order"]
    assert hits[0].score == pytest.approx(0.5)


@needs_semantic_schema
def test_fts_stage_matches_docstring_prose(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        hits = fts_chunks(conn, repo_id, commit_sha, "persist an order")
    assert {h.path for h in hits} >= {"svc/orders.py"}
    assert all(h.score > 0 for h in hits)


@needs_semantic_schema
def test_fts_stage_tolerates_an_unparseable_query(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        # Raw tsquery operators would be a syntax error with to_tsquery.
        assert fts_chunks(conn, repo_id, commit_sha, "&&& ||| !") == []


@needs_semantic_schema
def test_strict_fts_requires_every_term(seeded_repo: tuple[str, str]) -> None:
    """websearch_to_tsquery ANDs its terms, so a prose question whose words
    are not all present matches nothing. Pinned because the relaxed fallback
    below exists precisely to cover it."""
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        assert fts_chunks(conn, repo_id, commit_sha, "where is place_order defined?") == []


@needs_semantic_schema
def test_relaxed_fts_matches_any_term(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        hits = fts_chunks(
            conn, repo_id, commit_sha, "where is place_order defined?", relaxed=True
        )
    assert "svc/orders.py" in {h.path for h in hits}


@needs_semantic_schema
def test_relaxed_fts_preserves_the_quoted_phrase_operator(
    seeded_repo: tuple[str, str],
) -> None:
    """The `&` -> `|` rewrite runs on Postgres's parsed tsquery, so a quoted
    phrase stays a phrase (`<->`) and does not degrade into separate terms."""
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        assert (
            fts_chunks(
                conn, repo_id, commit_sha, '"nonexistent adjacent phrase"', relaxed=True
            )
            == []
        )


@needs_semantic_schema
def test_pipeline_reports_when_it_relaxed_the_keyword_query(
    seeded_repo: tuple[str, str],
) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        result = retrieve(
            conn,
            repo_id,
            commit_sha,
            RetrievalQuery(text="where is the order persisted exactly?", hops=0),
        )
    fts = next(s for s in result.stages if s.name == "fts")
    assert fts.status == "ran"
    assert fts.reason and "relaxed" in fts.reason


@needs_semantic_schema
def test_path_filter_excludes_other_directories(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        hits = fts_chunks(conn, repo_id, commit_sha, "order", path_prefixes=["web/"])
    assert hits == []


@needs_semantic_schema
def test_path_filter_matches_prefix_literally(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        # A LIKE wildcard must be matched as a literal character, not as "any".
        assert exact_symbol_chunks(
            conn, repo_id, commit_sha, ["save_order"], path_prefixes=["%"]
        ) == []
        assert exact_symbol_chunks(
            conn, repo_id, commit_sha, ["save_order"], path_prefixes=["svc/"]
        )


@needs_semantic_schema
def test_counts_report_no_embeddings_before_the_backfill(
    seeded_repo: tuple[str, str],
) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        total, embedded = count_chunks(conn, repo_id, commit_sha)
    assert (total, embedded) == (3, 0)


@needs_semantic_schema
def test_citation_lookups_read_the_snapshot(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        counts = file_line_counts(
            conn, repo_id, commit_sha, ["svc/store.py", "does/not/exist.py"]
        )
        symbols = symbols_by_path(conn, repo_id, commit_sha, ["svc/store.py"])
    assert counts["svc/store.py"] == len(FIXTURE_FILES["svc/store.py"].split("\n"))
    assert "does/not/exist.py" not in counts
    assert "save_order" in symbols["svc/store.py"]


@needs_semantic_schema
def test_pipeline_runs_without_embeddings_and_says_so(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        result = retrieve(
            conn,
            repo_id,
            commit_sha,
            RetrievalQuery(text="where is save_order persisted?"),
        )
    stages = {s.name: s for s in result.stages}
    assert stages["exact_symbol"].status == "ran"
    assert stages["fts"].status == "ran"
    # No embedder was supplied and nothing is embedded: skipped, with a reason.
    assert stages["vector"].status == "skipped"
    assert stages["vector"].reason
    assert stages["rerank"].status == "skipped"
    assert result.items, "FTS + exact symbol must answer on their own"
    assert all(c.path and c.line_start >= 1 for c in result.citations())


@needs_semantic_schema
def test_pipeline_budget_is_a_hard_constraint(seeded_repo: tuple[str, str]) -> None:
    repo_id, commit_sha = seeded_repo
    with api_connection() as conn:
        result = retrieve(
            conn, repo_id, commit_sha, RetrievalQuery(text="order", token_budget=25)
        )
    assert result.tokens_used <= 25
    assert result.dropped_for_budget >= 1


@needs_semantic_schema
def test_graph_expansion_pulls_in_a_related_file(
    seeded_repo: tuple[str, str],
) -> None:
    """An `imports` edge between the two file nodes must make the importer's
    chunks reachable from a query that only matches the imported file."""
    from app.knowledge import insert_edges, list_nodes
    from app.models import Edge

    repo_id, commit_sha = seeded_repo
    with worker_connection() as conn:
        files = {
            n.path: n.id for n in list_nodes(conn, repo_id, commit_sha, kind="file")
        }
        src = files["svc/orders.py"]
        dst = files["svc/store.py"]
        assert src is not None and dst is not None
        insert_edges(
            conn,
            [
                Edge(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="imports",
                    src_id=src,
                    dst_id=dst,
                    resolution_status="resolved",
                    analyzer="test",
                    analyzer_version="1",
                    status="verified",
                )
            ],
        )
    with api_connection() as conn:
        result = retrieve(
            conn,
            repo_id,
            commit_sha,
            RetrievalQuery(text="save_order", hops=1),
        )
    stages = {s.name: s for s in result.stages}
    assert stages["graph"].status == "ran"
    paths = {item.chunk.path for item in result.items}
    assert "svc/orders.py" in paths, "the importer should be reachable by expansion"


@needs_semantic_schema
def test_search_route_requires_membership(
    client: TestClient, seeded_repo: tuple[str, str], auth_header, make_user
) -> None:
    repo_id, commit_sha = seeded_repo
    resp = client.get(
        f"/repos/{repo_id}/search",
        params={"commit_sha": commit_sha, "q": "order"},
        headers=auth_header(make_user()),
    )
    assert resp.status_code == 403, resp.text


@needs_semantic_schema
def test_search_route_rejects_an_anonymous_caller(
    client: TestClient, seeded_repo: tuple[str, str]
) -> None:
    repo_id, commit_sha = seeded_repo
    resp = client.get(
        f"/repos/{repo_id}/search", params={"commit_sha": commit_sha, "q": "order"}
    )
    assert resp.status_code == 401


@needs_semantic_schema
def test_search_route_returns_cited_spans(
    client: TestClient, seeded_repo: tuple[str, str], auth_header, owner: tuple[str, str]
) -> None:
    repo_id, commit_sha = seeded_repo
    resp = client.get(
        f"/repos/{repo_id}/search",
        params={"commit_sha": commit_sha, "q": "save_order"},
        headers=auth_header(owner[0]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"], body
    first = body["results"][0]
    assert first["path"] and first["line_start"] >= 1 and first["content"]
    assert {s["name"] for s in body["stages"]} == {
        "exact_symbol",
        "filters",
        "fts",
        "vector",
        "graph",
        "rerank",
    }
    assert body["budget"]["tokens_used"] <= body["budget"]["token_budget"]


@needs_semantic_schema
def test_search_route_applies_path_filter(
    client: TestClient, seeded_repo: tuple[str, str], auth_header, owner: tuple[str, str]
) -> None:
    repo_id, commit_sha = seeded_repo
    resp = client.get(
        f"/repos/{repo_id}/search",
        params={
            "commit_sha": commit_sha,
            "q": "order",
            "path_prefix": "web/",
            "hops": 0,
        },
        headers=auth_header(owner[0]),
    )
    assert resp.status_code == 200, resp.text
    assert all(r["path"].startswith("web/") for r in resp.json()["results"])


def test_untracked_table_surfaces_as_unavailable_not_a_crash() -> None:
    """`psycopg.errors.UndefinedTable` is the exception the route maps to 503.
    Pinned as a test so a psycopg upgrade that renames it is caught here."""
    assert issubclass(psycopg.errors.UndefinedTable, psycopg.Error)
