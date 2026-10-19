"""Virtual Development Environment tables: blueprints, their versions, the
components/connections/proposed files that make up a design, and the layout
that positions them (build-plan.md Part 4.G, Part 6 "Virtual env").

Same rules as the rest of `app.knowledge`: this package is the only place
that knows SQL, every caller-supplied value is a bound `%s` parameter, and
dynamic WHERE fragments come from a fixed internal whitelist. Reads work on
the read-only `api_connection`; every write needs `worker_connection`.

Three things are load-bearing here:

* **A version is never mutated.** `create_version` writes a new row and
  `set_current_version` repoints the blueprint at it. An edit, a partial
  regeneration and a validation outcome are all new versions, so an exported
  context package can always be traced to the exact design it came from
  (Part 4.G.9) — the same discipline `context_packages` already follows.
* **Components, connections and proposed files reference each other by
  `key`, not by row id.** A new version re-inserts rows with new ids, and
  Part 4.G.9 requires untouched parts of a design to survive a partial
  regeneration unchanged. The per-version unique index on `key` plus
  `app.blueprint.validate` are what enforce those references; Postgres
  cannot express them as foreign keys (see the migration's header).
* **Nothing here generates or validates anything.** These functions persist
  and return what the generation and validation passes produced. A missing
  row is an honest "not proposed", never a cue to invent one (Part 0.3).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import psycopg
from psycopg.rows import class_row

from app.models import (
    Blueprint,
    BlueprintComponent,
    BlueprintConnection,
    BlueprintLayout,
    BlueprintVersion,
    ProposedFile,
    ValidationError,
)

MAX_LIST_LIMIT = 500

_BLUEPRINT_COLUMNS = """
    id, project_id, repo_id, commit_sha, task_id, title,
    current_version_id, created_by, created_at, updated_at
"""

_VERSION_COLUMNS = """
    id, blueprint_id, version, schema_version, status, validation_errors,
    origin_reason, analyzer, analyzer_version, created_by, created_at
"""

_COMPONENT_COLUMNS = """
    id, blueprint_version_id, key, name, kind, mapping, linked_component_id,
    repo_id, commit_sha, module_key, responsibility, interfaces,
    confidence::float8 as confidence, evidence, generation_status, locked, created_at
"""

_CONNECTION_COLUMNS = """
    id, blueprint_version_id, key, kind, source_key, target_key,
    semantics, data_shape, locked, created_at
"""

_PROPOSED_FILE_COLUMNS = """
    id, blueprint_version_id, path, status, purpose,
    implements_component_key, conventions_refs, locked, created_at
"""


def _capped(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


# --- blueprints -------------------------------------------------------------


def create_blueprint(conn: psycopg.Connection, blueprint: Blueprint) -> Blueprint:
    """Create the design effort itself. `current_version_id` stays null until
    the first version exists — a blueprint with no version is a real, if
    short-lived, state, and pretending otherwise would mean writing a version
    before there is anything to put in it."""
    with conn.cursor(row_factory=class_row(Blueprint)) as cur:
        row = cur.execute(
            f"""
            insert into codeaxis.blueprints
              (project_id, repo_id, commit_sha, task_id, title, created_by)
            values (%s, %s, %s, %s, %s, %s)
            returning {_BLUEPRINT_COLUMNS}
            """,
            (
                blueprint.project_id,
                blueprint.repo_id,
                blueprint.commit_sha,
                blueprint.task_id,
                blueprint.title,
                blueprint.created_by,
            ),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


def get_blueprint(conn: psycopg.Connection, blueprint_id: str) -> Blueprint | None:
    with conn.cursor(row_factory=class_row(Blueprint)) as cur:
        return cur.execute(
            f"select {_BLUEPRINT_COLUMNS} from codeaxis.blueprints where id = %s",
            (blueprint_id,),
        ).fetchone()


def list_blueprints(
    conn: psycopg.Connection,
    *,
    project_id: str | None = None,
    repo_id: str | None = None,
    task_id: str | None = None,
    commit_sha: str | None = None,
    limit: int = 100,
) -> list[Blueprint]:
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (
        ("project_id", project_id),
        ("repo_id", repo_id),
        ("task_id", task_id),
        ("commit_sha", commit_sha),
    ):
        if value is not None:
            clauses.append(f"{column} = %s")
            params.append(value)
    where = f"where {' and '.join(clauses)}" if clauses else ""
    query = (
        f"select {_BLUEPRINT_COLUMNS} from codeaxis.blueprints "
        f"{where} order by created_at desc, id limit %s"
    )
    with conn.cursor(row_factory=class_row(Blueprint)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


def set_blueprint_title(conn: psycopg.Connection, blueprint_id: str, title: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.blueprints set title = %s, updated_at = now() where id = %s",
            (title, blueprint_id),
        )
    conn.commit()


def delete_blueprint(conn: psycopg.Connection, blueprint_id: str) -> None:
    """Remove a design and everything under it. Versions, components,
    connections, proposed files and layouts all cascade from here; the
    `current_version_id` self-reference is cleared first so the cascade has
    no circular row to trip over."""
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.blueprints set current_version_id = null where id = %s",
            (blueprint_id,),
        )
        cur.execute("delete from codeaxis.blueprints where id = %s", (blueprint_id,))
    conn.commit()


# --- versions ---------------------------------------------------------------


def next_version_number(conn: psycopg.Connection, blueprint_id: str) -> int:
    with conn.cursor() as cur:
        row = cur.execute(
            "select coalesce(max(version), 0) + 1 from codeaxis.blueprint_versions "
            "where blueprint_id = %s",
            (blueprint_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def create_version(conn: psycopg.Connection, version: BlueprintVersion) -> BlueprintVersion:
    """Append a version. Does not repoint the blueprint: `set_current_version`
    is a separate step so a caller can write a version, fill it with
    components, validate it, and only then make it the one the UI serves —
    rather than briefly publishing an empty design."""
    with conn.cursor(row_factory=class_row(BlueprintVersion)) as cur:
        row = cur.execute(
            f"""
            insert into codeaxis.blueprint_versions
              (blueprint_id, version, schema_version, status, validation_errors,
               origin_reason, analyzer, analyzer_version, created_by)
            values (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
            returning {_VERSION_COLUMNS}
            """,
            (
                version.blueprint_id,
                version.version,
                version.schema_version,
                version.status,
                json.dumps([e.model_dump(mode="json") for e in version.validation_errors]),
                version.origin_reason,
                version.analyzer,
                version.analyzer_version,
                version.created_by,
            ),
        ).fetchone()
    conn.commit()
    assert row is not None
    return row


def get_version(conn: psycopg.Connection, version_id: str) -> BlueprintVersion | None:
    with conn.cursor(row_factory=class_row(BlueprintVersion)) as cur:
        return cur.execute(
            f"select {_VERSION_COLUMNS} from codeaxis.blueprint_versions where id = %s",
            (version_id,),
        ).fetchone()


def list_versions(
    conn: psycopg.Connection, blueprint_id: str, *, limit: int = 100
) -> list[BlueprintVersion]:
    """Newest first. The history is the audit trail Part 4.G.9 asks for, so
    nothing is pruned here."""
    with conn.cursor(row_factory=class_row(BlueprintVersion)) as cur:
        return cur.execute(  # pyright: ignore[reportArgumentType]
            f"select {_VERSION_COLUMNS} from codeaxis.blueprint_versions "
            "where blueprint_id = %s order by version desc limit %s",
            (blueprint_id, _capped(limit)),
        ).fetchall()


def get_current_version(conn: psycopg.Connection, blueprint_id: str) -> BlueprintVersion | None:
    """The version the blueprint points at — not simply the highest number.
    A newer version can exist while an older one is still current (a failed
    regeneration leaves an `invalid` version behind without promoting it)."""
    with conn.cursor(row_factory=class_row(BlueprintVersion)) as cur:
        return cur.execute(
            f"""
            select {_VERSION_COLUMNS} from codeaxis.blueprint_versions
            where id = (select current_version_id from codeaxis.blueprints where id = %s)
            """,
            (blueprint_id,),
        ).fetchone()


def set_current_version(conn: psycopg.Connection, blueprint_id: str, version_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.blueprints set current_version_id = %s, updated_at = now() "
            "where id = %s",
            (version_id, blueprint_id),
        )
    conn.commit()


def set_version_validation(
    conn: psycopg.Connection,
    version_id: str,
    *,
    status: str,
    validation_errors: Sequence[ValidationError],
) -> None:
    """Record a validation outcome on an existing version.

    The one place a version row is updated in place rather than superseded,
    and deliberately narrow: it writes only the verdict and its reasons, never
    the design. Validating a blueprint does not change what it proposes, so
    forcing a new version here would make the history unreadable — every
    validation run would look like a redesign.
    """
    payload = [e.model_dump(mode="json") for e in validation_errors]
    with conn.cursor() as cur:
        cur.execute(
            "update codeaxis.blueprint_versions set status = %s, validation_errors = %s::jsonb "
            "where id = %s",
            (status, json.dumps(payload), version_id),
        )
    conn.commit()


# --- components -------------------------------------------------------------


def insert_components(
    conn: psycopg.Connection, version_id: str, rows: Sequence[BlueprintComponent]
) -> list[str]:
    """Write a version's components, returning their ids in order."""
    ids: list[str] = []
    with conn.cursor() as cur:
        for row in rows:
            got = cur.execute(
                """
                insert into codeaxis.blueprint_components
                  (blueprint_version_id, key, name, kind, mapping, linked_component_id,
                   repo_id, commit_sha, module_key, responsibility, interfaces,
                   confidence, evidence, generation_status, locked)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)
                returning id
                """,
                (
                    version_id,
                    row.key,
                    row.name,
                    row.kind,
                    row.mapping,
                    row.linked_component_id,
                    row.repo_id,
                    row.commit_sha,
                    row.module_key,
                    row.responsibility,
                    json.dumps([i.model_dump(mode="json") for i in row.interfaces]),
                    row.confidence,
                    row.evidence.model_dump_json(),
                    row.generation_status,
                    row.locked,
                ),
            ).fetchone()
            assert got is not None
            ids.append(str(got[0]))
    conn.commit()
    return ids


def list_components(
    conn: psycopg.Connection, version_id: str, *, limit: int = 500
) -> list[BlueprintComponent]:
    with conn.cursor(row_factory=class_row(BlueprintComponent)) as cur:
        return cur.execute(  # pyright: ignore[reportArgumentType]
            f"select {_COMPONENT_COLUMNS} from codeaxis.blueprint_components "
            "where blueprint_version_id = %s order by module_key nulls first, key limit %s",
            (version_id, _capped(limit)),
        ).fetchall()


def update_component(
    conn: psycopg.Connection, component_id: str, fields: dict[str, object]
) -> BlueprintComponent | None:
    """Edit one component in place. Only the fields a human may change are
    accepted, from a fixed whitelist — a caller cannot move a component to
    another version, repoint its snapshot, or rewrite its evidence (which
    records what was actually read, not what someone would prefer it said).

    Changing anything marks the row `edited`/`locked` via `generation_status`
    so a later regeneration can tell human intent from generated output
    (Part 4.G.9).
    """
    allowed = {
        "name",
        "kind",
        "mapping",
        "linked_component_id",
        "module_key",
        "responsibility",
        "generation_status",
        "locked",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_component(conn, component_id)
    assignments = ", ".join(f"{k} = %s" for k in updates)
    query = (
        f"update codeaxis.blueprint_components set {assignments} "
        f"where id = %s returning {_COMPONENT_COLUMNS}"
    )
    with conn.cursor(row_factory=class_row(BlueprintComponent)) as cur:
        row = cur.execute(query, [*updates.values(), component_id]).fetchone()  # pyright: ignore[reportArgumentType]
    conn.commit()
    return row


def get_component(conn: psycopg.Connection, component_id: str) -> BlueprintComponent | None:
    with conn.cursor(row_factory=class_row(BlueprintComponent)) as cur:
        return cur.execute(
            f"select {_COMPONENT_COLUMNS} from codeaxis.blueprint_components where id = %s",
            (component_id,),
        ).fetchone()


# --- connections ------------------------------------------------------------


def insert_connections(
    conn: psycopg.Connection, version_id: str, rows: Sequence[BlueprintConnection]
) -> list[str]:
    ids: list[str] = []
    with conn.cursor() as cur:
        for row in rows:
            got = cur.execute(
                """
                insert into codeaxis.blueprint_connections
                  (blueprint_version_id, key, kind, source_key, target_key,
                   semantics, data_shape, locked)
                values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                returning id
                """,
                (
                    version_id,
                    row.key,
                    row.kind,
                    row.source_key,
                    row.target_key,
                    json.dumps(row.semantics),
                    json.dumps(row.data_shape) if row.data_shape is not None else None,
                    row.locked,
                ),
            ).fetchone()
            assert got is not None
            ids.append(str(got[0]))
    conn.commit()
    return ids


def list_connections(
    conn: psycopg.Connection, version_id: str, *, kind: str | None = None, limit: int = 500
) -> list[BlueprintConnection]:
    """All of a version's connections, or one kind of them. Part 6's
    `data_flows` is this with `kind='data_flow'` — there is no second
    collection to keep in step."""
    clauses = ["blueprint_version_id = %s"]
    params: list[object] = [version_id]
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    query = (
        f"select {_CONNECTION_COLUMNS} from codeaxis.blueprint_connections "
        f"where {' and '.join(clauses)} order by kind, key limit %s"
    )
    with conn.cursor(row_factory=class_row(BlueprintConnection)) as cur:
        return cur.execute(query, [*params, _capped(limit)]).fetchall()  # pyright: ignore[reportArgumentType]


def delete_connection(conn: psycopg.Connection, connection_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("delete from codeaxis.blueprint_connections where id = %s", (connection_id,))
    conn.commit()


# --- proposed files ---------------------------------------------------------


def insert_proposed_files(
    conn: psycopg.Connection, version_id: str, rows: Sequence[ProposedFile]
) -> list[str]:
    ids: list[str] = []
    with conn.cursor() as cur:
        for row in rows:
            got = cur.execute(
                """
                insert into codeaxis.proposed_files
                  (blueprint_version_id, path, status, purpose,
                   implements_component_key, conventions_refs, locked)
                values (%s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    version_id,
                    row.path,
                    row.status,
                    row.purpose,
                    row.implements_component_key,
                    row.conventions_refs,
                    row.locked,
                ),
            ).fetchone()
            assert got is not None
            ids.append(str(got[0]))
    conn.commit()
    return ids


def list_proposed_files(
    conn: psycopg.Connection, version_id: str, *, limit: int = 500
) -> list[ProposedFile]:
    with conn.cursor(row_factory=class_row(ProposedFile)) as cur:
        return cur.execute(  # pyright: ignore[reportArgumentType]
            f"select {_PROPOSED_FILE_COLUMNS} from codeaxis.proposed_files "
            "where blueprint_version_id = %s order by path limit %s",
            (version_id, _capped(limit)),
        ).fetchall()


# --- layout -----------------------------------------------------------------


def get_layout(conn: psycopg.Connection, version_id: str) -> BlueprintLayout | None:
    with conn.cursor(row_factory=class_row(BlueprintLayout)) as cur:
        return cur.execute(
            "select blueprint_version_id, layout, updated_at "
            "from codeaxis.blueprint_layouts where blueprint_version_id = %s",
            (version_id,),
        ).fetchone()


def set_layout(conn: psycopg.Connection, version_id: str, layout: dict) -> None:
    """Replace a version's layout. Upserted in place, not versioned: moving a
    box changes nothing about what is being built (Part 4.G.6)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into codeaxis.blueprint_layouts (blueprint_version_id, layout)
            values (%s, %s::jsonb)
            on conflict (blueprint_version_id)
              do update set layout = excluded.layout, updated_at = now()
            """,
            (version_id, json.dumps(layout)),
        )
    conn.commit()


# --- assembled read ---------------------------------------------------------


def linked_component_ids(conn: psycopg.Connection, version_id: str) -> list[int]:
    """The `codeaxis.nodes` ids a version's components claim to map to.

    Used by the validator to check in one query that every `existing`/
    `modified` component points at a node that really exists at this
    blueprint's own snapshot — Part 4.G.5's "every existing component maps to
    a real repo entity".
    """
    with conn.cursor() as cur:
        rows = cur.execute(
            "select linked_component_id from codeaxis.blueprint_components "
            "where blueprint_version_id = %s and linked_component_id is not null",
            (version_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def existing_node_ids(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    node_ids: Sequence[int],
    *,
    kind: str | None = None,
) -> set[int]:
    """Which of `node_ids` exist in this snapshot (optionally of one kind).

    Scoped to `(repo_id, commit_sha)` deliberately: a node id that exists at
    another commit is not a valid target for a design grounded at this one,
    and accepting it would be exactly the cross-snapshot mix Part 5 forbids.
    """
    if not node_ids:
        return set()
    clauses = ["repo_id = %s", "commit_sha = %s", "id = any(%s)"]
    params: list[object] = [repo_id, commit_sha, list(node_ids)]
    if kind is not None:
        clauses.append("kind = %s")
        params.append(kind)
    query = f"select id from codeaxis.nodes where {' and '.join(clauses)}"
    with conn.cursor() as cur:
        rows = cur.execute(query, params).fetchall()  # pyright: ignore[reportArgumentType]
    return {int(r[0]) for r in rows}


def existing_paths(
    conn: psycopg.Connection, repo_id: str, commit_sha: str, paths: Sequence[str]
) -> set[str]:
    """Which of `paths` are real files in this snapshot.

    Part 4.G.5 requires a proposed `new` file not to collide with an existing
    path, and a `modified`/`unchanged` one to correspond to a path that does
    exist. Both checks need the same answer, so they share one query.
    """
    if not paths:
        return set()
    with conn.cursor() as cur:
        rows = cur.execute(
            "select path from codeaxis.nodes "
            "where repo_id = %s and commit_sha = %s and kind = 'file' and path = any(%s)",
            (repo_id, commit_sha, list(paths)),
        ).fetchall()
    return {str(r[0]) for r in rows}
