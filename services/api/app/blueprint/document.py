"""Assembling the canonical blueprint JSON (build-plan.md Part 6, "Canonical
blueprint JSON"; `packages/schemas/blueprint.v1.json`).

A read shape, assembled from the tables on every request. Two properties are
deliberate:

* **`data_flows` is not assembled here.** `BlueprintDocument.data_flows` is a
  property over `connections`, so there is exactly one stored edge per data
  flow and the two can never drift.
* **Requirements and constraints are referenced, never copied.** The document
  carries their ids; the context engine owns the rows. Copying a requirement
  into a blueprint would give the design its own divergent idea of what was
  asked for.

`modules` is derived from the components' `module_key` the same way — a
module has no identity beyond the components in it, which is why it is not a
table. It is returned separately from `BlueprintDocument` because that model
has no `modules` field; a route that serves the JSON contract merges it in.

# STUB(CodeAxis): the `unresolved` list assembled here is the *stored*
# unresolved state — the version's advisory validation errors plus the task's
# open decisions and open questions. The generation pass also produces
# per-run entries (a dropped candidate, a capped slice, a question the model
# raised) and `blueprint_versions` has no column for them, so those reach the
# caller in `GenerationResult.unresolved` and are lost after the response. A
# column (or a `blueprint_unresolved` table) is the fix; inventing them at
# read time is not.
"""

from __future__ import annotations

import psycopg

from app.knowledge import (
    get_blueprint,
    get_layout,
    get_version,
    list_components,
    list_connections,
    list_constraints,
    list_decisions,
    list_proposed_files,
    list_requirements,
)
from app.models import BlueprintComponent, BlueprintDocument

from .validate import BLOCKING_CODES

MAX_REFERENCED = 500


def modules_of(components: list[BlueprintComponent]) -> list[dict]:
    """`[{key, name, components: [component key, ...]}]`, grouped by
    `module_key`. Components with no module are left out rather than placed
    in a fabricated default group."""
    grouped: dict[str, list[str]] = {}
    for component in components:
        if component.module_key:
            grouped.setdefault(component.module_key, []).append(component.key)
    return [
        {"key": key, "name": key, "components": sorted(members)}
        for key, members in sorted(grouped.items())
    ]


def build_document(conn: psycopg.Connection, version_id: str) -> BlueprintDocument:
    """One version as the canonical document. Read-only: safe on
    `api_connection`. Raises `LookupError` when the version or its blueprint
    is gone."""
    version = get_version(conn, version_id)
    if version is None:
        raise LookupError(f"blueprint version {version_id} not found")
    blueprint = get_blueprint(conn, version.blueprint_id)
    if blueprint is None:
        raise LookupError(f"blueprint {version.blueprint_id} not found")

    components = list_components(conn, version_id)
    layout = get_layout(conn, version_id)
    requirements = list_requirements(conn, blueprint.task_id, limit=MAX_REFERENCED)
    constraints = list_constraints(
        conn,
        blueprint.repo_id,
        blueprint.commit_sha,
        task_id=blueprint.task_id,
        limit=MAX_REFERENCED,
    )

    unresolved: list[dict] = [
        {
            "kind": "validation_advisory",
            "code": error.code,
            "entity_kind": error.entity_kind,
            "ref": error.entity_key,
            "detail": error.message,
        }
        for error in version.validation_errors
        if error.code not in BLOCKING_CODES
    ]
    unresolved.extend(
        {"kind": "open_decision", "ref": str(d.id), "detail": d.question}
        for d in list_decisions(conn, blueprint.task_id, status="open")
    )
    unresolved.extend(
        {"kind": "open_question", "ref": str(r.id), "detail": r.question or r.statement}
        for r in requirements
        if r.kind == "open_question" or (r.status in ("ambiguous", "unresolved") and r.question)
    )

    return BlueprintDocument(
        schema_version=version.schema_version,
        blueprint=blueprint,
        version=version,
        components=components,
        connections=list_connections(conn, version_id),
        proposed_files=list_proposed_files(conn, version_id),
        layout=layout.layout if layout else {},
        requirement_ids=[str(r.id) for r in requirements if r.id is not None],
        constraint_ids=[str(c.id) for c in constraints if c.id is not None],
        unresolved=unresolved,
    )


def build_current_document(
    conn: psycopg.Connection, blueprint_id: str
) -> BlueprintDocument | None:
    """The blueprint's current version as a document, or None when it has no
    current version — which is a real state, not an error (a blueprint exists
    before its first version does)."""
    blueprint = get_blueprint(conn, blueprint_id)
    if blueprint is None:
        raise LookupError(f"blueprint {blueprint_id} not found")
    if blueprint.current_version_id is None:
        return None
    return build_document(conn, blueprint.current_version_id)
