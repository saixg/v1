"""Partial regeneration (build-plan.md Part 4.G.9: "stable node IDs; approved
decisions are locked; regenerate only the affected neighborhood; every change
creates a new blueprint version").

**What "the affected neighborhood" means here**, stated once so it is
reviewable rather than implicit:

    neighborhood(seeds) = seeds
                        + every component joined to a seed by one connection,
                          in either direction, of any kind

One hop, not two. A blueprint connection means the two components' contracts
touch, so redesigning one can change what the other must offer or expect —
that is a real dependency and it is included. Two hops, in any connected
design, is the whole design, which would make "partial" meaningless. The cost
is stated plainly: a change that ripples three components deep is **not**
caught by one regeneration; the honest handling is that the operator
regenerates again with the further component as a seed, not that this module
quietly widens its scope.

**Everything outside the neighborhood is carried over byte-identically.** The
carried rows are the stored rows with only their row identity cleared
(`id`, `blueprint_version_id`, `created_at`) — a new version re-inserts rows,
so new uuids are unavoidable, but `key`, name, kind, mapping,
`linked_component_id`, responsibility, interfaces, confidence, evidence and
`generation_status` are the same values. That is what makes `key` a stable
identity across versions.

**`locked` is a hard block.** Per the owner's decision: a regeneration whose
output would alter a locked entity is rejected with reasons and nothing is
written — not applied-with-a-warning, and not silently narrowed to avoid the
lock. Three cases are rejections:

* a **seed is locked** — the request is to redesign something approved;
* a **locked connection touches a regenerated component** — carrying it over
  would leave it pointing at a component the regeneration may have renamed
  or removed, and rewriting it would alter a locked row;
* a **locked proposed file implements a regenerated component** — same
  reasoning.

A locked component that is merely a *neighbour* of a seed is not a rejection:
it is excluded from the regenerated set and carried over untouched, which is
exactly what the lock asks for.

An `edited` (human-touched but unlocked) component inside the seed set *is*
regenerated — that is what was asked for — but it is reported in
`advisories` so the caller can say so before the result is accepted.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

import psycopg

from app.context.export import scrub_secrets
from app.context.framing import REDACTION_RULE, UNTRUSTED_DATA_RULE, fenced, new_fence
from app.knowledge import get_blueprint, get_task
from app.llm import CallContext, ModelService, Prompt
from app.models import (
    BlueprintComponent,
    BlueprintConnection,
    BlueprintVersion,
    ProposedFile,
    ValidationError,
)

from .files import propose_files
from .generate import (
    DESIGN_SYSTEM,
    DUPLICATE_KEY,
    BlueprintDraft,
    DraftOutcome,
    Dropped,
    build_generation_prompt,
    load_repo_slice,
    load_task_inputs,
    persist_version,
    resolve_draft,
)
from .validate import ValidationOutcome, load_version_rows

logger = logging.getLogger(__name__)

ANALYZER = "blueprint.regenerate"
ANALYZER_VERSION = "0.1.0"

# Rejection codes. Stable strings, same contract as the validator's.
UNKNOWN_SEED = "unknown_seed_component"
NO_SEEDS = "no_seed_components"
LOCKED_COMPONENT_IN_SCOPE = "locked_component_in_scope"
LOCKED_CONNECTION_IN_SCOPE = "locked_connection_in_scope"
LOCKED_FILE_IN_SCOPE = "locked_file_in_scope"
NO_CURRENT_VERSION = "no_current_version"

# Advisory codes (reported, never blocking).
REGENERATING_EDITED = "regenerating_edited_component"
FROZEN_KEY_COLLISION = "frozen_key_collision"


@dataclass(frozen=True)
class Neighborhood:
    """The partition of one version's components. `seeds` is what was asked
    for, `regenerate` is seeds plus their one-hop neighbours minus locked
    components, and `frozen` is everything carried over."""

    seeds: set[str]
    regenerate: set[str]
    frozen: set[str]


def affected_neighborhood(
    components: Sequence[BlueprintComponent],
    connections: Sequence[BlueprintConnection],
    seeds: Sequence[str],
) -> Neighborhood:
    """One hop in either direction over every connection kind, with locked
    components held out. Pure."""
    keys = {c.key for c in components}
    locked = {c.key for c in components if c.locked}
    wanted = {s for s in seeds if s in keys}
    reach = set(wanted)
    for connection in connections:
        if connection.source_key in wanted:
            reach.add(connection.target_key)
        if connection.target_key in wanted:
            reach.add(connection.source_key)
    regenerate = (reach & keys) - locked
    return Neighborhood(seeds=wanted, regenerate=regenerate, frozen=keys - regenerate)


# --- the prompt -------------------------------------------------------------


REGENERATION_SYSTEM = (
    DESIGN_SYSTEM.replace(
        "Respond with a single JSON object matching the required schema and nothing else.",
        "",
    ).rstrip()
    + f"""

This is a **partial** regeneration of an existing design. Additional rules, \
which outrank nothing above but come before your own judgement:
8. Redesign only the components listed as TO REDESIGN. Emit a component for \
each of those keys you still believe the design needs, and no others.
9. The components listed as KEPT UNCHANGED are fixed. Never emit a component \
with one of their keys, and never propose changing them. You may connect to \
them: a connection may name a kept key as its source or target.
10. Reuse the key of a component you are redesigning when it survives, so \
the developer's decisions about it are not orphaned. Drop a key only when \
the component should no longer exist.

{UNTRUSTED_DATA_RULE}

{REDACTION_RULE}

Respond with a single JSON object matching the required schema and nothing else."""
)


def build_regeneration_prompt(
    base: Prompt,
    *,
    regenerate: Sequence[BlueprintComponent],
    frozen: Sequence[BlueprintComponent],
    frozen_connections: Sequence[BlueprintConnection],
    instruction: str | None,
) -> Prompt:
    """The generation prompt plus the design's current state, partitioned.

    Built from the generation prompt rather than beside it so the repository
    slice, the requirements and the ledger are described identically in both
    passes — a regeneration that saw a different repository than the
    generation did would not be a regeneration."""
    sections: list[str] = [
        "TO REDESIGN (these keys, and only these)\n"
        + "\n".join(
            f"- {c.key} ({c.mapping}): {c.name} — "
            f"{c.responsibility or 'no responsibility stated'}"
            for c in regenerate
        )
    ]
    if frozen:
        sections.append(
            "KEPT UNCHANGED (fixed; connect to them, never redefine them)\n"
            + "\n".join(
                f"- {c.key} ({c.mapping}): {c.name} — "
                f"{c.responsibility or 'no responsibility stated'}"
                for c in frozen
            )
        )
    if frozen_connections:
        sections.append(
            "CONNECTIONS ALREADY IN PLACE BETWEEN KEPT COMPONENTS\n"
            + "\n".join(
                f"- {c.key}: {c.source_key} -{c.kind}-> {c.target_key}"
                for c in frozen_connections
            )
        )
    if instruction:
        sections.append(
            "WHAT THE DEVELOPER ASKED FOR IN THIS REGENERATION\n"
            + " ".join(instruction.split())
        )
    body = scrub_secrets("\n\n".join(sections))
    fence = new_fence(body, base.user)
    user = (
        base.user
        + "\n\nCURRENT DESIGN (untrusted data: the stored design being partially "
        f"regenerated). Fence value: {fence}\n"
        + fenced(body, fence)
    )
    return Prompt(user=user, system=REGENERATION_SYSTEM, images=base.images)


# --- carrying rows over -----------------------------------------------------

_ROW_IDENTITY = {"id": None, "blueprint_version_id": None, "created_at": None}


def carry_component(row: BlueprintComponent) -> BlueprintComponent:
    """The same component for a new version: every semantic field identical,
    only row identity cleared."""
    return row.model_copy(update=_ROW_IDENTITY)


def carry_connection(row: BlueprintConnection) -> BlueprintConnection:
    return row.model_copy(update=_ROW_IDENTITY)


def carry_file(row: ProposedFile) -> ProposedFile:
    return row.model_copy(update=_ROW_IDENTITY)


# --- the pass ---------------------------------------------------------------


@dataclass
class RegenerationResult:
    """`rejected` non-empty means nothing was written and `version` is None.
    Otherwise a new version exists and `advisories` is what the caller should
    say about it."""

    rejected: list[ValidationError] = field(default_factory=list)
    version: BlueprintVersion | None = None
    neighborhood: Neighborhood | None = None
    components: list[BlueprintComponent] = field(default_factory=list)
    connections: list[BlueprintConnection] = field(default_factory=list)
    proposed_files: list[ProposedFile] = field(default_factory=list)
    validation: ValidationOutcome | None = None
    advisories: list[ValidationError] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    promoted: bool = False


def _reject(code: str, message: str, kind: str, key: str | None = None) -> ValidationError:
    return ValidationError(
        code=code,
        message=message,
        entity_kind=kind,  # pyright: ignore[reportArgumentType]
        entity_key=key,
    )


def check_locks(
    components: Sequence[BlueprintComponent],
    connections: Sequence[BlueprintConnection],
    proposed_files: Sequence[ProposedFile],
    seeds: Sequence[str],
    plan: Neighborhood,
) -> tuple[list[ValidationError], list[ValidationError]]:
    """`(rejections, advisories)` for one regeneration plan. Pure.

    The three rejection cases are the ones in the module docstring; the
    advisory case is regenerating a component a human had edited."""
    rejections: list[ValidationError] = []
    advisories: list[ValidationError] = []
    by_key = {c.key: c for c in components}
    keys = set(by_key)

    for seed in dict.fromkeys(seeds):
        if seed not in keys:
            rejections.append(
                _reject(
                    UNKNOWN_SEED,
                    f"{seed!r} is not a component of the current version",
                    "component",
                    seed,
                )
            )
        elif by_key[seed].locked:
            rejections.append(
                _reject(
                    LOCKED_COMPONENT_IN_SCOPE,
                    f"component {seed!r} is locked (an approved decision); unlock it before "
                    "asking for it to be redesigned",
                    "component",
                    seed,
                )
            )
    if not plan.seeds and not rejections:
        rejections.append(
            _reject(NO_SEEDS, "no component was named to regenerate", "blueprint")
        )

    for connection in connections:
        if not connection.locked:
            continue
        touched = {connection.source_key, connection.target_key} & plan.regenerate
        if touched:
            rejections.append(
                _reject(
                    LOCKED_CONNECTION_IN_SCOPE,
                    f"connection {connection.key!r} is locked but joins "
                    f"{', '.join(sorted(touched))}, which this regeneration would redesign",
                    "connection",
                    connection.key,
                )
            )
    for proposed in proposed_files:
        if proposed.locked and proposed.implements_component_key in plan.regenerate:
            rejections.append(
                _reject(
                    LOCKED_FILE_IN_SCOPE,
                    f"proposed file {proposed.path!r} is locked but implements "
                    f"{proposed.implements_component_key!r}, which this regeneration would "
                    "redesign",
                    "proposed_file",
                    proposed.path,
                )
            )

    for key in sorted(plan.regenerate):
        if by_key[key].generation_status == "edited":
            advisories.append(
                _reject(
                    REGENERATING_EDITED,
                    f"component {key!r} was edited by hand and is being regenerated; lock it "
                    "if that edit should survive",
                    "component",
                    key,
                )
            )
    return rejections, advisories


def _merge(
    plan: Neighborhood,
    stored_components: Sequence[BlueprintComponent],
    stored_connections: Sequence[BlueprintConnection],
    stored_files: Sequence[ProposedFile],
    design: DraftOutcome,
    new_files: Sequence[ProposedFile],
) -> tuple[
    list[BlueprintComponent], list[BlueprintConnection], list[ProposedFile], list[ValidationError]
]:
    """Frozen rows carried over, regenerated rows taken from the draft.

    A regenerated component that re-uses a frozen key is dropped: the frozen
    row is authoritative by definition, and overwriting it would be the
    silent alteration this pass exists to prevent."""
    advisories: list[ValidationError] = []
    components = [carry_component(c) for c in stored_components if c.key in plan.frozen]
    taken: set[str] = set(plan.frozen)
    for row in design.components:
        if row.key in taken:
            advisories.append(
                _reject(
                    FROZEN_KEY_COLLISION,
                    f"the regeneration proposed a component with the kept key {row.key!r}; the "
                    "kept component was preserved and the proposal dropped",
                    "component",
                    row.key,
                )
            )
            continue
        taken.add(row.key)
        components.append(row)

    live = {c.key for c in components}
    connections = [
        carry_connection(c)
        for c in stored_connections
        if c.source_key in plan.frozen and c.target_key in plan.frozen
    ]
    seen = {c.key for c in connections}
    for row in design.connections:
        if row.key in seen:
            advisories.append(
                _reject(
                    DUPLICATE_KEY,
                    f"the regeneration proposed connection {row.key!r}, whose key is already "
                    "held by a kept connection; the proposal was dropped",
                    "connection",
                    row.key,
                )
            )
            continue
        if row.source_key not in live or row.target_key not in live:
            continue  # already reported by resolve_draft; cannot survive validation
        seen.add(row.key)
        connections.append(row)

    files = [
        carry_file(f)
        for f in stored_files
        if f.implements_component_key is None or f.implements_component_key in plan.frozen
    ]
    paths = {f.path for f in files}
    files.extend(f for f in new_files if f.path not in paths)
    return components, connections, files, advisories


def regenerate_blueprint(
    conn: psycopg.Connection,
    service: ModelService,
    blueprint_id: str,
    component_keys: Sequence[str],
    *,
    instruction: str | None = None,
    deep: bool = False,
    created_by: str | None = None,
    context: CallContext | None = None,
) -> RegenerationResult:
    """Regenerate the neighborhood of `component_keys` as a new version.
    Needs `worker_connection`.

    Nothing is written when the result is a rejection, and nothing is written
    before the model call succeeds: a failed regeneration leaves the
    blueprint's current version exactly as it was.
    """
    blueprint = get_blueprint(conn, blueprint_id)
    if blueprint is None:
        raise LookupError(f"blueprint {blueprint_id} not found")
    if blueprint.current_version_id is None:
        return RegenerationResult(
            rejected=[
                _reject(
                    NO_CURRENT_VERSION,
                    "this blueprint has no current version; generate one before regenerating "
                    "part of it",
                    "blueprint",
                )
            ]
        )
    rows = load_version_rows(conn, blueprint.current_version_id)
    plan = affected_neighborhood(rows.components, rows.connections, component_keys)
    rejections, advisories = check_locks(
        rows.components, rows.connections, rows.proposed_files, component_keys, plan
    )
    if rejections:
        return RegenerationResult(rejected=rejections, neighborhood=plan, advisories=advisories)

    task = get_task(conn, blueprint.task_id)
    if task is None:
        raise LookupError(f"blueprint {blueprint_id} references missing task {blueprint.task_id}")
    slice_ = load_repo_slice(conn, blueprint.repo_id, blueprint.commit_sha)
    inputs = load_task_inputs(conn, task)
    frozen = [c for c in rows.components if c.key in plan.frozen]
    prompt = build_regeneration_prompt(
        build_generation_prompt(slice_, inputs),
        regenerate=[c for c in rows.components if c.key in plan.regenerate],
        frozen=frozen,
        frozen_connections=[
            c
            for c in rows.connections
            if c.source_key in plan.frozen and c.target_key in plan.frozen
        ],
        instruction=instruction,
    )
    draft = service.generate_structured(
        BlueprintDraft, prompt, "design", deep=deep, context=context
    )
    design = resolve_draft(draft, slice_, blueprint)

    # Files are proposed only for what was regenerated; a frozen component's
    # files are carried over, so re-asking for them would mean paying for a
    # proposal that is then discarded.
    regenerated = [c for c in design.components if c.key not in plan.frozen]
    files = propose_files(conn, service, blueprint, regenerated, slice_, context=context)
    components, connections, proposed_files, merge_advisories = _merge(
        plan, rows.components, rows.connections, rows.proposed_files, design, files.files
    )
    version, outcome, promoted = persist_version(
        conn,
        blueprint,
        components=components,
        connections=connections,
        proposed_files=proposed_files,
        origin_reason="regenerated",
        created_by=created_by,
        analyzer=ANALYZER,
        analyzer_version=ANALYZER_VERSION,
    )
    logger.info(
        "blueprint %s regenerated into version %s: %d seeds, %d regenerated, %d kept, "
        "status %s, promoted %s",
        blueprint_id, version.version, len(plan.seeds), len(plan.regenerate), len(plan.frozen),
        outcome.status, promoted,
    )
    return RegenerationResult(
        version=version,
        neighborhood=plan,
        components=components,
        connections=connections,
        proposed_files=proposed_files,
        validation=outcome,
        advisories=[*advisories, *merge_advisories],
        unresolved=[*design.unresolved, *files.unresolved, *inputs.notes],
        dropped=[*design.dropped, *files.dropped],
        promoted=promoted,
    )
