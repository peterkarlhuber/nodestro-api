"""Connected MCP services in a product app (N-23, ``docs/embedding.md``).

A product that selects the group ``mcp`` serves the studio's MCP routes unchanged, through the same workspace and
the same rules: the administrator's decision per tool, tokens kept write-only (sealed with the vault key), stdio
servers only in the Docker sandbox's containers, loopback servers only on the desktop. Nothing here is a second MCP
implementation: the product app is the studio app with fewer routes.

The rules a product must not lose:

- a tool that changes data stays behind the tool approval gate (G-2, ``NODESTRO_TOOL_APPROVALS``, off by default):
  while the gate is off it is never usable; with the gate on every call waits for a person's decision;
- an MCP server counts as a recipient outside the computer in the egress protection (N-08, runtime
  ``docs/content-provenance.md``): once a run holds content of a granted source, a call of a read-only MCP tool is
  asked like any outbound call, and a grant on exactly that server lets it go.
"""

from __future__ import annotations

from collections.abc import Iterator
import time
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nodestro_runtime.schemas import DEFAULT_AGENT_MODEL, DEFAULT_ASSISTANT_MODEL, SettingsIn
from nodestro_runtime.testing import ScriptedClient, text, tool_use
from nodestro_runtime.tool_approvals import ACCESS_ENV, ENABLE_ENV, PROVENANCE_ENV
import pytest

from nodestro_api.context import AppConfig
from nodestro_api.host import Branding, HostSwitches, create_product_app

SEARCH = "mcp__docs__search"
DELETE = "mcp__docs__delete_page"
#: What a product with connected services serves: the services, the tool list and panel, workflows and their runs.
GROUPS = ("mcp", "tools", "workflows", "runs", "approvals", "knowledge")
SWITCHES = (ENABLE_ENV, ACCESS_ENV, PROVENANCE_ENV, "NODESTRO_MCP_UNATTENDED", "NODESTRO_VAULT_KEY")


class Seen:
    """The calls that reached the MCP server."""

    def __init__(self) -> None:
        self.searched: list[str] = []
        self.deleted: list[str] = []


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every switch starts as shipped: off.
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def mcp_server() -> Iterator[tuple[Any, Seen]]:
    from mcp.server.mcpserver import MCPServer
    from mcp_test_server import serve
    from mcp_types import ToolAnnotations

    seen = Seen()
    server = MCPServer("nodestro-product-test")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search(query: str) -> str:
        """Search the test catalog."""
        seen.searched.append(query)
        return f"results for {query}"

    @server.tool()
    def delete_page(page_id: str) -> str:
        """Delete a page."""
        seen.deleted.append(page_id)
        return f"deleted {page_id}"

    handle = serve(server)
    yield handle, seen
    handle.stop()


def _product(
    tmp_path: Any,
    script: dict[str, list[Any]] | None = None,
    groups: tuple[str, ...] = GROUPS,
    branding: Branding | None = None,
) -> FastAPI:
    model = ScriptedClient(script or {})
    app = create_product_app(
        "desktop",
        tmp_path / "data",
        list(groups),
        branding or Branding(title="Test Product", version="1.0.0"),
        HostSwitches(scheduler=False),
        config=AppConfig(data_dir=tmp_path / "ignored"),
        integrations=[],
        client_factory=lambda _provider: model,
    )
    # The cloud defaults of an existing installation, which the scripted (Anthropic-shaped) client answers.
    app.state.nodestro.update_settings(
        SettingsIn(default_model=DEFAULT_AGENT_MODEL, assistant_model=DEFAULT_ASSISTANT_MODEL)
    )
    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _connect(client: TestClient, url: str, *, delete_access: str = "write") -> tuple[str, dict[str, Any]]:
    """Add the server, discover its tools, approve ``search`` (read) and decide ``delete_page``."""
    added = client.post("/api/mcp/servers", json={"name": "Docs", "url": url, "allow_loopback": True})
    assert added.status_code == 201, added.text
    server_id = str(added.json()["id"])
    found = client.post(f"/api/mcp/servers/{server_id}/discover")
    assert found.status_code == 200, found.text
    tools = {tool["name"]: tool for tool in found.json()["tools"]}
    decided: dict[str, Any] = {}
    for name, access in (("search", "read"), ("delete_page", delete_access)):
        answer = client.post(
            f"/api/mcp/servers/{server_id}/tools/{name}/decision",
            json={"approve": True, "access": access, "expected_hash": tools[name]["hash"]},
        )
        assert answer.status_code == 200, answer.text
        decided[name] = answer.json()
    return server_id, decided


def _workflow(client: TestClient, tools: list[str], **agent: Any) -> str:
    body = {
        "name": "Lookup",
        "nodes": [
            {"id": "start", "type": "start"},
            {
                "id": "a",
                "type": "agent",
                "agent": {"name": "Agent", "system_prompt": "LOOKUP", "tools": tools, **agent},
            },
        ],
        "edges": [{"id": "e1", "source": "start", "target": "a"}],
    }
    answer = client.post("/api/workflows", json=body)
    assert answer.status_code == 201, answer.text
    return str(answer.json()["id"])


def _start(client: TestClient, workflow_id: str) -> str:
    started = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"})
    assert started.status_code == 201, started.text
    return str(started.json()["id"])


def _finished(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def _waiting(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = client.get("/api/tool-approvals").json()
        if found:
            return found[0]  # type: ignore[no-any-return]
        time.sleep(0.02)
    raise AssertionError("no approval request")


# ---- the group ----------------------------------------------------------------------------------------------------


def test_the_mcp_group_serves_connected_services_in_a_product(tmp_path: Any, mcp_server: tuple[Any, Seen]) -> None:
    handle, seen = mcp_server
    with _client(_product(tmp_path)) as client:
        status = client.get("/api/mcp/status").json()
        assert status == {
            "available": True,
            "token_vault": False,
            "endpoint_list": None,
            "loopback_possible": True,
            "unattended_enabled": False,
            "write_tools_enabled": False,
            "stdio_possible": False,
        }
        server_id, decided = _connect(client, handle.url)
        assert decided["search"]["usable"] is True
        # The administrator's status per tool, the tool list and the tool panel are the studio's.
        tools = {tool["name"]: tool for tool in client.get(f"/api/mcp/servers/{server_id}/tools").json()}
        assert tools["search"]["status"] == "approved" and tools["delete_page"]["usable"] is False
        listed = [tool["name"] for tool in client.get("/api/tools").json() if tool["kind"] == "mcp"]
        assert listed == [SEARCH]
        tried = client.post(f"/api/tools/{SEARCH}/run", json={"input": {"query": "nodes"}})
        assert tried.status_code == 200 and "results for nodes" in tried.json()["output"], tried.text
        assert seen.searched == ["nodes"]
        paths = set(client.get("/api/openapi.json").json()["paths"])
        assert {"/api/mcp/status", "/api/mcp/servers", "/api/mcp/servers/{server_id}/oauth/start"} <= paths
        # The sign-in's browser callback is served too (outside the document); a forged one changes nothing.
        callback = client.get("/api/mcp/oauth/callback", params={"state": "x", "code": "y"}, follow_redirects=False)
        assert callback.status_code == 303
        # Without a page of its own the product gets the studio's default page.
        assert callback.headers["location"] == "/tools?tab=services&oauth_error=mcp.oauth_state_invalid"
        # Nodestro's own MCP server is another group; a product that does not select it does not serve it.
        assert client.post("/api/published/_mcp", json={}).json()["code"] == "route.not_found"


@pytest.mark.parametrize(("page", "location"), [("/connections", "/connections?"), ("/app?view=mcp", "/app?view=mcp&")])
def test_the_sign_in_continues_on_the_products_own_page(tmp_path: Any, page: str, location: str) -> None:
    """The callback is product neutral (E-P-1): a product names its own page; the studio's page is only the default."""
    branding = Branding(title="Test Product", version="1.0.0", mcp_sign_in_page=page)
    with _client(_product(tmp_path, branding=branding)) as client:
        forged = client.get("/api/mcp/oauth/callback", params={"state": "x", "code": "y"}, follow_redirects=False)
        assert forged.status_code == 303
        assert forged.headers["location"] == f"{location}oauth_error=mcp.oauth_state_invalid"
        denied = client.get("/api/mcp/oauth/callback", params={"error": "access_denied"}, follow_redirects=False)
        assert denied.headers["location"] == f"{location}oauth_error=mcp.oauth_denied"
        assert denied.headers["cache-control"] == "no-store" and denied.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "page",
    [
        "",
        "tools",
        "https://elsewhere.example/tools",
        "//elsewhere.example/tools",
        "/\\elsewhere.example",
        "/a#b",
        "/a b",
        "/a\nb",
    ],
)
def test_a_sign_in_page_of_another_site_is_refused(tmp_path: Any, page: str) -> None:
    with pytest.raises(ValueError, match="path of this origin"):
        _product(tmp_path, branding=Branding(mcp_sign_in_page=page))


def test_a_product_without_the_group_serves_no_mcp_route(tmp_path: Any) -> None:
    with _client(_product(tmp_path, groups=("tools", "workflows"))) as client:
        for method, path in (("GET", "/api/mcp/status"), ("GET", "/api/mcp/servers"), ("POST", "/api/mcp/servers")):
            answer = client.request(method, path, json={})
            assert answer.status_code == 404 and answer.json()["code"] == "route.not_found", path
        assert not [path for path in client.get("/api/openapi.json").json()["paths"] if path.startswith("/api/mcp")]


def test_tokens_stay_write_only_and_sealed_in_a_product(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    token = "nodestro-secret-token-123456"
    monkeypatch.setenv("NODESTRO_VAULT_KEY", base64.b64encode(b"k" * 32).decode())
    with _client(_product(tmp_path)) as client:
        assert client.get("/api/mcp/status").json()["token_vault"] is True
        body = {"name": "Docs", "url": "http://127.0.0.1:9/mcp", "allow_loopback": True, "token": token}
        server = client.post("/api/mcp/servers", json=body).json()
        assert server["token_set"] is True and server["token_storage"] == "vault"
        for path in ("/api/mcp/servers", f"/api/mcp/servers/{server['id']}", "/api/mcp/events"):
            assert token not in client.get(path).text, path
        stored = client.app.state.nodestro.mcp.server(server["id"])  # type: ignore[attr-defined]
        assert stored.token_sealed and token not in stored.token_sealed


# ---- G-2: tools that change data stay behind the gate -------------------------------------------------------------


def test_a_writing_mcp_tool_is_never_usable_while_the_gate_is_off(tmp_path: Any, mcp_server: tuple[Any, Seen]) -> None:
    handle, seen = mcp_server
    script = {"LOOKUP": [tool_use(DELETE, {"page_id": "7"}), text("done")]}
    with _client(_product(tmp_path, script)) as client:
        assert client.get("/api/mcp/status").json()["write_tools_enabled"] is False
        _server_id, decided = _connect(client, handle.url)
        assert decided["delete_page"]["usable"] is False
        assert decided["delete_page"]["refusal"] == "mcp.write_not_supported"
        assert [tool["name"] for tool in client.get("/api/tools").json() if tool["kind"] == "mcp"] == [SEARCH]
        # Not through the tool panel ...
        panel = client.post(f"/api/tools/{DELETE}/run", json={"input": {"page_id": "7"}})
        assert panel.status_code == 400 and panel.json()["code"] == "mcp.write_not_supported", panel.text
        # ... and not in a run: a run whose agent has it does not start.
        workflow_id = _workflow(client, [DELETE])
        started = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"})
        assert started.status_code == 400 and started.json()["code"] == "run.agents_invalid", started.text
        assert "changes data" in started.json()["detail"]
        assert seen.deleted == []


def test_with_the_gate_a_writing_mcp_call_waits_for_a_person(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Seen]
) -> None:
    """The gate is off by default; switched on (here, for the test) every changing call is asked first."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    script = {"LOOKUP": [tool_use(DELETE, {"page_id": "7"}), text("done")]}
    with _client(_product(tmp_path, script)) as client:
        client.app.state.nodestro.runs.tool_gate.approvals.poll_seconds = 0.02  # type: ignore[attr-defined]
        assert client.get("/api/mcp/status").json()["write_tools_enabled"] is True
        _connect(client, handle.url)
        run_id = _start(client, _workflow(client, [DELETE]))
        request = _waiting(client)
        assert request["tool"] == DELETE and request["capability"]["access"] == "write"
        assert seen.deleted == []
        rejected = client.post(
            f"/api/runs/{run_id}/tool-approvals/{request['id']}",
            json={"decision": "rejected", "arguments_hash": request["arguments_hash"]},
        )
        assert rejected.status_code == 200, rejected.text
        _finished(client, run_id)
        assert seen.deleted == []
        # The tool panel never runs it, gate or not.
        panel = client.post(f"/api/tools/{DELETE}/run", json={"input": {"page_id": "7"}})
        assert panel.status_code == 400 and panel.json()["code"] == "mcp.write_not_supported"
        assert seen.deleted == []


# ---- stdio only in containers, loopback only on the desktop -------------------------------------------------------


def test_stdio_servers_need_the_container_sandbox_in_a_product(tmp_path: Any) -> None:
    body = {"name": "Tables", "transport": "stdio", "stdio": {"requirements": ["tablib"], "module": "tablib"}}
    with _client(_product(tmp_path)) as client:
        assert client.get("/api/mcp/status").json()["stdio_possible"] is False
        refused = client.post("/api/mcp/servers", json=body)
        assert refused.status_code >= 400 and refused.json()["code"] == "sandbox.isolation_required", refused.text
        assert client.get("/api/mcp/servers").json() == []
        # With the Docker sandbox the same server is accepted and waits for its packages' approval.
        client.app.state.nodestro.python.sandbox_kind = lambda: "docker"  # type: ignore[attr-defined]
        assert client.get("/api/mcp/status").json()["stdio_possible"] is True
        added = client.post("/api/mcp/servers", json=body)
        assert added.status_code == 201, added.text
        assert added.json()["environment_state"] == "approval_needed"


def test_a_local_container_product_refuses_loopback_servers(tmp_path: Any) -> None:
    key = "k" * 43
    host = "nodestro-0123456789.localhost"
    app = create_product_app(
        "local-container",
        tmp_path / "data",
        ["mcp"],
        switches=HostSwitches(scheduler=False),
        config=AppConfig(
            data_dir=tmp_path / "ignored", host="0.0.0.0", access_key=key, browser_host=host, host_port=8765
        ),
        integrations=[],
    )
    with TestClient(app, base_url=f"http://{host}:8765") as client:
        assert client.get("/api/mcp/status").status_code == 401  # the access key first
        assert client.get("/api/launch", params={"token": key}, follow_redirects=False).status_code == 200
        assert client.get("/api/mcp/status").json()["loopback_possible"] is False
        # Inside a container, loopback is the container itself: no local development server.
        for url in ("http://127.0.0.1:9/mcp", "https://127.0.0.1:9/mcp"):
            answer = client.post("/api/mcp/servers", json={"name": "Dev", "url": url, "allow_loopback": True})
            assert answer.status_code == 400, answer.text
            assert answer.json()["code"] in ("mcp.https_required", "mcp.address_refused")


# ---- N-08: an MCP server is a recipient outside the computer ------------------------------------------------------


def test_an_mcp_server_counts_in_the_egress_protection_of_a_product(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Seen]
) -> None:
    """With content of a granted source (knowledge given in full) in the run, the read-only MCP call is asked before
    anything reaches the server; a grant on exactly that server lets the next run's call go."""
    for name in (ENABLE_ENV, ACCESS_ENV, PROVENANCE_ENV):
        monkeypatch.setenv(name, "1")
    handle, seen = mcp_server
    script = {"LOOKUP": [tool_use(SEARCH, {"query": "budget"}), text("done")] * 3}
    with _client(_product(tmp_path, script)) as client:
        ctx = client.app.state.nodestro  # type: ignore[attr-defined]
        ctx.runs.tool_gate.approvals.poll_seconds = 0.02
        assert ctx.runs.provenance_on()
        server_id, _decided = _connect(client, handle.url)

        def grant(resource_type: str, resource_id: str, action: str) -> None:
            body = {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "actions": [action],
                "duration": "until_revoked",
            }
            answer = client.post("/api/access/grants", json=body)
            assert answer.status_code == 201, answer.text

        # Without granted content the read-only call goes out as before.
        plain = _finished(client, _start(client, _workflow(client, [SEARCH])))
        assert plain["status"] == "succeeded" and not plain.get("granted_content"), plain
        assert seen.searched == ["budget"]
        seen.searched.clear()

        # Knowledge in the context marks the run; the cloud model has its own grant, the MCP server has none.
        uploaded = client.post("/api/knowledge", files=[("files", ("plan.txt", b"Budget 42", "text/plain"))])
        assert uploaded.status_code in (200, 201), uploaded.text
        knowledge = {"file_ids": [uploaded.json()[0]["id"]], "mode": "context"}
        grant("cloud_provider", "anthropic", "submit")
        marked = _workflow(client, [SEARCH], knowledge=knowledge)
        run_id = _start(client, marked)
        request = _waiting(client)
        assert request["tool"] == SEARCH and seen.searched == []
        client.post(
            f"/api/runs/{run_id}/tool-approvals/{request['id']}",
            json={"decision": "rejected", "arguments_hash": request["arguments_hash"]},
        )
        held = _finished(client, run_id)
        assert held["granted_content"] is True and seen.searched == []

        # A grant on exactly this server lets the call go.
        grant("mcp_server", server_id, "execute")
        granted = _finished(client, _start(client, marked))
        assert granted["status"] == "succeeded" and granted["granted_content"] is True, granted
        assert client.get("/api/tool-approvals").json() == []
        assert seen.searched == ["budget"]
