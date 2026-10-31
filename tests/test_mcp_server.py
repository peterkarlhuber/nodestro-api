"""Nodestro as an MCP server for published workflows (WP-11): off by default, two deliberate switches, same runs.

The interoperability case connects the official MCP SDK client over real HTTP to the app (uvicorn on the loopback
interface); the others drive the endpoint through the test client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import importlib.util
import json
import socket
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest
from test_published_api import auth, form_workflow, new_key, publish

from conftest import text

SDK = importlib.util.find_spec("mcp") is not None
needs_sdk = pytest.mark.skipif(not SDK, reason="the MCP SDK needs Python 3.11+")
ENDPOINT = "/api/published/_mcp"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client: TestClient, token: str | None, method: str, params: dict[str, Any] | None = None) -> Any:
    headers = {**HEADERS, **(auth(token) if token else {}), "MCP-Protocol-Version": "2025-11-25"}
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post(ENDPOINT, json=body, headers=headers)


@pytest.fixture
def offered(make_app: Callable[..., TestClient]) -> Callable[..., tuple[TestClient, dict[str, Any], str]]:
    def factory(**key: Any) -> tuple[TestClient, dict[str, Any], str]:
        client = make_app({"WRITER": [text("Angebot für ACME")] * 10})
        publication = publish(client, form_workflow(client), note="v1")["publication"]
        token = new_key(client, publication["id"], **key)
        return client, publication, token

    return factory


def offer(client: TestClient, publication: dict[str, Any], on: bool = True) -> None:
    answer = client.patch(f"/api/publications/{publication['id']}", json={"mcp_tool": on})
    assert answer.status_code == 200, answer.text
    assert answer.json()["mcp_tool"] is on


def test_the_mcp_server_is_off_by_default(offered: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NODESTRO_MCP_SERVER", raising=False)
    client, publication, token = offered()
    assert publication["mcp_tool"] is False
    offer(client, publication)
    answer = rpc(client, token, "tools/list")
    assert answer.status_code == 404 and answer.json()["error"]["code"] == "mcp_server.disabled"


@needs_sdk
def test_only_offered_publications_of_the_key_are_tools(
    offered: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODESTRO_MCP_SERVER", "1")
    client, publication, token = offered()
    assert rpc(client, None, "tools/list").status_code == 401
    assert rpc(client, "nk_wrong.secret", "tools/list").status_code == 401
    # Switched on by the operator, but not offered by an administrator: no tools.
    listed = rpc(client, token, "tools/list")
    assert listed.status_code == 200, listed.text
    assert listed.json()["result"]["tools"] == []
    offer(client, publication)
    tools = rpc(client, token, "tools/list").json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [publication["slug"]]
    assert tools[0]["inputSchema"]["type"] == "object"
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    history = nodestro.publications.get(publication["id"]).history
    assert any(event.kind == "mcp_tool_on" for event in history)
    # A key without the run scope, or for another API, sees nothing.
    reader = new_key(client, publication["id"], scopes=["read"])
    assert rpc(client, reader, "tools/list").json()["result"]["tools"] == []
    called = rpc(client, reader, "tools/call", {"name": publication["slug"], "arguments": {"values": {}}})
    assert called.json()["result"]["isError"] is True
    # Switching the API off removes the tool; switching the offer off as well.
    offer(client, publication, on=False)
    assert rpc(client, token, "tools/list").json()["result"]["tools"] == []


@needs_sdk
def test_a_tool_call_is_an_ordinary_run_of_the_published_api(
    offered: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODESTRO_MCP_SERVER", "1")
    client, publication, token = offered()
    offer(client, publication)
    arguments = {"values": {"customer": "ACME"}}
    called = rpc(client, token, "tools/call", {"name": publication["slug"], "arguments": arguments})
    assert called.status_code == 200, called.text
    result = called.json()["result"]
    run_id = result["structuredContent"]["run_id"]
    # Without synchronous waiting the answer names the run; the run is a normal API run with the caller's key.
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    nodestro.runs.wait_sync = None
    deadline = time.monotonic() + 15
    while (run := nodestro.runs.get(run_id)).status in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert run.status == "succeeded" and run.trigger == "api" and run.trigger_ref == publication["id"]
    assert run.api_key_id
    # Invalid arguments are refused by the API's own request validation.
    bad = rpc(client, token, "tools/call", {"name": publication["slug"], "arguments": {"values": {}}})
    assert bad.json()["result"]["isError"] is True
    assert "request.invalid" in json.dumps(bad.json()["result"])
    unknown = rpc(client, token, "tools/call", {"name": "nope", "arguments": {}})
    assert unknown.json()["result"]["isError"] is True


def test_the_mcp_server_answers_only_this_computer(
    offered: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODESTRO_MCP_SERVER", "1")
    client, _publication, token = offered()
    remote = TestClient(client.app, base_url="http://127.0.0.1", client=("192.0.2.7", 50000))
    answer = remote.post(
        ENDPOINT, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={**HEADERS, **auth(token)}
    )
    assert answer.status_code == 403 and answer.json()["error"]["code"] == "lan.not_enabled"


@pytest.fixture
def served_app(
    offered: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, dict[str, Any], str]]:
    import uvicorn

    monkeypatch.setenv("NODESTRO_MCP_SERVER", "1")
    client, publication, token = offered()
    offer(client, publication)
    limits = {**publication["limits"], "sync_enabled": True}
    patched = client.patch(f"/api/publications/{publication['id']}", json={"limits": limits})
    assert patched.status_code == 200, patched.text
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}{ENDPOINT}", publication, token
    server.should_exit = True
    thread.join(timeout=5)


@needs_sdk
def test_the_official_mcp_client_lists_and_calls_published_workflows(
    served_app: tuple[str, dict[str, Any], str],
) -> None:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    url, publication, token = served_app

    async def session() -> tuple[str, list[str], Any]:
        async with (
            httpx2.AsyncClient(headers=auth(token), timeout=30) as http,
            Client(streamable_http_client(url, http_client=http), mode="auto") as mcp,
        ):
            tools = await mcp.list_tools()
            result = await mcp.call_tool(publication["slug"], {"values": {"customer": "ACME"}})
            return str(mcp.protocol_version), [tool.name for tool in tools.tools], result

    version, names, result = asyncio.run(session())
    assert version
    assert names == [publication["slug"]]
    assert result.is_error is False
    assert "Angebot für ACME" in result.content[0].text
