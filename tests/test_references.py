"""Where knowledge files, tools and memory spaces are used (``GET /api/used-by``, UX-7)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from test_published_api import publish

from conftest import text
from test_flow import edge, save
from test_runs import agent_node


def _knowledge(client: TestClient, name: str) -> str:
    uploaded = client.post("/api/knowledge", files=[("files", (name, f"Wissen aus {name}".encode(), "text/plain"))])
    assert uploaded.status_code == 201, uploaded.text
    return str(uploaded.json()[0]["id"])


def _used_by(client: TestClient, kind: str, ref: str | None = None) -> list[dict[str, Any]]:
    answer = client.get("/api/used-by", params={"kind": kind, **({"ref": ref} if ref else {})})
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


def _setup(client: TestClient) -> dict[str, str]:
    contract, unused = _knowledge(client, "vertrag.txt"), _knowledge(client, "frei.txt")
    reviewer = agent_node(
        "reviewer",
        "REVIEW",
        tools=["calculator"],
        knowledge={"file_ids": [contract], "mode": "search"},
        memory={"mode": "persistent", "space": "Team"},
    )
    nodes = [
        {"id": "start", "type": "start", "start": {"file_ids": [contract]}},
        reviewer,
        agent_node("writer", "WRITE", tools=["calculator", "current_datetime"]),
        {"id": "out", "type": "output"},
    ]
    workflow = save(client, nodes, [edge("start", "reviewer"), edge("reviewer", "writer"), edge("writer", "out")])
    template = client.post(
        "/api/agents/templates",
        json={"agent": {"name": "Prüfer", "tools": ["calculator"], "knowledge": {"file_ids": [contract]}}},
    ).json()
    return {"contract": contract, "unused": unused, "workflow": workflow, "template": template["id"]}


def test_knowledge_lists_agents_start_nodes_and_library_agents(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    ids = _setup(client)
    listed = _used_by(client, "knowledge")
    # A file nobody uses is not listed.
    assert [item["ref"] for item in listed] == [ids["contract"]]
    assert listed[0]["users"] == [
        {
            "kind": "workflow",
            "id": ids["workflow"],
            "name": "Flow",
            "node_id": "start",
            "node_label": "Start",
            "role": "start",
            "release": None,
            "workflow_id": None,
        },
        {
            "kind": "workflow",
            "id": ids["workflow"],
            "name": "Flow",
            "node_id": "reviewer",
            "node_label": "Reviewer",
            "role": "agent",
            "release": None,
            "workflow_id": None,
        },
        {
            "kind": "template",
            "id": ids["template"],
            "name": "Prüfer",
            "node_id": None,
            "node_label": None,
            "role": "agent",
            "release": None,
            "workflow_id": None,
        },
    ]
    assert _used_by(client, "knowledge", ids["unused"]) == []
    assert [item["ref"] for item in _used_by(client, "knowledge", ids["contract"])] == [ids["contract"]]


def test_tools_and_memory_spaces_list_their_users(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    _setup(client)
    tools = {
        item["ref"]: [(user["kind"], user["node_id"]) for user in item["users"]] for item in _used_by(client, "tool")
    }
    assert tools == {
        "calculator": [("workflow", "reviewer"), ("workflow", "writer"), ("template", None)],
        "current_datetime": [("workflow", "writer")],
    }
    memory = _used_by(client, "memory")
    assert [(item["ref"], [user["node_id"] for user in item["users"]]) for item in memory] == [("Team", ["reviewer"])]
    # A session memory is not a use of a space.
    assert _used_by(client, "memory", "Other") == []


def test_a_published_api_uses_its_knowledge_and_memory_live(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"REVIEW": [text("ok")] * 3})
    contract = _knowledge(client, "vertrag.txt")
    reviewer = agent_node(
        "reviewer",
        "REVIEW",
        knowledge={"file_ids": [contract], "mode": "search"},
        memory={"mode": "persistent", "space": "Team"},
    )
    workflow = save(
        client,
        [{"id": "start", "type": "start"}, reviewer, {"id": "out", "type": "output"}],
        [edge("start", "reviewer"), edge("reviewer", "out")],
        name="Prüf-API",
        budget_usd=1.0,
    )
    result = publish(client, workflow)
    publication = result["publication"]
    # The saved workflow changes afterwards; the active release still reads the file and the space.
    changed = client.get(f"/api/workflows/{workflow}").json()
    changed["nodes"][1]["agent"]["knowledge"] = {"file_ids": []}
    changed["nodes"][1]["agent"]["memory"] = {"mode": "none"}
    assert client.put(f"/api/workflows/{workflow}", json=changed).status_code == 200
    expected = {
        "kind": "publication",
        "id": publication["id"],
        "name": publication["name"],
        "node_id": None,
        "node_label": None,
        "role": "agent",
        "release": 1,
        "workflow_id": workflow,
    }
    assert _used_by(client, "knowledge") == [{"ref": contract, "users": [expected]}]
    assert _used_by(client, "memory") == [{"ref": "Team", "users": [expected]}]
    # Tools are frozen into a release: deleting the studio's tool does not change it, so it is no user.
    assert _used_by(client, "tool") == []


def test_listing_users_changes_nothing_and_deleting_stays_as_it_was(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    ids = _setup(client)
    before = client.get(f"/api/workflows/{ids['workflow']}").json()
    _used_by(client, "knowledge")
    assert client.get(f"/api/workflows/{ids['workflow']}").json() == before
    # Deleting a used file is still allowed (the studio warns before), exactly as before.
    assert client.delete(f"/api/knowledge/{ids['contract']}").status_code == 204
    assert client.get(f"/api/workflows/{ids['workflow']}").json() == before


def test_an_unknown_kind_is_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    assert client.get("/api/used-by", params={"kind": "workflow"}).status_code == 422
    assert client.get("/api/used-by").status_code == 422
