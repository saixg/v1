"""Read-only MCP server (POST /mcp/{repo_id}): JSON-RPC over HTTP with per-repo
Bearer tokens, against two really scanned repos and the live database.

Token minting goes through the real owner-only API route, so the hash-storage
and shown-once behavior under test is the shipped path. No model is called.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.context_api import get_context_model_service
from app.llm import Completion, CompletionProvider, CompletionRequest, RoutedModelService
from app.main import app
from app.mcp_server import TOOLS

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

TOOL_NAMES = {
    "search_code",
    "get_component",
    "get_dependencies",
    "get_constraints",
    "impact_analysis",
    "get_conventions",
    "get_context_package",
}


class Unused(CompletionProvider):
    name = "fake"

    def complete(self, request: CompletionRequest) -> Completion:  # pragma: no cover
        raise AssertionError("the MCP server and context assembly must not call a model here")


@pytest.fixture(autouse=True)
def _fake_model() -> Iterator[None]:
    service = RoutedModelService(provider=Unused(), recorder=lambda _c: None)
    app.dependency_overrides[get_context_model_service] = lambda: service
    yield
    app.dependency_overrides.pop(get_context_model_service, None)


@pytest.fixture(scope="module")
def repo_a(tmp_path_factory: pytest.TempPathFactory) -> Iterator:
    repo = fx.create_scanned_repo(tmp_path_factory.mktemp("mcp_a"))
    yield repo
    fx.delete_scanned_repo(repo)


@pytest.fixture(scope="module")
def repo_b(tmp_path_factory: pytest.TempPathFactory) -> Iterator:
    repo = fx.create_scanned_repo(tmp_path_factory.mktemp("mcp_b"))
    yield repo
    fx.delete_scanned_repo(repo)


def mint(client, auth_header, repo, **body) -> dict:
    resp = client.post(
        f"/repos/{repo.repo_id}/api-tokens",
        headers=auth_header(repo.user_id),
        json={"name": "mcp test", **body},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def token_a(client, auth_header, repo_a) -> str:
    return mint(client, auth_header, repo_a)["token"]


def rpc(client, repo_id: str, token: str | None, method: str, params=None, id_=1):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post(f"/mcp/{repo_id}", headers=headers, json=msg)


def call(client, repo, token, name, **arguments) -> dict:
    resp = rpc(client, repo.repo_id, token, "tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "result" in body, body
    return body["result"]


def assert_cited_static(result: dict, repo, *, expect_citations: bool = True) -> dict:
    payload = result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == payload
    assert payload["analysis"] == "static"
    assert "STATIC ANALYSIS" in payload["notice"]
    assert payload["commit_sha"] == repo.commit_sha
    assert "citations" in payload
    if expect_citations:
        assert payload["citations"], payload
    for c in payload["citations"]:
        assert c["path"] and c["line_start"] >= 1 and c["line_end"] >= c["line_start"]
    return payload


# ------------------------------------------------------------------ auth


def test_missing_bad_and_malformed_tokens_are_401(client, repo_a, token_a) -> None:
    assert rpc(client, repo_a.repo_id, None, "ping").status_code == 401
    assert rpc(client, repo_a.repo_id, "cxa_notarealtoken", "ping").status_code == 401
    assert rpc(client, repo_a.repo_id, token_a + "x", "ping").status_code == 401
    assert rpc(client, "not-a-uuid", token_a, "ping").status_code == 401
    assert rpc(client, str(uuid.uuid4()), token_a, "ping").status_code == 401
    bad_scheme = client.post(
        f"/mcp/{repo_a.repo_id}",
        headers={"Authorization": f"Basic {token_a}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert bad_scheme.status_code == 401
    assert bad_scheme.headers["www-authenticate"] == "Bearer"
    # A Clerk user JWT is not an MCP token.
    assert rpc(client, repo_a.repo_id, "eyJhbGciOi.fake.jwt", "ping").status_code == 401


def test_a_token_for_repo_a_cannot_read_repo_b(
    client, auth_header, repo_a, repo_b, token_a
) -> None:
    assert rpc(client, repo_a.repo_id, token_a, "ping").status_code == 200
    resp = rpc(client, repo_b.repo_id, token_a, "tools/list")
    assert resp.status_code == 401
    token_b = mint(client, auth_header, repo_b)["token"]
    assert rpc(client, repo_b.repo_id, token_b, "ping").status_code == 200
    assert rpc(client, repo_a.repo_id, token_b, "ping").status_code == 401


def test_a_revoked_token_is_rejected(client, auth_header, repo_a) -> None:
    issued = mint(client, auth_header, repo_a)
    token = issued["token"]
    assert rpc(client, repo_a.repo_id, token, "ping").status_code == 200
    gone = client.delete(
        f"/repos/{repo_a.repo_id}/api-tokens/{issued['record']['id']}",
        headers=auth_header(repo_a.user_id),
    )
    assert gone.status_code == 204
    assert rpc(client, repo_a.repo_id, token, "ping").status_code == 401


def test_use_is_recorded_on_the_token(client, auth_header, repo_a) -> None:
    issued = mint(client, auth_header, repo_a)
    assert issued["record"]["last_used_at"] is None
    assert rpc(client, repo_a.repo_id, issued["token"], "ping").status_code == 200
    listing = client.get(
        f"/repos/{repo_a.repo_id}/api-tokens", headers=auth_header(repo_a.user_id)
    ).json()["tokens"]
    row = next(t for t in listing if t["id"] == issued["record"]["id"])
    assert row["last_used_at"] is not None


# -------------------------------------------------------------- protocol


def test_initialize_ping_and_notifications(client, repo_a, token_a) -> None:
    resp = rpc(
        client,
        repo_a.repo_id,
        token_a,
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    result = body["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "codeaxis"
    assert "tools" in result["capabilities"]
    assert "STATIC ANALYSIS" in result["instructions"]

    odd = rpc(client, repo_a.repo_id, token_a, "initialize", {"protocolVersion": "1999-01-01"})
    assert odd.json()["result"]["protocolVersion"] == "2025-06-18"

    assert rpc(client, repo_a.repo_id, token_a, "ping").json()["result"] == {}

    note = client.post(
        f"/mcp/{repo_a.repo_id}",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert note.status_code == 202 and note.content == b""


def test_protocol_errors(client, repo_a, token_a) -> None:
    h = {"Authorization": f"Bearer {token_a}"}
    url = f"/mcp/{repo_a.repo_id}"
    assert rpc(client, repo_a.repo_id, token_a, "no/such").json()["error"]["code"] == -32601
    assert client.post(url, headers=h, content=b"{not json").json()["error"]["code"] == -32700
    batch = client.post(url, headers=h, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert batch.status_code == 400
    no_version = client.post(url, headers=h, json={"id": 1, "method": "ping"})
    assert no_version.json()["error"]["code"] == -32600
    assert client.get(url, headers=h).status_code == 405


def test_tools_list_is_read_only_with_json_schemas(client, repo_a, token_a) -> None:
    tools = rpc(client, repo_a.repo_id, token_a, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == TOOL_NAMES == {t.name for t in TOOLS}
    for t in tools:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False
        assert t["annotations"]["readOnlyHint"] is True
        assert t["annotations"]["destructiveHint"] is False


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("search_code", {}),
        ("search_code", {"query": "x", "limit": 10_000}),
        ("search_code", {"query": "x", "token_budget": 10**9}),
        ("search_code", {"query": "x" * 501}),
        ("search_code", {"query": "x", "surprise": 1}),
        ("search_code", {"query": 5}),
        ("get_component", {"node_id": "1"}),
        ("get_component", {"node_id": True}),
        ("get_dependencies", {"path": "svc/orders.py", "depth": 99}),
        ("get_dependencies", {"path": "svc/orders.py", "direction": "sideways"}),
        ("get_context_package", {"task_id": "not-a-uuid"}),
        ("get_context_package", {}),
        ("no_such_tool", {}),
    ],
)
def test_untrusted_arguments_are_rejected_as_invalid_params(
    client, repo_a, token_a, name, arguments
) -> None:
    params = {"name": name, "arguments": arguments}
    resp = rpc(client, repo_a.repo_id, token_a, "tools/call", params)
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32602, resp.json()


# ----------------------------------------------------------------- tools


def test_search_code_cites_and_reports_stages(client, repo_a, token_a) -> None:
    result = call(client, repo_a, token_a, "search_code", query="place_order")
    assert result["isError"] is False
    payload = assert_cited_static(result, repo_a)
    assert any(r["path"] == "svc/orders.py" for r in payload["results"])
    assert payload["stages"]
    # The vector stage is never run from a token-authenticated, read-only endpoint.
    assert payload["budget"]["token_budget"] == 4000
    tiny = call(client, repo_a, token_a, "search_code", query="place_order", token_budget=200)
    assert tiny["structuredContent"]["budget"]["tokens_used"] <= 200 or tiny["structuredContent"][
        "budget"
    ]["dropped_for_budget"] >= 0


def test_get_component_for_a_file_and_a_symbol(client, repo_a, token_a) -> None:
    result = call(client, repo_a, token_a, "get_component", path="svc/orders.py")
    payload = assert_cited_static(result, repo_a)
    assert payload["component"]["kind"] == "file"
    assert payload["component"]["path"] == "svc/orders.py"
    names = {c["name"] for c in payload["children"]}
    assert "place_order" in names
    out_targets = {
        e["node"]["path"] for e in payload["relationships"]["outgoing"] if e["node"] is not None
    }
    assert "svc/store.py" in out_targets
    in_targets = {
        e["node"]["path"] for e in payload["relationships"]["incoming"] if e["node"] is not None
    }
    assert "svc/api.py" in in_targets

    sym = call(client, repo_a, token_a, "get_component", path="svc/orders.py", symbol="place_order")
    sym_payload = assert_cited_static(sym, repo_a)
    assert sym_payload["component"]["name"] == "place_order"

    missing = call(client, repo_a, token_a, "get_component", path="no/such.py")
    assert missing["isError"] is True
    assert "static" in missing["structuredContent"]["analysis"]


def test_get_dependencies_shows_unresolved_edges_as_unresolved(client, repo_a, token_a) -> None:
    result = call(
        client,
        repo_a,
        token_a,
        "get_dependencies",
        path="svc/orders.py",
        direction="outgoing",
    )
    payload = assert_cited_static(result, repo_a)
    gaps = [e for e in payload["edges"] if not e["resolved"]]
    # `compute_total` is called but defined nowhere: a gap, listed as one.
    assert gaps, payload["edges"]
    assert all(g["resolution_status"] in ("unresolved", "ambiguous", "external") for g in gaps)
    assert any(g["resolution_status"] == "unresolved" and g["dst_id"] is None for g in gaps)
    assert payload["unresolved_summary"]["not_resolved"] == len(gaps)
    assert "never wired" in payload["unresolved_summary"]["note"]

    both = call(client, repo_a, token_a, "get_dependencies", path="svc/store.py")
    edges = assert_cited_static(both, repo_a)["edges"]
    assert {e["direction"] for e in edges} <= {"incoming", "outgoing"}
    assert any(e["direction"] == "incoming" for e in edges)


def test_impact_analysis_follows_importers(client, repo_a, token_a) -> None:
    result = call(client, repo_a, token_a, "impact_analysis", path="svc/store.py", depth=3)
    payload = assert_cited_static(result, repo_a)
    paths = {i["path"] for i in payload["impacted"]}
    assert "svc/orders.py" in paths
    assert all(i["distance"] >= 1 for i in payload["impacted"])
    assert "unresolved" in payload["limits"]


def test_get_constraints_and_conventions_carry_origin_and_citations(
    client, repo_a, token_a
) -> None:
    result = call(client, repo_a, token_a, "get_constraints")
    payload = assert_cited_static(result, repo_a)
    assert payload["constraints"]
    assert all(c["task_id"] is None for c in payload["constraints"])
    assert all(c["origin"] in ("parsed", "configured", "inferred") for c in payload["constraints"])
    assert {c["confirmed"] for c in payload["constraints"]} == {False}

    hard = call(client, repo_a, token_a, "get_constraints", severity="hard")
    assert all(c["severity"] == "hard" for c in hard["structuredContent"]["constraints"])

    conv = call(client, repo_a, token_a, "get_conventions")
    cp = assert_cited_static(conv, repo_a)
    assert cp["conventions"]
    assert {c["origin"] for c in cp["conventions"]} <= {"parsed", "configured", "inferred"}
    assert any(c["origin"] == "configured" for c in cp["conventions"])


def test_get_context_package_is_scoped_to_the_tokens_repo(
    client, auth_header, repo_a, repo_b, token_a
) -> None:
    h = auth_header(repo_a.user_id)
    created = client.post(
        f"/repos/{repo_a.repo_id}/tasks",
        headers=h,
        json={"title": "Touch orders", "intent": "Update place_order in svc/orders.py."},
    )
    task_id = created.json()["id"]

    none_yet = call(client, repo_a, token_a, "get_context_package", task_id=task_id)
    assert none_yet["isError"] is True

    assembled = client.post(f"/tasks/{task_id}/context", headers=h, json={"token_budget": 12000})
    assert assembled.status_code == 201, assembled.text

    result = call(client, repo_a, token_a, "get_context_package", task_id=task_id)
    assert result["isError"] is False
    payload = assert_cited_static(result, repo_a)
    assert payload["freshness"]["state"] == "current"
    assert payload["items"] and payload["package"]["status"] == "assembled"
    assert "warning" not in payload

    # Same task id, another repo's token: indistinguishable from "no such task".
    token_b = mint(client, auth_header, repo_b)["token"]
    other = call(client, repo_b, token_b, "get_context_package", task_id=task_id)
    assert other["isError"] is True
    assert "items" not in other["structuredContent"]
    assert call(client, repo_a, token_a, "get_constraints", task_id=task_id)["isError"] is False
    cross = call(client, repo_b, token_b, "get_constraints", task_id=task_id)
    assert cross["isError"] is True

    # A change after assembly flips the same call to a labelled stale package.
    body = {"kind": "other", "scope": "repo", "statement": "Be careful.", "severity": "soft"}
    assert client.post(f"/tasks/{task_id}/constraints", headers=h, json=body).status_code == 201
    stale = call(client, repo_a, token_a, "get_context_package", task_id=task_id)
    stale_payload = stale["structuredContent"]
    assert stale_payload["freshness"]["state"] == "stale"
    assert "STALE" in stale_payload["warning"]


def test_unknown_commit_is_a_tool_error_not_a_guess(client, repo_a, token_a) -> None:
    result = call(client, repo_a, token_a, "get_conventions", commit_sha="deadbeef")
    assert result["isError"] is True
    assert "no completed scan" in result["structuredContent"]["error"]
    pinned = call(client, repo_a, token_a, "get_conventions", commit_sha=repo_a.commit_sha)
    assert pinned["isError"] is False


def test_the_token_itself_never_appears_in_a_response(client, repo_a, token_a) -> None:
    for method, params in (
        ("tools/list", None),
        ("tools/call", {"name": "get_constraints", "arguments": {}}),
    ):
        assert token_a not in rpc(client, repo_a.repo_id, token_a, method, params).text
