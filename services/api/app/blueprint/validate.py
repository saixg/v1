"""Blueprint validation (build-plan.md Part 4.G.5; Phase 5's exit criterion
"blueprint references only real entities; invalid blueprints rejected with
reasons").

**Nothing here repairs a blueprint.** Every check either passes or produces a
`ValidationError` with a stable `code`; a validation failure is a returned
value, never an exception. A design that fails is stored as it was proposed,
marked `invalid`, with its reasons attached — rewriting it would mean the
stored design is not the one anyone reviewed.

The checks, and why each exists:

1. **Schema version** — an unknown `schema_version` is rejected, never
   coerced into the shape this reader expects.
2. **Unique keys**, per entity type. Components, connections and proposed
   files each have their own namespace: a connection may share a key with a
   component without ambiguity, because nothing references a connection.
3. **Connection endpoints resolve** to a component key in the same version.
   This is the one referential check Postgres cannot express as a foreign
   key (see the migration header: endpoints are named by `key`, not by row
   id, precisely so partial regeneration can leave untouched rows alone), so
   it genuinely only holds if it is checked here.
4. **No cycle among `dependency` connections only.** `control_flow`,
   `data_flow` and `event` legitimately loop — retries, polling, pub/sub —
   and flagging those would be a false positive. Each mutually dependent
   group is reported with its actual member keys.
5. **`linked_component_id` resolves** to a real `codeaxis.nodes` row at this
   blueprint's own `(repo_id, commit_sha)`. The column's foreign key already
   guarantees the node exists *somewhere*; what it cannot express is "at this
   snapshot", so for a stored row this check is what catches a link to a node
   that exists at another commit or in another repository. For rows that have
   not been written yet it also catches an id that exists nowhere.
6. **Component snapshot matches the blueprint's.** A component grounded at
   another commit is the cross-snapshot mix Part 5 forbids.
7. **Proposed file statuses match reality**: `new` must not collide with a
   path that exists in the snapshot, `modified`/`unchanged` must correspond
   to one that does.
8. **`implements_component_key` resolves** within the same version.
9. **An `existing`/`modified` component carries evidence.** "This already
   exists" with nothing to point at is an unsupported claim, which is the
   one thing this project will not ship (CLAUDE.md non-negotiable #1).
10. **Ledger constraints** whose scope overlaps a touched path or component.

Decision, stated because a reviewer may disagree: check 5 requires
`kind='cluster'` **strictly**. `BlueprintComponent.linked_component_id` is
documented as "the real `cluster`-kind node this maps to", and Part 4.G.3
maps a proposed *capability* onto a repo *component* — which in this Repo
Model is exactly a `cluster` node produced by the architecture pass. A link
to a file or a symbol is a different and narrower claim; the model's own
comment says those finer-grained targets ride in `evidence`. So a link to a
node that exists but is not a cluster is reported as
`linked_component_not_cluster`, distinct from `linked_component_missing`,
rather than quietly accepted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import psycopg

from app.context.validate import path_in_scope
from app.knowledge import (
    existing_node_ids,
    existing_paths,
    get_blueprint,
    get_version,
    list_components,
    list_connections,
    list_constraints,
    list_proposed_files,
    set_version_validation,
)
from app.models import (
    BLUEPRINT_SCHEMA_VERSION,
    Blueprint,
    BlueprintComponent,
    BlueprintConnection,
    BlueprintVersion,
    BlueprintVersionStatus,
    Constraint,
    ProposedFile,
    ValidationError,
)

ANALYZER = "blueprint.validate"
ANALYZER_VERSION = "0.1.0"

# Every `schema_version` this reader understands. Add a value here only
# together with the code that reads that shape.
KNOWN_SCHEMA_VERSIONS: frozenset[str] = frozenset({BLUEPRINT_SCHEMA_VERSION})

# --- error codes ------------------------------------------------------------
# Stable strings: the API returns them, the UI keys its copy off them, and
# `BLOCKING_CODES` below decides the stored status from them.

UNKNOWN_SCHEMA_VERSION = "unknown_schema_version"
DUPLICATE_COMPONENT_KEY = "duplicate_component_key"
DUPLICATE_CONNECTION_KEY = "duplicate_connection_key"
DUPLICATE_FILE_PATH = "duplicate_file_path"
CONNECTION_SOURCE_MISSING = "connection_source_missing"
CONNECTION_TARGET_MISSING = "connection_target_missing"
DEPENDENCY_CYCLE = "dependency_cycle"
LINKED_COMPONENT_ABSENT = "linked_component_absent"
LINKED_COMPONENT_MISSING = "linked_component_missing"
LINKED_COMPONENT_NOT_CLUSTER = "linked_component_not_cluster"
COMPONENT_SNAPSHOT_MISMATCH = "component_snapshot_mismatch"
UNSUPPORTED_EXISTING_CLAIM = "unsupported_existing_claim"
NEW_FILE_PATH_COLLISION = "new_file_path_collision"
CHANGED_FILE_PATH_MISSING = "changed_file_path_missing"
IMPLEMENTS_COMPONENT_MISSING = "implements_component_missing"
HARD_CONSTRAINT_CONFLICT = "hard_constraint_conflict"
SOFT_CONSTRAINT_CONFLICT = "soft_constraint_conflict"
REPO_WIDE_CONSTRAINT_OVERLAP = "repo_wide_constraint_overlap"

# What a `validated` status may not coexist with. The two constraint codes
# left out are advisory: a soft ledger entry is a stated tradeoff, and a
# repo-wide scope overlaps every conceivable design, so neither is evidence
# that *this* blueprint is wrong.
BLOCKING_CODES: frozenset[str] = frozenset(
    {
        UNKNOWN_SCHEMA_VERSION,
        DUPLICATE_COMPONENT_KEY,
        DUPLICATE_CONNECTION_KEY,
        DUPLICATE_FILE_PATH,
        CONNECTION_SOURCE_MISSING,
        CONNECTION_TARGET_MISSING,
        DEPENDENCY_CYCLE,
        LINKED_COMPONENT_ABSENT,
        LINKED_COMPONENT_MISSING,
        LINKED_COMPONENT_NOT_CLUSTER,
        COMPONENT_SNAPSHOT_MISMATCH,
        UNSUPPORTED_EXISTING_CLAIM,
        NEW_FILE_PATH_COLLISION,
        CHANGED_FILE_PATH_MISSING,
        IMPLEMENTS_COMPONENT_MISSING,
        HARD_CONSTRAINT_CONFLICT,
    }
)

# Ledger kinds that say something about *not changing* a part of the repo, and
# so can be violated by a design. `ci_lint`, `performance`, `dependency` and
# `other` are about how work is done rather than where, and a scope match on
# them carries no information about whether this design is wrong.
CONFLICTING_CONSTRAINT_KINDS: frozenset[str] = frozenset(
    {"do_not_touch", "public_api", "db_schema", "auth_boundary"}
)

# Scopes that match everything. Treated as advisory for the reason given on
# `BLOCKING_CODES`: blocking on them would make every blueprint invalid.
_REPO_WIDE = frozenset({"repo", "*", "**", "/"})


@dataclass(frozen=True)
class VersionRows:
    """One version's stored rows plus the blueprint they belong to — the whole
    input to validation. Loaded by `load_version_rows`, or constructed
    directly by a caller that has just generated them and has not written
    them yet."""

    blueprint: Blueprint
    version: BlueprintVersion
    components: list[BlueprintComponent] = field(default_factory=list)
    connections: list[BlueprintConnection] = field(default_factory=list)
    proposed_files: list[ProposedFile] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationOutcome:
    """`errors` is everything found, blocking entries first. `status` is what
    the version row should carry, and `blocking` is why it is not
    `validated`."""

    status: BlueprintVersionStatus
    errors: list[ValidationError]

    @property
    def blocking(self) -> list[ValidationError]:
        return [e for e in self.errors if e.code in BLOCKING_CODES]

    @property
    def valid(self) -> bool:
        return self.status == "validated"


def _err(
    code: str,
    message: str,
    *,
    entity_kind: str | None = None,
    entity_key: str | None = None,
) -> ValidationError:
    return ValidationError(
        code=code,
        message=message,
        entity_kind=entity_kind,  # pyright: ignore[reportArgumentType]
        entity_key=entity_key,
    )


# --- 1-4, 7-9: checks that need no database ---------------------------------


def check_structure(rows: VersionRows) -> list[ValidationError]:
    """Schema version, key uniqueness, endpoint resolution, dependency
    cycles, intra-version references and the evidence requirement. Pure: no
    I/O, so the whole of this is testable on constructed rows."""
    out: list[ValidationError] = []

    if rows.version.schema_version not in KNOWN_SCHEMA_VERSIONS:
        out.append(
            _err(
                UNKNOWN_SCHEMA_VERSION,
                f"schema_version {rows.version.schema_version!r} is not one this build reads "
                f"({', '.join(sorted(KNOWN_SCHEMA_VERSIONS))}); it is rejected rather than "
                "read as if it were a known version",
                entity_kind="blueprint",
            )
        )

    component_keys: set[str] = set()
    for component in rows.components:
        if component.key in component_keys:
            out.append(
                _err(
                    DUPLICATE_COMPONENT_KEY,
                    f"component key {component.key!r} is used more than once in this version",
                    entity_kind="component",
                    entity_key=component.key,
                )
            )
        component_keys.add(component.key)

    connection_keys: set[str] = set()
    for connection in rows.connections:
        if connection.key in connection_keys:
            out.append(
                _err(
                    DUPLICATE_CONNECTION_KEY,
                    f"connection key {connection.key!r} is used more than once in this version",
                    entity_kind="connection",
                    entity_key=connection.key,
                )
            )
        connection_keys.add(connection.key)

    file_paths: set[str] = set()
    for proposed in rows.proposed_files:
        if proposed.path in file_paths:
            out.append(
                _err(
                    DUPLICATE_FILE_PATH,
                    f"path {proposed.path!r} is proposed more than once in this version",
                    entity_kind="proposed_file",
                    entity_key=proposed.path,
                )
            )
        file_paths.add(proposed.path)

    for connection in rows.connections:
        for code, role, key in (
            (CONNECTION_SOURCE_MISSING, "source_key", connection.source_key),
            (CONNECTION_TARGET_MISSING, "target_key", connection.target_key),
        ):
            if key not in component_keys:
                out.append(
                    _err(
                        code,
                        f"connection {connection.key!r} names {role} {key!r}, which is not a "
                        "component in this version",
                        entity_kind="connection",
                        entity_key=connection.key,
                    )
                )

    out.extend(_dependency_cycles(rows, component_keys))

    for component in rows.components:
        if component.kind == "proposed":
            continue
        if component.linked_component_id is None:
            # Enforced in SQL and in Pydantic; checked anyway because this
            # function also runs on rows that have not been through either.
            out.append(
                _err(
                    LINKED_COMPONENT_ABSENT,
                    f"component {component.key!r} is {component.kind!r} but names no repo entity",
                    entity_kind="component",
                    entity_key=component.key,
                )
            )
        if not component.evidence.spans and not component.evidence.node_ids:
            out.append(
                _err(
                    UNSUPPORTED_EXISTING_CLAIM,
                    f"component {component.key!r} claims to be {component.kind!r} in the "
                    "repository but carries no evidence spans and no node ids",
                    entity_kind="component",
                    entity_key=component.key,
                )
            )

    for proposed in rows.proposed_files:
        key = proposed.implements_component_key
        if key is not None and key not in component_keys:
            out.append(
                _err(
                    IMPLEMENTS_COMPONENT_MISSING,
                    f"proposed file {proposed.path!r} implements {key!r}, which is not a "
                    "component in this version",
                    entity_kind="proposed_file",
                    entity_key=proposed.path,
                )
            )

    return out


def _dependency_cycles(rows: VersionRows, component_keys: set[str]) -> list[ValidationError]:
    """Mutually dependent groups in the `dependency` subgraph. Each group is
    reported once, with its member keys, so the reader can see what to break
    rather than being told a cycle exists somewhere."""
    graph: dict[str, list[str]] = {key: [] for key in component_keys}
    for connection in rows.connections:
        if connection.kind != "dependency":
            continue
        if connection.source_key in graph and connection.target_key in graph:
            graph[connection.source_key].append(connection.target_key)
    out: list[ValidationError] = []
    for group in _strongly_connected(graph):
        if len(group) < 2:
            # A single node is only its own cycle via a self-loop, which the
            # schema forbids outright.
            continue
        members = sorted(group)
        out.append(
            _err(
                DEPENDENCY_CYCLE,
                "dependency connections form a cycle among components: "
                + " -> ".join(members)
                + f" -> {members[0]}",
                entity_kind="component",
                entity_key=members[0],
            )
        )
    return sorted(out, key=lambda e: e.entity_key or "")


def _strongly_connected(graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's SCCs, iteratively. Iterative because a blueprint may hold up
    to `MAX_LIST_LIMIT` components and a recursive walk over an adversarial
    chain of them would exhaust the interpreter's stack."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[list[str]] = []
    counter = 0
    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, cursor = work[-1]
            if cursor == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            neighbours = graph[node]
            while cursor < len(neighbours):
                child = neighbours[cursor]
                cursor += 1
                work[-1] = (node, cursor)
                if child not in index:
                    work.append((child, 0))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            if low[node] == index[node]:
                group: list[str] = []
                while True:
                    popped = stack.pop()
                    on_stack.discard(popped)
                    group.append(popped)
                    if popped == node:
                        break
                found.append(group)
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
    return found


# --- 5-7: checks that need the snapshot -------------------------------------


def check_grounding(conn: psycopg.Connection, rows: VersionRows) -> list[ValidationError]:
    """Every `linked_component_id` and every proposed path against the
    blueprint's own snapshot. Read-only: safe on `api_connection`."""
    repo_id = rows.blueprint.repo_id
    sha = rows.blueprint.commit_sha
    out: list[ValidationError] = []

    for component in rows.components:
        if component.repo_id != repo_id or component.commit_sha != sha:
            out.append(
                _err(
                    COMPONENT_SNAPSHOT_MISMATCH,
                    f"component {component.key!r} is grounded at "
                    f"({component.repo_id}, {component.commit_sha}) but the blueprint is at "
                    f"({repo_id}, {sha}); results from different snapshots may not mix",
                    entity_kind="component",
                    entity_key=component.key,
                )
            )

    wanted = {
        c.linked_component_id: c
        for c in rows.components
        if c.kind != "proposed"
        and c.linked_component_id is not None
        and c.repo_id == repo_id
        and c.commit_sha == sha
    }
    if wanted:
        node_ids = sorted(wanted)
        clusters = existing_node_ids(conn, repo_id, sha, node_ids, kind="cluster")
        any_kind = existing_node_ids(conn, repo_id, sha, node_ids)
        for node_id in node_ids:
            component = wanted[node_id]
            if node_id in clusters:
                continue
            if node_id in any_kind:
                out.append(
                    _err(
                        LINKED_COMPONENT_NOT_CLUSTER,
                        f"component {component.key!r} links to node {node_id}, which exists at "
                        f"{sha} but is not a component cluster; a file- or symbol-level target "
                        "belongs in `evidence`, not in linked_component_id",
                        entity_kind="component",
                        entity_key=component.key,
                    )
                )
            else:
                out.append(
                    _err(
                        LINKED_COMPONENT_MISSING,
                        f"component {component.key!r} links to node {node_id}, which does not "
                        f"exist at commit {sha}",
                        entity_kind="component",
                        entity_key=component.key,
                    )
                )

    if rows.proposed_files:
        paths = sorted({f.path for f in rows.proposed_files})
        real = existing_paths(conn, repo_id, sha, paths)
        for proposed in rows.proposed_files:
            if proposed.status == "new" and proposed.path in real:
                out.append(
                    _err(
                        NEW_FILE_PATH_COLLISION,
                        f"{proposed.path!r} is proposed as a new file but already exists at "
                        f"commit {sha}",
                        entity_kind="proposed_file",
                        entity_key=proposed.path,
                    )
                )
            elif proposed.status in ("modified", "unchanged") and proposed.path not in real:
                out.append(
                    _err(
                        CHANGED_FILE_PATH_MISSING,
                        f"{proposed.path!r} is proposed as {proposed.status!r} but is not a file "
                        f"at commit {sha}",
                        entity_kind="proposed_file",
                        entity_key=proposed.path,
                    )
                )

    return out


# --- 10: the constraint ledger ----------------------------------------------


def touched_paths(rows: VersionRows) -> dict[str, list[str]]:
    """Path -> the entity keys that would touch it.

    Two sources, both already stored: a proposed file's own path, and the
    evidence spans of an `existing`/`modified` component (which check 9
    guarantees are present). A `new`/`unchanged` file is included as well as
    a `modified` one — a ledger entry on a directory is about adding to it,
    not only about editing what is there.
    """
    touched: dict[str, list[str]] = {}
    for proposed in rows.proposed_files:
        touched.setdefault(proposed.path, []).append(f"proposed_file:{proposed.path}")
    for component in rows.components:
        if component.kind == "proposed":
            continue
        for span in component.evidence.spans:
            if span.path:
                touched.setdefault(span.path, []).append(f"component:{component.key}")
    return touched


def _hits(path: str, constraint: Constraint) -> bool:
    """Whether `path` falls inside `constraint.scope`.

    When the detector listed exactly which files it protects (spans, not
    truncated), only those count — this is what lets a design add a new
    migration beside existing ones without tripping the `do_not_touch` entry
    on that directory. Otherwise the scope is matched as a path, directory
    prefix or glob by the ledger's own `path_in_scope`.
    """
    listed = [span.path for span in constraint.evidence.spans]
    if listed and not constraint.evidence.truncated:
        return path in listed
    return path_in_scope(path, constraint.scope)


def check_constraints(
    rows: VersionRows, constraints: Sequence[Constraint]
) -> list[ValidationError]:
    """Ledger entries whose scope a touched path falls inside.

    Hard entries of a `CONFLICTING_CONSTRAINT_KINDS` kind block `validated`;
    soft ones, and any repo-wide scope, are reported as advisories. Pure: no
    I/O, no model.

    What this cannot catch, stated plainly:

    * A ledger entry whose scope is a **component key** or a symbol rather
      than a path. `Constraint.scope` is free text and all five detectors in
      `app.context.ledger` emit paths, directory prefixes, globs or `repo`,
      so matching component keys would be matching a form nothing currently
      writes. A user-stated constraint scoped to a component name will not
      be matched.
    * A path a design would touch but never **names**. An `existing`
      component mapped to a cluster whose members include a protected file
      only conflicts if that file appears in the component's evidence spans;
      the cluster's full membership is not expanded here. A design that
      edits a protected file without listing it is invisible to this check.
    * Anything about *how* the work is done — `ci_lint`, `performance`,
      `dependency` and `other` entries are not checked at all, by the
      reasoning on `CONFLICTING_CONSTRAINT_KINDS`.
    """
    touched = touched_paths(rows)
    if not touched:
        return []
    out: list[ValidationError] = []
    for constraint in constraints:
        if constraint.kind not in CONFLICTING_CONSTRAINT_KINDS:
            continue
        scope = constraint.scope.strip()
        repo_wide = scope in _REPO_WIDE
        for path in sorted(touched):
            if not _hits(path, constraint):
                continue
            entity = sorted(touched[path])[0]
            kind, _, key = entity.partition(":")
            reference = f"constraint {constraint.id}" if constraint.id else "an unsaved constraint"
            status = "confirmed" if constraint.confirmed else f"{constraint.source}, unconfirmed"
            if repo_wide:
                code = REPO_WIDE_CONSTRAINT_OVERLAP
                detail = (
                    "its scope is the whole repository, so the overlap says nothing specific "
                    "about this design and does not block validation"
                )
            elif constraint.severity == "hard":
                code = HARD_CONSTRAINT_CONFLICT
                detail = "the entry is hard, so this blocks validation"
            else:
                code = SOFT_CONSTRAINT_CONFLICT
                detail = "the entry is soft (a stated tradeoff), so this is reported, not blocking"
            out.append(
                _err(
                    code,
                    f"{path!r} falls under the {constraint.kind} constraint on "
                    f"{constraint.scope!r} ({constraint.severity}, {status}, {reference}); "
                    f"{detail}",
                    entity_kind=kind,  # pyright: ignore[reportArgumentType]
                    entity_key=key,
                )
            )
            break  # one report per constraint: the rest of its paths say nothing new
    return out


# --- the whole thing --------------------------------------------------------


def validate_rows(
    conn: psycopg.Connection,
    rows: VersionRows,
    constraints: Sequence[Constraint] | None = None,
) -> ValidationOutcome:
    """Run every check over `rows` and decide the version's status. Read-only;
    nothing is persisted (see `validate_and_record`).

    `constraints` defaults to the blueprint's task ledger plus the repo-level
    detections at the blueprint's snapshot — the same set
    `app.context.extract` checks requirements against.

    The status: `validated` when nothing at all was found, `invalid` when
    something blocking was, and `draft` when only advisories were. `draft`
    rather than `validated` for the advisory case because
    `BlueprintVersion` forbids a `validated` row from carrying errors, and
    dropping the advisories in order to stamp it `validated` would lose the
    flagged conflict Part 4.G.5 asks for. A soft conflict therefore does not
    invalidate a design; it only withholds the clean stamp.
    """
    if constraints is None:
        constraints = list_constraints(
            conn,
            rows.blueprint.repo_id,
            rows.blueprint.commit_sha,
            task_id=rows.blueprint.task_id,
        )
    errors = [
        *check_structure(rows),
        *check_grounding(conn, rows),
        *check_constraints(rows, constraints),
    ]
    errors.sort(key=lambda e: (e.code not in BLOCKING_CODES, e.code, e.entity_key or ""))
    status: BlueprintVersionStatus
    if any(e.code in BLOCKING_CODES for e in errors):
        status = "invalid"
    elif errors:
        status = "draft"
    else:
        status = "validated"
    return ValidationOutcome(status=status, errors=errors)


def load_version_rows(conn: psycopg.Connection, version_id: str) -> VersionRows:
    """Read one version and its blueprint. Raises `LookupError` if either is
    gone — that is a caller bug, not a validation failure."""
    version = get_version(conn, version_id)
    if version is None:
        raise LookupError(f"blueprint version {version_id} not found")
    blueprint = get_blueprint(conn, version.blueprint_id)
    if blueprint is None:
        raise LookupError(f"blueprint {version.blueprint_id} not found")
    return VersionRows(
        blueprint=blueprint,
        version=version,
        components=list_components(conn, version_id),
        connections=list_connections(conn, version_id),
        proposed_files=list_proposed_files(conn, version_id),
    )


def validate_and_record(
    conn: psycopg.Connection,
    version_id: str,
    constraints: Sequence[Constraint] | None = None,
) -> ValidationOutcome:
    """Validate a stored version and write the verdict onto it. Needs
    `worker_connection`.

    Only `status` and `validation_errors` are written: validating a design
    does not change what it proposes, so this is the one in-place update the
    version row allows (`set_version_validation`). An `approved` version is
    re-validated but not demoted — approval is a human act, and silently
    unapproving it because the snapshot moved would hide the change rather
    than surface it; the outcome is returned for the caller to act on.
    """
    rows = load_version_rows(conn, version_id)
    outcome = validate_rows(conn, rows, constraints)
    if rows.version.status != "approved":
        set_version_validation(
            conn, version_id, status=outcome.status, validation_errors=outcome.errors
        )
    return outcome
