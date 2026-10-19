"""Virtual Development Environment HTTP routes (build-plan.md Part 8):
authorization on every route, then the behaviour that the routes themselves
own rather than delegate.

Real JWT verification (local keypair), live database, a real scan of the Task
C fixture tree. No model is called anywhere in this file: the routes tested
here are the CRUD/read/edit surface, and the generation routes that do call a
model are covered where that pass lives.

What this file is really checking is the part of the taxonomy the *route*
layer is responsible for: that an edit cannot produce a contradictory
component, that a mapping cannot point at a node from another snapshot, that
a locked component refuses an edit, and that layout is not versioned.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.knowledge import (
    create_blueprint,
    create_version,
    insert_components,
    list_nodes,
    set_current_version,
    worker_connection,
)
from app.models import Blueprint, BlueprintComponent, BlueprintVersion

load_dotenv()


def _load_fixture_module():
    name = "taskc_fixture_repo"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("context") / "taskc_fixture_repo.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


fx = _load_fixture_module()

pytestmark = fx.needs_db

ANALYZER = "blueprint-test"
ANALYZER_VERSION = "0.1.0"


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> Iterator:
    repo = fx.create_scanned_repo(tmp_path_factory.mktemp("bpapi"))
    yield repo
    fx.delete_scanned_repo(repo)


@pytest.fixture
def task(scanned) -> str:
    task = fx.new_task(scanned, "Add a discount code to checkout", title="Discounts")
    assert task.id is not None
    return task.id


@pytest.fixture
def real_node_id(scanned) -> int:
    """A node that genuinely exists at the fixture's snapshot.

    `file` rather than `cluster`: the fixture repo is small enough that Leiden
    clustering legitimately produces no clusters, and a test that depended on
    one existing would be testing the clusterer, not the route.
    """
    with worker_connection() as conn:
        nodes = list_nodes(conn, scanned.repo_id, scanned.commit_sha, kind="file")
    assert nodes, "fixture scan produced no file nodes"
    node_id = nodes[0].id
    assert node_id is not None
    return node_id


@pytest.fixture
def blueprint(client, auth_header, scanned, task) -> str:
    resp = client.post(
        f"/repos/{scanned.repo_id}/blueprints",
        json={"task_id": task, "title": "Checkout discounts"},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["commit_sha"] == scanned.commit_sha
    return body["id"]


def _seed_version(blueprint_id: str, scanned, real_node_id: int) -> tuple[str, str]:
    """One version with two components: a linked `existing` one and an
    unlinked `proposed` one. Returns (version_id, existing_component_id)."""
    with worker_connection() as conn:
        version = create_version(
            conn,
            BlueprintVersion(
                blueprint_id=blueprint_id,
                version=1,
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
            ),
        )
        assert version.id is not None
        ids = insert_components(
            conn,
            version.id,
            [
                BlueprintComponent(
                    key="checkout",
                    name="Checkout",
                    kind="existing",
                    mapping="reuse",
                    linked_component_id=real_node_id,
                    repo_id=scanned.repo_id,
                    commit_sha=scanned.commit_sha,
                    module_key="svc",
                ),
                BlueprintComponent(
                    key="discounts",
                    name="Discounts",
                    kind="proposed",
                    mapping="new",
                    repo_id=scanned.repo_id,
                    commit_sha=scanned.commit_sha,
                    module_key="svc",
                ),
            ],
        )
        set_current_version(conn, blueprint_id, version.id)
    return version.id, ids[0]


# --------------------------------------------------------------- authorization


def test_every_blueprint_route_requires_a_token(client, scanned):
    assert client.get(f"/repos/{scanned.repo_id}/blueprints").status_code == 401
    assert client.post(f"/repos/{scanned.repo_id}/blueprints", json={}).status_code == 401
    unknown = uuid.uuid4()
    assert client.get(f"/blueprints/{unknown}").status_code == 401
    assert client.get(f"/blueprints/{unknown}/versions").status_code == 401
    assert client.get(f"/blueprints/{unknown}/layout").status_code == 401


def test_non_member_cannot_reach_a_blueprint(client, auth_header, make_user, blueprint):
    outsider = make_user()
    resp = client.get(f"/blueprints/{blueprint}", headers=auth_header(outsider))
    assert resp.status_code == 403
    assert "not a member" in resp.json()["detail"]


def test_unknown_blueprint_is_404_not_403(client, auth_header, scanned):
    resp = client.get(f"/blueprints/{uuid.uuid4()}", headers=auth_header(scanned.user_id))
    assert resp.status_code == 404


# ----------------------------------------------------------------- lifecycle


def test_create_rejects_a_task_from_another_repo(client, auth_header, scanned):
    resp = client.post(
        f"/repos/{scanned.repo_id}/blueprints",
        json={"task_id": str(uuid.uuid4()), "title": "x"},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 404
    assert "task not found" in resp.json()["detail"]


def test_create_rejects_an_unscanned_commit(client, auth_header, scanned, task):
    resp = client.post(
        f"/repos/{scanned.repo_id}/blueprints",
        json={"task_id": task, "title": "x", "commit_sha": "never-scanned"},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 404
    assert "no completed scan" in resp.json()["detail"]


def test_blueprint_with_no_version_is_404_with_a_reason(client, auth_header, scanned, blueprint):
    """A design that has never been generated is not an empty design. The
    route says so rather than returning a blank canvas."""
    resp = client.get(f"/blueprints/{blueprint}", headers=auth_header(scanned.user_id))
    assert resp.status_code == 404
    assert "no version yet" in resp.json()["detail"]


def test_document_assembles_with_derived_data_flows_and_modules(
    client, auth_header, scanned, blueprint, real_node_id
):
    _seed_version(blueprint, scanned, real_node_id)
    resp = client.get(f"/blueprints/{blueprint}", headers=auth_header(scanned.user_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["schema_version"] == "1.0.0"
    assert {c["key"] for c in body["components"]} == {"checkout", "discounts"}
    # Derived views, not stored collections.
    assert body["data_flows"] == []
    assert body["modules"] == [
        {"key": "svc", "name": "svc", "components": ["checkout", "discounts"]}
    ]
    # Requirements and constraints are referenced, never copied in.
    assert "requirement_ids" in body and "constraint_ids" in body
    assert "proposal_notice" in body and "existing" in body["proposal_notice"]
    assert "Nothing was observed by running" in body["notice"]


def test_rename_is_the_only_blueprint_edit(client, auth_header, scanned, blueprint):
    resp = client.patch(
        f"/blueprints/{blueprint}",
        json={"title": "Renamed"},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Renamed"

    blank = client.patch(
        f"/blueprints/{blueprint}",
        json={"title": "   "},
        headers=auth_header(scanned.user_id),
    )
    assert blank.status_code == 422


# ----------------------------------------------------------- component edits


def test_edit_refuses_a_contradictory_kind_mapping_pair(
    client, auth_header, scanned, blueprint, real_node_id
):
    _version, component_id = _seed_version(blueprint, scanned, real_node_id)
    resp = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"kind": "proposed", "mapping": "reuse"},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 422
    assert "pairs with mapping" in resp.json()["detail"]


def test_kind_change_derives_the_mapping_and_clears_a_stale_link(
    client, auth_header, scanned, blueprint, real_node_id
):
    """Naming only `kind` is enough: the pair is fixed, so the route derives
    the other half instead of making the client restate it."""
    _version, component_id = _seed_version(blueprint, scanned, real_node_id)
    resp = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"kind": "proposed"},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "proposed"
    assert body["mapping"] == "new"
    # A component that no longer claims to exist must not keep a link to
    # something that does.
    assert body["linked_component_id"] is None
    assert body["generation_status"] == "edited"


def test_becoming_existing_requires_a_real_node_at_this_snapshot(
    client, auth_header, scanned, blueprint, real_node_id
):
    _version, component_id = _seed_version(blueprint, scanned, real_node_id)
    # First make it proposed so there is no link to inherit.
    client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"kind": "proposed"},
        headers=auth_header(scanned.user_id),
    )
    missing_link = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"kind": "existing"},
        headers=auth_header(scanned.user_id),
    )
    assert missing_link.status_code == 422
    assert "linked_component_id" in missing_link.json()["detail"]

    bogus_node = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"kind": "existing", "linked_component_id": 2_000_000_000},
        headers=auth_header(scanned.user_id),
    )
    assert bogus_node.status_code == 422
    assert "does not exist at this blueprint's snapshot" in bogus_node.json()["detail"]

    real = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"kind": "existing", "linked_component_id": real_node_id},
        headers=auth_header(scanned.user_id),
    )
    assert real.status_code == 200, real.text
    assert real.json()["linked_component_id"] == real_node_id


def test_locked_component_refuses_an_edit_until_unlocked(
    client, auth_header, scanned, blueprint, real_node_id
):
    """`locked` is a hard block, not a warning: a human approved this, so an
    edit is refused with the way out named rather than quietly applied."""
    _version, component_id = _seed_version(blueprint, scanned, real_node_id)
    headers = auth_header(scanned.user_id)

    locked = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"locked": True},
        headers=headers,
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["locked"] is True
    assert locked.json()["generation_status"] == "locked"

    refused = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"name": "Renamed while locked"},
        headers=headers,
    )
    assert refused.status_code == 409
    assert "locked" in refused.json()["detail"]

    unlocked = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"locked": False},
        headers=headers,
    )
    assert unlocked.status_code == 200
    assert unlocked.json()["locked"] is False

    renamed = client.patch(
        f"/blueprints/{blueprint}/components/{component_id}",
        json={"name": "Renamed after unlock"},
        headers=headers,
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed after unlock"


def test_component_of_another_blueprint_is_404(
    client, auth_header, scanned, blueprint, task, real_node_id
):
    _version, component_id = _seed_version(blueprint, scanned, real_node_id)
    with worker_connection() as conn:
        other = create_blueprint(
            conn,
            Blueprint(
                project_id=scanned.project_id,
                repo_id=scanned.repo_id,
                commit_sha=scanned.commit_sha,
                task_id=task,
                title="Other",
            ),
        )
    assert other.id is not None
    resp = client.patch(
        f"/blueprints/{other.id}/components/{component_id}",
        json={"name": "x"},
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------- layout


def test_layout_round_trips_without_creating_a_version(
    client, auth_header, scanned, blueprint, real_node_id
):
    """Part 4.G.6: arranging the diagram changes nothing about what is being
    built, so it must not produce a new version of the design."""
    _seed_version(blueprint, scanned, real_node_id)
    headers = auth_header(scanned.user_id)

    before = client.get(f"/blueprints/{blueprint}/versions", headers=headers).json()
    layout = {"nodes": [{"key": "checkout", "x": 10, "y": 20}], "viewport": {"zoom": 1.5}}

    put = client.put(f"/blueprints/{blueprint}/layout", json={"layout": layout}, headers=headers)
    assert put.status_code == 204

    got = client.get(f"/blueprints/{blueprint}/layout", headers=headers)
    assert got.status_code == 200
    assert got.json()["layout"] == layout

    after = client.get(f"/blueprints/{blueprint}/versions", headers=headers).json()
    assert len(after) == len(before)

    # And it reaches the assembled document.
    document = client.get(f"/blueprints/{blueprint}", headers=headers).json()
    assert document["layout"] == layout


# ------------------------------------------------------------------- versions


def test_versions_list_marks_the_current_one(
    client, auth_header, scanned, blueprint, real_node_id
):
    version_id, _component = _seed_version(blueprint, scanned, real_node_id)
    resp = client.get(f"/blueprints/{blueprint}/versions", headers=auth_header(scanned.user_id))
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == version_id
    assert rows[0]["is_current"] is True
    assert rows[0]["status"] == "draft"


def test_version_of_another_blueprint_is_404(client, auth_header, scanned, blueprint):
    resp = client.get(
        f"/blueprints/{blueprint}/versions/{uuid.uuid4()}",
        headers=auth_header(scanned.user_id),
    )
    assert resp.status_code == 404


# ----------------------------------------------------------------- task list


def test_task_list_is_scoped_to_the_repo(client, auth_header, scanned, task):
    resp = client.get(f"/repos/{scanned.repo_id}/tasks", headers=auth_header(scanned.user_id))
    assert resp.status_code == 200, resp.text
    ids = {t["id"] for t in resp.json()}
    assert task in ids
    assert all(t["repo_id"] == scanned.repo_id for t in resp.json())


def test_schema_version_is_served(client, auth_header, scanned):
    resp = client.get("/blueprints/schema-version", headers=auth_header(scanned.user_id))
    assert resp.status_code == 200
    assert resp.json() == {"schema_version": "1.0.0"}
