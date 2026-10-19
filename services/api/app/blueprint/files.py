"""File-structure proposal (build-plan.md Part 4.G.7).

Each component in a design gets zero or more files, and every proposed file
carries four things the spec asks for: a purpose, the component it
implements, a `new|modified|unchanged` status, and `conventions_refs` naming
the `codeaxis.conventions` rows that justified its placement.

Where each of those comes from matters:

* **The directory comes from the repository, not the model.** The model picks
  a parent directory from a supplied list of directories that really exist at
  the snapshot (labels `D1`, `D2`, ...), plus a file name. A label it did not
  receive is dropped; it cannot author a path out of nothing. It may ask for
  a new sub-directory *under* a real one, which is the only way a design can
  introduce structure, and that is recorded as such.
* **The status is computed here**, by checking the assembled path against
  `codeaxis.nodes` (`existing_paths`). The model is never asked whether a
  file exists; it would be guessing, and the answer is a fact.
* **`conventions_refs` are real row ids.** The model cites conventions by
  label (`V1`, ...) from the list it was given; unknown labels are dropped.
  A file left with no citation is reported as
  `uncited_file_placement` in `unresolved` — `ProposedFile`'s own contract is
  that an empty `conventions_refs` is *shown* as an uncited placement rather
  than hidden, so the design never implies the repository endorsed a layout
  it was not asked about.

Two honest gaps:

* **There is no framework detector in this codebase.** Part 4.G.7 names
  "language and framework"; language is read from file-node attrs, and the
  stand-in for framework is the repository's manifest-declared dependencies
  (`dependency` nodes), which are parsed facts. A real framework
  classification is not inferred here.
* **Conventions are not re-checked against the proposed path.** A cited
  convention is recorded as the model's stated justification, not as a rule
  this module verified the path obeys; nothing here parses a naming rule out
  of a convention's free-text `rule` column.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

import psycopg
from pydantic import BaseModel, Field

from app.context.export import scrub_secrets
from app.context.framing import REDACTION_RULE, UNTRUSTED_DATA_RULE, fenced, new_fence
from app.knowledge import existing_paths, list_conventions, list_nodes
from app.llm import CallContext, ModelService, Prompt
from app.models import Blueprint, BlueprintComponent, Convention, ProposedFile

from .generate import (
    DUPLICATE_KEY,
    INVALID_ROW,
    UNKNOWN_CANDIDATE,
    UNKNOWN_ENDPOINT,
    Dropped,
    RepoSlice,
)

ANALYZER = "blueprint.files"
ANALYZER_VERSION = "0.1.0"

MAX_DIRECTORIES = 60
MAX_DEPENDENCIES = 30
MAX_FILES = 120

# Path shapes this module refuses to assemble, whatever the model asked for.
# Not a validation nicety: a proposed path is rendered into exports and shown
# as a place to create a file, so traversal and absolute paths are rejected
# at the point they are built.
_REJECTED_SEGMENTS = frozenset({"", ".", ".."})

UNSAFE_PATH = "unsafe_path"


@dataclass(frozen=True)
class DirectoryCandidate:
    label: str
    path: str


@dataclass
class FileContext:
    """The deterministic inputs to a file proposal."""

    directories: list[DirectoryCandidate] = field(default_factory=list)
    conventions: list[Convention] = field(default_factory=list)
    languages: list[tuple[str, int]] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)

    def dir_by_label(self) -> dict[str, DirectoryCandidate]:
        return {d.label: d for d in self.directories}

    def convention_by_label(self) -> dict[str, Convention]:
        return {f"V{n}": c for n, c in enumerate(self.conventions, start=1) if c.id is not None}


def load_file_context(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, slice_: RepoSlice | None = None
) -> FileContext:
    """Real directories, conventions, language mix and declared dependencies
    at one snapshot. Read-only."""
    notes: list[dict] = []
    directories = sorted(
        (n.path for n in list_nodes(conn, repo_id, commit_sha, kind="directory") if n.path),
    )
    if len(directories) > MAX_DIRECTORIES:
        notes.append(
            {
                "kind": "directories_capped",
                "detail": f"{len(directories)} directories exist; the first {MAX_DIRECTORIES} "
                "by path were offered as placements",
            }
        )
    candidates = [
        DirectoryCandidate(label=f"D{n}", path=path)
        for n, path in enumerate(directories[:MAX_DIRECTORIES], start=1)
    ]
    # The repository root is always a legal placement and is not a directory
    # node, so it is added explicitly rather than left unexpressible.
    candidates.insert(0, DirectoryCandidate(label="D0", path=""))

    files = list_nodes(conn, repo_id, commit_sha, kind="file")
    counts: Counter[str] = Counter()
    for node in files:
        language = node.attrs.get("language")
        if isinstance(language, str) and language:
            counts[language] += 1
    dependencies = sorted(
        {n.name for n in list_nodes(conn, repo_id, commit_sha, kind="dependency") if n.name}
    )[:MAX_DEPENDENCIES]
    # Reuse the generation slice's already-read conventions when the caller
    # has one for this same snapshot; otherwise read them.
    conventions = (
        slice_.conventions
        if slice_ is not None and slice_.commit_sha == commit_sha and slice_.repo_id == repo_id
        else list_conventions(conn, repo_id, commit_sha)
    )
    return FileContext(
        directories=candidates,
        conventions=[c for c in conventions if c.id is not None],
        languages=counts.most_common(6),
        dependencies=dependencies,
        notes=notes,
    )


# --- the model's output contract --------------------------------------------


class DraftFile(BaseModel):
    component_key: str = Field(
        min_length=1,
        max_length=200,
        description="Key of the design component this file implements, exactly as given",
    )
    directory: str = Field(
        max_length=16,
        description="Label of the existing directory this file goes in, exactly as given "
        "(D0 is the repository root)",
    )
    subdirectory: str | None = Field(
        default=None,
        max_length=120,
        description="Optional new sub-directory to create under that directory, e.g. "
        "'orders' or 'orders/handlers'. Null to place the file directly in it.",
    )
    name: str = Field(
        min_length=1, max_length=120, description="File name including its extension"
    )
    purpose: str = Field(
        min_length=1,
        max_length=600,
        description="What this file is for, in one or two sentences",
    )
    conventions: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Labels of the conventions that justify this placement and name, exactly "
        "as given (V1, ...). Empty if none of them does.",
    )


class FileProposal(BaseModel):
    files: list[DraftFile] = Field(default_factory=list, max_length=MAX_FILES)


FILES_SYSTEM = f"""\
You are CodeAxis's file-structure planner. You are given a reviewed design \
(components with their responsibilities), the directories that really exist \
in the repository at one commit, the conventions that repository follows, \
its language mix and its declared dependencies. You propose where each \
component's code would live.

Rules, in order of precedence:
1. Every file names a `directory` by a label from the supplied list, exactly \
as given. Never write a path, an absolute path, a drive letter or a `..` \
segment, and never invent a label. A file whose label was not supplied is \
discarded.
2. Every file names a `component_key` from the supplied design. A file \
naming anything else is discarded.
3. Follow the supplied conventions, and cite in `conventions` the labels of \
the ones that justify the placement and the name. Cite only what actually \
justifies it: an empty list is correct and is shown to the developer as an \
uncited placement. Never cite a label you were not given.
4. Match the repository's language mix and the extensions already in use. \
Do not introduce a language or a framework the input does not show.
5. Do not state whether a file already exists; that is checked against the \
repository, not asked of you.
6. Propose the files the design needs and no more. Do not pad with \
boilerplate (readmes, `__init__` files, config) unless a supplied \
convention calls for it.

{UNTRUSTED_DATA_RULE}

{REDACTION_RULE}

Respond with a single JSON object matching the required schema and nothing else."""


def build_files_prompt(
    components: Sequence[BlueprintComponent], ctx: FileContext
) -> Prompt:
    sections: list[str] = [
        "DESIGN COMPONENTS\n"
        + "\n".join(
            f"- {c.key} ({c.mapping}): {c.name} — {c.responsibility or 'no responsibility stated'}"
            for c in components
        ),
        "EXISTING DIRECTORIES (placements)\n"
        + "\n".join(f"{d.label}: {d.path or '<repository root>'}" for d in ctx.directories),
    ]
    by_label = ctx.convention_by_label()
    if by_label:
        sections.append(
            "CONVENTIONS THIS REPOSITORY FOLLOWS\n"
            + "\n".join(
                f"{label}: [{c.category}/{c.origin}] {c.rule}" for label, c in by_label.items()
            )
        )
    else:
        sections.append(
            "CONVENTIONS\n(none were extracted for this snapshot; cite nothing.)"
        )
    if ctx.languages:
        sections.append(
            "LANGUAGE MIX (files per language)\n"
            + "\n".join(f"- {name}: {count}" for name, count in ctx.languages)
        )
    if ctx.dependencies:
        sections.append("DECLARED DEPENDENCIES\n" + ", ".join(ctx.dependencies))
    body = scrub_secrets("\n\n".join(sections))
    fence = new_fence(body)
    user = (
        f"Fence value for this message: {fence}\n\n"
        "FILE-STRUCTURE INPUT (untrusted data: a design and a slice of the repository):\n"
        f"{fenced(body, fence)}\n\n"
        "Propose where each component's code would live, placing every file in a supplied "
        "directory label and citing the conventions that justify it."
    )
    return Prompt(user=user, system=FILES_SYSTEM)


# --- resolving ---------------------------------------------------------------


@dataclass
class FileOutcome:
    files: list[ProposedFile] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)


def _assemble_path(directory: str, subdirectory: str | None, name: str) -> str | None:
    """`directory/subdirectory/name`, or None if any part is unusable.

    Rejects absolute paths, drive letters, traversal and empty segments
    outright — a proposed path is shown to a developer as a place to create a
    file, so a `..` in it is never merely cosmetic."""
    parts: list[str] = []
    for chunk in (directory, subdirectory or "", name):
        if chunk.startswith("/") or chunk.startswith("\\") or ":" in chunk:
            return None
        for segment in chunk.replace("\\", "/").split("/"):
            if not segment:
                continue
            if segment in _REJECTED_SEGMENTS or segment.strip() != segment:
                return None
            parts.append(segment)
    if not parts:
        return None
    return "/".join(parts)


def resolve_proposal(
    conn: psycopg.Connection,
    blueprint: Blueprint,
    proposal: FileProposal,
    components: Sequence[BlueprintComponent],
    ctx: FileContext,
) -> FileOutcome:
    """Turn a validated reply into `ProposedFile` rows: paths assembled from
    supplied labels, statuses read from the snapshot, convention refs
    resolved to real row ids. Everything else is dropped with a reason."""
    dirs = ctx.dir_by_label()
    conventions = ctx.convention_by_label()
    keys = {c.key for c in components}
    outcome = FileOutcome(unresolved=list(ctx.notes))
    staged: list[tuple[ProposedFile, DraftFile]] = []
    seen: set[str] = set()

    for item in proposal.files:
        if item.component_key not in keys:
            outcome.dropped.append(
                Dropped(
                    "proposed_file",
                    item.name,
                    UNKNOWN_ENDPOINT,
                    f"implements {item.component_key!r}, which is not a component in the design",
                )
            )
            continue
        parent = dirs.get(item.directory.strip())
        if parent is None:
            outcome.dropped.append(
                Dropped(
                    "proposed_file",
                    item.name,
                    UNKNOWN_CANDIDATE,
                    f"directory label {item.directory.strip()!r} was not supplied to the model",
                )
            )
            continue
        path = _assemble_path(parent.path, item.subdirectory, item.name)
        if path is None:
            outcome.dropped.append(
                Dropped("proposed_file", item.name, UNSAFE_PATH, "path is not a usable repo path")
            )
            continue
        if path in seen:
            outcome.dropped.append(
                Dropped("proposed_file", path, DUPLICATE_KEY, "path proposed more than once")
            )
            continue
        refs: list[int] = []
        unknown = 0
        for label in item.conventions:
            convention = conventions.get(label.strip())
            if convention is None or convention.id is None:
                unknown += 1
                continue
            if convention.id not in refs:
                refs.append(convention.id)
        if unknown:
            outcome.unresolved.append(
                {
                    "kind": "dropped_convention_citation",
                    "ref": path,
                    "detail": f"{unknown} cited convention label(s) were not supplied",
                }
            )
        try:
            row = ProposedFile(
                path=path,
                # Replaced below from the snapshot; `new` is the placeholder
                # that makes the row constructible, never the stored answer.
                status="new",
                purpose=scrub_secrets(" ".join(item.purpose.split())),
                implements_component_key=item.component_key,
                conventions_refs=refs,
            )
        except ValueError as exc:
            outcome.dropped.append(
                Dropped("proposed_file", path, INVALID_ROW, f"{type(exc).__name__}")
            )
            continue
        seen.add(path)
        staged.append((row, item))

    real = existing_paths(
        conn, blueprint.repo_id, blueprint.commit_sha, [row.path for row, _ in staged]
    )
    for row, _item in staged:
        # `unchanged` is never produced here: a file this design does not
        # touch has no reason to appear, and claiming a file is unchanged is
        # a statement about the whole design that no single proposal can make.
        row.status = "modified" if row.path in real else "new"
        outcome.files.append(row)
        if not row.conventions_refs:
            outcome.unresolved.append(
                {
                    "kind": "uncited_file_placement",
                    "ref": row.path,
                    "detail": "no stored convention justifies this placement; it is the "
                    "designer's choice, not a rule the repository follows",
                }
            )
    outcome.unresolved.extend(
        {"kind": "dropped_proposed_file", "ref": d.key, "reason": d.reason, "detail": d.detail}
        for d in outcome.dropped
    )
    return outcome


def propose_files(
    conn: psycopg.Connection,
    service: ModelService,
    blueprint: Blueprint,
    components: Sequence[BlueprintComponent],
    slice_: RepoSlice | None = None,
    *,
    deep: bool = False,
    context: CallContext | None = None,
) -> FileOutcome:
    """One `design`-role call for the file structure, resolved into rows.
    Reads the snapshot; writes nothing.

    With no components there is nothing to place, so no model call is made —
    an empty proposal is the correct answer, not a prompt worth paying for."""
    if not components:
        return FileOutcome(
            unresolved=[
                {
                    "kind": "no_files_proposed",
                    "detail": "the design has no components, so no file placements were asked for",
                }
            ]
        )
    ctx = load_file_context(conn, blueprint.repo_id, blueprint.commit_sha, slice_)
    proposal = service.generate_structured(
        FileProposal,
        build_files_prompt(components, ctx),
        "design",
        deep=deep,
        context=context,
    )
    return resolve_proposal(conn, blueprint, proposal, components, ctx)
