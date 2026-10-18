"""The Component Inspector's grounded-explanation endpoints (build-plan.md
Part 7): stored summaries served with their citations, and an honest 404 when
nothing was summarized. Generation never happens on request.

Runs against the live Supabase database, like the other repo-scoped route
tests, and goes through the real auth dependency with a locally minted token.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import pytest
from dotenv import load_dotenv

from app.knowledge import create_repo, insert_nodes, upsert_summaries, worker_connection
from app.models import Citation, Node, Repo, Summary, SummaryEvidence

load_dotenv()

pytestmark = pytest.mark.skipif(
    not os.environ.get("WORKER_DATABASE_URL"), reason="WORKER_DATABASE_URL not set"
)


@pytest.fixture
def seeded(db_project, make_user):
    """A repo with one file node, one stored file-level summary and one
    repo-level summary. Returns (repo_id, commit_sha, file_node_id)."""
    sha = f"test-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        repo = create_repo(conn, Repo(project_id=db_project, local_path="/nonexistent"))
        assert repo.id is not None
        (file_id,) = insert_nodes(
            conn,
            [
                Node(
                    repo_id=repo.id,
                    commit_sha=sha,
                    kind="file",
                    name="mod.py",
                    path="pkg/mod.py",
                    analyzer="test",
                    analyzer_version="0.0.0",
                    status="verified",
                )
            ],
        )
        upsert_summaries(
            conn,
            [
                Summary(
                    repo_id=repo.id,
                    commit_sha=sha,
                    level="file",
                    node_id=file_id,
                    scope_key="pkg/mod.py",
                    summary="Defines the module's two helpers.",
                    evidence=SummaryEvidence(
                        input="child_summaries",
                        chunk_ids=[7],
                        node_ids=[file_id],
                        spans=[
                            Citation(
                                path="pkg/mod.py", line_start=1, line_end=12, symbol="alpha"
                            )
                        ],
                        children=[{"level": "symbol", "scope_key": "pkg/mod.py#alpha@1-12"}],
                    ),
                    model="claude-haiku-5-5",
                    analyzer="semantic.summaries",
                    analyzer_version="0.1.0",
                ),
                Summary(
                    repo_id=repo.id,
                    commit_sha=sha,
                    level="repo",
                    scope_key="",
                    summary="A one-package Python project.",
                    evidence=SummaryEvidence(input="child_summaries"),
                    analyzer="semantic.summaries",
                    analyzer_version="0.1.0",
                ),
            ],
        )
    yield repo.id, sha, file_id
    with worker_connection() as conn, conn.cursor() as cur:
        for table in ("summaries", "conventions", "chunks", "blobs", "edges", "nodes",
                      "coverage", "pass_runs"):
            cur.execute(f"delete from codeaxis.{table} where repo_id = %s", (repo.id,))
        cur.execute("delete from codeaxis.repos where id = %s", (repo.id,))
        conn.commit()


def _owner(db_project: str) -> str:
    with worker_connection() as conn:
        row = conn.execute(
            "select owner_id from codeaxis.projects where id = %s", (db_project,)
        ).fetchone()
    assert row is not None
    return row[0]


def test_node_explanation_serves_stored_summary_with_citations(
    client, auth_header: Callable[[str], dict[str, str]], db_project, seeded
):
    repo_id, sha, file_id = seeded
    response = client.get(
        f"/repos/{repo_id}/nodes/{file_id}/explanation",
        params={"commit_sha": sha},
        headers=auth_header(_owner(db_project)),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["level"] == "file"
    assert body["scope_key"] == "pkg/mod.py"
    assert body["node_id"] == file_id
    assert body["summary"] == "Defines the module's two helpers."
    # Never presented as parsed fact.
    assert body["origin"] == "inferred"
    assert body["model"] == "claude-haiku-5-5"
    assert body["citations"] == [
        {"path": "pkg/mod.py", "line_start": 1, "line_end": 12, "symbol": "alpha"}
    ]
    assert body["evidence"]["input"] == "child_summaries"
    assert body["evidence"]["chunk_ids"] == [7]
    assert body["evidence"]["children"] == [
        {"level": "symbol", "scope_key": "pkg/mod.py#alpha@1-12"}
    ]


def test_scope_explanation_serves_the_repo_level(
    client, auth_header: Callable[[str], dict[str, str]], db_project, seeded
):
    repo_id, sha, _ = seeded
    response = client.get(
        f"/repos/{repo_id}/explanation",
        params={"commit_sha": sha, "level": "repo"},
        headers=auth_header(_owner(db_project)),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["level"] == "repo"
    assert body["scope_key"] == ""
    assert body["summary"] == "A one-package Python project."
    assert body["origin"] == "inferred"


def test_missing_summary_is_404_not_generated_on_request(
    client, auth_header: Callable[[str], dict[str, str]], db_project, seeded
):
    repo_id, sha, _ = seeded
    response = client.get(
        f"/repos/{repo_id}/explanation",
        params={"commit_sha": sha, "level": "component", "scope_key": "cluster-9"},
        headers=auth_header(_owner(db_project)),
    )
    assert response.status_code == 404
    assert "never generated on request" in response.json()["detail"]


def test_unknown_level_is_rejected(
    client, auth_header: Callable[[str], dict[str, str]], db_project, seeded
):
    repo_id, sha, _ = seeded
    response = client.get(
        f"/repos/{repo_id}/explanation",
        params={"commit_sha": sha, "level": "nonsense"},
        headers=auth_header(_owner(db_project)),
    )
    assert response.status_code == 422


def test_explanation_requires_membership(
    client, auth_header: Callable[[str], dict[str, str]], make_user, seeded
):
    repo_id, sha, file_id = seeded
    outsider = make_user()
    response = client.get(
        f"/repos/{repo_id}/nodes/{file_id}/explanation",
        params={"commit_sha": sha},
        headers=auth_header(outsider),
    )
    assert response.status_code == 403


def test_explanation_requires_a_token(client, seeded):
    repo_id, sha, file_id = seeded
    response = client.get(
        f"/repos/{repo_id}/nodes/{file_id}/explanation", params={"commit_sha": sha}
    )
    assert response.status_code == 401
