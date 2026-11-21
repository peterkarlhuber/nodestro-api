"""Who sees what of a conversation with an own agent on a PostgreSQL server (W3-S1 audit; the desktop app with several
principals: ``test_conversation_access_audit.py``, whose route scans these tests reuse).

Two members of one tenant (the owner and an administrator), a viewer, a member of another tenant, and a principal
without a role in the tenant. The answer runs in a worker, as on every server. Only the owner sees the conversation's
title, messages, file, answer and run; everybody else gets nothing of it on any route."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.conversations import CONVERSATIONS_ENV
import pytest
from test_conversation_access_audit import (
    BASE,
    MEMBER_PERMISSIONS,
    SECRET,
    leaking_reads,
    make_owned,
    reachable_by_id,
)
from test_identity import _wait
from test_worker import Background, connect, make_worker, schema, server  # noqa: F401  (PostgreSQL fixtures)

from nodestro_api.permissions import Principal

from conftest import text, tool_use

#: The request header that makes a browser of the test the principal without a role.
_NO_ROLE = "x-test-no-role"


def _no_role_browser(server: Any, tenant: str) -> TestClient:  # noqa: F811
    """A browser whose principal has no role in ``tenant`` (no member: a service, a machine). The server's own
    resolver never builds one today; a later one (service tokens) must not see conversations either."""
    principal = Principal(id="svc_reporting", permissions=MEMBER_PERMISSIONS, tenant_id=tenant)
    resolve = server.app.state.principal_resolver
    server.app.state.principal_resolver = lambda connection: (
        principal if connection.headers.get(_NO_ROLE) else resolve(connection)
    )
    browser: TestClient = server.browser()
    browser.headers[_NO_ROLE] = "1"
    return browser


def test_two_members_and_a_principal_without_a_role_see_nothing_of_anothers_conversation(
    server: Any,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    alice = server.member("acme", "alice", "owner")
    bob = server.member("acme", "bob", "admin")
    victor = server.member("acme", "victor", "viewer")
    mallory = server.member("globex", "mallory", "owner")
    owned = make_owned(alice)
    worker = make_worker(
        {"AGENT": [tool_use("calculator", {"expression": "6 * 7"}), text(f"It is 42. {SECRET} answer")]}
    )
    job = worker.call(worker.worker.runner.run_once())
    assert job is not None and job.ref == owned.run_id
    finished = _wait(alice, owned.run_id)
    assert finished["status"] == "succeeded" and SECRET in finished["output"], finished["status"]
    assert finished["trigger"] == "conversation"

    # The owner sees it everywhere the scan looks (the scan is a real probe).
    seen_by_owner = {path for path, _status in leaking_reads(alice, owned)}
    assert {
        "/api/conversations",
        "/api/conversations/{conversation_id}",
        "/api/runs/{run_id}",
        "/api/inputs/{file_id}/content",
        "/api/search",
        "/api/assistant/conversations",
    } <= seen_by_owner, seen_by_owner

    nobody = _no_role_browser(server, "acme")
    assert nobody.get(BASE).status_code == 200  # the principal reaches the routes, and gets nothing
    for viewer in (bob, victor, mallory, nobody):
        assert leaking_reads(viewer, owned) == []
        assert reachable_by_id(viewer, owned) == []
    # The principal without a role starts no conversation (none would be shared by every such principal).
    refused = nobody.post(BASE, json={"agent": {"template_id": "tpl_x"}, "model": "anthropic:claude-sonnet-5-5"})
    assert (refused.status_code, refused.json()["code"]) == (403, "access.person_required")
    refused = nobody.post("/api/assistant/conversations", json={})
    assert (refused.status_code, refused.json()["code"]) == (403, "access.person_required")
    # The administrator's activity log names the answer, without contents.
    events = bob.get("/api/activity", params={"run_id": owned.run_id}).json()["events"]
    assert events and SECRET not in str(events)
    # Nothing was changed by the others.
    detail = alice.get(f"{BASE}/{owned.conversation_id}")
    assert detail.status_code == 200 and len(detail.json()["messages"]) == 2
    assert alice.get(f"/api/inputs/{owned.file_id}/content").text == f"{SECRET} file body"
    assert [item["id"] for item in alice.get("/api/assistant/conversations").json()] == [owned.assistant_id]
