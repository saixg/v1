"""With/without-context measurement harness (build-plan.md Part 9 headline
test; Part 10 Phase 4 exit: "measurable lift vs. no context").

READ THIS FIRST - what this is and is not
=========================================
Part 9's headline test is: *give the same task to Claude Code with and
without a CodeAxis context package, and compare completion, correct files,
constraint violations and tokens.* **This module does not run that test.**
It cannot: no coding agent is run here, and no model is called anywhere. What
it measures is the **quality of the context** a package delivers against
hand-written ground truth, deterministically, next to a no-context baseline
given the same token budget. That is a necessary condition for the headline
test (an agent cannot use a file or a rule it was never shown) and nowhere
near sufficient: it says nothing about whether an agent given the package
finishes the task, avoids the violation, or writes the right code.

So a green run means "the package surfaces the right files and rules better
than naive retrieval on this corpus", **not** "agents do better with CodeAxis".
The remaining half is a **named gap**: a human-run (or separately-funded
agent-run) comparison, same task and same agent with and without the
exported package. `--emit DIR` writes each task's exported CLAUDE.md, and the
run prints each task's ground truth and acceptance criteria, as the protocol
for that comparison. No lift number is reported for it, because none was
measured.

What is measured (all deterministic, no model)
----------------------------------------------
(a) **Correct-file recall.** Ground truth: the files a correct change would
    touch, written by hand for each task in `CORPUS`. Package: distinct paths
    cited by the package's `code` and `test` items. Baseline: see below.
    Precision (share of cited files that are ground truth) is printed beside
    it, because recall alone rewards citing everything.
(b) **Constraint coverage.** Ground truth: every *hard* constraint on the
    task - the task's own seeded rules plus the repo's auto-detected ones as
    the ledger holds them. Covered means the constraint's statement appears
    verbatim in the context. Reported split by seeded vs detected: the
    baseline can only get credit for a rule the task text happens to repeat
    (T3 is built that way, so the baseline is not zero by construction), while
    the package carries the whole ledger by design - the point is that the
    pipeline *delivers* what the ledger knows, not that it discovered it.
(c) **Token cost.** Package `token_count` (what assembly measured) against
    the baseline's tokens and against "send the whole repo" (sum of the real
    `chunks.token_count`). NOT thresholded: the fixture repo is smaller than a
    package's fixed overhead (intent, constraints, conventions, limitations),
    so the package can legitimately cost *more* than the whole repo here. The
    ratio only becomes meaningful on a repo larger than the budget, which no
    fixture in this repository is. Printed so nobody has to guess.

Baseline ("no context")
-----------------------
What an agent has with no CodeAxis: the task text, the README, the file list,
and naive lexical retrieval - files ranked by how many distinct task terms
(lowercased, split on non-alphanumerics, so `place_order` -> place, order;
stopwords and words under 3 letters removed; trailing plural `s` stripped)
appear in the file's path or text, zero-score files excluded, whole files
added in rank order until the SAME token budget the package got is used up.
Both sides get the same budget; the baseline spends part of it on the README
and file list. It is deliberately not a strawman (it finds files by the words
the task uses) and deliberately not a coding agent (it cannot follow imports,
which is exactly what grounding and graph expansion add).

Pass/fail thresholds, fixed before the first run
------------------------------------------------
- package mean recall >= 0.75 and strictly greater than baseline mean recall;
- package covers 100% of hard constraints on every task, and its mean
  coverage is strictly greater than the baseline's;
- every package is `assembled` (not `invalid`) and within the budget.
Token cost and precision are reported, not thresholded.

Limits of this corpus (also named gaps)
---------------------------------------
Five tasks on one fixture of eleven source files: enough to catch a
regression, far too small to estimate a lift with any confidence. Ground truth
was written by the fixture's author, who also knows how the retriever works.
No embeddings are configured, so the vector stage is off (recorded by the
package as `embedding_not_configured`); a deployment with embeddings would
retrieve differently.

Usage:
    python evals/graders/with_without_context.py
    python evals/graders/with_without_context.py --emit ./emitted-packages
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from _context_support import (
    Checks,
    ScannedFixture,
    delete_fixture,
    report,
    scan_fixture,
    skip_reason,
)

from app.context import assemble_context_package, render_export  # noqa: E402
from app.knowledge import (  # noqa: E402
    api_connection,
    insert_constraints,
    list_constraints,
    list_context_items,
    worker_connection,
)
from app.knowledge.chunks import list_chunks  # noqa: E402
from app.llm.tokens import estimate_tokens  # noqa: E402
from app.models import Chunk, Constraint  # noqa: E402

FIXTURE = "orders-service"
# Chosen before the first run and not tuned afterwards: roomy enough for the
# fixed head (intent + constraints) plus several code sections, tight enough
# that not every file fits.
BUDGET = 1500

MIN_PACKAGE_RECALL = 0.75


# --------------------------------------------------------------- corpus


@dataclass(frozen=True)
class EvalTask:
    id: str
    text: str
    # Files a correct change would touch. Hand-written ground truth.
    truth_files: frozenset[str]
    # Hard constraints seeded on this task as (scope, statement).
    constraints: tuple[tuple[str, str], ...]
    # What "done" looks like. Not measured here; printed for the human run.
    acceptance: tuple[str, ...]


CORPUS: tuple[EvalTask, ...] = (
    EvalTask(
        id="T1 empty cart",
        text="Reject an empty cart in place_order before it is saved.",
        truth_files=frozenset({"svc/orders.py", "svc/test_orders.py"}),
        constraints=(
            ("svc/store.py", "Do not change the signature of save_order in svc/store.py."),
        ),
        acceptance=(
            "place_order raises ValueError for an empty cart",
            "no order row is written for an empty cart",
        ),
    ),
    EvalTask(
        id="T2 discount codes",
        text="Support percentage discount codes at checkout so the discount reduces the "
        "order total.",
        truth_files=frozenset(
            {"svc/api.py", "svc/orders.py", "svc/pricing.py", "svc/test_orders.py"}
        ),
        constraints=(("svc/pricing.py", "Keep apply_discount in svc/pricing.py free of I/O."),),
        acceptance=(
            "a request carrying a discount code is priced with apply_discount",
            "a request without a code is priced exactly as before",
        ),
    ),
    EvalTask(
        id="T3 refund email",
        text="Email the customer when a refund is issued. Do not edit anything under vendor/.",
        truth_files=frozenset({"svc/refunds.py", "svc/notify.py", "svc/test_refunds.py"}),
        # Stated verbatim in the task text on purpose: the baseline gets
        # credit for exactly what a developer would have typed anyway.
        constraints=(("vendor/", "Do not edit anything under vendor/."),),
        acceptance=(
            "refund_order notifies the customer once per refund",
            "nothing under vendor/ is modified",
        ),
    ),
    EvalTask(
        id="T4 refund reason",
        text="Record the reason for a refund in the orders table.",
        truth_files=frozenset(
            {"svc/api.py", "svc/refunds.py", "svc/store.py", "svc/test_refunds.py"}
        ),
        constraints=(("migrations/", "Do not edit an existing migration; add a new one."),),
        acceptance=(
            "handle_refund accepts a reason and stores it with the order",
            "the schema change is a new migration file",
        ),
    ),
    EvalTask(
        id="T5 rename",
        text="Rename place_order to submit_order everywhere it is used.",
        truth_files=frozenset({"svc/orders.py", "svc/api.py", "svc/test_orders.py"}),
        constraints=(("svc/api.py", "Keep handle_checkout's public name unchanged."),),
        acceptance=(
            "no reference to place_order remains",
            "handle_checkout still exists and still places the order",
        ),
    ),
)


# ------------------------------------------------------------- baseline

STOPWORDS = frozenset(
    [
        "the", "and", "for", "with", "that", "this", "from", "when", "into",
        "are", "was", "were", "all", "any", "not", "but", "its", "each",
        "every", "than", "then", "them", "they", "you", "your", "out", "use",
        "used", "using", "make", "made", "sure", "also", "should", "must",
        "will", "can", "may", "has", "have", "had", "their", "there",
        "where", "which", "who", "what", "how", "why",
    ]
)
_WORD = re.compile(r"[a-z0-9]+")


def terms(text: str) -> set[str]:
    """Lexical terms of `text`. Camel case is split first so `placeOrder` and
    `place_order` agree; underscores already split because `_` is not in the
    word class."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).lower()
    out: set[str] = set()
    for word in _WORD.findall(spaced):
        if len(word) < 3 or word in STOPWORDS:
            continue
        out.add(word[:-1] if word.endswith("s") and len(word) > 3 else word)
    return out


@dataclass(frozen=True)
class Baseline:
    cited: frozenset[str]
    text: str
    tokens: int


def naive_baseline(task: EvalTask, chunks: list[Chunk], budget: int) -> Baseline:
    by_path: dict[str, list[Chunk]] = {}
    for c in chunks:
        by_path.setdefault(c.path, []).append(c)
    paths = sorted(by_path)
    contents = {p: "\n".join(c.content for c in by_path[p]) for p in paths}
    file_tokens = {p: sum(c.token_count for c in by_path[p]) for p in paths}

    readme = contents.get("README.md", "")
    listing = "\n".join(paths)
    spent = file_tokens.get("README.md", 0) + estimate_tokens(listing)

    wanted = terms(task.text)
    scored = []
    for p in paths:
        if p == "README.md":
            continue
        score = len(wanted & terms(p + "\n" + contents[p]))
        if score > 0:
            scored.append((-score, p))
    scored.sort()

    chosen: list[str] = []
    for _, p in scored:
        if spent + file_tokens[p] <= budget:
            chosen.append(p)
            spent += file_tokens[p]
    text = "\n".join([task.text, readme, listing, *(contents[p] for p in chosen)])
    return Baseline(frozenset(chosen), text, spent)


# --------------------------------------------------------------- scoring


def recall(cited: frozenset[str], truth: frozenset[str]) -> float:
    return len(cited & truth) / len(truth) if truth else 1.0


def precision(cited: frozenset[str], truth: frozenset[str]) -> float:
    return len(cited & truth) / len(cited) if cited else 0.0


@dataclass
class Row:
    task: EvalTask
    status: str
    pkg_files: frozenset[str]
    base_files: frozenset[str]
    pkg_recall: float
    base_recall: float
    pkg_precision: float
    base_precision: float
    seeded_total: int
    detected_total: int
    pkg_seeded: int
    pkg_detected: int
    base_seeded: int
    base_detected: int
    pkg_tokens: int
    base_tokens: int
    repo_tokens: int
    emitted: dict[str, str]

    @property
    def hard_total(self) -> int:
        return self.seeded_total + self.detected_total

    @property
    def pkg_coverage(self) -> float:
        return (self.pkg_seeded + self.pkg_detected) / self.hard_total if self.hard_total else 1.0

    @property
    def base_coverage(self) -> float:
        return (self.base_seeded + self.base_detected) / self.hard_total if self.hard_total else 1.0


def measure(fx: ScannedFixture, task: EvalTask, chunks: list[Chunk]) -> Row:
    row_task = fx.new_task(task.text, title=task.id)
    assert row_task.id is not None
    with worker_connection() as conn:
        insert_constraints(
            conn,
            [
                Constraint(
                    task_id=row_task.id,
                    repo_id=fx.repo_id,
                    commit_sha=fx.commit_sha,
                    kind="do_not_touch",
                    scope=scope,
                    statement=statement,
                    severity="hard",
                    source="user_stated",
                    origin="configured",
                )
                for scope, statement in task.constraints
            ],
        )
        built = assemble_context_package(conn, row_task.id, token_budget=BUDGET)
        hard = list_constraints(
            conn, fx.repo_id, fx.commit_sha, task_id=row_task.id, severity="hard"
        )
    with api_connection() as conn:
        items = list_context_items(conn, row_task.id, package_id=built.package.id)

    pkg_files = frozenset(
        s.path for i in items if i.kind in ("code", "test") for s in i.evidence.spans
    )
    delivered = "\n".join(i.content for i in items if i.kind == "constraint")
    base = naive_baseline(task, chunks, BUDGET)

    seeded = [c for c in hard if c.task_id == row_task.id]
    detected = [c for c in hard if c.task_id is None]
    emitted = {fmt: render_export(built.package, items, fmt) for fmt in ("claude_md",)}  # pyright: ignore[reportArgumentType]
    return Row(
        task=task,
        status=built.package.status,
        pkg_files=pkg_files,
        base_files=base.cited,
        pkg_recall=recall(pkg_files, task.truth_files),
        base_recall=recall(base.cited, task.truth_files),
        pkg_precision=precision(pkg_files, task.truth_files),
        base_precision=precision(base.cited, task.truth_files),
        seeded_total=len(seeded),
        detected_total=len(detected),
        pkg_seeded=sum(c.statement in delivered for c in seeded),
        pkg_detected=sum(c.statement in delivered for c in detected),
        base_seeded=sum(c.statement in base.text for c in seeded),
        base_detected=sum(c.statement in base.text for c in detected),
        pkg_tokens=built.package.token_count,
        base_tokens=base.tokens,
        repo_tokens=sum(c.token_count for c in chunks),
        emitted=emitted,
    )


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def print_table(rows: list[Row]) -> None:
    head = (
        f"{'task':<20} {'recall pkg/base':>16} {'prec pkg/base':>14} "
        f"{'hard-constraint cov pkg/base':>29} {'tokens pkg/base/repo':>22}"
    )
    print(head)
    print("-" * len(head))
    for r in rows:
        print(
            f"{r.task.id:<20} {r.pkg_recall:>7.2f} /{r.base_recall:>6.2f} "
            f"{r.pkg_precision:>7.2f} /{r.base_precision:>5.2f} "
            f"{r.pkg_seeded + r.pkg_detected:>5}/{r.hard_total:<3} vs "
            f"{r.base_seeded + r.base_detected:>2}/{r.hard_total:<3}"
            f"(seeded {r.pkg_seeded}/{r.seeded_total} vs {r.base_seeded}/{r.seeded_total}) "
            f"{r.pkg_tokens:>6}/{r.base_tokens:>5}/{r.repo_tokens:<5}"
        )
    print("-" * len(head))
    print(
        f"{'MEAN':<20} {mean([r.pkg_recall for r in rows]):>7.2f} /"
        f"{mean([r.base_recall for r in rows]):>6.2f} "
        f"{mean([r.pkg_precision for r in rows]):>7.2f} /"
        f"{mean([r.base_precision for r in rows]):>5.2f} "
        f"{mean([r.pkg_coverage for r in rows]):>12.2f} vs "
        f"{mean([r.base_coverage for r in rows]):.2f}"
    )
    print()
    for r in rows:
        print(f"  {r.task.id}: ground truth {sorted(r.task.truth_files)}")
        print(f"      package cited  {sorted(r.pkg_files)}")
        print(f"      baseline cited {sorted(r.base_files)}")


def print_human_protocol(rows: list[Row]) -> None:
    print()
    print("NOT MEASURED - the headline test itself (named gap)")
    print("  Same task, same agent, with vs without the exported package; record per task:")
    print("  completion, files touched vs ground truth, constraint violations, tokens used.")
    print("  Pass/fail inputs for that run:")
    for r in rows:
        print(f"    {r.task.id}: touch {sorted(r.task.truth_files)}")
        for scope, statement in r.task.constraints:
            print(f"        must not violate ({scope}): {statement}")
        for criterion in r.task.acceptance:
            print(f"        acceptance: {criterion}")


def run(emit: Path | None) -> bool:
    checks = Checks()
    fx = scan_fixture(FIXTURE)
    try:
        with worker_connection() as conn:
            chunks = list_chunks(conn, fx.repo_id, fx.commit_sha)
        rows = [measure(fx, task, chunks) for task in CORPUS]
    finally:
        delete_fixture(fx)

    print(f"corpus: {len(rows)} tasks on '{FIXTURE}'; budget {BUDGET} tokens for both sides\n")
    print_table(rows)

    pkg_recall = mean([r.pkg_recall for r in rows])
    base_recall = mean([r.base_recall for r in rows])
    checks.check(
        pkg_recall >= MIN_PACKAGE_RECALL,
        f"package mean recall {pkg_recall:.2f} is below the {MIN_PACKAGE_RECALL} floor",
    )
    checks.check(
        pkg_recall > base_recall,
        f"package mean recall {pkg_recall:.2f} does not beat the baseline {base_recall:.2f}",
    )
    for r in rows:
        checks.check(
            r.pkg_coverage == 1.0,
            f"{r.task.id}: package delivered {r.pkg_seeded + r.pkg_detected}/{r.hard_total} "
            "hard constraints",
        )
        checks.check(r.status == "assembled", f"{r.task.id}: package status is {r.status}")
        checks.check(
            r.pkg_tokens <= BUDGET, f"{r.task.id}: package used {r.pkg_tokens} > budget {BUDGET}"
        )
    pkg_cov = mean([r.pkg_coverage for r in rows])
    base_cov = mean([r.base_coverage for r in rows])
    checks.check(
        pkg_cov > base_cov,
        f"package constraint coverage {pkg_cov:.2f} does not beat the baseline {base_cov:.2f}",
    )

    if emit is not None:
        emit.mkdir(parents=True, exist_ok=True)
        for r in rows:
            slug = re.sub(r"[^a-z0-9]+", "-", r.task.id.lower()).strip("-")
            for fmt, text in r.emitted.items():
                (emit / f"{slug}.{fmt}.md").write_text(text, encoding="utf-8", newline="\n")
        print(f"\nexported packages written to {emit}")

    print_human_protocol(rows)
    print()
    metrics: dict[str, object] = {
        "package_mean_recall": f"{pkg_recall:.3f}",
        "baseline_mean_recall": f"{base_recall:.3f}",
        "package_constraint_coverage": f"{pkg_cov:.3f}",
        "baseline_constraint_coverage": f"{base_cov:.3f}",
        "package_tokens_total": sum(r.pkg_tokens for r in rows),
        "baseline_tokens_total": sum(r.base_tokens for r in rows),
        "whole_repo_tokens_per_task": rows[0].repo_tokens if rows else 0,
        "scope": "context quality only; end-to-end agent completion NOT measured",
    }
    return report("with/without context (context quality, not agent completion)", checks, metrics)


def main(argv: list[str]) -> int:
    emit: Path | None = None
    if "--emit" in argv:
        at = argv.index("--emit")
        if at + 1 >= len(argv):
            print("--emit needs a directory")
            return 2
        emit = Path(argv[at + 1])
    reason = skip_reason()
    if reason is not None:
        print(f"[SKIP] with/without context: {reason}")
        return 0
    return 0 if run(emit) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
