"""MCP management on the multi-tenant server: role permissions, tenant isolation and the egress list (needs PostgreSQL).

Uses the server fixtures of ``test_identity`` (stub OpenID Connect provider, one PostgreSQL schema per test). Nothing
here connects to an MCP server: routes of another tenant answer 404 before any connection is made.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

import httpx
from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.server_settings import SecretBox
from nodestro_runtime.storage_postgres import PostgresDatabase
import stub_oidc
from test_identity import Server, database  # noqa: F401  (fixture)

from nodestro_api.identity import OidcClient, OidcSettings
from nodestro_api.server_app import TenantComposition, create_tenant_app

URL = "https://mcp.example.com/mcp"
TOKEN = "tenant-secret-token-987654"


@pytest.fixture(autouse=True)
def endpoints(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NODESTRO_MCP_ENDPOINTS", "https://mcp.example.com")
    monkeypatch.delenv("NODESTRO_MCP_UNATTENDED", raising=False)
    yield


def _server(db: PostgresDatabase, data_root: Path, secrets: SecretBox | None) -> Iterator[Server]:
    provider = stub_oidc.StubProvider()
    oidc = OidcClient(
        OidcSettings(
            issuer=stub_oidc.ISSUER,
            client_id=stub_oidc.CLIENT_ID,
            client_secret=stub_oidc.CLIENT_SECRET,
            redirect_uri=stub_oidc.REDIRECT_URI,
        ),
        http=httpx.Client(transport=provider.transport()),
    )
    identity = IdentityStore(db)
    app = create_tenant_app(
        TenantComposition(
            database=db,
            identity=identity,
            oidc=oidc,
            origin=stub_oidc.ORIGIN,
            data_root=data_root,
            secrets=secrets,
            integrations=[],
        )
    )
    built = Server(app, identity, provider, [])
    yield built
    for client in built.clients:
        client.__exit__(None, None, None)


@pytest.fixture
def server(database: PostgresDatabase, tmp_path: Path) -> Iterator[Server]:  # noqa: F811
    """A server with its secret key (``NODESTRO_SECRET_KEY`` in production)."""
    yield from _server(database, tmp_path / "server", SecretBox(b"s" * 32))


@pytest.fixture
def keyless(database: PostgresDatabase, tmp_path: Path) -> Iterator[Server]:  # noqa: F811
    yield from _server(database, tmp_path / "keyless", None)


def _add(browser: TestClient, **extra: object) -> dict[str, object]:
    answer = browser.post("/api/mcp/servers", json={"name": "Docs", "url": URL, **extra})
    assert answer.status_code == 201, answer.text
    return answer.json()  # type: ignore[no-any-return]


# role: (read, admin)
ROLES = {
    "viewer": (True, False),
    "operator": (True, False),
    "editor": (True, False),
    "admin": (True, True),
    "owner": (True, True),
}


@pytest.mark.parametrize("role", list(ROLES))
def test_reading_needs_read_and_managing_needs_admin(server: Server, role: str) -> None:
    owner = server.member("acme", "acme-boss", "owner")
    existing = _add(owner)
    browser = server.member("acme", f"user-{role}", role)
    can_read, can_admin = ROLES[role]
    sid = existing["id"]
    for path in ("/api/mcp/status", "/api/mcp/servers", f"/api/mcp/servers/{sid}", f"/api/mcp/servers/{sid}/tools"):
        assert (browser.get(path).status_code == 200) is can_read, path
    changes = [
        ("POST", "/api/mcp/servers", {"name": "Other", "url": URL}),
        ("PUT", f"/api/mcp/servers/{sid}", {"name": "Docs", "url": URL, "enabled": False}),
        ("POST", f"/api/mcp/servers/{sid}/tools/search/decision", {"approve": True, "access": "read"}),
        ("GET", "/api/mcp/events", None),
        ("DELETE", f"/api/mcp/servers/{sid}", None),
    ]
    for method, path, body in changes:
        answer = browser.request(method, path, json=body)
        if can_admin:
            assert answer.status_code != 403, (method, path, answer.text)
        else:
            assert answer.status_code == 403, (method, path, answer.text)
            assert answer.json()["params"] == {"permission": "admin"}
    # Discovery connects out, so it is an administrator's action too (checked without connecting).
    if not can_admin:
        assert browser.post(f"/api/mcp/servers/{sid}/discover").status_code == 403


def test_no_tenant_sees_or_uses_another_tenants_servers(server: Server) -> None:
    alice = server.member("acme", "alice", "owner")
    bob = server.member("globex", "bob", "owner")
    added = _add(alice, token=TOKEN)
    assert added["token_storage"] == "vault" and added["token_set"] is True
    sid = added["id"]
    assert bob.get("/api/mcp/servers").json() == []
    assert bob.get("/api/mcp/events").json() == []
    hidden = [
        ("GET", f"/api/mcp/servers/{sid}", None),
        ("PUT", f"/api/mcp/servers/{sid}", {"name": "x", "url": URL}),
        ("DELETE", f"/api/mcp/servers/{sid}", None),
        ("POST", f"/api/mcp/servers/{sid}/discover", None),
        ("GET", f"/api/mcp/servers/{sid}/tools", None),
        ("POST", f"/api/mcp/servers/{sid}/tools/search/decision", {"approve": True, "access": "read"}),
    ]
    for method, path, body in hidden:
        answer = bob.request(method, path, json=body)
        assert answer.status_code == 404, (method, path, answer.status_code, answer.text[:200])
    # Alice's server and token are untouched; no response carries the token.
    mine = alice.get(f"/api/mcp/servers/{sid}")
    assert mine.status_code == 200 and mine.json()["token_set"] is True
    for browser in (alice, bob):
        for path in ("/api/mcp/servers", "/api/mcp/events", "/api/tools"):
            assert TOKEN not in browser.get(path).text, path
    # Tokens are sealed per tenant: the other tenant's workspace cannot open the sealed value.
    tenants = server.app.state.tenants
    acme, globex = tenants.get("acme"), tenants.get("globex")
    sealed = acme.mcp.server(sid)
    assert TOKEN not in (sealed.token_sealed or "")
    assert acme.mcp.token(sealed) == TOKEN
    assert globex.mcp.vault.open(sid, sealed.token_sealed) is None


def test_the_server_profile_needs_the_operators_endpoint_list(server: Server) -> None:
    alice = server.member("acme", "alice", "owner")
    status = alice.get("/api/mcp/status").json()
    assert status["endpoint_list"] == ["https://mcp.example.com:443"] and status["loopback_possible"] is False
    refused = alice.post("/api/mcp/servers", json={"name": "x", "url": "https://other.example.com/mcp"})
    assert refused.status_code == 400 and refused.json()["code"] == "mcp.endpoint_not_allowed"
    loopback = alice.post(
        "/api/mcp/servers", json={"name": "x", "url": "http://127.0.0.1:8000/mcp", "allow_loopback": True}
    )
    assert loopback.status_code == 400 and loopback.json()["code"] in {"mcp.https_required", "mcp.address_refused"}


def test_without_a_server_key_tokens_are_never_stored(keyless: Server) -> None:
    alice = keyless.member("acme", "alice", "owner")
    assert alice.get("/api/mcp/status").json()["token_vault"] is False
    added = _add(alice, token=TOKEN)
    assert added["token_storage"] == "session" and added["token_set"] is True
    stored = keyless.app.state.tenants.get("acme").mcp.server(added["id"])
    assert stored.token_sealed is None
