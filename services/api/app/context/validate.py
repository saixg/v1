"""Requirement validation (build-plan.md Part 4.F.3): schema, grounding,
duplicates, links, gaps, and contradictions — deterministic checks first,
one model-assisted pass last and only for what rules cannot see.

Order matters and is fixed:

1. **Grounding** (`check_grounding`). Every extracted statement must cite an
   intake chunk that exists *and* quote a span that is actually in that
   chunk's (redacted) text. Anything else is dropped — not rephrased, not
   repaired — and the drop is reported with a reason code. This is the
   requirement-side twin of the chat citation validator: the model may only
   restate what the developer's own input says.
2. **Duplicates** (`dedupe`). Exact duplicates (after normalization) are
   dropped; near-duplicates are kept and marked ambiguous with a question,
   because "are these two the same requirement?" is the developer's call.
3. **Acceptance links** (`check_acceptance_links`). An acceptance criterion
   that verifies no known requirement is marked ambiguous, not dropped: the
   criterion is real input, its target is what is unclear.
4. **Gaps** (`gap_questions`). No objective, no acceptance criteria -> an
   `open_question` requirement saying so. Part 4.F.3: never invent the
   missing requirement; surface the gap.
5. **Contradictions with the ledger** (`find_constraint_conflicts`).
   Deterministic, pure path logic: a requirement that names a path inside a
   hard or confirmed `do_not_touch` scope is marked ambiguous with a question
   that cites the constraint. No model.
6. **Statement-vs-statement contradictions**
   (`find_statement_contradictions`). The only model-assisted check, used as
   the fallback for what string logic cannot judge. Its output is validated:
   each contradiction must cite two distinct, existing requirement keys or it
   is dropped. Both requirements become ambiguous with a question naming the
   other; nothing is resolved silently.

Ambiguous is a stored state, never an error (Part 0.3).
"""

from __future__ import annotations

import difflib
import fnmatch
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.llm import CallContext, ModelService, Prompt
from app.models import Constraint, OriginTag, RequirementKind, RequirementPriority

from .framing import REDACTION_RULE, UNTRUSTED_DATA_RULE, fenced, new_fence
from .intake import IntakeChunk, IntakeImage

# --- drafts -------------------------------------------------------------------


@dataclass
class DraftRequirement:
    """A requirement between extraction and persistence. Mutable on purpose:
    each validation stage may only *add* questions and notes to it (or drop
    it whole); none rewrites its statement."""

    key: str
    kind: RequirementKind
    statement: str
    priority: RequirementPriority | None = None
    chunk_id: str | None = None
    quote: str | None = None
    # Keys of the requirements an acceptance criterion verifies.
    verifies: list[str] = field(default_factory=list)
    # Region inside an image, as the vision model described it.
    region: str | None = None
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Constraint ids this requirement conflicts with (deterministic pass).
    constraint_ids: list[str] = field(default_factory=list)
    # Requirement keys this one contradicts (model-assisted pass).
    contradicts: list[str] = field(default_factory=list)
    # Origin of the row's text: `inferred` for everything a model extracted;
    # `parsed` for gap questions produced by the deterministic checks here.
    origin: OriginTag = "inferred"
    # True when the cited source is an image: the quote cannot be checked
    # against text (no OCR), so the row is kept but held as ambiguous.
    unverifiable_quote: bool = False

    @property
    def status(self) -> str:
        # An open question *is* its question; the kind already says it is
        # unresolved, so its status stays 'open' (with `question` set).
        return "ambiguous" if self.questions and self.kind != "open_question" else "open"

    def question_text(self) -> str | None:
        return " ".join(self.questions) if self.questions else None


# Reason codes for a dropped requirement. Stable strings: the API returns
# them and the UI keys copy off them.
MISSING_SOURCE_REF = "missing_source_ref"
SOURCE_NOT_FOUND = "source_not_found"
QUOTE_MISSING = "quote_missing"
QUOTE_TOO_SHORT = "quote_too_short"
QUOTE_NOT_IN_SOURCE = "quote_not_in_source"
DUPLICATE = "duplicate"
DUPLICATE_KEY = "duplicate_key"
INVALID_PAIR = "invalid_pair"


@dataclass(frozen=True)
class Dropped:
    """A model output that was not persisted, and why. `detail` never quotes
    the statement: the statement is untrusted text and the report goes to
    logs and API responses."""

    key: str | None
    kind: str
    reason: str
    detail: str


# --- normalization --------------------------------------------------------------

_QUOTES = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
        "–": "-", "—": "-", "−": "-", " ": " ",
        "­": "",
    }
)
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Casefold, unify typographic quotes/dashes, collapse whitespace. PDF and
    DOCX text routinely differ from what a model echoes back only in these
    ways; anything beyond them is a real mismatch."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return _WS.sub(" ", text).strip().casefold()


# A quote shorter than this proves nothing about where a statement came
# from ("the", "must") unless it is the whole chunk.
MIN_QUOTE_CHARS = 12


# --- 1. grounding ---------------------------------------------------------------


def check_grounding(
    drafts: Sequence[DraftRequirement],
    chunks: Mapping[str, IntakeChunk],
    images: Mapping[str, IntakeImage] | None = None,
) -> tuple[list[DraftRequirement], list[Dropped]]:
    """Keep only drafts whose `chunk_id` resolves and whose `quote` occurs in
    that chunk. Open questions may omit a chunk (a gap is often an absence),
    but one that names a chunk must name a real one."""
    images = images or {}
    kept: list[DraftRequirement] = []
    dropped: list[Dropped] = []
    for d in drafts:
        if d.kind == "open_question":
            if d.chunk_id is not None and d.chunk_id not in chunks and d.chunk_id not in images:
                dropped.append(
                    Dropped(d.key, d.kind, SOURCE_NOT_FOUND, f"cites unknown source {d.chunk_id!r}")
                )
                continue
            kept.append(d)
            continue
        if not d.chunk_id:
            dropped.append(Dropped(d.key, d.kind, MISSING_SOURCE_REF, "no source chunk cited"))
            continue
        if d.chunk_id in images:
            # Visual reading of a diagram: there is no text to hold the quote
            # against, so the row cannot be verified and is never presented
            # as a clean requirement.
            d.unverifiable_quote = True
            d.questions.append(
                f"Read from image {images[d.chunk_id].name} by a vision model; the source "
                "has no text to verify it against. Confirm it reflects the diagram."
            )
            kept.append(d)
            continue
        chunk = chunks.get(d.chunk_id)
        if chunk is None:
            dropped.append(
                Dropped(d.key, d.kind, SOURCE_NOT_FOUND, f"cites unknown source {d.chunk_id!r}")
            )
            continue
        if not d.quote or not d.quote.strip():
            dropped.append(Dropped(d.key, d.kind, QUOTE_MISSING, "no supporting quote"))
            continue
        quote = normalize(d.quote)
        body = normalize(chunk.text)
        if len(quote) < MIN_QUOTE_CHARS and quote != body:
            dropped.append(
                Dropped(d.key, d.kind, QUOTE_TOO_SHORT,
                        f"quote shorter than {MIN_QUOTE_CHARS} characters")
            )
            continue
        if quote not in body:
            dropped.append(
                Dropped(d.key, d.kind, QUOTE_NOT_IN_SOURCE, f"quote does not occur in {d.chunk_id}")
            )
            continue
        kept.append(d)
    return kept, dropped


# --- 2. duplicates -----------------------------------------------------------------

# SequenceMatcher ratio at or above which two same-kind statements are
# flagged as possibly the same requirement.
NEAR_DUPLICATE_RATIO = 0.9


def dedupe(drafts: Sequence[DraftRequirement]) -> tuple[list[DraftRequirement], list[Dropped]]:
    kept: list[DraftRequirement] = []
    dropped: list[Dropped] = []
    seen_keys: set[str] = set()
    seen_exact: dict[tuple[str, str], str] = {}
    for d in drafts:
        if d.key in seen_keys:
            dropped.append(Dropped(d.key, d.kind, DUPLICATE_KEY, f"key {d.key} used twice"))
            continue
        norm = normalize(d.statement)
        exact = (d.kind, norm)
        if exact in seen_exact:
            dropped.append(
                Dropped(d.key, d.kind, DUPLICATE, f"same statement as {seen_exact[exact]}")
            )
            continue
        for other in kept:
            if other.kind != d.kind or other.kind == "open_question":
                continue
            ratio = difflib.SequenceMatcher(None, normalize(other.statement), norm).ratio()
            if ratio >= NEAR_DUPLICATE_RATIO:
                d.questions.append(
                    f"Nearly identical to {other.key}; is this a separate requirement?"
                )
                break
        seen_keys.add(d.key)
        seen_exact[exact] = d.key
        kept.append(d)
    return kept, dropped


# --- 3. acceptance links -------------------------------------------------------------


def check_acceptance_links(drafts: Sequence[DraftRequirement]) -> None:
    """In place: drop unknown `verifies` targets (noting them) and mark an
    acceptance criterion with no valid target ambiguous."""
    targets = {d.key for d in drafts if d.kind not in ("acceptance_criteria", "open_question")}
    for d in drafts:
        if d.kind != "acceptance_criteria":
            continue
        unknown = [k for k in d.verifies if k not in targets]
        if unknown:
            d.notes.append("dropped unknown verifies targets: " + ", ".join(unknown))
        d.verifies = [k for k in dict.fromkeys(d.verifies) if k in targets]
        if not d.verifies:
            d.questions.append(
                "This acceptance criterion does not reference any extracted requirement; "
                "which requirement does it verify?"
            )


# --- 4. gaps ---------------------------------------------------------------------------


def gap_questions(drafts: Sequence[DraftRequirement]) -> list[DraftRequirement]:
    """Open questions for the structural gaps Part 4.F.3 names. Deterministic
    (`origin='parsed'`): the absence is a fact about the extracted set."""
    kinds = {d.kind for d in drafts}
    gaps: list[DraftRequirement] = []

    def gap(text: str) -> None:
        gaps.append(
            DraftRequirement(
                key=f"G{len(gaps) + 1}",
                kind="open_question",
                statement=text,
                questions=[text],
                origin="parsed",
                notes=["raised by the deterministic gap check, not by a model"],
            )
        )

    if "objective" not in kinds:
        gap(
            "No objective could be grounded in the task input. What is this task meant "
            "to achieve?"
        )
    if "acceptance_criteria" not in kinds:
        gap("No acceptance criteria were stated. How will we know this task is done?")
    return gaps


# --- 5. contradictions with the constraint ledger (deterministic) ------------------------

_URL = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)
_EXT = (
    "py|pyi|ts|tsx|js|jsx|mjs|cjs|sql|json|jsonc|toml|ya?ml|lock|md|cfg|ini|env|prisma|"
    "go|rs|java|kt|rb|php|cs|c|h|cpp|hpp|swift|sh|graphql|proto|css|scss|html|txt|xml|sum"
)
_PATH_TOKEN = re.compile(
    r"(?<![\w@])(?:\./)?[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?"
    r"|(?<![\w/@.])[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*\.(?:" + _EXT + r")\b"
)
_FILE_EXT = re.compile(r"\.(?:" + _EXT + r")$")


def mentioned_paths(text: str) -> list[str]:
    """Repository-path-shaped tokens in free text: anything with a slash, or
    a bare file name with a known extension. URLs are removed first so
    `https://host/a/b` is not read as the path `host/a/b`."""
    found: list[str] = []
    for m in _PATH_TOKEN.finditer(_URL.sub(" ", text)):
        token = m.group(0).strip(".,;:)(\"'`").removeprefix("./")
        if token and token not in found and not token.startswith(".."):
            found.append(token)
    return found


def _is_dir_token(path: str) -> bool:
    return path.endswith("/") or not _FILE_EXT.search(path.rsplit("/", 1)[-1])


def path_in_scope(path: str, scope: str) -> bool:
    """Is `path` inside `scope`? Scope is a path, a directory prefix, a glob,
    or 'repo' (repo-wide)."""
    p = path.strip("/")
    s = scope.strip()
    if s in ("repo", "*", "**", "/"):
        return True
    if any(ch in s for ch in "*?["):
        if fnmatch.fnmatchcase(p, s.lstrip("/")):
            return True
        # `dir/**` also covers `dir` itself and anything nested under it,
        # which fnmatch's single-segment-agnostic `*` already handles except
        # for the bare directory.
        static = re.split(r"[*?\[]", s, maxsplit=1)[0].strip("/")
        return bool(static) and s.rstrip("/").endswith("**") and p == static
    base = s.strip("/")
    return p == base or p.startswith(base + "/")


def _constraint_hits(path: str, constraint: Constraint) -> bool:
    spans = [c.path for c in constraint.evidence.spans]
    p = path.strip("/")
    if spans and not constraint.evidence.truncated:
        # The analyzer listed exactly which files are protected (e.g. the
        # *existing* migrations): only those, or a directory containing one
        # of them, conflict -- adding a new file beside them does not.
        if p in spans:
            return True
        return _is_dir_token(path) and any(sp.startswith(p + "/") for sp in spans)
    if path_in_scope(p, constraint.scope):
        return True
    # A directory the requirement names that *contains* the protected scope.
    base = constraint.scope.strip().strip("/")
    return _is_dir_token(path) and bool(base) and base.startswith(p + "/")


@dataclass(frozen=True)
class ConstraintConflict:
    requirement_key: str
    constraint_id: str | None
    constraint_kind: str
    scope: str
    path: str
    confirmed: bool
    severity: str


def find_constraint_conflicts(
    drafts: Sequence[DraftRequirement], constraints: Sequence[Constraint]
) -> list[ConstraintConflict]:
    """Requirements naming a path inside a `do_not_touch` scope that is hard
    or human-confirmed. Marks each affected draft ambiguous, in place, with a
    question citing the constraint. Pure string/path logic; no model."""
    active = [
        c for c in constraints
        if c.kind == "do_not_touch" and (c.confirmed or c.severity == "hard")
    ]
    conflicts: list[ConstraintConflict] = []
    if not active:
        return conflicts
    for d in drafts:
        if d.kind == "open_question":
            continue
        text = d.statement + "\n" + (d.quote or "")
        for path in mentioned_paths(text):
            for c in active:
                if not _constraint_hits(path, c):
                    continue
                cid = str(c.id) if c.id is not None else None
                conflicts.append(
                    ConstraintConflict(
                        requirement_key=d.key,
                        constraint_id=cid,
                        constraint_kind=c.kind,
                        scope=c.scope,
                        path=path,
                        confirmed=c.confirmed,
                        severity=c.severity,
                    )
                )
                status = "confirmed" if c.confirmed else "auto-detected, unconfirmed"
                d.questions.append(
                    f"Mentions {path}, which falls under the do-not-touch constraint on "
                    f"{c.scope} ({c.severity}, {status}"
                    + (f", constraint {cid}" if cid else "")
                    + "). Should the requirement change, or the constraint?"
                )
                if cid and cid not in d.constraint_ids:
                    d.constraint_ids.append(cid)
    return conflicts


# --- 6. statement-vs-statement contradictions (model-assisted fallback) ------------------


class StatementContradiction(BaseModel):
    first: str = Field(description="Key of one requirement, exactly as labelled (e.g. R2)")
    second: str = Field(description="Key of the other requirement it contradicts")
    explanation: str = Field(
        min_length=1,
        max_length=400,
        description="One sentence: why both cannot be satisfied together",
    )


class ContradictionReport(BaseModel):
    """Empty is the expected, and preferred, answer when nothing conflicts."""

    contradictions: list[StatementContradiction] = Field(default_factory=list, max_length=40)


CONTRADICTION_SYSTEM = f"""\
You check a list of software requirements, extracted from one developer task, \
for pairs that directly contradict each other: two statements that cannot both \
be satisfied by any implementation.

Rules:
1. Report only direct contradictions. Two requirements that are merely \
different, overlapping, vague, or about different things are not a \
contradiction. When unsure, do not report it.
2. Identify each requirement only by its key exactly as labelled (R1, R2, ...). \
Never invent a key.
3. Do not resolve a contradiction, rank the two, or suggest which one wins.
4. If nothing contradicts, return an empty list.

{UNTRUSTED_DATA_RULE}

{REDACTION_RULE}

Respond with a single JSON object matching the required schema and nothing else."""


@dataclass(frozen=True)
class Contradiction:
    first: str
    second: str
    explanation: str


def contradiction_prompt(drafts: Sequence[DraftRequirement]) -> Prompt:
    body = "\n".join(f"{d.key} [{d.kind}]: {d.statement}" for d in drafts)
    fence = new_fence(body)
    user = (
        f"Fence value for this message: {fence}\n\n"
        f"REQUIREMENTS (untrusted data):\n{fenced(body, fence)}\n\n"
        "List every pair of the requirements above that directly contradict each other."
    )
    return Prompt(user=user, system=CONTRADICTION_SYSTEM)


def find_statement_contradictions(
    service: ModelService,
    drafts: Sequence[DraftRequirement],
    *,
    context: CallContext | None = None,
) -> tuple[list[Contradiction], list[Dropped]]:
    """One `synth` call over the grounded statements. Each reported pair must
    cite two distinct keys that exist; others are dropped and reported. Both
    sides of an accepted pair get a question naming the other, in place.

    Raises whatever `generate_structured` raises: the caller must fail the
    whole extraction rather than persist a set it claims was checked."""
    candidates = [d for d in drafts if d.kind != "open_question"]
    if len(candidates) < 2:
        return [], []
    report = service.generate_structured(
        ContradictionReport, contradiction_prompt(candidates), "synth", context=context
    )
    by_key = {d.key: d for d in candidates}
    accepted: list[Contradiction] = []
    dropped: list[Dropped] = []
    seen: set[frozenset[str]] = set()
    for item in report.contradictions:
        a, b = item.first.strip(), item.second.strip()
        if a not in by_key or b not in by_key or a == b:
            dropped.append(
                Dropped(None, "contradiction", INVALID_PAIR,
                        "contradiction does not cite two distinct known keys")
            )
            continue
        pair = frozenset((a, b))
        if pair in seen:
            continue
        seen.add(pair)
        explanation = " ".join(item.explanation.split())
        accepted.append(Contradiction(a, b, explanation))
        for this, other in ((a, b), (b, a)):
            d = by_key[this]
            d.contradicts.append(other)
            d.questions.append(
                f"Possibly contradicts {other} (model-flagged, unverified): {explanation} "
                "Which one should hold?"
            )
    return accepted, dropped
