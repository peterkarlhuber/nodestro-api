"""The API of the access model v2 (N-06): the switch, who may write (a signed-in person with ``admin``, never an API
key or a model), "who may do what and why", refused starts and model requests. Tenant isolation on a server:
``test_access_api_server``."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.tool_approvals import ACCESS_ENV, ENABLE_ENV, AccessRules
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
import pytest
from test_published_api import PUBLISHED, auth, form_workflow, new_key, publish

import nodestro_api
from nodestro_api.permissions import ALL_PERMISSIONS, Permission, Principal

from conftest import text

ACCESS = "/api/access"
TOOL = "celsius_to_fahrenheit"
ROUTES: list[tuple[str, str]] = [
    ("get", f"{ACCESS}/grants"),
    ("get", f"{ACCESS}/denies"),
    ("post", f"{ACCESS}/grants"),
    ("post", f"{ACCESS}/denies"),
    ("delete", f"{ACCESS}/grants/grt_x"),
    ("delete", f"{ACCESS}/denies/dny_x"),
    ("get", f"{ACCESS}/who-may"),
    ("get", f"{ACCESS}/explain?resource_type=tool&resource_id=x"),
]
DENY_BODY = {"resource_type": "workflow", "resource_id": "wf_x", "actions": ["execute"]}


def flow(name: str = "Access") -> dict[str, Any]:
    return {
        "name": name,
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "agent", "agent": {"name": "Agent", "system_prompt": "AGENT", "tools": [TOOL]}},
            {"id": "out", "type": "output"},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
    }


def with_tool(client: TestClient) -> None:
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    result, _ = ctx.tools.save_custom(
        CUSTOM_TOOL_TEMPLATE, CUSTOM_TESTS_TEMPLATE, TOOL, trust=ctx.tools.editor_trust(TOOL, CUSTOM_TOOL_TEMPLATE)
    )
    assert result.ok, result


@pytest.fixture
def granting(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(ENABLE_ENV, "1")
    monkeypatch.setenv(ACCESS_ENV, "1")
    client = make_app({"AGENT": [text("done")] * 5, "WRITER": [text("Angebot")] * 5})
    with_tool(client)
    return client


def _call(client: TestClient, method: str, path: str) -> Any:
    if method == "post":
        return client.post(path, json={**DENY_BODY, "duration": "until_revoked"})
    return getattr(client, method)(path)


def _as(client: TestClient, principal: Principal) -> None:
    client.app.state.principal_resolver = lambda _connection: principal  # type: ignore[attr-defined]


def _workflow(client: TestClient) -> str:
    created = client.post("/api/workflows", json=flow())
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.mark.parametrize("approvals", [False, True])
def test_without_the_switch_every_route_but_the_state_is_not_found(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, approvals: bool
) -> None:
    """Off by default, also with per-call approvals alone: nothing is read or written."""
    monkeypatch.delenv(ACCESS_ENV, raising=False)
    if approvals:
        monkeypatch.setenv(ENABLE_ENV, "1")
    else:
        monkeypatch.delenv(ENABLE_ENV, raising=False)
        monkeypatch.setenv(ACCESS_ENV, "1")  # the access switch alone does nothing
    client = make_app()
    state = client.get(f"{ACCESS}/state")
    assert state.status_code == 200
    assert state.json()["enabled"] is False and state.json()["safety_limits"]
    for method, path in ROUTES:
        response = _call(client, method, path)
        assert (response.status_code, response.json()["code"]) == (404, "access.grants_disabled"), (method, path)
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    assert AccessRules(store).grants() == [] and AccessRules(store).denies() == []


def test_a_person_grants_denies_and_revokes_and_the_server_fills_in_who_and_the_tool(granting: TestClient) -> None:
    assert granting.get(f"{ACCESS}/state").json()["enabled"] is True
    body = {
        "resource_type": "tool",
        "resource_id": TOOL,
        "actions": ["execute"],
        "duration": "until_revoked",
        "mode": "supervised",
        # Fields the server owns are not taken from the body.
        "granted_by": "mallory",
        "tool": {"version": 99, "access": "read", "reach": "internal"},
    }
    created = granting.post(f"{ACCESS}/grants", json=body)
    assert created.status_code == 201, created.text
    grant = created.json()
    assert grant["granted_by"] == "local" and grant["status"] == "active"
    ctx = granting.app.state.nodestro  # type: ignore[attr-defined]
    info = ctx.tools.get(TOOL)
    assert grant["tool"]["version"] == info.version and grant["tool"]["access"] == info.capability.access
    assert [item["id"] for item in granting.get(f"{ACCESS}/grants").json()] == [grant["id"]]
    unknown = granting.post(f"{ACCESS}/grants", json={**body, "resource_id": "no_such_tool"})
    assert unknown.status_code == 404
    deny = granting.post(f"{ACCESS}/denies", json={**DENY_BODY, "resource_id": "wf_1"})
    assert deny.status_code == 201, deny.text
    assert deny.json()["denied_by"] == "local"
    revoked = granting.delete(f"{ACCESS}/grants/{grant['id']}")
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_by"] == "local"
    assert granting.delete(f"{ACCESS}/grants/{grant['id']}").json()["status"] == "revoked"  # twice: no change
    assert granting.delete(f"{ACCESS}/denies/{deny.json()['id']}").json()["status"] == "revoked"
    missing = granting.delete(f"{ACCESS}/denies/dny_missing")
    assert (missing.status_code, missing.json()["code"]) == (404, "access.not_found")
    # Revoked records stay listed (traceable) but no longer decide anything.
    assert granting.get(f"{ACCESS}/grants").json()[0]["status"] == "revoked"
    assert granting.get(f"{ACCESS}/who-may").json() == []


def test_records_that_cannot_be_stored_are_refused_with_their_code(granting: TestClient) -> None:
    destructive = granting.post(
        f"{ACCESS}/grants",
        json={"resource_type": "workflow", "resource_id": "wf_1", "actions": ["delete"], "duration": "until_revoked"},
    )
    assert (destructive.status_code, destructive.json()["code"]) == (422, "access.grant_destructive")
    unscoped = granting.post(f"{ACCESS}/denies", json={**DENY_BODY, "scope": "agent"})
    assert unscoped.status_code == 422


def test_writing_needs_admin_and_a_person(granting: TestClient) -> None:
    """Reading the state needs ``read``; every other route ``admin``. A principal that is not a person (an API key,
    a machine) cannot write even with every permission: nothing is stored."""
    runner = Principal(id="runner", permissions=frozenset({Permission.READ, Permission.RUN, Permission.EDIT}))
    _as(granting, runner)
    assert granting.get(f"{ACCESS}/state").status_code == 200
    for method, path in ROUTES:
        response = _call(granting, method, path)
        assert (response.status_code, response.json()["params"]) == (403, {"permission": "admin"}), (method, path)
    for machine in ("api:key_1", "system", "mcp:client"):
        _as(granting, Principal(id=machine, permissions=ALL_PERMISSIONS))
        refused = granting.post(f"{ACCESS}/denies", json=DENY_BODY)
        assert (refused.status_code, refused.json()["code"]) == (403, "access.person_required"), machine
    store = granting.app.state.nodestro.store  # type: ignore[attr-defined]
    assert AccessRules(store).denies() == []


def test_who_may_and_explain_name_the_deciding_record(granting: TestClient) -> None:
    workflow_id = _workflow(granting)
    agent = f"{workflow_id}/a"
    deny = granting.post(
        f"{ACCESS}/denies", json={"resource_type": "tool", "resource_id": TOOL, "actions": ["execute"]}
    ).json()
    grant = granting.post(
        f"{ACCESS}/grants",
        json={
            "resource_type": "cloud_provider",
            "resource_id": "anthropic",
            "actions": ["submit"],
            "duration": "until_revoked",
            "scope": "agent",
            "agents": [agent],
        },
    ).json()
    found = granting.get(f"{ACCESS}/who-may").json()
    assert {(item["resource"]["id"], item["source"]) for item in found} >= {
        (TOOL, f"deny:{deny['id']}"),
        ("anthropic", f"grant:{grant['id']}"),
    }
    narrowed = granting.get(f"{ACCESS}/who-may", params={"resource_type": "tool", "resource_id": TOOL}).json()
    assert {item["resource"]["id"] for item in narrowed} == {TOOL}
    assert all(item["allowed"] is False for item in narrowed)
    half = granting.get(f"{ACCESS}/who-may", params={"resource_type": "tool"})
    assert (half.status_code, half.json()["code"]) == (422, "access.invalid")
    explained = granting.get(f"{ACCESS}/explain", params={"resource_type": "tool", "resource_id": TOOL})
    assert explained.status_code == 200, explained.text
    assert (explained.json()["action"], explained.json()["source"]) == ("execute", f"deny:{deny['id']}")
    assert explained.json()["outcome"] == "refused" and explained.json()["deny"]["id"] == deny["id"]
    cloud = granting.get(
        f"{ACCESS}/explain", params={"resource_type": "cloud_provider", "resource_id": "anthropic", "agent": agent}
    ).json()
    assert (cloud["action"], cloud["source"], cloud["allowed"]) == ("submit", f"grant:{grant['id']}", True)


def test_a_refused_start_answers_403_with_its_source_for_a_person(granting: TestClient) -> None:
    workflow_id = _workflow(granting)
    deny = granting.post(f"{ACCESS}/denies", json={**DENY_BODY, "resource_id": workflow_id}).json()
    refused = granting.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"})
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "access.denied"
    assert refused.json()["params"] == {
        "resource_type": "workflow",
        "resource_id": workflow_id,
        "source": f"deny:{deny['id']}",
    }
    granting.delete(f"{ACCESS}/denies/{deny['id']}")
    assert granting.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"}).status_code == 201


def test_a_blocked_cloud_model_refuses_the_assistants_for_the_person_who_asks(granting: TestClient) -> None:
    """The callers of ``connect_model`` pass the signed-in person: the refusal is audited for them and names its
    source (L-A)."""
    from nodestro_runtime.tool_approvals import audit_events

    deny = granting.post(
        f"{ACCESS}/denies", json={"resource_type": "cloud_provider", "resource_id": "anthropic", "actions": ["submit"]}
    ).json()
    refused = granting.post("/api/memory/propose", json={"wishes": "remember customers"})
    assert refused.status_code == 403, refused.text
    assert (refused.json()["code"], refused.json()["params"]["source"]) == ("access.denied", f"deny:{deny['id']}")
    store = granting.app.state.nodestro.store  # type: ignore[attr-defined]
    events = [item for item in audit_events(store) if item.kind == "access_refused"]
    assert [item.actor for item in events] == ["local"]


def test_the_published_api_answers_403_without_the_source(granting: TestClient) -> None:
    """An API key starts no run of a blocked workflow and never learns which record decided (L-3); no published
    route reads or writes grants or denies."""
    workflow_id = form_workflow(granting)
    result = publish(granting, workflow_id, note="v1")
    token = new_key(granting, result["publication"]["id"])
    slug = result["publication"]["slug"]
    granting.post(f"{ACCESS}/denies", json={**DENY_BODY, "resource_id": workflow_id})
    refused = granting.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "ACME"}}, headers=auth(token))
    assert refused.status_code == 403, refused.text
    error = refused.json()["error"]
    assert (error["status"], error["code"]) == (403, "access.denied") and "source" not in error["params"]
    spec = granting.get(f"{PUBLISHED}/{slug}/openapi.json", headers=auth(token))
    assert "/access" not in spec.text


def test_the_published_api_strips_the_source_from_run_errors() -> None:
    from nodestro_runtime.schemas import RunEvent

    from nodestro_api.serving import ServingCore

    event = RunEvent(
        run_id="run_1",
        type="node_finished",
        node_id="a",
        data={"status": "failed", "error_code": "access.denied", "error_params": {"resource_id": "x", "source": "s"}},
    )
    public = ServingCore.public_event(event, text=False)
    assert public is not None and public["error_params"] == {"resource_id": "x"}


def test_only_the_admin_routes_write_grants_and_denies() -> None:
    """No tool, assistant, MCP server route or published route builds a :class:`Person` or writes a record: only
    :mod:`nodestro_api.api_access`, behind ``admin`` and the signed-in principal."""
    package = Path(nodestro_api.__file__).parent
    writers: set[str] = set()
    for source in package.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in ("AccessRules", "Person"):
                writers.add(source.name)
    assert writers == {"api_access.py"}
