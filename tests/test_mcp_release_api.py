"""MCP tools in published APIs (WP-9) and local stdio servers through the API (WP-8).

A published release freezes its MCP tools as safe references (server id, hash, classification; no address or token),
a run of the published API revalidates them on every call, and an export refuses them. The stdio routes plan the
packages of a local server through the Python environments; it runs only in the Docker sandbox's containers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import importlib.util
import json
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.testing import FakeUv
import pytest
from test_published_api import PUBLISHED, auth, finish, new_key

from conftest import text, tool_use
from test_flow import edge, save
from test_runs import agent_node

SDK = importlib.util.find_spec("mcp") is not None
needs_sdk = pytest.mark.skipif(not SDK, reason="the MCP SDK needs Python 3.11+")
TOOL = "mcp__docs__search"


@pytest.fixture
def loopback() -> Iterator[Any]:
    from mcp_test_server import serve

    handle = serve()
    yield handle
    handle.stop()


def connect(client: TestClient, url: str) -> str:
    added = client.post("/api/mcp/servers", json={"name": "Docs", "url": url, "allow_loopback": True})
    assert added.status_code == 201, added.text
    server_id = str(added.json()["id"])
    assert client.post(f"/api/mcp/servers/{server_id}/discover").status_code == 200
    decided = client.post(
        f"/api/mcp/servers/{server_id}/tools/search/decision", json={"approve": True, "access": "read"}
    )
    assert decided.status_code == 200, decided.text
    return server_id


def mcp_workflow(client: TestClient) -> str:
    start = {"id": "start", "type": "start", "start": {"fields": [{"name": "q", "label": "Q", "type": "text"}]}}
    nodes = [start, agent_node("finder", "FINDER", tools=[TOOL]), {"id": "out", "type": "output"}]
    return save(client, nodes, [edge("start", "finder"), edge("finder", "out")], name="Finder", budget_usd=1.0)


def script() -> dict[str, list[Any]]:
    return {"FINDER": [tool_use(TOOL, {"query": "invoices"}), text("done")] * 4}


@needs_sdk
def test_a_release_freezes_its_mcp_tools_without_address_or_token(
    make_app: Callable[..., TestClient], loopback: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NODESTRO_MCP_UNATTENDED", raising=False)
    client = make_app(script())
    server_id = connect(client, loopback.url)
    workflow_id = mcp_workflow(client)
    check = client.post(f"/api/workflows/{workflow_id}/api/check", json={}).json()
    codes = {item["code"]: item["level"] for item in check["problems"]}
    assert codes.get("api.mcp_tools") == "info"
    assert codes.get("api.mcp_unattended_off") == "warning"
    published = client.post(f"/api/workflows/{workflow_id}/api/publish", json={"note": "v1"})
    assert published.status_code == 201, published.text
    publication = published.json()["publication"]
    release = client.get(f"/api/publications/{publication['id']}/releases/1").json()
    ref = release["bundle"]["mcp_tools"][TOOL]
    assert ref["server_id"] == server_id and ref["server_slug"] == "docs" and ref["access"] == "read"
    assert ref["hash"].startswith("sha256:")
    dumped = json.dumps(release)
    assert loopback.url not in dumped and "127.0.0.1" not in dumped

    # Without the operator's switch, an API run (unattended) does not use the MCP tool.
    token = new_key(client, publication["id"])
    started = client.post(f"{PUBLISHED}/{publication['slug']}/runs", json={"values": {"q": "x"}}, headers=auth(token))
    assert started.status_code == 202, started.text
    refused = finish(client, publication["slug"], token, started.json()["run_id"])
    assert refused["status"] == "failed", refused
    assert "mcp.unattended_not_enabled" in json.dumps(refused)

    # With it, the frozen tool runs; then a reclassification stops the release until it is published again.
    monkeypatch.setenv("NODESTRO_MCP_UNATTENDED", "1")
    started = client.post(f"{PUBLISHED}/{publication['slug']}/runs", json={"values": {"q": "x"}}, headers=auth(token))
    done = finish(client, publication["slug"], token, started.json()["run_id"])
    assert done["status"] == "succeeded", done
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    run = nodestro.runs.get(started.json()["run_id"])
    call = run.nodes["finder"].tool_calls[0]
    assert "results for invoices" in call.output and not call.is_error
    assert call.provenance["release_hash"] == ref["hash"]

    tool = nodestro.mcp.tool(server_id, "search")
    tool.approved_hash = tool.hash = "sha256:" + "0" * 64
    nodestro.mcp.store.put("mcp_tools", tool.key, tool)
    started = client.post(f"{PUBLISHED}/{publication['slug']}/runs", json={"values": {"q": "x"}}, headers=auth(token))
    stopped = finish(client, publication["slug"], token, started.json()["run_id"])
    final = nodestro.runs.get(started.json()["run_id"])
    assert "mcp.release_tool_changed" in final.model_dump_json(), stopped


@needs_sdk
def test_unusable_mcp_tools_block_publishing_and_exports_refuse_them(
    make_app: Callable[..., TestClient], loopback: Any
) -> None:
    client = make_app(script())
    server_id = connect(client, loopback.url)
    workflow_id = mcp_workflow(client)
    publication = client.post(f"/api/workflows/{workflow_id}/api/publish", json={}).json()["publication"]
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}}).json()
    assert any(item["code"] == "export.mcp_tools" and item["level"] == "error" for item in check["problems"])
    # A tool that is no longer approved cannot be published.
    client.post(f"/api/mcp/servers/{server_id}/tools/search/decision", json={"approve": False})
    blocked = client.post(f"/api/workflows/{workflow_id}/api/check", json={}).json()
    assert not blocked["ok"]
    assert any(item["code"] == "api.mcp_tool_unusable" for item in blocked["problems"])
    assert client.post(f"/api/workflows/{workflow_id}/api/publish", json={}).status_code >= 400


INDEX = {"tablib": [("1.0", [], "tablib\n")]}


def test_stdio_servers_need_the_container_sandbox(make_app: Callable[..., TestClient]) -> None:
    client = make_app(python_runner=FakeUv(INDEX))
    assert client.get("/api/mcp/status").json()["stdio_possible"] is False
    body = {"name": "Tables", "transport": "stdio", "stdio": {"requirements": ["tablib"], "module": "tablib"}}
    refused = client.post("/api/mcp/servers", json=body)
    assert refused.status_code >= 400 and refused.json()["code"] == "sandbox.isolation_required"


def test_a_stdio_servers_packages_are_planned_for_approval(make_app: Callable[..., TestClient]) -> None:
    client = make_app(python_runner=FakeUv(INDEX))
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    nodestro.python.sandbox_kind = lambda: "docker"
    assert client.get("/api/mcp/status").json()["stdio_possible"] is True
    body = {"name": "Tables", "transport": "stdio", "stdio": {"requirements": ["tablib"], "module": "tablib"}}
    added = client.post("/api/mcp/servers", json=body)
    assert added.status_code == 201, added.text
    server = added.json()
    assert server["transport"] == "stdio" and server["url"] == "stdio:tablib"
    assert server["environment_state"] == "approval_needed"
    environment = client.get(f"/api/mcp/servers/{server['id']}/environment").json()
    assert environment["state"] == "approval_needed" and environment["scope"] == f"mcp:{server['id']}"
    plan = client.post(f"/api/mcp/servers/{server['id']}/environment/plan").json()
    assert plan["scope"] == f"mcp:{server['id']}" and plan["needs_approval"] is True
    assert [item["name"] for item in plan["packages"] if item["name"] == "tablib"]
    # A token or a sign-in is refused for a local server.
    with_token = client.post("/api/mcp/servers", json={**body, "auth": "bearer", "token": "x"})
    assert with_token.status_code >= 400 and with_token.json()["code"] == "mcp.stdio_invalid"
