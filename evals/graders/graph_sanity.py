"""Phase 2 eval harness, started (build-plan.md Part 9/10): runs the real
scan pipeline against a fixture repo under `evals/repos/` and checks its
results against that fixture's hand-written `expected.json` -- known
imports (with resolution_status), entry points found, test associations,
and coverage numbers. Three fixtures only, as Part 10's Phase 2 line calls
for ("eval harness started"), not the full 8-10 golden-repo target.

Runs `app.analysis.jobs.run_scan_pass` directly against a disposable
(repo_id, commit_sha) under a disposable throwaway project, rather than
going through the HTTP API -- every assertion here is about what landed in
the Repo Model, so a TestClient round-trip would add nothing but noise.
Requires WORKER_DATABASE_URL (same convention as the live-DB tests under
services/api/tests/); skips with a clear message if it isn't set.

Usage:
    python evals/graders/graph_sanity.py               # all fixtures
    python evals/graders/graph_sanity.py py-minimal     # one fixture, by name
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_SRC = REPO_ROOT / "services" / "api"
sys.path.insert(0, str(API_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from app.analysis.jobs import run_scan_pass  # noqa: E402
from app.knowledge import (  # noqa: E402
    create_project,
    create_repo,
    list_coverage,
    list_nodes,
    neighborhood,
    worker_connection,
)
from app.models import Repo  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "evals" / "repos"
RELATIONSHIP_KINDS = ["imports", "calls", "extends", "implements", "tests"]
GRAPH_TABLES = ("blobs", "edges", "nodes", "coverage", "pass_runs")


def _new_user_id() -> str:
    return f"user_eval_{uuid.uuid4().hex}"


def _cleanup(repo_id: str, project_id: str, user_id: str) -> None:
    with worker_connection() as conn, conn.cursor() as cur:
        for table in GRAPH_TABLES:
            cur.execute(f"delete from codeaxis.{table} where repo_id = %s", (repo_id,))
        cur.execute("delete from codeaxis.repos where id = %s", (repo_id,))
        cur.execute("delete from codeaxis.project_memberships where project_id = %s", (project_id,))
        cur.execute("delete from codeaxis.projects where id = %s", (project_id,))
        cur.execute("delete from codeaxis.profiles where id = %s", (user_id,))
        conn.commit()


def run_fixture(fixture_dir: Path) -> list[str]:
    """Scans `fixture_dir` and checks it against its expected.json. Returns
    a list of failure descriptions (empty means it passed)."""
    expected_path = fixture_dir / "expected.json"
    if not expected_path.is_file():
        return [f"no expected.json in {fixture_dir}"]
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    commit_sha = f"eval-{uuid.uuid4().hex[:12]}"
    user_id = _new_user_id()

    with worker_connection() as conn:
        conn.execute(
            "insert into codeaxis.profiles (id, email) values (%s, %s)",
            (user_id, f"{user_id}@codeaxis.invalid"),
        )
        conn.commit()
        project = create_project(conn, f"eval-{fixture_dir.name}", user_id)
        repo = create_repo(
            conn, Repo(project_id=project.id, local_path=str(fixture_dir))
        )
    assert repo.id is not None
    repo_id = repo.id
    failures: list[str] = []

    try:
        run_scan_pass(repo_id, commit_sha, str(fixture_dir))

        with worker_connection() as conn:
            nodes = list_nodes(conn, repo_id, commit_sha)
            # imports/calls/extends/implements/tests edges are all
            # attributed to the *file* node, not the symbols inside it (see
            # app/analysis/scan/relationships.py), and a symbol node's own
            # `path` is the file that contains it -- so this must only
            # index file nodes, or a same-named symbol would shadow its
            # file in the lookup.
            by_path = {n.path: n for n in nodes if n.path is not None and n.kind == "file"}

            for exp in expected.get("imports", []):
                failures.extend(
                    _check_edge(
                        conn, repo_id, commit_sha, by_path, "imports", exp, require_status=True
                    )
                )

            for exp in expected.get("tests", []):
                failures.extend(
                    _check_edge(
                        conn, repo_id, commit_sha, by_path, "tests", exp, require_status=False
                    )
                )

            for path in expected.get("entry_points", []):
                node = by_path.get(path)
                if node is None:
                    failures.append(f"entry point check: no node at path {path!r}")
                elif node.attrs.get("entry_point") is not True:
                    failures.append(f"expected entry point not detected at {path!r}")

            coverage_rows = {c.metric: c for c in list_coverage(conn, repo_id, commit_sha)}
            for metric, exp_counts in expected.get("coverage", {}).items():
                row = coverage_rows.get(metric)
                if row is None:
                    failures.append(f"missing coverage metric: {metric!r}")
                    continue
                if row.resolved != exp_counts["resolved"] or row.total != exp_counts["total"]:
                    failures.append(
                        f"coverage {metric}: got {row.resolved}/{row.total}, "
                        f"expected {exp_counts['resolved']}/{exp_counts['total']}"
                    )
    finally:
        _cleanup(repo_id, project.id, user_id)

    return failures


def _check_edge(
    conn, repo_id: str, commit_sha: str, by_path: dict, kind: str, exp: dict, *, require_status: bool
) -> list[str]:
    src = by_path.get(exp["src_path"])
    if src is None or src.id is None:
        return [f"{kind} check: no source node at path {exp['src_path']!r}"]
    dst = by_path.get(exp["dst_path"])
    _, edges, _ = neighborhood(conn, repo_id, commit_sha, src.id, depth=1, kinds=[kind])
    match = next((e for e in edges if dst is not None and e.dst_id == dst.id), None)
    if match is None:
        return [f"expected {kind} edge {exp['src_path']} -> {exp['dst_path']} not found"]
    if require_status and match.resolution_status != exp.get("resolution_status"):
        return [
            f"{kind} edge {exp['src_path']} -> {exp['dst_path']} has "
            f"resolution_status={match.resolution_status!r}, "
            f"expected {exp.get('resolution_status')!r}"
        ]
    return []


def main(argv: list[str]) -> int:
    if not os.environ.get("WORKER_DATABASE_URL"):
        print("WORKER_DATABASE_URL not set -- skipping graph_sanity (no live DB available).")
        return 0

    if argv:
        fixtures = [FIXTURES_DIR / name for name in argv]
    else:
        fixtures = sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir())

    overall_ok = True
    for fixture_dir in fixtures:
        failures = run_fixture(fixture_dir)
        ok = len(failures) == 0
        overall_ok = overall_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {fixture_dir.name}")
        for f in failures:
            print(f"    - {f}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
