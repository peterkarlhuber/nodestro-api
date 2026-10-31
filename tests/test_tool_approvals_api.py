"""The API of per-call tool approvals (S-2b, decision D10): deciding, the inbox, pre-grants, the audit trail, the
activation step (published releases included), permissions and tenant isolation."""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.tool_approvals import ENABLE_ENV, ToolApprovals
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from nodestro_runtime.tool_policy import ToolCapability
import pytest
from test_worker import connect, schema, server  # noqa: F401  (PostgreSQL server fixtures)

from nodestro_api.permissions import Permission, Principal

from conftest import text, tool_use

TOOL = "celsius_to_fahrenheit"
WRITE = ToolCapability(access="write", reach="external", source="default")


def flow(name: str = "Approvals") -> dict[str, Any]:
    return {
        "name": name,
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "agent", "agent": {"name": "Agent", "system_prompt": "AGENT", "tools": [TOOL]}},
            {"id": "out", "type": "output"},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
    }


def with_tool(client: TestClient) -> Any:
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    result, _ = ctx.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, TOOL, trust=ctx.tools.editor_trust(TOOL, CUSTOM_TOOL_TEMPLATE)
    )
    assert result.ok, result
    return ctx


@pytest.fixture
def gated(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(ENABLE_ENV, "1")
    client = make_app({"AGENT": [tool_use(TOOL, {"celsius": 100}), text("done")]})
    ctx = with_tool(client)
    assert ctx.runs.tool_gate is not None
    ctx.runs.tool_gate.approvals.poll_seconds = 0.02
    return client


def start(client: TestClient) -> str:
    workflow = client.post("/api/workflows", json=flow())
    assert workflow.status_code == 201, workflow.text
    run = client.post(f"/api/workflows/{workflow.json()['id']}/runs", json={"input": "x"})
    assert run.status_code == 201, run.text
    return str(run.json()["id"])


def waiting(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        found = [item for item in client.get("/api/tool-approvals").json() if item["run_id"] == run_id]
        if found:
            return found[0]
        time.sleep(0.02)
    raise AssertionError("no approval request")


def finished(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("the run did not finish")


def test_a_person_approves_a_call_and_the_run_goes_on(gated: TestClient) -> None:
    run_id = start(gated)
    request = waiting(gated, run_id)
    assert request["tool"] == TOOL and request["input"] == {"celsius": 100} and request["status"] == "pending"
    assert gated.get(f"/api/runs/{run_id}/tool-approvals").json()[0]["id"] == request["id"]
    answer = gated.post(
        f"/api/runs/{run_id}/tool-approvals/{request['id']}",
        json={"decision": "approved", "arguments_hash": request["arguments_hash"], "comment": "ok"},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "approved" and answer.json()["decided_by"] == "local"
    run = finished(gated, run_id)
    assert run["status"] == "succeeded", run
    [call] = run["nodes"]["a"]["tool_calls"]
    assert call["output"] == "212.0" and call["approval"]["status"] == "approved"
    assert gated.get("/api/tool-approvals").json() == []
    [event] = gated.get("/api/tool-approvals/audit").json()
    assert (event["kind"], event["actor"], event["outcome"]) == ("decided", "local", "approved")


def test_stale_late_and_unknown_decisions_are_refused(gated: TestClient) -> None:
    run_id = start(gated)
    request = waiting(gated, run_id)
    path = f"/api/runs/{run_id}/tool-approvals/{request['id']}"
    stale = gated.post(path, json={"decision": "approved", "arguments_hash": "0" * 64})
    assert stale.status_code == 409 and stale.json()["code"] == "tool.approval_stale"
    shown = request["arguments_hash"]
    rejected = gated.post(path, json={"decision": "rejected", "arguments_hash": shown, "comment": "Wrong unit"})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    again = gated.post(path, json={"decision": "approved", "arguments_hash": shown})
    assert again.status_code == 409 and again.json()["code"] == "tool.approval_decided"
    assert (
        gated.post(
            f"/api/runs/{run_id}/tool-approvals/tap_missing", json={"decision": "approved", "arguments_hash": shown}
        ).status_code
        == 404
    )
    assert (
        gated.post(
            f"/api/runs/run_missing/tool-approvals/{request['id']}",
            json={"decision": "approved", "arguments_hash": shown},
        ).status_code
        == 404
    )
    assert gated.get("/api/runs/run_missing/tool-approvals").status_code == 404
    run = finished(gated, run_id)
    [call] = run["nodes"]["a"]["tool_calls"]
    assert call["is_error"] and "Wrong unit" in call["output"]
    outcomes = [item["outcome"] for item in reversed(gated.get("/api/tool-approvals/audit").json())]
    assert outcomes == ["tool.approval_stale", "rejected", "tool.approval_decided", "tool.approval_not_found"]


def test_deciding_needs_run_and_granting_needs_admin(gated: TestClient) -> None:
    ctx = gated.app.state.nodestro  # type: ignore[attr-defined]
    request = ToolApprovals(ctx.store).request(
        run_id="run_x", node_id="a", call_id="c", tool=TOOL, arguments={}, capability=WRITE
    )
    reader = Principal(id="reader", permissions=frozenset({Permission.READ}))
    runner = Principal(id="runner", permissions=frozenset({Permission.READ, Permission.RUN}))
    gated.app.state.principal_resolver = lambda _connection: reader  # type: ignore[attr-defined]
    assert [item["id"] for item in gated.get("/api/tool-approvals").json()] == [request.id]
    body = {"decision": "approved", "arguments_hash": request.arguments_hash}
    refused = gated.post(f"/api/runs/run_x/tool-approvals/{request.id}", json=body)
    assert refused.status_code == 403 and refused.json()["params"] == {"permission": "run"}
    gated.app.state.principal_resolver = lambda _connection: runner  # type: ignore[attr-defined]
    grant = {"tool": TOOL, "scope": "schedule", "scope_id": "sch_1"}
    assert gated.post("/api/tool-pre-grants", json=grant).json()["params"] == {"permission": "admin"}
    assert gated.post("/api/tool-approvals/legacy").status_code == 403
    assert gated.get("/api/tool-approvals/audit").status_code == 403
    assert ToolApprovals(ctx.store).get("run_x", request.id).status == "pending"  # type: ignore[union-attr]


def test_pre_grants_are_narrow_traceable_and_revocable(gated: TestClient) -> None:
    workflow = gated.post("/api/workflows", json=flow()).json()
    schedule = gated.post("/api/schedules", json={"workflow_id": workflow["id"], "kind": "daily", "at": "08:00"})
    assert schedule.status_code == 201, schedule.text
    scope_id = schedule.json()["id"]
    missing = gated.post("/api/tool-pre-grants", json={"tool": TOOL, "scope": "schedule", "scope_id": "sch_missing"})
    assert missing.status_code == 404
    unknown = gated.post(
        "/api/tool-pre-grants", json={"tool": "no_such_tool", "scope": "schedule", "scope_id": scope_id}
    )
    assert unknown.status_code == 404
    created = gated.post("/api/tool-pre-grants", json={"tool": TOOL, "scope": "schedule", "scope_id": scope_id})
    assert created.status_code == 201, created.text
    grant = created.json()
    assert grant["granted_by"] == "local" and grant["version"] == 1 and grant["revoked_at"] is None
    assert [item["id"] for item in gated.get("/api/tool-pre-grants").json()] == [grant["id"]]
    revoked = gated.delete(f"/api/tool-pre-grants/schedule/{scope_id}/{grant['id']}")
    assert revoked.status_code == 200 and revoked.json()["revoked_by"] == "local"
    assert gated.delete(f"/api/tool-pre-grants/schedule/{scope_id}/tpg_missing").status_code == 404
    kinds = [item["kind"] for item in reversed(gated.get("/api/tool-approvals/audit").json())]
    assert kinds == ["pre_granted", "pre_grant_revoked"]


def test_destructive_tools_cannot_be_pre_granted(gated: TestClient) -> None:
    workflow = gated.post("/api/workflows", json=flow()).json()
    scope_id = gated.post(
        "/api/schedules", json={"workflow_id": workflow["id"], "kind": "daily", "at": "08:00"}
    ).json()["id"]
    ctx = gated.app.state.nodestro  # type: ignore[attr-defined]
    info = ctx.tools.get(TOOL)
    destructive = info.model_copy(update={"capability": WRITE.model_copy(update={"access": "destructive"})})
    ctx.tools.get = lambda _name: destructive
    answer = gated.post("/api/tool-pre-grants", json={"tool": TOOL, "scope": "schedule", "scope_id": scope_id})
    assert answer.status_code == 400 and answer.json()["code"] == "tool.pre_grant_destructive"
    assert gated.get("/api/tool-pre-grants").json() == []


def test_activation_stamps_workflows_and_published_releases_visibly(gated: TestClient) -> None:
    workflow = gated.post("/api/workflows", json=flow()).json()
    published = gated.post(f"/api/workflows/{workflow['id']}/api/publish", json={"slug": "convert"})
    assert published.status_code == 201, published.text
    publication = published.json()["publication"]
    ctx = gated.app.state.nodestro  # type: ignore[attr-defined]
    [before] = ctx.publications.releases(publication["id"])

    state = gated.get("/api/tool-approvals/legacy").json()
    assert state == {"enabled": True, "legacy": None}
    report = gated.post("/api/tool-approvals/legacy").json()["legacy"]
    places = {(item["release"], item["workflow_version"], item["tool"]) for item in report["bindings"]}
    assert (None, None, TOOL) in places  # the workflow itself
    assert (f"{publication['id']}:{before.number}", before.workflow_version, TOOL) in places
    [after] = ctx.publications.releases(publication["id"])
    binding = after.bundle.workflow.nodes[1].agent.tool_policies[TOOL]
    assert binding.legacy and binding.ask == "never" and binding.bound_version == 1
    assert after.digest != before.digest
    history = ctx.publications.get(publication["id"]).history
    assert history[-1].kind == "legacy_stamped"
    assert history[-1].details["releases"][0]["digest_before"] == before.digest

    # idempotent: nothing new is stamped, and the earlier exemptions stay listed
    again = gated.post("/api/tool-approvals/legacy").json()["legacy"]
    assert again == report
    assert ctx.publications.releases(publication["id"])[0].digest == after.digest
    [event, *_] = gated.get("/api/tool-approvals/audit").json()
    assert event["kind"] == "legacy_stamped"


def test_without_the_switch_the_state_says_so(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENABLE_ENV, raising=False)
    client = make_app()
    assert client.get("/api/tool-approvals/legacy").json() == {"enabled": False, "legacy": None}
    assert client.get("/api/tool-approvals").json() == []


def test_questions_of_another_tenant_are_not_found(server: Any) -> None:  # noqa: F811
    """On a server every route works on the request's tenant only (ADR-0003)."""
    from test_worker import _start

    alice = server.member("acme", "alice", "owner")
    mallory = server.member("globex", "mallory", "owner")
    run_id = _start(alice)
    acme = server.app.state.tenants.get("acme")
    request = ToolApprovals(acme.store).request(
        run_id=run_id, node_id="a", call_id="c", tool=TOOL, arguments={"to": "x"}, capability=WRITE
    )
    body = {"decision": "approved", "arguments_hash": request.arguments_hash}
    assert mallory.get("/api/tool-approvals").json() == []
    assert mallory.get(f"/api/runs/{run_id}/tool-approvals").status_code == 404
    assert mallory.post(f"/api/runs/{run_id}/tool-approvals/{request.id}", json=body).status_code == 404
    assert ToolApprovals(acme.store).get(run_id, request.id).status == "pending"  # type: ignore[union-attr]
    viewer = server.member("acme", "victor", "viewer")
    assert [item["id"] for item in viewer.get("/api/tool-approvals").json()] == [request.id]
    assert viewer.post(f"/api/runs/{run_id}/tool-approvals/{request.id}", json=body).status_code == 403
    decided = alice.post(f"/api/runs/{run_id}/tool-approvals/{request.id}", json=body)
    assert decided.status_code == 200, decided.text
    alice_id = alice.get("/api/auth/me").json()["user_id"]
    assert decided.json()["decided_by"] == alice_id


def test_repeating_the_activation_never_exempts_later_workflows_or_releases(gated: TestClient) -> None:
    """Regression (security acceptance 2026-10-31): the step runs once, so what is created after the first call stays a
    new binding; and an interrupted first call stamps only the releases that existed when the workspace was stamped."""
    ctx = gated.app.state.nodestro  # type: ignore[attr-defined]
    old = gated.post("/api/workflows", json=flow()).json()
    old_pub = gated.post(f"/api/workflows/{old['id']}/api/publish", json={"slug": "before"}).json()["publication"]
    ctx.stamp_legacy_tool_bindings(by="local")  # the first call stops after the workspace part (e.g. a crash)
    later = gated.post("/api/workflows", json=flow()).json()
    later_pub = gated.post(f"/api/workflows/{later['id']}/api/publish", json={"slug": "after"}).json()["publication"]

    report = gated.post("/api/tool-approvals/legacy").json()["legacy"]
    assert report["stamped_at"] is not None and report["publications_stamped_at"] is not None
    released = {item["release"].split(":")[0] for item in report["bindings"] if item["release"]}
    assert released == {old_pub["id"]}
    [late] = ctx.publications.releases(later_pub["id"])
    assert late.bundle.workflow.nodes[1].agent.tool_policies == {}
    assert ctx.workflow(later["id"]).nodes[1].agent.tool_policies == {}

    newest = gated.post("/api/workflows", json=flow()).json()
    again = gated.post("/api/tool-approvals/legacy").json()["legacy"]
    assert again == report  # nothing new
    assert ctx.workflow(newest["id"]).nodes[1].agent.tool_policies == {}
    repeated = [item for item in gated.get("/api/tool-approvals/audit").json() if item["details"].get("repeated")]
    assert len(repeated) == 2  # the API call after the interrupted one, and the explicit repeat
