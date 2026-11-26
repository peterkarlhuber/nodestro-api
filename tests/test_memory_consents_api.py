"""The owner's consent before a shared workflow uses a private memory space, and private runs in every channel
(RT-089d; needs PostgreSQL).

A workflow may use a person's memory space only after that person allowed exactly its content
(``/api/memory-consents``): a missing, changed or revoked consent fails closed with ``memory.consent_required``, and
nobody but the owner can give one. A run private to a person's memory tells nobody else about itself: not the inbox
(``/api/notifications``, list and unread count), not the workflow list's last status, not the usage report's workflow
names. The event stream's snapshot is in ``test_memory_private_streams.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from fastapi.testclient import TestClient
from test_conversation_owners import command_schema, process_logging  # noqa: F401  (fixtures)
from test_identity import Server, _wait, database  # noqa: F401  (fixture)
from test_memory_space_owners import (  # noqa: F401
    AUDIT_NOTE,
    AUDIT_SPACE,
    _memory_workflow,
    allow_workflow,
    scripted_server,
)

from nodestro_api import access_audit

MARKER = "zanzibar-ledger"


def _note(client: TestClient) -> None:
    stored = client.put(f"/api/memory/{AUDIT_SPACE}/file", json={"path": AUDIT_NOTE, "content": MARKER})
    assert stored.status_code == 200, stored.text


def _run(client: TestClient, workflow_id: str) -> dict[str, Any]:
    started = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "go"})
    assert started.status_code == 201, started.text
    return _wait(client, str(started.json()["id"]))


def _agent_error(run: dict[str, Any]) -> str | None:
    return run["nodes"]["agent"].get("error_code")  # type: ignore[no-any-return]


def _user(client: TestClient) -> str:
    return str(client.get("/api/auth/me").json()["user_id"])


def test_poc_a_shared_workflow_uses_alices_memory_only_with_her_consent(scripted_server: Server) -> None:  # noqa: F811
    """PoC (RT-089d): Bob, an editor, builds a workflow that names Alice's space. Before, Alice's run of it read her
    memory into Bob's workflow. Now her run fails with ``memory.consent_required`` before any model call; after her
    consent it runs; Bob's next change is not covered, and her run of the changed workflow fails closed again."""
    alice = scripted_server.member("acme", "alice", "editor")
    bob = scripted_server.member("acme", "bob", "editor")
    _note(alice)
    workflow_id = _memory_workflow(bob, AUDIT_SPACE)
    refused = _run(alice, workflow_id)
    assert refused["status"] == "failed" and _agent_error(refused) == "memory.consent_required"
    assert MARKER not in str(refused) and refused["usage"]["requests"] == 0
    preview = alice.get("/api/memory-consents/preview", params={"workflow_id": workflow_id})
    assert preview.status_code == 200, preview.text
    digest = preview.json()["digest"]
    allowed = alice.post(
        "/api/memory-consents", json={"space": AUDIT_SPACE, "workflow_id": workflow_id, "digest": digest}
    )
    assert allowed.status_code == 201, allowed.text
    (consent,) = allowed.json()
    shown = preview.json()["targets"][0]["digest"]  # each consent records its workflow's own digest
    assert (consent["space"], consent["workflow_id"], consent["digest"]) == (AUDIT_SPACE, workflow_id, shown)
    succeeded = _run(alice, workflow_id)
    assert succeeded["status"] == "succeeded" and MARKER in str(succeeded)
    # Bob changes the workflow: the consent does not cover the new content.
    current = bob.get(f"/api/workflows/{workflow_id}").json()
    current["nodes"][1]["agent"]["tools"] = ["calculator"]
    assert bob.put(f"/api/workflows/{workflow_id}", json=current).status_code == 200
    again = _run(alice, workflow_id)
    assert _agent_error(again) == "memory.consent_required" and MARKER not in str(again)
    # A consent for what Alice was shown before is refused now: she must review the changed workflow.
    body = {"space": AUDIT_SPACE, "workflow_id": workflow_id, "digest": digest}
    stale = alice.post("/api/memory-consents", json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "memory.consent_stale"


def test_only_the_owner_gives_lists_and_revokes_a_consent_and_each_is_audited(
    scripted_server: Server,  # noqa: F811
) -> None:
    alice = scripted_server.member("acme", "alice", "editor")
    bob = scripted_server.member("acme", "bob", "admin")
    _note(alice)
    workflow_id = _memory_workflow(alice, AUDIT_SPACE)
    # Bob (an administrator) cannot allow anything for Alice's space: as for a space that does not exist.
    refused = allow_workflow(bob, workflow_id)
    assert refused.status_code == 404 and refused.json()["code"] == "memory.space_not_found"
    digest = "0" * 64
    unknown = alice.post(
        "/api/memory-consents", json={"space": AUDIT_SPACE, "workflow_id": "wf_unknown", "digest": digest}
    )
    assert unknown.status_code == 404
    both = alice.post(
        "/api/memory-consents",
        json={
            "space": AUDIT_SPACE,
            "workflow_id": workflow_id,
            "agent": {"name": "A", "system_prompt": "x"},
            "digest": digest,
        },
    )
    assert both.status_code == 422
    # Review F2: a grant names the digest the owner was shown; without one it is refused, never granted unseen.
    unseen = alice.post("/api/memory-consents", json={"space": AUDIT_SPACE, "workflow_id": workflow_id})
    assert unseen.status_code == 422
    wrong = alice.post(
        "/api/memory-consents", json={"space": AUDIT_SPACE, "workflow_id": workflow_id, "digest": digest}
    )
    assert wrong.status_code == 409 and wrong.json()["code"] == "memory.consent_stale"
    agent = {"name": "A", "system_prompt": "x", "memory": {"mode": "persistent", "space": AUDIT_SPACE}}
    stale_agent = alice.post("/api/memory-consents", json={"space": AUDIT_SPACE, "agent": agent, "digest": digest})
    assert stale_agent.status_code == 409 and stale_agent.json()["code"] == "memory.consent_stale"
    assert alice.get("/api/memory-consents").json() == []
    (consent,) = allow_workflow(alice, workflow_id).json()
    assert [item["id"] for item in alice.get("/api/memory-consents").json()] == [consent["id"]]
    assert bob.get("/api/memory-consents").json() == []
    assert bob.delete(f"/api/memory-consents/{consent['id']}").status_code == 404
    assert alice.delete(f"/api/memory-consents/{consent['id']}").status_code == 204
    assert alice.get("/api/memory-consents").json() == []
    assert _agent_error(_run(alice, workflow_id)) == "memory.consent_required"
    store = scripted_server.app.state.tenants.get("acme").store
    granted = access_audit.events(store, kind="memory_consent_granted")
    revoked = access_audit.events(store, kind="memory_consent_revoked")
    assert [(event.actor, event.target_id, event.details["space"]) for event in granted + revoked] == [
        (_user(alice), _user(alice), AUDIT_SPACE)
    ] * 2
    assert MARKER not in str([event.model_dump() for event in granted + revoked])


def test_a_grant_is_refused_when_any_workflow_it_covers_changed_since_the_preview(
    scripted_server: Server,  # noqa: F811
) -> None:
    """Review F2: before, the shown digest was compared with the top-level workflow's only, so a sub-workflow changed
    between the preview and the grant was allowed unseen. Now the preview's digest covers every workflow of the
    consent, and any change of any of them is refused with ``409 memory.consent_stale``."""
    alice = scripted_server.member("acme", "alice", "editor")
    bob = scripted_server.member("acme", "bob", "editor")
    _note(alice)
    child_id = _memory_workflow(alice, AUDIT_SPACE)
    parent = {
        "name": "Parent",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "sub", "type": "subworkflow", "subworkflow": {"workflow_id": child_id}},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "sub"}],
    }
    created = alice.post("/api/workflows", json=parent)
    assert created.status_code == 201, created.text
    parent_id = created.json()["id"]
    preview = alice.get("/api/memory-consents/preview", params={"workflow_id": parent_id}).json()
    assert [target["workflow_id"] for target in preview["targets"]] == [parent_id, child_id]
    assert preview["digest"] not in {target["digest"] for target in preview["targets"]}  # one over all of them
    child = bob.get(f"/api/workflows/{child_id}").json()
    child["nodes"][1]["agent"]["tools"] = ["calculator"]
    assert bob.put(f"/api/workflows/{child_id}", json=child).status_code == 200
    body = {"space": AUDIT_SPACE, "workflow_id": parent_id, "digest": preview["digest"]}
    stale = alice.post("/api/memory-consents", json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "memory.consent_stale"
    assert alice.get("/api/memory-consents").json() == []
    assert allow_workflow(alice, parent_id).status_code == 201  # after reviewing it again


def test_a_private_run_is_left_out_of_other_members_inbox_workflow_list_and_usage_names(
    scripted_server: Server,  # noqa: F811
) -> None:
    """Requirement 2 (RT-089d): before, the inbox told every member about Alice's private run (its workflow's name and
    status), the workflow list showed its status, and the usage report its workflow. Now only Alice learns of it."""
    from nodestro_runtime.notifications import Notification, notification_id

    alice = scripted_server.member("acme", "alice", "editor")
    bob = scripted_server.member("acme", "bob", "admin")
    _note(alice)
    workflow_id = _memory_workflow(alice, AUDIT_SPACE)
    flow = alice.get(f"/api/workflows/{workflow_id}").json()
    flow["notifications"] = {"events": ["run_finished"], "recipients": "members"}
    assert alice.put(f"/api/workflows/{workflow_id}", json=flow).status_code == 200
    allowed = allow_workflow(alice, workflow_id)
    assert allowed.status_code == 201
    run = _run(alice, workflow_id)
    assert run["status"] == "succeeded"
    workspace = scripted_server.app.state.tenants.get("acme")
    # A message written for every member before the run became private (or by an earlier version) is hidden too.
    key = "nodestro:notify:approval_pending:legacy"
    workspace.inbox.add(
        Notification(id=notification_id(key), key=key, kind="approval_pending", run_id=run["id"], workflow_name="Notes")
    )

    def inbox(client: TestClient) -> tuple[list[str | None], int]:
        page = client.get("/api/notifications").json()
        return [item.get("run_id") for item in page["items"]], page["unread"]

    assert inbox(alice) == ([run["id"], run["id"]], 2)
    assert inbox(bob) == ([], 0)

    def status(client: TestClient) -> Any:
        listed = client.get("/api/workflows").json()
        return next(item for item in listed if item["id"] == workflow_id)["last_run_status"]

    assert (status(alice), status(bob)) == ("succeeded", None)

    def names(client: TestClient) -> set[str]:
        return {bucket["key"] for bucket in client.get("/api/usage").json()["by_workflow"]}

    assert workflow_id in names(alice) and "private_runs" not in names(alice)
    assert names(bob) == {"private_runs"}
    assert bob.get("/api/usage").json()["total"]["runs"] == alice.get("/api/usage").json()["total"]["runs"]


def test_consents_exist_only_for_members_of_a_server(scripted_server: Server) -> None:  # noqa: F811
    from test_conversation_access_audit import MEMBER_PERMISSIONS

    from nodestro_api.permissions import Principal

    principal = Principal(id="svc_reporting", permissions=MEMBER_PERMISSIONS, tenant_id="acme")
    scripted_server.member("acme", "alice", "owner")
    resolve = scripted_server.app.state.principal_resolver
    scripted_server.app.state.principal_resolver = lambda connection: (
        principal if connection.headers.get("x-test-no-role") else resolve(connection)
    )
    nobody = scripted_server.browser()
    nobody.headers["x-test-no-role"] = "1"
    answer = nobody.get("/api/memory-consents")
    assert answer.status_code == 404 and answer.json()["code"] == "memory.consent_unavailable"
