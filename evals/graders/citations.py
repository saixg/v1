"""Grounding + retrieval grader (build-plan.md Part 9).

Two of the Part 9 eval rows, automated:

- **Grounding** — "every cited path/symbol/line must exist (automated);
  unsupported-claim rate". A corpus of narrated answers, each with a known
  verdict, is run through the real citation validator against the fixture
  repository on disk. The grader fails if any verdict differs or if the
  measured unsupported-claim rate is not the expected one.
- **Retrieval** — "question -> expected-files pairs; recall@k / MRR". Each
  fixture's questions are run through the real hybrid retrieval pipeline
  against the real SQL, and recall@k and MRR are reported and thresholded.

Both halves grade *deterministic* machinery. The grounding half needs neither
a database nor an API key: the validator takes a `SnapshotIndex` protocol, and
`FixtureSnapshotIndex` below is that protocol backed by files on disk, so the
same code the API runs is graded here with nothing mocked but the backing
store. The retrieval half needs a database (WORKER_DATABASE_URL) because
FTS is Postgres's, not Python's — grading a reimplementation of it would grade
the wrong thing. It skips, loudly, when the database or the semantic migration
is unavailable.

No model is ever called. The "model answers" in the grounding corpus are
fixed, hand-written payloads: the thing under test is the validator that
catches a bad citation, not a model's ability to produce one.

Usage:
    python evals/graders/citations.py                 # everything
    python evals/graders/citations.py grounding       # grounding only
    python evals/graders/citations.py retrieval       # retrieval only
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_SRC = REPO_ROOT / "services" / "api"
sys.path.insert(0, str(API_SRC))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from app.analysis.semantic.citations import (  # noqa: E402
    LINE_OUT_OF_RANGE,
    LINE_RANGE_INVERTED,
    PATH_NOT_IN_SNAPSHOT,
    SYMBOL_NOT_IN_FILE,
    validate_claim_citations,
)
from app.models import Citation  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "evals" / "repos"

# Grading thresholds. Deliberately strict on grounding (the validator is
# deterministic: a wrong verdict is a defect, not a regression in quality) and
# honest about retrieval, where the numbers are what they are and the job of
# the threshold is to catch a fall, not to flatter the pipeline.
REQUIRED_RECALL_AT_K = 1.0
REQUIRED_MRR = 0.5
RECALL_K = 5


# ------------------------------------------------------- snapshot on disk


class FixtureSnapshotIndex:
    """`SnapshotIndex` over a fixture directory, read straight from disk.

    Line counts are computed the same way the SQL reader computes them
    (newline count + 1), so a citation graded here and the same citation
    checked against the database get the same answer."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lines: dict[str, int] = {}
        self._symbols: dict[str, set[str]] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name == "expected.json":
                continue
            rel = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            self._lines[rel] = text.count("\n") + 1
            self._symbols[rel] = _declared_names(text) | {path.name}

    def line_count(self, path: str) -> int | None:
        return self._lines.get(path)

    def symbols(self, path: str) -> set[str]:
        return self._symbols.get(path, set())


def _declared_names(text: str) -> set[str]:
    """Names a fixture file declares.

    Deliberately crude — these fixtures are a handful of tiny Python and
    TypeScript files, and this stands in for the `codeaxis.nodes` rows the
    database-backed index reads. It is not a parser and is not used by
    anything the API serves."""
    names: set[str] = set()
    for raw in text.split("\n"):
        line = raw.strip()
        for keyword in ("def ", "class ", "function ", "export function "):
            if line.startswith(keyword):
                rest = line[len(keyword) :]
                name = rest.split("(")[0].split(":")[0].split("<")[0].strip()
                if name:
                    names.add(name)
    return names


# ---------------------------------------------------- grounding corpus


@dataclass(frozen=True)
class GroundingCase:
    """One narrated answer with a known verdict.

    `claims` is `[(claim text, [citation, ...]), ...]`. `expected_reasons` is
    the rejection reason expected for each bad citation, in order; an empty
    list means the whole answer must pass."""

    name: str
    fixture: str
    claims: list[tuple[str, list[Citation]]]
    expected_reasons: list[str]
    expected_unsupported_claims: int


def _c(path: str, start: int, end: int, symbol: str | None = None) -> Citation:
    return Citation(path=path, line_start=start, line_end=end, symbol=symbol)


GROUNDING_CASES: list[GroundingCase] = [
    GroundingCase(
        name="grounded answer over two files",
        fixture="py-minimal",
        claims=[
            ("main() delegates to helper().", [_c("a.py", 4, 5, "main")]),
            ("helper() returns the string 'hi'.", [_c("b.py", 1, 2, "helper")]),
        ],
        expected_reasons=[],
        expected_unsupported_claims=0,
    ),
    GroundingCase(
        name="invented file path",
        fixture="py-minimal",
        claims=[("The handler lives in services/handler.py.", [_c("services/handler.py", 1, 10)])],
        expected_reasons=[PATH_NOT_IN_SNAPSHOT],
        expected_unsupported_claims=1,
    ),
    GroundingCase(
        name="real file, line past its end",
        fixture="py-minimal",
        claims=[("helper() is defined near the bottom of b.py.", [_c("b.py", 40, 60)])],
        expected_reasons=[LINE_OUT_OF_RANGE],
        expected_unsupported_claims=1,
    ),
    GroundingCase(
        name="inverted line range",
        fixture="py-minimal",
        claims=[("main() is here.", [_c("a.py", 9, 2)])],
        expected_reasons=[LINE_RANGE_INVERTED],
        expected_unsupported_claims=1,
    ),
    GroundingCase(
        name="invented symbol in a real file",
        fixture="py-minimal",
        claims=[("b.py defines a cache.", [_c("b.py", 1, 2, "build_cache")])],
        expected_reasons=[SYMBOL_NOT_IN_FILE],
        expected_unsupported_claims=1,
    ),
    GroundingCase(
        name="one good claim, one hallucinated",
        fixture="py-minimal",
        claims=[
            ("helper() returns 'hi'.", [_c("b.py", 1, 2, "helper")]),
            ("It also writes to Redis.", [_c("cache/redis.py", 1, 3)]),
        ],
        expected_reasons=[PATH_NOT_IN_SNAPSHOT],
        expected_unsupported_claims=1,
    ),
    GroundingCase(
        name="claim with no citation at all",
        fixture="py-minimal",
        claims=[("This service is production-ready.", [])],
        expected_reasons=[],
        expected_unsupported_claims=1,
    ),
    GroundingCase(
        name="grounded answer in the typescript fixture",
        fixture="ts-minimal",
        claims=[("main() is exported from a.ts.", [_c("a.ts", 3, 5, "main")])],
        expected_reasons=[],
        expected_unsupported_claims=0,
    ),
    GroundingCase(
        name="cross-package path that does not exist in the monorepo",
        fixture="mixed-monorepo",
        claims=[
            ("helper() is in the python service.", [_c("py_service/b.py", 1, 2, "helper")]),
            ("The gateway proxies it.", [_c("gateway/proxy.ts", 1, 4)]),
        ],
        expected_reasons=[PATH_NOT_IN_SNAPSHOT],
        expected_unsupported_claims=1,
    ),
]


def run_grounding() -> tuple[list[str], dict[str, float]]:
    """Grade the citation validator. Returns (failures, metrics)."""
    failures: list[str] = []
    indexes = {
        name: FixtureSnapshotIndex(FIXTURES_DIR / name)
        for name in sorted({case.fixture for case in GROUNDING_CASES})
    }

    total_claims = 0
    total_unsupported = 0
    total_citations = 0
    total_rejected = 0

    for case in GROUNDING_CASES:
        index = indexes[case.fixture]
        report = validate_claim_citations(index, [citations for _text, citations in case.claims])

        total_claims += len(case.claims)
        total_unsupported += report.unsupported_claim_count
        total_citations += report.checked
        total_rejected += len(report.rejected)

        reasons = [r.reason for r in report.rejected]
        if reasons != case.expected_reasons:
            failures.append(
                f"{case.name}: rejection reasons {reasons} != expected {case.expected_reasons}"
            )
        if report.unsupported_claim_count != case.expected_unsupported_claims:
            failures.append(
                f"{case.name}: {report.unsupported_claim_count} unsupported claims, "
                f"expected {case.expected_unsupported_claims}"
            )
        should_pass = not case.expected_reasons and case.expected_unsupported_claims == 0
        if report.ok is not should_pass:
            failures.append(
                f"{case.name}: report.ok={report.ok}, expected {should_pass} — an answer "
                "with an unresolvable citation must never be reported as acceptable"
            )

    metrics = {
        "claims": float(total_claims),
        "unsupported_claims": float(total_unsupported),
        "unsupported_claim_rate": (total_unsupported / total_claims) if total_claims else 0.0,
        "citations_checked": float(total_citations),
        "citations_rejected": float(total_rejected),
    }
    return failures, metrics


# ---------------------------------------------------- retrieval corpus


@dataclass(frozen=True)
class RetrievalCase:
    question: str
    expected_paths: tuple[str, ...]


# Question -> expected-files pairs, per fixture. Kept here rather than in each
# fixture's expected.json so this grader stays one self-contained file and
# does not change a file graph_sanity.py also reads.
RETRIEVAL_CASES: dict[str, tuple[RetrievalCase, ...]] = {
    "py-minimal": (
        RetrievalCase("what does helper return?", ("b.py",)),
        RetrievalCase("where is main defined?", ("a.py",)),
        RetrievalCase("which test covers main?", ("test_a.py",)),
    ),
    "ts-minimal": (
        RetrievalCase("what does helper return?", ("b.ts",)),
        RetrievalCase("where is main exported?", ("a.ts",)),
        RetrievalCase("which test asserts main returns hi?", ("a.test.ts",)),
    ),
    "mixed-monorepo": (
        RetrievalCase("what does helper return in the python service?", ("py_service/b.py",)),
        RetrievalCase("where is the typescript main function?", ("ts_service/a.ts",)),
        RetrievalCase("which test covers the python main?", ("py_service/test_a.py",)),
    ),
}


def _recall_at_k(ranked_paths: list[str], expected: tuple[str, ...], k: int) -> float:
    top = ranked_paths[:k]
    found = sum(1 for path in expected if path in top)
    return found / len(expected) if expected else 1.0


def _reciprocal_rank(ranked_paths: list[str], expected: tuple[str, ...]) -> float:
    for rank, path in enumerate(ranked_paths, start=1):
        if path in expected:
            return 1.0 / rank
    return 0.0


def run_retrieval(fixture_names: list[str]) -> tuple[list[str], dict[str, float]]:
    """Grade hybrid retrieval against the real SQL. Returns (failures, metrics)."""
    from app.analysis.semantic.retrieve import RetrievalQuery, retrieve
    from app.knowledge import create_project, create_repo, upsert_blobs, worker_connection
    from app.knowledge.chunks import insert_chunks
    from app.models import Blob, Chunk, Repo

    failures: list[str] = []
    recalls: list[float] = []
    rrs: list[float] = []

    for name in fixture_names:
        fixture_dir = FIXTURES_DIR / name
        cases = RETRIEVAL_CASES.get(name)
        if cases is None:
            failures.append(f"{name}: no retrieval cases defined for this fixture")
            continue

        files = {
            p.relative_to(fixture_dir).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(fixture_dir.rglob("*"))
            if p.is_file() and p.name != "expected.json"
        }

        user_id = f"user_eval_{uuid.uuid4().hex}"
        commit_sha = f"eval-{uuid.uuid4().hex[:12]}"
        with worker_connection() as conn:
            conn.execute(
                "insert into codeaxis.profiles (id, email) values (%s, %s)",
                (user_id, f"{user_id}@codeaxis.invalid"),
            )
            conn.commit()
            project = create_project(conn, f"eval-citations-{name}", user_id)
            repo = create_repo(conn, Repo(project_id=project.id, local_path=str(fixture_dir)))
        assert repo.id is not None
        repo_id = repo.id

        try:
            with worker_connection() as conn:
                upsert_blobs(
                    conn,
                    [
                        Blob(
                            repo_id=repo_id,
                            commit_sha=commit_sha,
                            path=path,
                            content=content,
                            content_hash=f"h-{path}",
                        )
                        for path, content in files.items()
                    ],
                )
                # One `module` chunk per file. Chunking granularity belongs to
                # the chunking pass and its own tests; what is graded here is
                # retrieval's ability to rank the right *file* first, so a
                # whole-file chunk is the honest unit for this measurement and
                # keeps the grader independent of the chunker's current shape.
                insert_chunks(
                    conn,
                    [
                        Chunk(
                            repo_id=repo_id,
                            commit_sha=commit_sha,
                            path=path,
                            symbol=None,
                            kind="module",
                            line_start=1,
                            line_end=content.count("\n") + 1,
                            content=content,
                            content_hash=f"chunk-{path}",
                            token_count=max(1, len(content) // 4),
                            analyzer="evals.citations",
                            analyzer_version="1",
                        )
                        for path, content in files.items()
                    ],
                )

                for case in cases:
                    result = retrieve(
                        conn,
                        repo_id,
                        commit_sha,
                        RetrievalQuery(text=case.question, hops=0, use_vector=False),
                    )
                    ranked = [item.chunk.path for item in result.items]
                    recall = _recall_at_k(ranked, case.expected_paths, RECALL_K)
                    rr = _reciprocal_rank(ranked, case.expected_paths)
                    recalls.append(recall)
                    rrs.append(rr)
                    if recall < REQUIRED_RECALL_AT_K:
                        failures.append(
                            f"{name}: recall@{RECALL_K}={recall:.2f} for "
                            f"{case.question!r}; expected {list(case.expected_paths)}, "
                            f"got {ranked[:RECALL_K]}"
                        )
                    # Every citation retrieval emits must itself resolve: the
                    # retrieval side of the pipeline is held to the same bar
                    # as the narration side.
                    index = FixtureSnapshotIndex(fixture_dir)
                    report = validate_claim_citations(index, [[c] for c in result.citations()])
                    if not report.ok:
                        failures.append(
                            f"{name}: retrieval emitted an unresolvable citation for "
                            f"{case.question!r}: "
                            f"{[r.as_dict() for r in report.rejected]}"
                        )
        finally:
            with worker_connection() as conn, conn.cursor() as cur:
                for table in ("chunks", "blobs", "edges", "nodes", "coverage", "pass_runs"):
                    cur.execute(f"delete from codeaxis.{table} where repo_id = %s", (repo_id,))
                cur.execute("delete from codeaxis.repos where id = %s", (repo_id,))
                cur.execute(
                    "delete from codeaxis.project_memberships where project_id = %s",
                    (project.id,),
                )
                cur.execute("delete from codeaxis.projects where id = %s", (project.id,))
                cur.execute("delete from codeaxis.profiles where id = %s", (user_id,))
                conn.commit()

    mrr = sum(rrs) / len(rrs) if rrs else 0.0
    if rrs and mrr < REQUIRED_MRR:
        failures.append(f"MRR={mrr:.3f} is below the {REQUIRED_MRR} floor")

    metrics = {
        "questions": float(len(rrs)),
        f"recall@{RECALL_K}": (sum(recalls) / len(recalls)) if recalls else 0.0,
        "mrr": mrr,
    }
    return failures, metrics


# -------------------------------------------------------------- runner


def _semantic_schema_present() -> bool:
    from app.knowledge import worker_connection

    with worker_connection() as conn:
        row = conn.execute(
            "select 1 from information_schema.tables "
            "where table_schema = 'codeaxis' and table_name = 'chunks'"
        ).fetchone()
    return row is not None


def _report(section: str, failures: list[str], metrics: dict[str, float]) -> bool:
    ok = not failures
    print(f"[{'PASS' if ok else 'FAIL'}] {section}")
    for key, value in metrics.items():
        print(f"    {key}: {value:g}")
    for failure in failures:
        print(f"    - {failure}")
    return ok


def main(argv: list[str]) -> int:
    sections = argv or ["grounding", "retrieval"]
    overall_ok = True

    if "grounding" in sections:
        failures, metrics = run_grounding()
        overall_ok = _report("grounding (citation validator)", failures, metrics) and overall_ok

    if "retrieval" in sections:
        if not os.environ.get("WORKER_DATABASE_URL"):
            print("[SKIP] retrieval (recall@k / MRR): WORKER_DATABASE_URL not set")
        elif not _semantic_schema_present():
            print(
                "[SKIP] retrieval (recall@k / MRR): codeaxis.chunks does not exist — "
                "apply supabase/migrations/20261019000000_semantic.sql"
            )
        else:
            failures, metrics = run_retrieval(sorted(RETRIEVAL_CASES))
            overall_ok = _report("retrieval (recall@k / MRR)", failures, metrics) and overall_ok

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
