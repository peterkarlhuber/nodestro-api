"""Backend fixes requested by the UX audit.

- A save based on a stale revision is refused with 409 ``workflow.conflict`` (AD-07).
- ``/api/meta`` names the profile (AD-05).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

FLOW = {
    "name": "Flow",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "ALPHA"}},
        {"id": "out", "type": "output"},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
}


def _ctx(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


# ---- AD-07: concurrent edits ----------------------------------------------------------------------------------


def test_a_save_based_on_a_stale_revision_is_refused(make_app: Any) -> None:
    client = make_app()
    opened = client.post("/api/workflows", json=FLOW).json()
    workflow_id, revision = opened["id"], opened["updated_at"]

    # Tab B saves first, based on what it opened.
    saved_b = client.put(
        f"/api/workflows/{workflow_id}", json={**FLOW, "description": "B"}, headers={"If-Match": revision}
    )
    assert saved_b.status_code == 200
    assert saved_b.json()["updated_at"] != revision

    # Tab A saves later, based on the same (now stale) revision.
    refused = client.put(
        f"/api/workflows/{workflow_id}", json={**FLOW, "description": "A"}, headers={"If-Match": f'"{revision}"'}
    )
    assert refused.status_code == 409
    body = refused.json()
    assert body["code"] == "workflow.conflict"
    assert body["params"] == {"updated_at": saved_b.json()["updated_at"], "name": "Flow"}
    assert client.get(f"/api/workflows/{workflow_id}").json()["description"] == "B"
    assert len(client.get(f"/api/workflows/{workflow_id}/versions").json()) == 2

    # With the current revision the save goes through.
    current = body["params"]["updated_at"]
    saved_a = client.put(
        f"/api/workflows/{workflow_id}", json={**FLOW, "description": "A"}, headers={"If-Match": current}
    )
    assert saved_a.status_code == 200 and saved_a.json()["description"] == "A"


def test_a_save_without_a_revision_keeps_the_last_save_wins_behavior(make_app: Any) -> None:
    client = make_app()
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    assert client.put(f"/api/workflows/{workflow_id}", json={**FLOW, "description": "B"}).status_code == 200
    assert client.put(f"/api/workflows/{workflow_id}", json={**FLOW, "description": "A"}).status_code == 200
    assert (
        client.put(
            f"/api/workflows/{workflow_id}", json={**FLOW, "description": "C"}, headers={"If-Match": "*"}
        ).json()["description"]
        == "C"
    )


def test_an_unreadable_revision_is_a_bad_request(make_app: Any) -> None:
    client = make_app()
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    refused = client.put(f"/api/workflows/{workflow_id}", json=FLOW, headers={"If-Match": "yesterday"})
    assert refused.status_code == 400 and refused.json()["code"] == "workflow.revision_invalid"


def test_a_save_that_races_another_save_is_refused(make_app: Any) -> None:
    """The check and the write are one step: a save landing after this save's read but before its write wins."""
    from nodestro_runtime.schemas import WorkflowIn, now
    import pytest

    from nodestro_api import api

    client = make_app()
    ctx = _ctx(client)
    opened = client.post("/api/workflows", json=FLOW).json()
    current = api._workflow(ctx, opened["id"])
    # Another save lands between this save's read and its write.
    ctx.store.put("workflows", current.id, current.model_copy(update={"description": "other", "updated_at": now()}))
    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr(api, "_workflow", lambda _ctx, _id: current)
        with pytest.raises(api.ApiError) as refused:
            api.update_workflow(
                current.id, WorkflowIn(**{**FLOW, "description": "mine"}), ctx, note="", if_match=opened["updated_at"]
            )
    assert refused.value.status == 409 and refused.value.code == "workflow.conflict"
    assert client.get(f"/api/workflows/{opened['id']}").json()["description"] == "other"


def test_a_save_cannot_create_a_legacy_tool_exemption(make_app: Any) -> None:
    """The API save keeps what Workspace.save_workflow guarantees: exemptions come only from the activation step."""
    client = make_app()
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    forged = {
        **FLOW,
        "nodes": [
            FLOW["nodes"][0],
            {
                "id": "a",
                "type": "agent",
                "label": "A",
                "agent": {
                    "name": "A",
                    "system_prompt": "ALPHA",
                    "tools": ["send_mail"],
                    "tool_policies": {"send_mail": {"ask": "never", "legacy": True, "bound_version": 1}},
                },
            },
            FLOW["nodes"][2],
        ],
    }
    saved = client.put(f"/api/workflows/{workflow_id}", json=forged).json()
    agent = next(node for node in saved["nodes"] if node["id"] == "a")["agent"]
    assert "send_mail" not in agent["tool_policies"]


# ---- AD-05: the profile in meta -------------------------------------------------------------------------------


def test_meta_names_the_desktop_profile(make_app: Any) -> None:
    assert make_app().get("/api/meta").json()["profile"] == "desktop"


# ---- AD-08: import and save validation ------------------------------------------------------------------------


def test_importing_json_that_is_not_a_workflow_is_refused(make_app: Any) -> None:
    client = make_app()
    refused = client.post("/api/workflows/import", json={"foo": 1})
    assert refused.status_code == 400
    assert refused.json()["code"] == "workflow.import_invalid" and refused.json()["params"] == {"missing": ["nodes"]}
    assert client.get("/api/workflows").json() == []
    # An export (the editor's file) still imports.
    imported = client.post("/api/workflows/import", json={**FLOW, "python": None})
    assert imported.status_code == 201 and imported.json()["name"] == "Flow"


def test_dangling_edges_are_refused_like_the_check_does(make_app: Any) -> None:
    client = make_app()
    broken = {**FLOW, "edges": [*FLOW["edges"], {"id": "e3", "source": "a", "target": "ghost"}]}
    checked = client.post("/api/workflows/check", json=broken).json()
    assert any(issue["code"] == "workflow.edge_dangling" for issue in checked["problems"])
    for answer in (
        client.post("/api/workflows", json=broken),
        client.post("/api/workflows/import", json=broken),
    ):
        assert answer.status_code == 400
        assert answer.json()["code"] == "workflow.edge_dangling" and answer.json()["params"] == {"edge": "e3"}
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    refused = client.put(f"/api/workflows/{workflow_id}", json=broken)
    assert refused.status_code == 400 and refused.json()["code"] == "workflow.edge_dangling"
    duplicate = {**FLOW, "nodes": [*FLOW["nodes"], {"id": "a", "type": "output"}]}
    assert client.post("/api/workflows", json=duplicate).json()["code"] == "workflow.node_duplicate"


def test_a_stored_workflow_with_a_dangling_edge_still_loads_and_copies(make_app: Any) -> None:
    from nodestro_runtime.schemas import Workflow

    client = make_app()
    ctx = _ctx(client)
    legacy = Workflow(**{**FLOW, "edges": [{"id": "e9", "source": "a", "target": "ghost"}]})
    ctx.store.put("workflows", legacy.id, legacy)
    assert client.get(f"/api/workflows/{legacy.id}").status_code == 200
    assert any(item["id"] == legacy.id for item in client.get("/api/workflows").json())
    assert client.post(f"/api/workflows/{legacy.id}/duplicate").status_code == 201


def test_a_workflow_created_without_a_name_is_named_in_the_interface_language(make_app: Any) -> None:
    client = make_app()
    created = client.post("/api/workflows", json={"nodes": []}, headers={"Accept-Language": "en-US"})
    assert created.json()["name"] == "New workflow"
    created = client.post("/api/workflows", json={}, headers={"Accept-Language": "de-DE"})
    assert created.json()["name"] == "Neuer Workflow"


# ---- AX-24/AX-25: paging the run list -------------------------------------------------------------------------


def _stored_runs(client: TestClient, count: int, **fields: Any) -> list[str]:
    from datetime import timedelta

    from nodestro_runtime.schemas import Run, Workflow, now

    ctx = _ctx(client)
    workflow = Workflow(id="wf_paging", name="Paging")
    base = now()
    ids = []
    for index in range(count):
        run = Run(
            id=f"run_{index:04d}",
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            workflow=workflow,
            status="succeeded",
            # Two runs share each time: the cursor must still not skip or repeat one.
            created_at=base - timedelta(seconds=count - index // 2 * 2),
            **fields,
        )
        ctx.store.put("runs", run.id, run)
        ids.append(run.id)
    return ids


def test_the_run_list_pages_with_a_cursor(make_app: Any) -> None:
    client = make_app()
    ids = _stored_runs(client, 25)
    first = client.get("/api/runs", params={"limit": 10})
    assert first.status_code == 200 and isinstance(first.json(), list) and len(first.json()) == 10
    assert first.headers["X-Has-More"] == "true"
    seen = [run["id"] for run in first.json()]
    cursor = first.headers["X-Next-Before"]
    while cursor:
        page = client.get("/api/runs", params={"limit": 10, "before": cursor})
        assert page.status_code == 200
        seen += [run["id"] for run in page.json()]
        cursor = page.headers.get("X-Next-Before", "")
    assert page.headers["X-Has-More"] == "false"
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen))
    # The same paging with a workflow filter; a plain time as cursor; an unreadable cursor.
    filtered = client.get("/api/runs", params={"workflow_id": "wf_paging", "limit": 20})
    assert len(filtered.json()) == 20 and filtered.headers["X-Has-More"] == "true"
    everything = client.get("/api/runs")
    assert len(everything.json()) == 25 and everything.headers["X-Has-More"] == "false"
    assert "X-Next-Before" not in everything.headers
    oldest = everything.json()[-1]["created_at"]
    assert client.get("/api/runs", params={"before": oldest}).json() == []
    refused = client.get("/api/runs", params={"before": "last week"})
    assert refused.status_code == 400 and refused.json()["code"] == "runs.cursor_invalid"


# ---- settings: "Automatic" language ---------------------------------------------------------------------------


def test_automatic_language_clears_the_stored_one(make_app: Any) -> None:
    """The studio's "Automatic" sends ``language: null``: it clears the stored language; an absent field keeps it."""
    client = make_app()
    assert client.put("/api/settings", json={"language": "it"}).json()["language"] == "it"
    assert client.put("/api/settings", json={"default_budget_usd": 2}).json()["language"] == "it"
    assert client.put("/api/settings", json={"language": None}).json()["language"] is None
    assert client.get("/api/settings").json()["language"] is None
