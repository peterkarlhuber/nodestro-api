"""API fixes from the independent MCP review: who may test agents and tools (H-1), who may decide about destructive
MCP calls (M-3), approvals without the reviewed hash (L-3), and combinations of these.

The run-only role (``operator``: read + run) must not reach an MCP tool that changes data through any direct path:
not by a free agent configuration in the agent test, not through the tool panel, and not by approving a destructive
call. Every case counts the calls that reached the MCP server.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import importlib.util
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.tool_approvals import ENABLE_ENV
import pytest

from nodestro_api.identity import ROLE_PERMISSIONS
from nodestro_api.permissions import LOCAL_OWNER, Principal

from conftest import text, tool_use

pytestmark = pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="the MCP SDK needs Python 3.11+")

MakeApp = Callable[..., TestClient]
DELETE = "mcp__docs__delete_page"
SEARCH = "mcp__docs__search"


class Counting:
    def __init__(self) -> None:
        self.deleted: list[str] = []


@pytest.fixture
def mcp_server() -> Iterator[tuple[Any, Counting]]:
    from mcp.server.mcpserver import MCPServer
    from mcp_test_server import serve
    from mcp_types import ToolAnnotations

    seen = Counting()
    server = MCPServer("nodestro-review-test")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search(query: str) -> str:
        """Search the test catalog."""
        return f"results for {query}"

    @server.tool()
    def delete_page(page_id: str) -> str:
        """Delete a page."""
        seen.deleted.append(page_id)
        return f"deleted {page_id}"

    handle = serve(server)
    yield handle, seen
    handle.stop()


def as_role(client: TestClient, role: str | None) -> None:
    principal = LOCAL_OWNER if role is None else Principal(id=role, permissions=ROLE_PERMISSIONS[role], role=role)  # type: ignore[index]
    client.app.state.principal_resolver = lambda _connection: principal  # type: ignore[attr-defined]


def connected(make_app: MakeApp, url: str, script: dict[str, list[Any]], *, delete_access: str = "write") -> TestClient:
    client = make_app(script)
    server = client.post("/api/mcp/servers", json={"name": "Docs", "url": url, "allow_loopback": True})
    assert server.status_code == 201, server.text
    server_id = server.json()["id"]
    tools = {tool["name"]: tool for tool in client.post(f"/api/mcp/servers/{server_id}/discover").json()["tools"]}
    for name, access in (("search", "read"), ("delete_page", delete_access)):
        answer = client.post(
            f"/api/mcp/servers/{server_id}/tools/{name}/decision",
            json={"approve": True, "access": access, "expected_hash": tools[name]["hash"]},
        )
        assert answer.status_code == 200, answer.text
    return client


def agent(tools: list[str]) -> dict[str, Any]:
    return {"name": "Agent", "system_prompt": "LOOKUP", "tools": tools}


def save_workflow(client: TestClient, tools: list[str]) -> str:
    body = {
        "name": "Review",
        "nodes": [{"id": "start", "type": "start"}, {"id": "a", "type": "agent", "agent": agent(tools)}],
        "edges": [{"id": "e1", "source": "start", "target": "a"}],
    }
    answer = client.post("/api/workflows", json=body)
    assert answer.status_code == 201, answer.text
    return str(answer.json()["id"])


SCRIPT = {"LOOKUP": [tool_use(DELETE, {"page_id": "7"}), text("done")]}


def test_a_run_only_user_cannot_test_a_free_agent_configuration(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client = connected(make_app, handle.url, SCRIPT)
    as_role(client, "operator")
    answer = client.post("/api/agents/test", json={"agent": agent([DELETE]), "input": "x"})
    assert answer.status_code == 403, answer.text
    assert answer.json()["code"] == "access.forbidden" and answer.json()["params"]["permission"] == "edit"
    assert seen.deleted == []


def test_a_saved_agent_is_tested_with_its_assignment_and_no_changing_call_runs(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client = connected(make_app, handle.url, SCRIPT)
    workflow_id = save_workflow(client, [DELETE])
    as_role(client, "operator")
    # Exactly the saved configuration: allowed, but the changing call is refused like in a run that cannot ask.
    answer = client.post("/api/agents/test", json={"agent": agent([DELETE]), "input": "x", "workflow_id": workflow_id})
    assert answer.status_code == 200, answer.text
    [call] = [c for c in answer.json()["tool_calls"] if c["name"] == DELETE]
    assert call["approval"]["status"] == "denied" and call["approval"]["code"] == "tool.approval_not_in_test"
    # Combined: the saved workflow's id with a changed tool list or prompt is a free configuration again.
    for changed in (agent([DELETE, SEARCH]), {**agent([DELETE]), "system_prompt": "LOOKUP and more"}):
        refused = client.post("/api/agents/test", json={"agent": changed, "input": "x", "workflow_id": workflow_id})
        assert refused.status_code == 403, refused.text
    assert seen.deleted == []


def test_an_editor_tests_freely_but_the_same_tool_rules_apply(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client = connected(make_app, handle.url, SCRIPT)
    as_role(client, "editor")
    answer = client.post("/api/agents/test", json={"agent": agent([DELETE]), "input": "x"})
    assert answer.status_code == 200, answer.text
    calls = [c for c in answer.json()["tool_calls"] if c["name"] == DELETE]
    assert calls and all(c["approval"]["status"] == "denied" for c in calls)
    assert seen.deleted == []


def test_the_tool_panel_needs_edit_and_never_runs_a_changing_mcp_tool(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client = connected(make_app, handle.url, {})
    as_role(client, "operator")
    for name in (DELETE, SEARCH):
        refused = client.post(f"/api/tools/{name}/run", json={"input": {"page_id": "7", "query": "q"}})
        assert refused.status_code == 403 and refused.json()["params"]["permission"] == "edit", refused.text
    as_role(client, "editor")
    write = client.post(f"/api/tools/{DELETE}/run", json={"input": {"page_id": "7"}})
    assert write.status_code == 400 and write.json()["code"] == "mcp.write_not_supported", write.text
    read = client.post(f"/api/tools/{SEARCH}/run", json={"input": {"query": "q"}})
    assert read.status_code == 200 and "results for q" in read.json()["output"], read.text
    assert seen.deleted == []


def _waiting(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = client.get("/api/tool-approvals").json()
        if found:
            return found[0]  # type: ignore[no-any-return]
        time.sleep(0.02)
    raise AssertionError("no approval request")


def _finished(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def test_only_an_administrator_decides_about_a_destructive_mcp_call(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client = connected(make_app, handle.url, SCRIPT, delete_access="destructive")
    client.app.state.nodestro.runs.tool_gate.approvals.poll_seconds = 0.02  # type: ignore[attr-defined]
    workflow_id = save_workflow(client, [DELETE])
    started = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"})
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    request = _waiting(client)
    assert request["tool"] == DELETE and request["capability"]["access"] == "destructive"
    path = f"/api/runs/{run_id}/tool-approvals/{request['id']}"
    for role in ("operator", "editor"):
        as_role(client, role)
        for decision in ("approved", "rejected"):
            refused = client.post(path, json={"decision": decision, "arguments_hash": request["arguments_hash"]})
            assert refused.status_code == 403, refused.text
            assert refused.json()["params"]["permission"] == "admin"
    assert seen.deleted == []
    as_role(client, None)
    assert client.get(path.rsplit("/", 1)[0]).json()[0]["status"] == "pending"
    as_role(client, "admin")
    approved = client.post(path, json={"decision": "approved", "arguments_hash": request["arguments_hash"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["decided_by"] == "admin"
    assert _finished(client, run_id)["status"] == "succeeded"
    assert seen.deleted == ["7"]


def test_a_write_mcp_call_is_still_decided_with_run(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    """M-3 raises the bar for destructive MCP calls only; ``write`` calls keep the D10 rule (``run``)."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client = connected(make_app, handle.url, SCRIPT)
    client.app.state.nodestro.runs.tool_gate.approvals.poll_seconds = 0.02  # type: ignore[attr-defined]
    workflow_id = save_workflow(client, [DELETE])
    run_id = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"}).json()["id"]
    request = _waiting(client)
    as_role(client, "operator")
    answer = client.post(
        f"/api/runs/{run_id}/tool-approvals/{request['id']}",
        json={"decision": "rejected", "arguments_hash": request["arguments_hash"]},
    )
    assert answer.status_code == 200, answer.text
    as_role(client, None)
    _finished(client, run_id)
    assert seen.deleted == []


def test_approving_a_tool_needs_the_reviewed_hash(make_app: MakeApp, mcp_server: tuple[Any, Counting]) -> None:
    handle, _seen = mcp_server
    client = make_app()
    server_id = client.post(
        "/api/mcp/servers", json={"name": "Docs", "url": handle.url, "allow_loopback": True}
    ).json()["id"]
    client.post(f"/api/mcp/servers/{server_id}/discover")
    missing = client.post(
        f"/api/mcp/servers/{server_id}/tools/search/decision", json={"approve": True, "access": "read"}
    )
    assert missing.status_code == 400 and missing.json()["code"] == "mcp.hash_required", missing.text
    # Rejecting needs no hash.
    rejected = client.post(f"/api/mcp/servers/{server_id}/tools/search/decision", json={"approve": False})
    assert rejected.status_code == 200, rejected.text
