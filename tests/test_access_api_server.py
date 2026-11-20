"""The access model v2 API on a server (N-06): every route works on the request's tenant only (ADR-0003), and the
writer of a record is the signed-in member."""

from __future__ import annotations

from typing import Any

from nodestro_runtime.tool_approvals import ACCESS_ENV, ENABLE_ENV, AccessRules, Person
from nodestro_runtime.tool_policy import AccessResource
import pytest
from test_worker import connect, schema, server  # noqa: F401  (PostgreSQL server fixtures)

ACCESS = "/api/access"
DENY_BODY = {"resource_type": "workflow", "resource_id": "wf_x", "actions": ["execute"]}


def test_records_of_another_tenant_are_not_found(server: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """On a server every route works on the request's tenant only, and the writer is the signed-in member."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ACCESS_ENV, "1")
    alice = server.member("acme", "alice", "owner")
    mallory = server.member("globex", "mallory", "owner")
    created = alice.post(f"{ACCESS}/denies", json=DENY_BODY)
    assert created.status_code == 201, created.text
    alice_id = alice.get("/api/auth/me").json()["user_id"]
    assert created.json()["denied_by"] == alice_id
    deny_id = created.json()["id"]
    assert mallory.get(f"{ACCESS}/denies").json() == []
    assert mallory.get(f"{ACCESS}/who-may").json() == []
    assert mallory.delete(f"{ACCESS}/denies/{deny_id}").status_code == 404
    viewer = server.member("acme", "victor", "viewer")
    assert viewer.get(f"{ACCESS}/denies").status_code == 403
    assert viewer.delete(f"{ACCESS}/denies/{deny_id}").status_code == 403
    acme = server.app.state.tenants.get("acme")
    assert [item.status for item in AccessRules(acme.store).denies()] == ["active"]
    assert AccessRules(acme.store).revoke_deny(deny_id, by=Person(alice_id)).status == "revoked"
    assert AccessResource(type="workflow", id="wf_x") == AccessRules(acme.store).denies()[0].resource
