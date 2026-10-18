"""Shared fixture for Task C's context-engine tests (package, grounding,
export, reground): a real repository tree on disk, run through the real scan
pass (`app.analysis.jobs.run_scan_pass`: tree-sitter scan, relationship
extraction, architecture clustering, chunking) and the deterministic
conventions pass, against the live database.

Not a conftest: the four test files load it by path (pytest runs with
`--import-mode=importlib`, and `tests/` is not a package), which keeps this
directory free of a conftest another change might also want to own.

No model is called. The scan's summary pass (the one stage that can reach a
model) is replaced by the conventions pass with `service=None` — its
deterministic half — and `VOYAGE_API_KEY` is removed from the environment by
the root conftest's autouse fixture, so chunking records
`embedding_not_configured` instead of embedding.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest

from app.knowledge import create_project, create_repo, create_task, worker_connection
from app.models import Repo, Task

# A small but real repo: an import chain (api -> orders -> store), a test
# associated by naming + import, a call that resolves to nothing (so the
# limitation section has an honest gap to report), two files sharing a
# basename (so grounding has an ambiguous path to refuse), and a ruff config
# (so a `configured` convention exists).
FILES: dict[str, str] = {
    "svc/__init__.py": "",
    "svc/orders.py": (
        "from svc.store import save_order\n"
        "\n"
        "\n"
        "def place_order(cart):\n"
        '    """Validate a cart and persist it as an order."""\n'
        "    total = compute_total(cart)\n"
        "    return save_order(cart)\n"
    ),
    "svc/store.py": (
        "def save_order(order):\n"
        '    """Write one order row to the database."""\n'
        "    return order\n"
    ),
    "svc/api.py": (
        "from svc.orders import place_order\n"
        "\n"
        "\n"
        "def handle_checkout(request):\n"
        "    return place_order(request)\n"
    ),
    "svc/test_orders.py": (
        "from svc.orders import place_order\n"
        "\n"
        "\n"
        "def test_place_order():\n"
        "    assert place_order([]) == []\n"
    ),
    "svc/helpers.py": "def svc_helper():\n    return 1\n",
    "web/helpers.py": "def web_helper():\n    return 2\n",
    "web/ui.py": (
        "from web.helpers import web_helper\n"
        "\n"
        "\n"
        "def render_button(label):\n"
        "    return web_helper()\n"
    ),
    "pyproject.toml": '[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = ["E", "F"]\n',
}


def db_available() -> bool:
    return bool(os.environ.get("WORKER_DATABASE_URL") and os.environ.get("DATABASE_URL"))


needs_db = pytest.mark.skipif(
    not db_available(), reason="DATABASE_URL / WORKER_DATABASE_URL not set"
)


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def scan_into(repo_id: str, commit_sha: str, root: Path) -> None:
    """Run the shipped scan pass over `root` as snapshot `commit_sha`, with the
    model-backed summary stage swapped for deterministic conventions only."""
    from app.analysis import jobs
    from app.analysis.semantic.conventions import write_conventions_pass

    def conventions_only(conn: psycopg.Connection, rid: str, sha: str) -> None:
        write_conventions_pass(conn, rid, sha, service=None)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(jobs, "write_semantic_pass", conventions_only)
        mp.delenv("VOYAGE_API_KEY", raising=False)
        jobs.run_scan_pass(repo_id, commit_sha, str(root))


@dataclass(frozen=True)
class ScannedRepo:
    user_id: str
    project_id: str
    repo_id: str
    commit_sha: str
    root: Path


def create_scanned_repo(root: Path, files: dict[str, str] | None = None) -> ScannedRepo:
    """A fresh profile, project and repo, scanned once. Pair with
    `delete_scanned_repo` in a fixture's teardown."""
    owner_url = os.environ["DATABASE_URL"]
    user_id = f"user_{uuid.uuid4().hex}"
    with psycopg.connect(owner_url, connect_timeout=10) as conn:
        conn.execute(
            "insert into codeaxis.profiles (id, email) values (%s, %s)",
            (user_id, f"{user_id}@codeaxis.invalid"),
        )
        conn.commit()
    write_tree(root, files or FILES)
    sha = f"ctxc-{uuid.uuid4().hex[:12]}"
    with worker_connection() as conn:
        project = create_project(conn, "task c context test", user_id)
        repo = create_repo(conn, Repo(project_id=project.id, local_path=str(root)))
    assert repo.id is not None
    scan_into(repo.id, sha, root)
    return ScannedRepo(user_id, project.id, repo.id, sha, root)


def delete_scanned_repo(repo: ScannedRepo) -> None:
    """Graph tables have no cascade from repos (context, chunk and convention
    tables do), so they go first, then the repo, project and profile."""
    owner_url = os.environ["DATABASE_URL"]
    with psycopg.connect(owner_url, connect_timeout=10) as conn:
        conn.execute("delete from codeaxis.tasks where repo_id = %s", (repo.repo_id,))
        for table in ("chunks", "conventions", "summaries", "blobs", "edges", "nodes"):
            conn.execute(f"delete from codeaxis.{table} where repo_id = %s", (repo.repo_id,))
        for table in ("coverage", "pass_runs"):
            conn.execute(f"delete from codeaxis.{table} where repo_id = %s", (repo.repo_id,))
        conn.execute("delete from codeaxis.repos where id = %s", (repo.repo_id,))
        conn.execute("delete from codeaxis.projects where id = %s", (repo.project_id,))
        conn.execute("delete from codeaxis.profiles where id = %s", (repo.user_id,))
        conn.commit()


def new_task(repo: ScannedRepo, intent: str, *, title: str = "Change checkout") -> Task:
    with worker_connection() as conn:
        task = create_task(
            conn,
            Task(
                project_id=repo.project_id,
                repo_id=repo.repo_id,
                commit_sha=repo.commit_sha,
                title=title,
                intent=intent,
                created_by=repo.user_id,
            ),
        )
    assert task.id is not None
    return task
