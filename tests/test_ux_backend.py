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
