"""Phase 6 failure mode: unauthorized access.

`tests/test_auth.py` sweeps every route for a missing or invalid *token*. This
file covers the other half: a valid token held by someone who is not allowed.
It is driven by the app's own route table, not a hand-kept list, so a route
added to any router tomorrow is covered the day it lands. For every route that
addresses a repo, task or blueprint:

* a signed-in user outside the project is refused with 403, before any of the
  resource's data is read (so the status code does not reveal whether the id
  exists in some other tenant's project);
* a signed-in user the database has never seen is refused the same way;
* and a member of a *different* project gets no more than a stranger does.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.main import app

load_dotenv()


def _load_fixture_module():
    name = "taskc_fixture_repo"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent.parent / "context" / "taskc_fixture_repo.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fx = _load_fixture_module()

pytestmark = fx.needs_db

# Param names that identify something owned by a project. Every other path
# parameter (a version, a component, a token...) is a child of one of these.
OWNED = ("repo_id", "task_id", "blueprint_id")
PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")
# Routes that legitimately take no project: they create or list one's own.
NOT_PROJECT_SCOPED_PREFIXES = ("/mcp/",)  # per-repo API-token auth; see test_mcp.py


def _owned_routes() -> list[tuple[str, str]]:
    # Same traversal as test_auth.py; kept local so this file stands alone.
    from fastapi.routing import APIRoute

    def walk(routes, prefix=""):
        for r in routes:
            if isinstance(r, APIRoute):
                yield prefix + r.path, r
            elif getattr(r, "original_router", None) is not None:
                mounted = getattr(getattr(r, "include_context", None), "prefix", "") or ""
                yield from walk(r.original_router.routes, prefix + mounted)

    found = set()
    for template, route in walk(app.routes):
        if template.startswith(NOT_PROJECT_SCOPED_PREFIXES):
            continue
        if not any(p in OWNED for p in PARAM.findall(template)):
            continue
        for method in route.methods or ():
            found.add((method, template))
    return sorted(found)


OWNED_ROUTES = _owned_routes()


@pytest.fixture(scope="module")
def scanned(tmp_path_factory: pytest.TempPathFactory) -> Iterator:
    repo = fx.create_scanned_repo(tmp_path_factory.mktemp("authz"))
    yield repo
    fx.delete_scanned_repo(repo)


@pytest.fixture(scope="module")
def ids(scanned) -> dict[str, str]:
    """One real task and blueprint, so the 403 is provably about membership and
    not about the id being unknown."""
    task = fx.new_task(scanned, "Add a discount code to checkout")
    assert task.id is not None
    from app.knowledge import create_blueprint, worker_connection
    from app.models import Blueprint

    with worker_connection() as conn:
        blueprint = create_blueprint(
            conn,
            Blueprint(
                project_id=scanned.project_id,
                repo_id=scanned.repo_id,
                task_id=task.id,
                commit_sha=scanned.commit_sha,
                title="Discounts",
                created_by=scanned.user_id,
            ),
        )
    assert blueprint.id is not None
    return {"repo_id": scanned.repo_id, "task_id": task.id, "blueprint_id": blueprint.id}


def _fill(template: str, ids: dict[str, str]) -> str:
    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in ids:
            return ids[name]
        if match.group(0).endswith(":path}"):
            return "a/b.py"
        return "1" if name in {"node_id", "sid"} else str(uuid.uuid4())

    return PARAM.sub(fill, template)


def test_the_sweep_sees_the_routes_it_is_meant_to_guard() -> None:
    templates = {t for _, t in OWNED_ROUTES}
    assert any(t.startswith("/repos/{repo_id}") for t in templates)
    assert any(t.startswith("/tasks/{task_id}") for t in templates)
    assert any(t.startswith("/blueprints/{blueprint_id}") for t in templates)
    assert len(OWNED_ROUTES) >= 40, "the route table shrank or the traversal broke"


@pytest.mark.parametrize(("method", "template"), OWNED_ROUTES)
def test_a_signed_in_stranger_is_refused_with_403(
    client, auth_header, make_user, ids, method, template
) -> None:
    resp = client.request(
        method, _fill(template, ids), headers=auth_header(make_user()), json={}
    )
    assert resp.status_code == 403, (method, template, resp.status_code, resp.text[:200])


@pytest.mark.parametrize(("method", "template"), OWNED_ROUTES)
def test_a_user_with_no_profile_at_all_is_refused_the_same_way(
    client, auth_header, ids, method, template
) -> None:
    ghost = f"user_{uuid.uuid4().hex}"
    resp = client.request(method, _fill(template, ids), headers=auth_header(ghost), json={})
    assert resp.status_code == 403, (method, template, resp.status_code, resp.text[:200])


def test_membership_of_one_project_grants_nothing_in_another(
    client, auth_header, make_user, ids
) -> None:
    """The failure this guards is an authorization check that asks 'is this
    caller a member of *a* project' instead of 'of *this* project'."""
    from app.knowledge import create_project, worker_connection

    other_user = make_user()
    with worker_connection() as conn:
        create_project(conn, "someone else's project", other_user)
    headers = auth_header(other_user)

    for path in (
        f"/repos/{ids['repo_id']}/status",
        f"/repos/{ids['repo_id']}/constraints",
        f"/tasks/{ids['task_id']}",
        f"/blueprints/{ids['blueprint_id']}",
        f"/blueprints/{ids['blueprint_id']}/versions",
    ):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 403, (path, resp.status_code)
    assert client.get(f"/repos/{ids['repo_id']}/files/svc/orders.py", headers=headers,
                      params={"commit_sha": "x"}).status_code == 403
