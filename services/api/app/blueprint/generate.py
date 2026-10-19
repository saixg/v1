"""Blueprint generation (build-plan.md Part 4.G.1-4).

The division of labour is the project's governing rule applied to design:
**the repository slice is read deterministically, the task's requirements and
constraints are stored records, and the model only arranges them into a
typed design.** It is given a fixed list of candidate repo components under
opaque labels (`K1`, `K2`, ...) and may reference nothing else; the node ids
behind those labels are substituted back here, so a `linked_component_id`
can never be a number a model produced.

The pipeline:

1. **Repo slice** (`load_repo_slice`): real `cluster` nodes from the
   architecture pass with their member files, real entry-point files, real
   `codeaxis.conventions` rows. Facts only.
2. **Task inputs** (`load_task_inputs`): the task's intent and its stored
   requirements and constraints — Pillar 2's records, referenced, never
   re-extracted here.
3. **One structured `design`-role call** (Part 3's routing table assigns
   blueprints to `design`). `ModelService` does the one retry; a second
   schema failure raises and nothing is persisted.
4. **Resolve and drop** (`resolve_draft`): anything the model returned that
   does not resolve against what it was given is dropped with a recorded
   reason, exactly as `app.context.extract` drops a requirement whose
   citation fails. Nothing is repaired.

Two consequences worth naming:

* **Summaries are not sent.** `codeaxis.summaries` rows are model-written
  prose, and CLAUDE.md forbids a generated explanation from becoming the
  input to another one. The slice carries node names, paths, layers and
  member counts instead — all of which an analyzer parsed.
* **A capability the model could not ground is dropped, not downgraded.** A
  component marked `reuse`/`extend` whose candidate label does not resolve
  is recorded in `unresolved` and removed. Turning it into a `proposed`
  component would silently convert "build this on top of what exists" into
  "build it from scratch" — a design decision neither the model nor this
  code is entitled to make.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import psycopg
from pydantic import BaseModel, Field

from app.context.export import scrub_secrets
from app.context.framing import REDACTION_RULE, UNTRUSTED_DATA_RULE, fenced, new_fence
from app.knowledge import (
    create_version,
    get_blueprint,
    get_task,
    insert_components,
    insert_connections,
    insert_proposed_files,
    list_constraints,
    list_conventions,
    list_nodes,
    list_requirements,
    neighborhood,
    next_version_number,
    set_current_version,
    set_version_validation,
)
from app.llm import CallContext, ModelService, Prompt
from app.models import (
    Blueprint,
    BlueprintComponent,
    BlueprintConnection,
    BlueprintConnectionKind,
    BlueprintInterface,
    BlueprintVersion,
    BlueprintVersionReason,
    Citation,
    Constraint,
    ContextEvidence,
    Convention,
    Node,
    ProposedFile,
    Requirement,
    Task,
)

from .validate import ValidationOutcome, VersionRows, validate_rows

logger = logging.getLogger(__name__)

ANALYZER = "blueprint.generate"
ANALYZER_VERSION = "0.1.0"

# Caps on the slice. Each one that bites is reported in `unresolved` rather
# than silently shrinking what the model was shown.
MAX_CANDIDATE_CLUSTERS = 40
MAX_MEMBERS_LISTED = 6
MAX_ENTRY_POINTS = 25
MAX_CONVENTIONS = 40
MAX_REQUIREMENTS = 120
MAX_CONSTRAINTS = 80
# Member node ids and spans recorded as a mapped component's evidence.
MAX_EVIDENCE_MEMBERS = 12

# Reason codes for a dropped draft entity. Stable strings: the API returns
# them and the UI keys its copy off them.
UNKNOWN_CANDIDATE = "unknown_candidate"
MISSING_CANDIDATE = "missing_candidate"
DUPLICATE_KEY = "duplicate_key"
UNKNOWN_ENDPOINT = "unknown_endpoint"
SELF_CONNECTION = "self_connection"
INVALID_ROW = "invalid_row"


# --- the repo slice ---------------------------------------------------------


@dataclass(frozen=True)
class ClusterCandidate:
    """One real component of the repository, offered to the model under
    `label`. `node_id` never crosses into the prompt."""

    label: str
    node_id: int
    name: str
    layer: str | None
    member_count: int
    members: list[Node]


@dataclass
class RepoSlice:
    """Deterministically retrieved facts about one snapshot. No prose, no
    model output (see the module docstring)."""

    repo_id: str
    commit_sha: str
    candidates: list[ClusterCandidate] = field(default_factory=list)
    entry_points: list[Node] = field(default_factory=list)
    conventions: list[Convention] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)

    def by_label(self) -> dict[str, ClusterCandidate]:
        return {c.label: c for c in self.candidates}


def load_repo_slice(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> RepoSlice:
    """Read the clusters, entry points and conventions of one snapshot.
    Read-only: safe on `api_connection`."""
    clusters = [
        n for n in list_nodes(conn, repo_id, commit_sha, kind="cluster") if n.id is not None
    ]
    notes: list[dict] = []
    if len(clusters) > MAX_CANDIDATE_CLUSTERS:
        notes.append(
            {
                "kind": "candidate_clusters_capped",
                "detail": f"{len(clusters)} components exist; the first "
                f"{MAX_CANDIDATE_CLUSTERS} by name were offered as mapping candidates",
            }
        )
    clusters.sort(key=lambda n: (n.name, n.id or 0))
    candidates: list[ClusterCandidate] = []
    for index, cluster in enumerate(clusters[:MAX_CANDIDATE_CLUSTERS], start=1):
        assert cluster.id is not None
        members, _edges, _truncated = neighborhood(
            conn,
            repo_id,
            commit_sha,
            cluster.id,
            depth=1,
            kinds=["belongs_to"],
            # `belongs_to` runs cluster -> member (app.analysis.architecture
            # .writer), so a cluster's members are one hop forward.
            direction="forward",
        )
        files = sorted(
            (n for n in members if n.kind == "file" and n.path),
            key=lambda n: n.path or "",
        )
        layer = cluster.attrs.get("layer")
        candidates.append(
            ClusterCandidate(
                label=f"K{index}",
                node_id=cluster.id,
                name=cluster.name,
                layer=str(layer) if layer else None,
                member_count=len(files),
                members=files,
            )
        )

    files = list_nodes(conn, repo_id, commit_sha, kind="file")
    entry_points = sorted(
        (n for n in files if n.attrs.get("entry_point")), key=lambda n: n.path or ""
    )
    if len(entry_points) > MAX_ENTRY_POINTS:
        notes.append(
            {
                "kind": "entry_points_capped",
                "detail": f"{len(entry_points)} entry points detected; "
                f"{MAX_ENTRY_POINTS} were shown",
            }
        )
    conventions = list_conventions(conn, repo_id, commit_sha)
    if len(conventions) > MAX_CONVENTIONS:
        notes.append(
            {
                "kind": "conventions_capped",
                "detail": f"{len(conventions)} conventions stored; {MAX_CONVENTIONS} were shown",
            }
        )
    if not candidates:
        notes.append(
            {
                "kind": "no_repo_components",
                "detail": "the architecture pass produced no component clusters at this "
                "snapshot, so nothing can be mapped to `reuse` or `extend`",
            }
        )
    return RepoSlice(
        repo_id=repo_id,
        commit_sha=commit_sha,
        candidates=candidates,
        entry_points=entry_points[:MAX_ENTRY_POINTS],
        conventions=conventions[:MAX_CONVENTIONS],
        notes=notes,
    )


# --- the task's own records -------------------------------------------------


@dataclass
class TaskInputs:
    task: Task
    requirements: list[Requirement] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)


def load_task_inputs(conn: psycopg.Connection, task: Task) -> TaskInputs:
    """The task's stored requirements and ledger at its own snapshot.
    Requirements pinned to another commit are listed as a gap rather than
    mixed in (Part 5)."""
    assert task.id is not None
    notes: list[dict] = []
    rows = list_requirements(conn, task.id, limit=MAX_REQUIREMENTS)
    requirements = [
        r
        for r in rows
        if r.commit_sha == task.commit_sha and r.status != "rejected" and r.kind != "open_question"
    ]
    for r in rows:
        if r.commit_sha != task.commit_sha and r.status != "rejected":
            notes.append(
                {
                    "kind": "requirement_pinned_to_older_commit",
                    "ref": f"requirement:{r.id}",
                    "commit_sha": r.commit_sha,
                }
            )
        if r.kind == "open_question" or r.status in ("ambiguous", "unresolved"):
            kind = "open_question" if r.kind == "open_question" else f"{r.status}_requirement"
            notes.append({"kind": kind, "ref": f"requirement:{r.id}"})
    constraints = list_constraints(
        conn, task.repo_id, task.commit_sha, task_id=task.id, limit=MAX_CONSTRAINTS
    )
    return TaskInputs(task=task, requirements=requirements, constraints=constraints, notes=notes)


# --- the model's output contract --------------------------------------------

DraftMapping = Literal["reuse", "extend", "new"]


class DraftInterface(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["api", "event", "function"]
    signature: str | None = Field(default=None, max_length=400)


class DraftComponent(BaseModel):
    """One capability in the proposed design."""

    key: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_-]{0,60}$",
        description="Stable lower-case slug identifying this component, e.g. 'order-intake'. "
        "Unique within the design; reused verbatim by connections and files.",
    )
    name: str = Field(min_length=1, max_length=300)
    mapping: DraftMapping = Field(
        description="reuse = an existing repo component already does this and is used as it "
        "stands; extend = an existing repo component is changed to do it; new = nothing in "
        "the repository does this",
    )
    candidate: str | None = Field(
        default=None,
        max_length=16,
        description="For reuse/extend only: the label of the repo component this maps to, "
        "exactly as given in the candidate list (K1, K2, ...). Null for new.",
    )
    module_key: str | None = Field(
        default=None, max_length=200, description="Optional grouping label shared by components"
    )
    responsibility: str = Field(
        min_length=1,
        max_length=1200,
        description="What this component is responsible for, in two or three sentences",
    )
    interfaces: list[DraftInterface] = Field(default_factory=list, max_length=20)
    confidence: float | None = Field(
        default=None, ge=0, le=1, description="How confident the mapping is; null if unsure"
    )


class DraftConnection(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,60}$")
    kind: BlueprintConnectionKind = Field(
        description="dependency = source needs target to exist; control_flow = source invokes "
        "target; data_flow = data moves from source to target; event = source publishes an "
        "event target consumes",
    )
    source_key: str = Field(min_length=1, max_length=200)
    target_key: str = Field(min_length=1, max_length=200)
    protocol: str | None = Field(default=None, max_length=120)
    trigger: str | None = Field(default=None, max_length=200)
    description: str = Field(min_length=1, max_length=600)
    data_shape: str | None = Field(
        default=None,
        max_length=600,
        description="data_flow only: what moves across this connection",
    )


class DraftUnresolved(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    concerns: list[str] = Field(default_factory=list, max_length=20)


class BlueprintDraft(BaseModel):
    components: list[DraftComponent] = Field(default_factory=list, max_length=80)
    connections: list[DraftConnection] = Field(default_factory=list, max_length=200)
    unresolved: list[DraftUnresolved] = Field(default_factory=list, max_length=30)


DESIGN_SYSTEM = f"""\
You are CodeAxis's blueprint designer. A developer has described work they \
want done in an existing repository. You are given that task's extracted \
requirements and constraints, and a deterministically retrieved slice of the \
repository: its real components (each under a label K1, K2, ...), its real \
entry points, and its real conventions. You produce a typed design.

What a design is here: a set of components, each mapped to `reuse`, `extend` \
or `new`, and the connections between them. It is a plan for review. Nothing \
you write modifies the repository.

Rules, in order of precedence:
1. You may reference a repository component **only** by a label from the \
candidate list, exactly as given. Never invent a label, a numeric id, a file \
path, a module name or a symbol. If no candidate fits, the component is \
`new` and `candidate` is null.
2. `reuse` and `extend` require a `candidate`. `new` requires \
`candidate` to be null. A component whose candidate does not appear in the \
list given to you is discarded.
3. Map a capability to `reuse` or `extend` only when the candidate list \
shows a component that plausibly already covers it. A generic assumption \
about how such repositories are usually built is not evidence; when in \
doubt, use `new`.
4. Every connection names two different components by the `key` you gave \
them. A connection to anything else is discarded.
5. Use `dependency` only for "the source cannot exist without the target". \
Cycles of `dependency` connections are rejected, so model invocation, data \
movement and retries as `control_flow`, `data_flow` or `event` instead.
6. Derive the design from the requirements you were given. Never invent a \
requirement, a technology, a version or a constraint. Where the input is \
insufficient to decide something, add an entry to `unresolved` instead of \
choosing for the developer.
7. Do not describe the repository beyond what the slice states, and never \
claim the code was run or observed executing.

{UNTRUSTED_DATA_RULE}

{REDACTION_RULE}

Respond with a single JSON object matching the required schema and nothing else."""


def _candidate_block(slice_: RepoSlice) -> str:
    if not slice_.candidates:
        return "(The architecture pass found no components at this snapshot.)"
    lines: list[str] = []
    for c in slice_.candidates:
        head = f"{c.label}: {c.name}"
        if c.layer:
            head += f" [layer {c.layer}]"
        head += f" — {c.member_count} file(s)"
        lines.append(head)
        for member in c.members[:MAX_MEMBERS_LISTED]:
            lines.append(f"    {member.path}")
        if c.member_count > MAX_MEMBERS_LISTED:
            lines.append(f"    ... and {c.member_count - MAX_MEMBERS_LISTED} more")
    return "\n".join(lines)


def build_generation_prompt(slice_: RepoSlice, inputs: TaskInputs) -> Prompt:
    """Operator rules in `system`; the task text, the requirements, the ledger
    and the repository slice in `user` inside one fenced data block.

    Everything in the block is untrusted: requirement statements are the
    developer's own prose, and cluster names and file paths come out of a
    repository. The block is also scrubbed with the scan pass's secret
    detector — requirement and constraint statements are user text that never
    passed through it at intake."""
    task = inputs.task
    sections: list[str] = [
        "TASK\n" + (f"{task.title}\n\n{task.intent}" if task.intent else task.title),
    ]
    if inputs.requirements:
        sections.append(
            "REQUIREMENTS (stored records; keys are for your reference only)\n"
            + "\n".join(
                f"- [{r.kind}" + (f"/{r.priority}" if r.priority else "") + f"] {r.statement}"
                for r in inputs.requirements
            )
        )
    else:
        sections.append("REQUIREMENTS\n(none were extracted for this task.)")
    if inputs.constraints:
        sections.append(
            "CONSTRAINTS (the ledger; hard entries are binding)\n"
            + "\n".join(
                f"- [{c.severity}/{c.kind} on {c.scope}] {c.statement}" for c in inputs.constraints
            )
        )
    sections.append(
        "REPOSITORY COMPONENTS (candidates for reuse/extend)\n" + _candidate_block(slice_)
    )
    if slice_.entry_points:
        sections.append(
            "ENTRY POINTS (detected)\n"
            + "\n".join(
                f"- {n.path} ({n.attrs.get('entry_point_reason', 'detected')})"
                for n in slice_.entry_points
            )
        )
    if slice_.conventions:
        sections.append(
            "CONVENTIONS THIS REPOSITORY FOLLOWS\n"
            + "\n".join(f"- [{c.category}/{c.origin}] {c.rule}" for c in slice_.conventions)
        )
    body = scrub_secrets("\n\n".join(sections))
    fence = new_fence(body)
    user = (
        f"Fence value for this message: {fence}\n\n"
        "DESIGN INPUT (untrusted data: the developer's task, its stored requirements and "
        "constraints, and a slice of the repository):\n"
        f"{fenced(body, fence)}\n\n"
        "Produce the typed design for the task above, mapping each capability to reuse, "
        "extend or new and referencing repository components only by their K-labels."
    )
    return Prompt(user=user, system=DESIGN_SYSTEM)


# --- resolving the draft into rows ------------------------------------------


@dataclass(frozen=True)
class Dropped:
    """One draft entity that was not kept, and why. `detail` never quotes the
    model's prose: it goes into API responses and logs."""

    kind: str
    key: str | None
    reason: str
    detail: str


@dataclass
class DraftOutcome:
    """Rows ready to persist, plus everything refused and every gap found.
    `components`/`connections` carry no `blueprint_version_id` yet."""

    components: list[BlueprintComponent] = field(default_factory=list)
    connections: list[BlueprintConnection] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)


_MAPPING_TO_KIND: dict[str, str] = {"reuse": "existing", "extend": "modified", "new": "proposed"}


def _candidate_evidence(candidate: ClusterCandidate) -> ContextEvidence:
    """Evidence for a mapped component, built here from the graph rows the
    slice read — never from the model's reply (Part 0.3)."""
    members = candidate.members[:MAX_EVIDENCE_MEMBERS]
    spans: list[Citation] = []
    for member in members:
        if not member.path:
            continue
        loc = member.attrs.get("loc")
        spans.append(
            Citation(
                path=member.path,
                line_start=1,
                line_end=loc if isinstance(loc, int) and loc > 0 else 1,
                node_id=member.id,
            )
        )
    return ContextEvidence(
        spans=spans,
        node_ids=[candidate.node_id, *(m.id for m in members if m.id is not None)],
        notes=f"component cluster {candidate.name!r} and its members, via belongs_to edges",
        truncated=candidate.member_count > MAX_EVIDENCE_MEMBERS,
    )


def resolve_draft(
    draft: BlueprintDraft, slice_: RepoSlice, blueprint: Blueprint
) -> DraftOutcome:
    """Turn a validated model reply into rows, dropping whatever does not
    resolve against what the model was given. Pure: no I/O, no model.

    Drop rules, each recorded with a reason: a duplicate key; a `reuse`/
    `extend` component with no candidate or an unknown one; a connection
    naming a component that was not kept, or joining a component to itself; a
    row the typed model rejects (which is a bug in these rules, so it is
    reported rather than swallowed)."""
    by_label = slice_.by_label()
    outcome = DraftOutcome()
    seen: set[str] = set()

    for item in draft.components:
        if item.key in seen:
            outcome.dropped.append(
                Dropped("component", item.key, DUPLICATE_KEY, "key used more than once")
            )
            continue
        candidate: ClusterCandidate | None = None
        if item.mapping in ("reuse", "extend"):
            label = (item.candidate or "").strip()
            if not label:
                outcome.dropped.append(
                    Dropped(
                        "component",
                        item.key,
                        MISSING_CANDIDATE,
                        f"mapping {item.mapping!r} names no repository component",
                    )
                )
                continue
            candidate = by_label.get(label)
            if candidate is None:
                outcome.dropped.append(
                    Dropped(
                        "component",
                        item.key,
                        UNKNOWN_CANDIDATE,
                        f"candidate {label!r} was not in the list supplied to the model",
                    )
                )
                continue
        elif item.candidate:
            # `new` with a candidate is contradictory. The candidate is what
            # is discarded, not the component: "nothing here does this" is the
            # claim the mapping makes, and it stands on its own.
            outcome.dropped.append(
                Dropped(
                    "candidate",
                    item.key,
                    INVALID_ROW,
                    "a 'new' component named a repository candidate; the candidate was ignored",
                )
            )
        try:
            row = BlueprintComponent(
                key=item.key,
                name=item.name,
                kind=_MAPPING_TO_KIND[item.mapping],  # pyright: ignore[reportArgumentType]
                mapping=item.mapping,  # pyright: ignore[reportArgumentType]
                linked_component_id=candidate.node_id if candidate else None,
                repo_id=blueprint.repo_id,
                commit_sha=blueprint.commit_sha,
                module_key=item.module_key,
                responsibility=scrub_secrets(item.responsibility),
                interfaces=[
                    BlueprintInterface(name=i.name, kind=i.kind, signature=i.signature)
                    for i in item.interfaces
                ],
                confidence=item.confidence,
                evidence=_candidate_evidence(candidate) if candidate else ContextEvidence(),
            )
        except ValueError as exc:
            outcome.dropped.append(
                Dropped("component", item.key, INVALID_ROW, f"{type(exc).__name__}")
            )
            continue
        seen.add(item.key)
        outcome.components.append(row)

    kept_keys = {c.key for c in outcome.components}
    seen_connections: set[str] = set()
    for edge in draft.connections:
        if edge.key in seen_connections:
            outcome.dropped.append(
                Dropped("connection", edge.key, DUPLICATE_KEY, "key used more than once")
            )
            continue
        missing = [k for k in (edge.source_key, edge.target_key) if k not in kept_keys]
        if missing:
            outcome.dropped.append(
                Dropped(
                    "connection",
                    edge.key,
                    UNKNOWN_ENDPOINT,
                    f"names {len(missing)} component(s) not in the kept design",
                )
            )
            continue
        if edge.source_key == edge.target_key:
            outcome.dropped.append(
                Dropped("connection", edge.key, SELF_CONNECTION, "source and target are the same")
            )
            continue
        semantics = {
            k: scrub_secrets(v)
            for k, v in (
                ("protocol", edge.protocol),
                ("trigger", edge.trigger),
                ("description", edge.description),
            )
            if v
        }
        shape = (
            {"description": scrub_secrets(edge.data_shape)}
            if edge.kind == "data_flow" and edge.data_shape
            else None
        )
        try:
            row = BlueprintConnection(
                key=edge.key,
                kind=edge.kind,
                source_key=edge.source_key,
                target_key=edge.target_key,
                semantics=semantics,
                data_shape=shape,
            )
        except ValueError as exc:
            outcome.dropped.append(
                Dropped("connection", edge.key, INVALID_ROW, f"{type(exc).__name__}")
            )
            continue
        seen_connections.add(edge.key)
        outcome.connections.append(row)

    outcome.unresolved = [
        *slice_.notes,
        *[
            {
                "kind": "design_question",
                "detail": scrub_secrets(" ".join(q.question.split())),
                "concerns": [c for c in q.concerns if c in kept_keys],
            }
            for q in draft.unresolved
        ],
        *[
            {"kind": "dropped_" + d.kind, "ref": d.key, "reason": d.reason, "detail": d.detail}
            for d in outcome.dropped
        ],
    ]
    if not outcome.components:
        outcome.unresolved.append(
            {
                "kind": "no_components_survived",
                "detail": "every proposed component was dropped; the design is empty rather "
                "than filled with ungrounded guesses",
            }
        )
    return outcome


def propose_design(
    service: ModelService,
    slice_: RepoSlice,
    inputs: TaskInputs,
    blueprint: Blueprint,
    *,
    deep: bool = False,
    context: CallContext | None = None,
) -> DraftOutcome:
    """One `design`-role call, resolved into rows. No database access.

    Raises whatever `generate_structured` raises (schema failure after the
    one retry, refusal, truncation, transport); the caller then persists
    nothing. `deep=True` escalates to the route's escalation model, which
    Part 3 reserves for deep-plan mode and failed validation."""
    draft = service.generate_structured(
        BlueprintDraft,
        build_generation_prompt(slice_, inputs),
        "design",
        deep=deep,
        context=context,
    )
    outcome = resolve_draft(draft, slice_, blueprint)
    outcome.unresolved.extend(inputs.notes)
    return outcome


# --- persistence ------------------------------------------------------------


@dataclass(frozen=True)
class GenerationResult:
    version: BlueprintVersion
    components: list[BlueprintComponent]
    connections: list[BlueprintConnection]
    proposed_files: list[ProposedFile]
    validation: ValidationOutcome
    unresolved: list[dict]
    dropped: list[Dropped]
    promoted: bool


def persist_version(
    conn: psycopg.Connection,
    blueprint: Blueprint,
    *,
    components: Sequence[BlueprintComponent],
    connections: Sequence[BlueprintConnection],
    proposed_files: Sequence[ProposedFile],
    origin_reason: BlueprintVersionReason,
    created_by: str | None = None,
    analyzer: str = ANALYZER,
    analyzer_version: str = ANALYZER_VERSION,
) -> tuple[BlueprintVersion, ValidationOutcome, bool]:
    """Append a version, fill it, validate it, record the verdict, and promote
    it if it may be served. Needs `worker_connection`.

    Returns `(version, outcome, promoted)`. The version is written as `draft`
    and only then validated, so a reader can never catch a half-filled
    version claiming to be `validated`.

    Promotion rule: a version becomes the blueprint's current one unless
    validation found something blocking *and* the blueprint already has a
    current version to fall back on. A failed regeneration therefore leaves
    the previous design in place (`get_current_version`'s own stated
    contract), while a first generation that failed validation is still
    shown — with its reasons — because the alternative is a blueprint with
    nothing to look at and no explanation.
    """
    assert blueprint.id is not None
    version = create_version(
        conn,
        BlueprintVersion(
            blueprint_id=blueprint.id,
            version=next_version_number(conn, blueprint.id),
            status="draft",
            origin_reason=origin_reason,
            analyzer=analyzer,
            analyzer_version=analyzer_version,
            created_by=created_by,
        ),
    )
    assert version.id is not None
    insert_components(conn, version.id, components)
    insert_connections(conn, version.id, connections)
    insert_proposed_files(conn, version.id, proposed_files)

    rows = VersionRows(
        blueprint=blueprint,
        version=version,
        components=list(components),
        connections=list(connections),
        proposed_files=list(proposed_files),
    )
    outcome = validate_rows(conn, rows)
    set_version_validation(
        conn, version.id, status=outcome.status, validation_errors=outcome.errors
    )
    stored = version.model_copy(
        update={"status": outcome.status, "validation_errors": outcome.errors}
    )
    promoted = outcome.status != "invalid" or blueprint.current_version_id is None
    if promoted:
        set_current_version(conn, blueprint.id, version.id)
    return stored, outcome, promoted


def generate_blueprint(
    conn: psycopg.Connection,
    service: ModelService,
    blueprint_id: str,
    *,
    deep: bool = False,
    created_by: str | None = None,
    context: CallContext | None = None,
) -> GenerationResult:
    """Generate a blueprint's first (or a wholly new) version end to end:
    slice, task inputs, one model call, file proposal, validation,
    persistence. Needs `worker_connection`.

    Any model or read failure raises before the version row is created, so a
    failed generation leaves the blueprint exactly as it was.
    """
    from .files import propose_files

    blueprint = get_blueprint(conn, blueprint_id)
    if blueprint is None:
        raise LookupError(f"blueprint {blueprint_id} not found")
    task = get_task(conn, blueprint.task_id)
    if task is None:
        raise LookupError(f"blueprint {blueprint_id} references missing task {blueprint.task_id}")
    if task.commit_sha != blueprint.commit_sha or task.repo_id != blueprint.repo_id:
        raise ValueError(
            f"blueprint {blueprint_id} is grounded at ({blueprint.repo_id}, "
            f"{blueprint.commit_sha}) but its task is at ({task.repo_id}, {task.commit_sha}); "
            "re-ground the task or create a new blueprint rather than mixing snapshots"
        )

    slice_ = load_repo_slice(conn, blueprint.repo_id, blueprint.commit_sha)
    inputs = load_task_inputs(conn, task)
    design = propose_design(
        service, slice_, inputs, blueprint, deep=deep, context=context
    )
    files = propose_files(
        conn, service, blueprint, design.components, slice_, context=context
    )
    version, outcome, promoted = persist_version(
        conn,
        blueprint,
        components=design.components,
        connections=design.connections,
        proposed_files=files.files,
        origin_reason="generated",
        created_by=created_by,
    )
    unresolved = [*design.unresolved, *files.unresolved]
    # Counts only: responsibilities, paths and questions are untrusted text.
    logger.info(
        "blueprint %s version %s: %d components, %d connections, %d files, status %s, "
        "%d dropped, %d unresolved",
        blueprint_id, version.version, len(design.components), len(design.connections),
        len(files.files), outcome.status, len(design.dropped) + len(files.dropped),
        len(unresolved),
    )
    return GenerationResult(
        version=version,
        components=design.components,
        connections=design.connections,
        proposed_files=files.files,
        validation=outcome,
        unresolved=unresolved,
        dropped=[*design.dropped, *files.dropped],
        promoted=promoted,
    )
