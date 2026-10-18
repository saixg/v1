"""The citation validator (build-plan.md Part 9: "every cited path/symbol/line
must exist (automated); unsupported-claim rate").

This is the mechanism behind Phase 3's exit criterion. A narrated answer is a
list of claims, each carrying at least one `Citation`. Before that answer is
returned or persisted, every citation is resolved against the snapshot:

- the path must exist in this repo at this commit,
- the line range must be inside that file,
- a named symbol must actually be declared in that file.

A citation that does not resolve is a **failure**, reported with its reason.
It is never rephrased, never softened into an uncited sentence, and never
dropped so the surviving claims look clean — the caller surfaces the rejection
(see `app.analysis.semantic.chat`).

The validator depends on a `SnapshotIndex` protocol rather than on a database
connection, for one deliberate reason: the eval harness
(`evals/graders/citations.py`) grades the same code against a fixture
directory on disk, with no database and no API key. One validator, two
backings, no second implementation to drift.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

import psycopg

from app.knowledge.retrieval import file_line_counts, symbols_by_path
from app.models import Citation

# Why a citation was rejected. Stable strings: the API returns them, the eval
# grader aggregates by them, and the frontend renders them.
PATH_NOT_IN_SNAPSHOT = "path_not_in_snapshot"
LINE_RANGE_INVERTED = "line_range_inverted"
LINE_OUT_OF_RANGE = "line_out_of_range"
SYMBOL_NOT_IN_FILE = "symbol_not_in_file"


class SnapshotIndex(Protocol):
    """What the validator needs to know about a snapshot.

    Both methods answer about the *snapshot*, not about what retrieval
    happened to return: a model that cites a real file it was never shown is
    still citing something real, while a model that cites a plausible-looking
    path that does not exist must fail."""

    def line_count(self, path: str) -> int | None:
        """Number of lines in `path`, or None if the path is not in the
        snapshot at all."""
        ...

    def symbols(self, path: str) -> set[str]:
        """Names declared in `path` (symbols, plus the file's own node name)."""
        ...


@dataclass(frozen=True)
class RejectedCitation:
    citation: Citation
    reason: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "citation": self.citation.model_dump(exclude_none=True),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CitationReport:
    """The verdict on one answer's citations."""

    checked: int
    accepted: list[Citation]
    rejected: list[RejectedCitation]
    # Indices of the claims that carried at least one rejected citation. These
    # are the claims the caller must refuse to publish.
    rejected_claim_indices: tuple[int, ...] = ()
    # Claims that carried no citation at all. The prompt and the schema's
    # field descriptions both forbid it, so this is normally empty; it is
    # counted rather than assumed away.
    uncited_claim_indices: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.rejected and not self.uncited_claim_indices

    @property
    def unsupported_claim_count(self) -> int:
        return len(set(self.rejected_claim_indices) | set(self.uncited_claim_indices))


class DbSnapshotIndex:
    """`SnapshotIndex` over the Repo Model. Batches its two lookups: one
    `codeaxis.blobs` query for line counts, one `codeaxis.nodes` query for
    symbol names, for every path cited anywhere in the answer."""

    def __init__(
        self, conn: psycopg.Connection, repo_id: str, commit_sha: str, paths: Iterable[str]
    ) -> None:
        wanted = sorted({p for p in paths if p})
        self._lines = file_line_counts(conn, repo_id, commit_sha, wanted)
        self._symbols = symbols_by_path(conn, repo_id, commit_sha, wanted)

    def line_count(self, path: str) -> int | None:
        return self._lines.get(path)

    def symbols(self, path: str) -> set[str]:
        return self._symbols.get(path, set())


def check_citation(index: SnapshotIndex, citation: Citation) -> RejectedCitation | None:
    """Resolve one citation. Returns None when it holds up."""
    lines = index.line_count(citation.path)
    if lines is None:
        return RejectedCitation(
            citation,
            PATH_NOT_IN_SNAPSHOT,
            f"{citation.path!r} is not a file in this repo at this commit",
        )
    if citation.line_end < citation.line_start:
        return RejectedCitation(
            citation,
            LINE_RANGE_INVERTED,
            f"line_end {citation.line_end} is before line_start {citation.line_start}",
        )
    if citation.line_start < 1 or citation.line_start > lines or citation.line_end > lines:
        return RejectedCitation(
            citation,
            LINE_OUT_OF_RANGE,
            f"{citation.path} has {lines} lines; cited "
            f"{citation.line_start}-{citation.line_end}",
        )
    if citation.symbol is not None and citation.symbol not in index.symbols(citation.path):
        return RejectedCitation(
            citation,
            SYMBOL_NOT_IN_FILE,
            f"no symbol named {citation.symbol!r} is declared in {citation.path}",
        )
    return None


def validate_claim_citations(
    index: SnapshotIndex, claim_citations: Sequence[Sequence[Citation]]
) -> CitationReport:
    """Validate every citation of every claim, in claim order.

    `claim_citations[i]` is claim `i`'s citations. A claim with an empty list
    is recorded as uncited and fails the report. The answer schema asks the
    model for at least one citation per claim in its field description but
    does not enforce a minimum, deliberately: an uncited claim should arrive
    here and be reported with a reason, not be rejected upstream as an opaque
    "the model failed schema validation"."""
    accepted: list[Citation] = []
    rejected: list[RejectedCitation] = []
    bad_claims: list[int] = []
    uncited: list[int] = []
    checked = 0

    for i, citations in enumerate(claim_citations):
        if not citations:
            uncited.append(i)
            continue
        claim_failed = False
        for citation in citations:
            checked += 1
            failure = check_citation(index, citation)
            if failure is None:
                accepted.append(citation)
            else:
                rejected.append(failure)
                claim_failed = True
        if claim_failed:
            bad_claims.append(i)

    return CitationReport(
        checked=checked,
        accepted=accepted,
        rejected=rejected,
        rejected_claim_indices=tuple(bad_claims),
        uncited_claim_indices=tuple(uncited),
    )


def dedupe_citations(citations: Iterable[Citation]) -> list[Citation]:
    """Stable-order dedupe on (path, line_start, line_end, symbol) — the parts
    that identify a span. `chunk_id`/`node_id` are ignored for identity: the
    same span retrieved twice is one citation."""
    seen: set[tuple[str, int, int, str | None]] = set()
    out: list[Citation] = []
    for c in citations:
        key = (c.path, c.line_start, c.line_end, c.symbol)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out
