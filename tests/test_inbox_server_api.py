"""The local inbox on the multi-tenant server (NAL-A3): tenant isolation and per-member read state (needs PostgreSQL).

Uses the server fixtures of ``test_identity`` and ``test_mcp_server_api``. Axentra's inbox is off here, so each
tenant's workspace keeps its own local inbox.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from nodestro_runtime.notifications import Notification, notification_id, notification_key
from nodestro_runtime.storage_postgres import PostgresDatabase
from test_identity import Server, database  # noqa: F401  (fixture)
from test_mcp_server_api import _server


@pytest.fixture
def server(database: PostgresDatabase, tmp_path: Path) -> Iterator[Server]:  # noqa: F811
    yield from _server(database, tmp_path / "server", None)


def _notify(server: Server, tenant: str, record: str, user_id: str | None = None) -> str:
    inbox = server.app.state.tenants.get(tenant).inbox
    assert inbox is not None
    key = notification_key("run_failed", record)
    item = Notification(id=notification_id(key), key=key, kind="run_failed", run_id=record, user_id=user_id)
    assert inbox.add(item)
    return item.id


def test_no_tenant_sees_or_marks_another_tenants_notifications(server: Server) -> None:
    alice = server.member("acme", "alice", "viewer")
    bob = server.member("globex", "bob", "owner")
    mine = _notify(server, "acme", "run_acme")
    assert [item["id"] for item in alice.get("/api/notifications").json()["items"]] == [mine]
    assert bob.get("/api/notifications").json() == {"items": [], "unread": 0, "next_cursor": None}
    assert bob.post("/api/notifications/read", json={"ids": [mine]}).json() == {"marked": 0}
    assert bob.post("/api/notifications/read", json={}).json() == {"marked": 0}
    assert alice.get("/api/notifications").json()["unread"] == 1


def test_read_state_and_personal_messages_belong_to_one_member(server: Server) -> None:
    alice = server.member("acme", "alice", "editor")
    carol = server.member("acme", "carol", "operator")
    shared = _notify(server, "acme", "run_shared")
    assert alice.post("/api/notifications/read", json={"ids": [shared]}).json() == {"marked": 1}
    assert alice.get("/api/notifications").json()["unread"] == 0
    assert carol.get("/api/notifications").json()["unread"] == 1
    # A message for one member (``starter``) is only theirs.
    alice_user = next(m.user_id for m in server.identity.members("acme") if m.role == "editor")
    personal = _notify(server, "acme", "run_personal", user_id=alice_user)
    assert personal in [item["id"] for item in alice.get("/api/notifications").json()["items"]]
    assert personal not in [item["id"] for item in carol.get("/api/notifications").json()["items"]]
    assert carol.post("/api/notifications/read", json={"ids": [personal]}).json() == {"marked": 0}
