"""The local inbox (NAL-A3): /api/notifications on the desktop, filled by workflow rules and notify_user."""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.testing import text, tool_use

from test_flow import edge, save
from test_runs import agent_node


def _finished(client: TestClient, run: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while run["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.05)
        run = client.get(f"/api/runs/{run['id']}").json()
    return run


def test_runs_fill_the_inbox_by_their_rules(make_app: Callable[..., TestClient]) -> None:
    calls = [tool_use("notify_user", {"title": "Check the totals", "message": "<b>Line 4</b>"}), text("done")]
    client = make_app({"NOTE": calls, "BREAK": [RuntimeError("down")]})
    assert client.get("/api/notifications").json() == {"items": [], "unread": 0, "next_cursor": None}
    noted = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("a", "NOTE", tools=["notify_user"]),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "a"), edge("a", "out")],
        name="Totals",
    )
    run = _finished(client, client.post(f"/api/workflows/{noted}/runs", json={"input": "go"}).json())
    assert run["status"] == "succeeded", run
    broken = save(
        client,
        [{"id": "start", "type": "start"}, agent_node("a", "BREAK"), {"id": "out", "type": "output"}],
        [edge("start", "a"), edge("a", "out")],
        name="Broken",
    )
    failed = _finished(client, client.post(f"/api/workflows/{broken}/runs", json={"input": "go"}).json())
    assert failed["status"] == "failed"

    page = client.get("/api/notifications").json()
    assert page["unread"] == 2
    kinds = {item["kind"]: item for item in page["items"]}
    message, failure = kinds["message"], kinds["run_failed"]
    # The agent's text is plain text as written; interfaces never render it as HTML.
    assert (message["subject"], message["text"], message["run_id"]) == ("Check the totals", "<b>Line 4</b>", run["id"])
    assert failure["subject"] is None and failure["params"] == {"error_code": failed["error_code"]}
    assert failure["workflow_name"] == "Broken" and failure["level"] == "error"

    first = client.get("/api/notifications", params={"limit": 1}).json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = client.get("/api/notifications", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["items"][0]["id"] != first["items"][0]["id"] and second["next_cursor"] is None
    assert client.get("/api/notifications", params={"cursor": "bad!"}).json()["code"] == "notifications.invalid_cursor"

    assert client.post("/api/notifications/read", json={"ids": [failure["id"]]}).json() == {"marked": 1}
    assert [item["id"] for item in client.get("/api/notifications", params={"unread_only": True}).json()["items"]] == [
        message["id"]
    ]
    assert client.post("/api/notifications/read", json={}).json() == {"marked": 1}
    assert client.get("/api/notifications").json()["unread"] == 0


def test_a_workflow_without_rules_stays_quiet(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"BREAK": [RuntimeError("down")]})
    quiet = save(
        client,
        [{"id": "start", "type": "start"}, agent_node("a", "BREAK"), {"id": "out", "type": "output"}],
        [edge("start", "a"), edge("a", "out")],
        name="Quiet",
        notifications={"events": [], "recipients": "members"},
    )
    stored = client.get(f"/api/workflows/{quiet}").json()
    assert stored["notifications"] == {"events": [], "recipients": "members"}
    failed = _finished(client, client.post(f"/api/workflows/{quiet}/runs", json={"input": "go"}).json())
    assert failed["status"] == "failed"
    assert client.get("/api/notifications").json()["items"] == []
