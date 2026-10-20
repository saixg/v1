"""The blueprint engine end to end against the live database: generation into
a real snapshot, the grounding checks the validator can only make with a
snapshot, partial regeneration, and export into a context package.

No model is called: the provider is scripted (`fakes.py`) and the repository
is the Task C fixture repo, scanned by the real scan pass. Skipped when
`DATABASE_URL` / `WORKER_DATABASE_URL` are unset.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from app.blueprint import (
    VersionRows,
    build_document,
    export_blueprint_context,
    generate_blueprint,
    load_repo_slice,
    load_version_rows,
    regenerate_blueprint,
    validate_and_record,
    validate_rows,
)
from app.blueprint.export import InvalidBlueprintError
from app.blueprint.regenerate import LOCKED_COMPONENT_IN_SCOPE
from app.blueprint.validate import (
    CHANGED_FILE_PATH_MISSING,
    LINKED_COMPONENT_MISSING,
    LINKED_COMPONENT_NOT_CLUSTER,
    NEW_FILE_PATH_COLLISION,
)
from app.knowledge import (
    create_blueprint,
    create_version,
    get_blueprint,
    insert_components,
    insert_proposed_files,
    list_components,
    list_nodes,
    next_version_number,
    set_version_validation,
    update_component,
    worker_connection,
)
from app.models import (
    Blueprint,
    BlueprintComponent,
    BlueprintVersion,
    ContextEvidence,
    ProposedFile,
)


def _load(name: str, path: Path):
    """Load a test helper by path, the way `tests/context/test_package.py`
    already does: `tests/` is not a package and pytest runs with
    `--import-mode=importlib`."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


fakes = _load("blueprint_fakes", Path(__file__).with_name("fakes.py"))
fake_service, reply = fakes.fake_service, fakes.reply

# The Task C fixture repo: a real tree, scanned by the real scan pass. Reused
# rather than re-invented so blueprint tests see the same graph the context
# tests do.
fx = _load(
    "taskc_fixture_repo",
    Path(__file__).parent.parent / "context" / "taskc_fixture_repo.py",
)
ScannedRepo = fx.ScannedRepo
create_scanned_repo = fx.create_scanned_repo
delete_scanned_repo = fx.delete_scanned_repo
new_task = fx.new_task

pytestmark = fx.needs_db


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ScannedRepo]:
    repo = create_scanned_repo(tmp_path_factory.mktemp("blueprint-repo"))
    try:
        yield repo
    finally:
        _delete_blueprints(repo.repo_id)
        delete_scanned_repo(repo)


def _delete_blueprints(repo_id: str) -> None:
    import os

    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10) as conn:
        conn.execute(
            "update codeaxis.blueprints set current_version_id = null where repo_id = %s",
            (repo_id,),
        )
        conn.execute("delete from codeaxis.blueprints where repo_id = %s", (repo_id,))
        conn.commit()


@pytest.fixture
def made(scanned: ScannedRepo) -> Blueprint:
    task = new_task(scanned, "Add a CSV export of placed orders.")
    with worker_connection() as conn:
        blueprint = create_blueprint(
            conn,
            Blueprint(
                project_id=scanned.project_id,
                repo_id=scanned.repo_id,
                commit_sha=scanned.commit_sha,
                task_id=task.id,  # pyright: ignore[reportArgumentType]
                title="CSV export",
                created_by=scanned.user_id,
            ),
        )
    return blueprint


def _first_label(scanned: ScannedRepo) -> str:
    with worker_connection() as conn:
        slice_ = load_repo_slice(conn, scanned.repo_id, scanned.commit_sha)
    assert slice_.candidates, "the fixture repo must produce at least one component cluster"
    return slice_.candidates[0].label


def _design(label: str) -> dict:
    return {
        "components": [
            {
                "key": "order-store",
                "name": "Order store",
                "mapping": "extend",
                "candidate": label,
                "responsibility": "Reads placed orders for export.",
                "interfaces": [{"name": "list_orders", "kind": "function"}],
            },
            {
                "key": "csv-writer",
                "name": "CSV writer",
                "mapping": "new",
                "candidate": None,
                "responsibility": "Serializes orders to CSV.",
            },
        ],
        "connections": [
            {
                "key": "writer-needs-store",
                "kind": "dependency",
                "source_key": "csv-writer",
                "target_key": "order-store",
                "description": "the writer reads from the store",
            }
        ],
        "unresolved": [],
    }


_FILES = {
    "files": [
        {
            "component_key": "csv-writer",
            "directory": "D0",
            "subdirectory": "svc",
            "name": "export_csv.py",
            "purpose": "Writes the CSV export.",
            "conventions": [],
        }
    ]
}


# --- generation ---------------------------------------------------------------


def test_generation_grounds_every_mapped_component_in_the_snapshot(
    scanned: ScannedRepo, made: Blueprint
):
    label = _first_label(scanned)
    service, _provider, calls = fake_service([reply(_design(label)), reply(_FILES)])
    assert made.id is not None
    with worker_connection() as conn:
        result = generate_blueprint(conn, service, made.id)
        clusters = {n.id for n in list_nodes(conn, scanned.repo_id, scanned.commit_sha,
                                             kind="cluster")}

    assert result.validation.status == "validated", result.validation.errors
    assert result.promoted is True
    assert result.version.version == 1
    mapped = next(c for c in result.components if c.key == "order-store")
    assert mapped.kind == "modified" and mapped.mapping == "extend"
    assert mapped.linked_component_id in clusters
    assert mapped.evidence.spans, "a mapped component must carry evidence"
    proposed = next(c for c in result.components if c.key == "csv-writer")
    assert proposed.linked_component_id is None
    assert [f.path for f in result.proposed_files] == ["svc/export_csv.py"]
    assert [f.status for f in result.proposed_files] == ["new"]
    # One `design`-role call per pass, both recorded.
    assert [c.role for c in calls] == ["design", "design"]


def test_the_document_assembles_what_was_stored(scanned: ScannedRepo, made: Blueprint):
    label = _first_label(scanned)
    service, _p, _c = fake_service([reply(_design(label)), reply(_FILES)])
    assert made.id is not None
    with worker_connection() as conn:
        result = generate_blueprint(conn, service, made.id)
        assert result.version.id is not None
        document = build_document(conn, result.version.id)

    assert document.schema_version == "1.0.0"
    assert {c.key for c in document.components} == {"order-store", "csv-writer"}
    assert [c.key for c in document.connections] == ["writer-needs-store"]
    assert document.data_flows == []  # the only connection is a dependency
    assert document.requirement_ids == [] or all(document.requirement_ids)
    assert document.constraint_ids, "the repo-level ledger should have been detected"


# --- the grounding checks the validator can only make against a snapshot -----


def test_a_link_to_a_node_outside_this_snapshot_is_rejected_with_a_reason(
    scanned: ScannedRepo, made: Blueprint
):
    """The column's foreign key already guarantees the node exists somewhere,
    so the rows here are constructed rather than inserted: what the validator
    adds over the FK is "and at *this* snapshot", which cannot be expressed as
    a constraint. Checked through the real SQL on the real snapshot."""
    assert made.id is not None
    with worker_connection() as conn:
        version = create_version(
            conn,
            BlueprintVersion(
                blueprint_id=made.id,
                version=next_version_number(conn, made.id),
                analyzer="test",
                analyzer_version="0",
            ),
        )
        assert version.id is not None
        blueprint = get_blueprint(conn, made.id)
        assert blueprint is not None
        rows = VersionRows(
            blueprint=blueprint,
            version=version,
            components=[
                BlueprintComponent(
                    key="ghost",
                    name="Ghost",
                    kind="existing",
                    mapping="reuse",
                    linked_component_id=999_999_999,
                    repo_id=scanned.repo_id,
                    commit_sha=scanned.commit_sha,
                    evidence=ContextEvidence(node_ids=[999_999_999]),
                )
            ],
        )
        outcome = validate_rows(conn, rows)
        # The verdict is still recordable on the real version row.
        set_version_validation(
            conn, version.id, status=outcome.status, validation_errors=outcome.errors
        )
        stored = load_version_rows(conn, version.id)

    assert [e.code for e in outcome.errors] == [LINKED_COMPONENT_MISSING]
    assert outcome.status == "invalid"
    assert stored.version.status == "invalid"
    assert [e.code for e in stored.version.validation_errors] == [LINKED_COMPONENT_MISSING]


def test_a_link_to_a_file_node_is_not_accepted_as_a_component_link(
    scanned: ScannedRepo, made: Blueprint
):
    assert made.id is not None
    with worker_connection() as conn:
        files = list_nodes(conn, scanned.repo_id, scanned.commit_sha, kind="file")
        target = next(n for n in files if n.path == "svc/orders.py")
        assert target.id is not None
        version = create_version(
            conn,
            BlueprintVersion(
                blueprint_id=made.id,
                version=next_version_number(conn, made.id),
                analyzer="test",
                analyzer_version="0",
            ),
        )
        assert version.id is not None
        insert_components(
            conn,
            version.id,
            [
                BlueprintComponent(
                    key="file-link",
                    name="File link",
                    kind="existing",
                    mapping="reuse",
                    linked_component_id=target.id,
                    repo_id=scanned.repo_id,
                    commit_sha=scanned.commit_sha,
                    evidence=ContextEvidence(node_ids=[target.id]),
                )
            ],
        )
        outcome = validate_and_record(conn, version.id)

    assert [e.code for e in outcome.errors] == [LINKED_COMPONENT_NOT_CLUSTER]


def test_file_statuses_are_checked_against_real_paths(scanned: ScannedRepo, made: Blueprint):
    assert made.id is not None
    with worker_connection() as conn:
        version = create_version(
            conn,
            BlueprintVersion(
                blueprint_id=made.id,
                version=next_version_number(conn, made.id),
                analyzer="test",
                analyzer_version="0",
            ),
        )
        assert version.id is not None
        insert_proposed_files(
            conn,
            version.id,
            [
                # svc/orders.py exists in the fixture repo, so 'new' collides.
                ProposedFile(path="svc/orders.py", status="new", purpose="p"),
                # This one does not, so 'modified' has nothing to modify.
                ProposedFile(path="svc/nowhere.py", status="modified", purpose="p"),
            ],
        )
        outcome = validate_and_record(conn, version.id)

    # Subset, not equality: this version has files but no components, which is
    # separately invalid (EMPTY_BLUEPRINT). What is under test here is that
    # both file statuses were checked against real paths in the snapshot.
    found = {e.code for e in outcome.errors}
    assert {NEW_FILE_PATH_COLLISION, CHANGED_FILE_PATH_MISSING} <= found


# --- partial regeneration -----------------------------------------------------


def test_regeneration_preserves_a_locked_component_by_refusing(
    scanned: ScannedRepo, made: Blueprint
):
    label = _first_label(scanned)
    service, _p, _c = fake_service([reply(_design(label)), reply(_FILES)])
    assert made.id is not None
    with worker_connection() as conn:
        first = generate_blueprint(conn, service, made.id)
        assert first.version.id is not None
        stored = list_components(conn, first.version.id)
        target = next(c for c in stored if c.key == "csv-writer")
        assert target.id is not None
        update_component(conn, target.id, {"generation_status": "locked", "locked": True})

        boom, _bp, _bc = fake_service([])  # must not be called
        result = regenerate_blueprint(conn, boom, made.id, ["csv-writer"])
        after = get_blueprint(conn, made.id)

    assert [e.code for e in result.rejected] == [LOCKED_COMPONENT_IN_SCOPE]
    assert result.version is None
    assert after is not None and after.current_version_id == first.version.id


def test_regeneration_creates_a_new_version_and_keeps_what_was_out_of_scope(
    scanned: ScannedRepo, made: Blueprint
):
    label = _first_label(scanned)
    service, _p, _c = fake_service(
        [
            reply(_design(label)),
            reply(_FILES),
            # The regeneration redesigns csv-writer (and its one-hop
            # neighbour order-store, which the connection joins to it).
            reply(
                {
                    "components": [
                        {
                            "key": "csv-writer",
                            "name": "CSV writer v2",
                            "mapping": "new",
                            "candidate": None,
                            "responsibility": "Streams orders to CSV.",
                        },
                        {
                            "key": "order-store",
                            "name": "Order store",
                            "mapping": "extend",
                            "candidate": label,
                            "responsibility": "Reads placed orders for export.",
                        },
                    ],
                    "connections": [],
                    "unresolved": [],
                }
            ),
            reply(_FILES),
        ]
    )
    assert made.id is not None
    with worker_connection() as conn:
        first = generate_blueprint(conn, service, made.id)
        result = regenerate_blueprint(conn, service, made.id, ["csv-writer"])
        after = get_blueprint(conn, made.id)

    assert result.rejected == []
    assert result.version is not None
    assert result.version.version == first.version.version + 1
    assert result.neighborhood is not None
    assert result.neighborhood.regenerate == {"csv-writer", "order-store"}
    rewritten = next(c for c in result.components if c.key == "csv-writer")
    assert rewritten.name == "CSV writer v2"
    assert result.validation is not None and result.validation.status == "validated"
    assert after is not None and after.current_version_id == result.version.id


# --- export -------------------------------------------------------------------


def test_export_folds_per_component_context_into_a_real_context_package(
    scanned: ScannedRepo, made: Blueprint
):
    label = _first_label(scanned)
    service, _p, _c = fake_service([reply(_design(label)), reply(_FILES)])
    assert made.id is not None
    with worker_connection() as conn:
        result = generate_blueprint(conn, service, made.id)
        assert result.version.id is not None
        assembled = export_blueprint_context(conn, result.version.id, token_budget=20000)

    architecture = [i for i in assembled.items if i.kind == "architecture"]
    content = "\n".join(i.content for i in architecture)
    assert "Component csv-writer" in content
    assert "svc/export_csv.py" in content
    assert "Verification:" in content
    assert any(i.kind == "limitation" and "has been built" in i.content for i in assembled.items)
    assert assembled.package.commit_sha == scanned.commit_sha
    # Constraints are still the never-truncated head of the package.
    sections = {s["kind"]: s for s in assembled.package.manifest["sections"]}
    assert sections["constraint"]["truncatable"] is False


def test_an_invalid_version_is_not_exportable_as_implementation_context(
    scanned: ScannedRepo, made: Blueprint
):
    assert made.id is not None
    with worker_connection() as conn:
        version = create_version(
            conn,
            BlueprintVersion(
                blueprint_id=made.id,
                version=next_version_number(conn, made.id),
                analyzer="test",
                analyzer_version="0",
            ),
        )
        assert version.id is not None
        insert_proposed_files(
            conn,
            version.id,
            [ProposedFile(path="svc/orders.py", status="new", purpose="p")],
        )
        validate_and_record(conn, version.id)
        with pytest.raises(InvalidBlueprintError):
            export_blueprint_context(conn, version.id)
        # ... unless the caller says so explicitly, and then it is labelled.
        assembled = export_blueprint_context(conn, version.id, allow_invalid=True)

    assert any(
        i.kind == "limitation" and NEW_FILE_PATH_COLLISION in i.content for i in assembled.items
    )
