"""Service connections on the multi-tenant server: role permissions and tenant isolation (needs PostgreSQL).

Uses the server fixtures of ``test_identity`` and ``test_mcp_server_api`` (stub OpenID Connect provider, one
PostgreSQL schema per test). Nothing here contacts a service (ADR-0001 checks are offline).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from nodestro_runtime.server_settings import SecretBox
from nodestro_runtime.storage_postgres import PostgresDatabase
from test_identity import Server, database  # noqa: F401  (fixture)
from test_mcp_server_api import _server

SECRET = "tenant-connection-secret-31c7"
SMTP = {"type": "smtp", "name": "Mail", "settings": {"host": "smtp.example.com", "from_address": "bot@example.com"}}


@pytest.fixture
def server(database: PostgresDatabase, tmp_path: Path) -> Iterator[Server]:  # noqa: F811
    yield from _server(database, tmp_path / "server", SecretBox(b"s" * 32))


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
    made = owner.post("/api/connections", json=SMTP)
    assert made.status_code == 201, made.text
    cid = made.json()["id"]
    browser = server.member("acme", f"user-{role}", role)
    can_read, can_admin = ROLES[role]
    for path in ("/api/connections/status", "/api/connections/types", "/api/connections", f"/api/connections/{cid}"):
        assert (browser.get(path).status_code == 200) is can_read, path
    assert (browser.get("/api/tools/builtin").status_code == 200) is can_read
    changes = [
        ("POST", "/api/connections", {**SMTP, "name": "Other"}),
        ("PATCH", f"/api/connections/{cid}", {"description": "x"}),
        ("PUT", f"/api/connections/{cid}/secret", {"secret": SECRET}),
        ("POST", f"/api/connections/{cid}/check", None),
        ("GET", "/api/connections/events", None),
        ("DELETE", f"/api/connections/{cid}", None),
    ]
    for method, path, body in changes:
        answer = browser.request(method, path, json=body)
        if can_admin:
            assert answer.status_code != 403, (method, path, answer.text)
        else:
            assert answer.status_code == 403, (method, path, answer.text)
            assert answer.json()["params"] == {"permission": "admin"}


def test_no_tenant_sees_or_uses_another_tenants_connections(server: Server) -> None:
    alice = server.member("acme", "alice", "owner")
    bob = server.member("globex", "bob", "owner")
    made = alice.post("/api/connections", json=SMTP).json()
    cid = made["id"]
    stored = alice.put(f"/api/connections/{cid}/secret", json={"secret": SECRET}).json()
    assert stored["secret_set"] is True and stored["secret_storage"] == "vault"
    assert alice.get("/api/connections/status").json()["mode"] == "server"
    assert bob.get("/api/connections").json() == []
    assert bob.get("/api/connections/events").json() == []
    hidden = [
        ("GET", f"/api/connections/{cid}", None),
        ("PATCH", f"/api/connections/{cid}", {"description": "x"}),
        ("PUT", f"/api/connections/{cid}/secret", {"secret": "other"}),
        ("POST", f"/api/connections/{cid}/check", None),
        ("DELETE", f"/api/connections/{cid}", None),
    ]
    for method, path, body in hidden:
        answer = bob.request(method, path, json=body)
        assert answer.status_code == 404, (method, path, answer.status_code, answer.text[:200])
    mine = alice.get(f"/api/connections/{cid}")
    assert mine.status_code == 200 and mine.json()["secret_set"] is True
    for browser in (alice, bob):
        for path in ("/api/connections", "/api/connections/events", f"/api/connections/{cid}"):
            assert SECRET not in browser.get(path).text, path
    # Secrets are sealed per tenant: the other tenant's vault cannot open the sealed value.
    tenants = server.app.state.tenants
    acme, globex = tenants.get("acme"), tenants.get("globex")
    sealed = acme.connections.get(cid)
    assert sealed.secret_sealed and SECRET not in sealed.secret_sealed
    assert acme.connections.resolve(cid).secret == SECRET
    assert globex.connections.vault.open(cid, sealed.secret_sealed) is None


def test_the_server_profile_refuses_loopback_addresses(server: Server) -> None:
    alice = server.member("acme", "alice", "owner")
    refused = alice.post(
        "/api/connections", json={"type": "webhook", "name": "Local", "settings": {"url": "http://127.0.0.1:9/x"}}
    )
    assert refused.status_code == 400 and refused.json()["code"] == "connection.address_invalid"
