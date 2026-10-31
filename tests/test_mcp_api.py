"""The API of connected MCP services (ADR-0007, WP-3): routes, permissions, tokens and tenant isolation.

The desktop cases talk to a local MCP test server on the loopback interface (allowed per server on the desktop
profile only). The MCP SDK needs Python 3.11+; on 3.10 the cases that connect are skipped and the status route reports
``available: false``. The multi-tenant cases need PostgreSQL (see ``test_identity``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import importlib.util
import sys
from typing import Any

from fastapi.testclient import TestClient
import pytest

MakeApp = Callable[..., TestClient]
SDK = importlib.util.find_spec("mcp") is not None
needs_sdk = pytest.mark.skipif(not SDK, reason="the MCP SDK needs Python 3.11+")
TOKEN = "nodestro-secret-token-123456"


def test_the_sdk_is_present_where_it_can_be() -> None:
    # A skipped connection test is not a proof: on 3.11+ the SDK must be installed.
    assert SDK is (sys.version_info >= (3, 11))


@pytest.fixture
def loopback() -> Iterator[Any]:
    from mcp_test_server import serve

    handle = serve()
    yield handle
    handle.stop()


def _add(client: TestClient, url: str, **extra: Any) -> dict[str, Any]:
    answer = client.post("/api/mcp/servers", json={"name": "Docs", "url": url, "allow_loopback": True, **extra})
    assert answer.status_code == 201, answer.text
    return answer.json()  # type: ignore[no-any-return]


def test_the_status_describes_the_desktop_setup(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NODESTRO_VAULT_KEY", raising=False)
    monkeypatch.delenv("NODESTRO_MCP_UNATTENDED", raising=False)
    status = make_app().get("/api/mcp/status").json()
    assert status == {
        "available": SDK,
        "token_vault": False,
        "endpoint_list": None,
        "loopback_possible": True,
        "unattended_enabled": False,
        "write_tools_enabled": False,
        "stdio_possible": False,
    }


def test_servers_are_added_changed_and_removed(make_app: MakeApp) -> None:
    client = make_app()
    assert client.get("/api/mcp/servers").json() == []
    server = _add(client, "http://127.0.0.1:9/mcp")
    assert server["slug"] == "docs" and server["enabled"] is True and server["token_set"] is False
    assert client.get(f"/api/mcp/servers/{server['id']}").json()["name"] == "Docs"
    changed = client.put(
        f"/api/mcp/servers/{server['id']}",
        json={"name": "Docs", "url": "http://127.0.0.1:9/mcp", "allow_loopback": True, "enabled": False},
    )
    assert changed.status_code == 200 and changed.json()["enabled"] is False
    assert client.delete(f"/api/mcp/servers/{server['id']}").status_code == 204
    for method, path in (
        ("GET", f"/api/mcp/servers/{server['id']}"),
        ("DELETE", f"/api/mcp/servers/{server['id']}"),
        ("GET", f"/api/mcp/servers/{server['id']}/tools"),
        ("POST", f"/api/mcp/servers/{server['id']}/discover"),
    ):
        assert client.request(method, path).status_code == 404, path
    kinds = [event["kind"] for event in client.get("/api/mcp/events").json()]
    assert {"server_added", "server_changed", "server_removed"} <= set(kinds)


@pytest.mark.parametrize(
    ("url", "loopback", "code"),
    [
        ("https://127.0.0.1:9/mcp", False, "mcp.address_refused"),
        ("http://127.0.0.1:9/mcp", False, "mcp.https_required"),
        ("http://docs.example.com/mcp", False, "mcp.https_required"),
        ("https://user:pw@docs.example.com/mcp", False, "mcp.url_credentials"),
        ("https://169.254.169.254/mcp", True, "mcp.address_refused"),
        ("ftp://docs.example.com", False, "mcp.url_invalid"),
    ],
)
def test_unsafe_addresses_are_refused(make_app: MakeApp, url: str, loopback: bool, code: str) -> None:
    answer = make_app().post("/api/mcp/servers", json={"name": "x", "url": url, "allow_loopback": loopback})
    assert answer.status_code == 400 and answer.json()["code"] == code, answer.text


def test_tokens_are_write_only_and_kept_for_the_session_without_a_vault(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NODESTRO_VAULT_KEY", raising=False)
    client = make_app()
    server = _add(client, "http://127.0.0.1:9/mcp", token=TOKEN)
    assert server["token_set"] is True and server["token_storage"] == "session"
    for path in ("/api/mcp/servers", f"/api/mcp/servers/{server['id']}", "/api/mcp/events"):
        assert TOKEN not in client.get(path).text, path
    # Without a token in the change the token is kept; an empty token removes it.
    body = {"name": "Docs", "url": "http://127.0.0.1:9/mcp", "allow_loopback": True}
    assert client.put(f"/api/mcp/servers/{server['id']}", json=body).json()["token_set"] is True
    assert client.put(f"/api/mcp/servers/{server['id']}", json={**body, "token": ""}).json()["token_set"] is False


def test_tokens_are_sealed_with_the_desktop_vault_key(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    monkeypatch.setenv("NODESTRO_VAULT_KEY", base64.b64encode(b"k" * 32).decode())
    client = make_app()
    assert client.get("/api/mcp/status").json()["token_vault"] is True
    server = _add(client, "http://127.0.0.1:9/mcp", token=TOKEN)
    assert server["token_storage"] == "vault"
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    stored = ctx.mcp.server(server["id"])
    assert stored.token_sealed and TOKEN not in stored.token_sealed
    assert ctx.mcp.token(stored) == TOKEN


@needs_sdk
def test_discovery_approval_and_the_tool_list(make_app: MakeApp, loopback: Any) -> None:
    client = make_app()
    server = _add(client, loopback.url)
    found = client.post(f"/api/mcp/servers/{server['id']}/discover")
    assert found.status_code == 200, found.text
    result = found.json()
    assert result["protocol_version"] and result["server"]["last_error_code"] is None
    tools = {tool["name"]: tool for tool in result["tools"]}
    assert set(tools) == {"search", "delete_page"}
    search = tools["search"]
    assert search["status"] == "new" and search["usable"] is False and search["refusal"] == "mcp.tool_not_approved"
    assert search["suggested_access"] == "read" and tools["delete_page"]["suggested_access"] == "write"
    # Not approved: agents do not see it.
    assert not [tool for tool in client.get("/api/tools").json() if tool["kind"] == "mcp"]
    # The administrator approves what they reviewed; a different hash is refused.
    stale = client.post(
        f"/api/mcp/servers/{server['id']}/tools/search/decision",
        json={"approve": True, "access": "read", "expected_hash": "0" * 64},
    )
    assert stale.status_code == 400 and stale.json()["code"] == "mcp.definition_moved"
    approved = client.post(
        f"/api/mcp/servers/{server['id']}/tools/search/decision",
        json={"approve": True, "access": "read", "expected_hash": search["hash"]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["usable"] is True and approved.json()["approved_hash"] == search["hash"]
    listed = [tool for tool in client.get("/api/tools").json() if tool["kind"] == "mcp"]
    assert [tool["name"] for tool in listed] == [search["tool_name"]]
    assert listed[0]["server_id"] == server["id"] and listed[0]["definition_hash"] == search["hash"]
    # The tool panel's test call goes through the same checks and reaches the service.
    tried = client.post(f"/api/tools/{search['tool_name']}/run", json={"input": {"query": "nodes"}})
    assert tried.status_code == 200, tried.text
    assert tried.json()["is_error"] is False and "results for nodes" in tried.json()["output"]
    # A tool that changes data stays unusable until per-call approval exists (WP-5).
    write = client.post(
        f"/api/mcp/servers/{server['id']}/tools/delete_page/decision", json={"approve": True, "access": "write"}
    ).json()
    assert write["usable"] is False and write["refusal"] == "mcp.write_not_supported"
    # Rejecting takes the tool away again.
    rejected = client.post(f"/api/mcp/servers/{server['id']}/tools/search/decision", json={"approve": False}).json()
    assert rejected["status"] == "rejected" and rejected["usable"] is False
    assert (
        client.post(f"/api/mcp/servers/{server['id']}/tools/missing/decision", json={"approve": True}).status_code
        == 404
    )
    kinds = [event["kind"] for event in client.get("/api/mcp/events").json()]
    assert "tool_approved" in kinds and "tool_rejected" in kinds


@needs_sdk
def test_an_unreachable_server_is_reported_with_a_stable_code(make_app: MakeApp) -> None:
    from mcp_test_server import _free_port

    client = make_app()
    server = _add(client, f"http://127.0.0.1:{_free_port()}/mcp")
    answer = client.post(f"/api/mcp/servers/{server['id']}/discover")
    assert answer.status_code == 400 and answer.json()["code"] == "mcp.unreachable", answer.text
    assert client.get(f"/api/mcp/servers/{server['id']}").json()["last_error_code"] == "mcp.unreachable"


@pytest.fixture
def oauth_servers() -> Iterator[tuple[Any, Any]]:
    from mcp_oauth_test_server import serve_auth_server, serve_protected

    auth, runner = serve_auth_server()
    protected = serve_protected(auth)
    yield auth, protected
    protected.stop()
    runner.should_exit = True


@needs_sdk
def test_an_oauth_server_is_signed_in_through_the_browser_callback(
    make_app: MakeApp, oauth_servers: tuple[Any, Any]
) -> None:
    from urllib.parse import parse_qs, urlsplit

    auth, protected = oauth_servers
    client = make_app()
    server = _add(client, protected.url, auth="oauth")
    assert client.post(f"/api/mcp/servers/{server['id']}/discover").json()["code"] == "mcp.unauthorized"

    started = client.post(f"/api/mcp/servers/{server['id']}/oauth/start")
    assert started.status_code == 200, started.text
    url = started.json()["authorization_url"]
    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert query["redirect_uri"] == "http://127.0.0.1/api/mcp/oauth/callback"  # the desktop's own loopback address
    assert query["code_challenge_method"] == "S256" and query["resource"] == protected.url

    callback = auth.authorize(url)  # what the browser does at the authorization server
    back = client.get("/api/mcp/oauth/callback", params=callback, follow_redirects=False)
    assert back.status_code == 303
    assert back.headers["location"] == f"/tools?tab=services&oauth=connected&server={server['id']}"
    assert back.headers["referrer-policy"] == "no-referrer" and "code" not in back.headers["location"]
    view = client.get(f"/api/mcp/servers/{server['id']}").json()
    assert view["token_set"] is True and view["oauth"]["issuer"] == auth.issuer
    access = next(iter(auth.access_tokens))
    assert access not in str(view) and access not in client.get("/api/mcp/events").text
    discovered = client.post(f"/api/mcp/servers/{server['id']}/discover")
    assert discovered.status_code == 200 and {tool["name"] for tool in discovered.json()["tools"]} >= {"search"}

    # A replayed or forged callback changes nothing and says why.
    again = client.get("/api/mcp/oauth/callback", params=callback, follow_redirects=False)
    assert again.headers["location"] == "/tools?tab=services&oauth_error=mcp.oauth_state_invalid"
    denied = client.get("/api/mcp/oauth/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert denied.headers["location"] == "/tools?tab=services&oauth_error=mcp.oauth_denied"

    signed_out = client.post(f"/api/mcp/servers/{server['id']}/oauth/sign-out").json()
    assert signed_out["token_set"] is False and signed_out["oauth"] == {}


def test_the_oauth_routes_refuse_what_they_must(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_app()
    plain = _add(client, "http://127.0.0.1:9/mcp")
    refused = client.post(f"/api/mcp/servers/{plain['id']}/oauth/start")
    assert refused.status_code == 400 and refused.json()["code"] == "mcp.oauth_not_configured"
    assert client.post("/api/mcp/servers/missing/oauth/start").status_code == 404
    # On a desktop reached under another (allowed) host name, the sign-in is refused: the callback must be the
    # loopback address, never one taken from a Host header.
    monkeypatch.setenv("NODESTRO_ALLOWED_HOSTS", "nodestro.localhost")
    foreign = make_app()
    foreign.base_url = "http://nodestro.localhost"  # type: ignore[assignment]
    server = _add(foreign, "http://127.0.0.1:9/mcp", auth="oauth")
    answer = foreign.post(f"/api/mcp/servers/{server['id']}/oauth/start")
    assert answer.status_code == 400 and answer.json()["code"] == "mcp.oauth_redirect_invalid"


def test_a_server_signs_in_with_its_configured_origin_not_the_host_header() -> None:
    from types import SimpleNamespace

    from nodestro_api.api_mcp import _redirect_uri

    request: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(public_origin="https://nodestro.example.com")),
        base_url=SimpleNamespace(hostname="evil.example", scheme="https", netloc="evil.example"),
    )
    assert _redirect_uri(request) == "https://nodestro.example.com/api/mcp/oauth/callback"
