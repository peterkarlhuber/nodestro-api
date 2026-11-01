"""The API of the live run chat (LRC-1): view, questions, instructions, idempotency, errors, permissions, the event
stream, export, deletion and tenant isolation. Every model answer is scripted."""

from __future__ import annotations

from collections.abc import Callable
import json
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest
from test_worker import connect, schema, server  # noqa: F401  (PostgreSQL server fixtures)

from nodestro_api import api_run_chat
from nodestro_api.permissions import Permission, Principal

from conftest import parsed, text

BOSS = "You coordinate teams"
CHAT = "You answer a person's question about a workflow run"


def edge(source: str, target: str) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source": source, "target": target}


def setup(make_app: Callable[..., TestClient], extra: dict[str, list[Any]] | None = None) -> tuple[TestClient, str]:
    """An orchestrator whose team waits at an approval: the run stays paused until the test decides."""
    ids: dict[str, str] = {}
    client = make_app(
        {
            BOSS: [
                parsed(lambda model: model(action="delegate", member=ids["team"], task="Draft", reason="start")),
                parsed(lambda model: model(action="finish", answer="final", reason="done")),
            ],
            "WRITER": [text("draft")],
            CHAT: [text("One team is working; it waits for an approval.")],
            **(extra or {}),
        }
    )
    team = client.post(
        "/api/workflows",
        json={
            "name": "Writer",
            "description": "writes",
            "nodes": [
                {"id": "s", "type": "start"},
                {"id": "w", "type": "agent", "label": "Writer", "agent": {"system_prompt": "WRITER"}},
                {"id": "ok", "type": "approval", "label": "Check", "approval": {"on_reject": "branch"}},
            ],
            "edges": [edge("s", "w"), edge("w", "ok")],
        },
    )
    assert team.status_code == 201, team.text
    ids["team"] = team.json()["id"]
    lead = client.post(
        "/api/workflows",
        json={
            "name": "Lead",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "lead",
                    "type": "orchestrator",
                    "label": "Lead",
                    "orchestrator": {"members": [{"workflow_id": ids["team"]}]},
                },
            ],
            "edges": [edge("start", "lead")],
        },
    )
    assert lead.status_code == 201, lead.text
    run = client.post(f"/api/workflows/{lead.json()['id']}/runs", json={"input": "go"})
    assert run.status_code == 201, run.text
    run_id = str(run.json()["id"])
    until(lambda: client.get(f"/api/runs/{run_id}").json()["status"] == "paused")
    return client, run_id


def until(probe: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def message(client: TestClient, run_id: str, message_id: str) -> dict[str, Any]:
    found = client.get(f"/api/runs/{run_id}/chat/messages/{message_id}")
    assert found.status_code == 200, found.text
    return dict(found.json())


def test_questions_and_instructions_through_the_api(make_app: Callable[..., TestClient]) -> None:
    client, run_id = setup(make_app)
    view = client.get(f"/api/runs/{run_id}/chat").json()
    assert view["active"] and view["run_status"] == "paused" and view["messages"] == []
    (target,) = view["targets"]
    assert (target["run_id"], target["node_id"], target["label"]) == (run_id, "lead", "Lead")
    assert target["steering"] and target["teams"][0]["name"] == "Writer" and target["teams"][0]["status"] == "paused"
    assert view["max_chars"] == 2000 and view["remaining_budget_usd"] == 0.5

    body = {"kind": "instruction", "target_run_id": run_id, "node_id": "lead", "content": "Add a summary"}
    sent = client.post(f"/api/runs/{run_id}/chat/messages", json={**body, "client_id": "tab-1-a"})
    assert sent.status_code == 201, sent.text
    assert (sent.json()["status"] == "queued" and "client_id" not in sent.json()) or sent.json()["client_id"] is None
    again = client.post(f"/api/runs/{run_id}/chat/messages", json={**body, "client_id": "tab-1-a"})
    assert again.status_code == 200 and again.json()["id"] == sent.json()["id"]
    other_window = client.post(f"/api/runs/{run_id}/chat/messages", json={**body, "client_id": "tab-2-a"})
    assert other_window.status_code == 200 and other_window.json()["id"] == sent.json()["id"]

    asked = client.post(
        f"/api/runs/{run_id}/chat/messages",
        json={"kind": "question", "target_run_id": run_id, "node_id": "lead", "content": "Where are we?"},
    )
    assert asked.status_code == 201 and asked.json()["status"] == "pending"
    until(lambda: message(client, run_id, asked.json()["id"])["status"] == "answered")
    answered = client.get(f"/api/runs/{run_id}/chat").json()
    answer = next(item for item in answered["messages"] if item["kind"] == "answer")
    assert answer["reply_to"] == asked.json()["id"] and "waits for an approval" in answer["content"]
    assert answer["usage"]["requests"] == 1 and answer["snapshot_digest"]
    assert answered["questions_left"] == 29
    usage = client.get("/api/usage").json()
    assert "run_chat" in json.dumps(usage)
    # The question changed nothing: still paused, the instruction still waits.
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "paused"
    assert message(client, run_id, sent.json()["id"])["status"] == "queued"

    approvals = client.get("/api/approvals").json()
    (waiting,) = approvals
    decided = client.post(
        f"/api/runs/{waiting['run_id']}/approvals/{waiting['node_id']}", json={"decision": "approved"}
    )
    assert decided.status_code == 200, decided.text
    until(lambda: client.get(f"/api/runs/{run_id}").json()["status"] == "succeeded")
    done = message(client, run_id, sent.json()["id"])
    assert done["status"] == "considered" and done["step"] == 1 and done["decision"]["action"] == "finish"
    boss_prompts = [str(call["messages"]) for call in client.fake.messages.calls if BOSS in str(call.get("system"))]  # type: ignore[attr-defined]
    assert "Add a summary" in boss_prompts[1] and "Add a summary" not in boss_prompts[0]

    # After the end: the history stays readable, nothing can be sent any more.
    ended = client.get(f"/api/runs/{run_id}/chat").json()
    assert not ended["active"] and len(ended["messages"]) == 3
    late = client.post(f"/api/runs/{run_id}/chat/messages", json={**body, "content": "One more"})
    assert late.status_code == 409 and late.json()["code"] == "run_chat.run_not_active"
    late_question = client.post(f"/api/runs/{run_id}/chat/messages", json={**body, "kind": "question"})
    assert late_question.status_code == 409 and late_question.json()["code"] == "run_chat.run_not_active"

    # The event stream of an ended run: the snapshot, then the end.
    with client.stream("GET", f"/api/runs/{run_id}/chat/events") as stream:
        lines = [line for line in stream.iter_lines() if line]
    assert lines[0] == "event: snapshot" and json.loads(lines[1][6:])["run_id"] == run_id
    assert lines[-2] == "event: end"


def test_errors_and_limits(make_app: Callable[..., TestClient]) -> None:
    client, run_id = setup(make_app)
    url = f"/api/runs/{run_id}/chat/messages"
    base = {"kind": "instruction", "target_run_id": run_id, "node_id": "lead"}
    missing = client.post(url, json={**base, "node_id": "nope", "content": "x"})
    assert missing.status_code == 404 and missing.json()["code"] == "run_chat.unknown_target"
    assert client.post(url, json={**base, "content": "   "}).json()["code"] == "run_chat.empty_message"
    too_long = client.post(url, json={**base, "content": "x" * 2001})
    assert too_long.status_code == 400 and too_long.json()["code"] == "run_chat.message_too_long"
    assert client.post(url, json={**base, "content": "x" * 5000}).status_code == 422  # capped by the route model
    assert client.post(url, json={**base, "content": "ok", "client_id": "bad key!"}).status_code == 400
    assert client.get("/api/runs/run_unknown/chat").status_code == 404
    assert client.get(f"/api/runs/{run_id}/chat/messages/msg_unknown").status_code == 404
    for number in range(2):
        asked = client.post(url, json={**base, "kind": "question", "content": f"Q{number}"})
        assert asked.status_code == 201
    # The answers come from the script; the third question within the minute ... is fine, the limit is 6.
    until(
        lambda: all(item["status"] != "pending" for item in client.get(f"/api/runs/{run_id}/chat").json()["messages"])
    )


def test_reading_needs_read_and_sending_needs_run(make_app: Callable[..., TestClient]) -> None:
    client, run_id = setup(make_app)
    reader = Principal(id="reader", permissions=frozenset({Permission.READ}))
    client.app.state.principal_resolver = lambda _connection: reader  # type: ignore[attr-defined]
    assert client.get(f"/api/runs/{run_id}/chat").status_code == 200
    refused = client.post(
        f"/api/runs/{run_id}/chat/messages",
        json={"kind": "instruction", "target_run_id": run_id, "node_id": "lead", "content": "Go"},
    )
    assert refused.status_code == 403 and refused.json()["params"] == {"permission": "run"}
    runner = Principal(id="runner", permissions=frozenset({Permission.READ, Permission.RUN}))
    client.app.state.principal_resolver = lambda _connection: runner  # type: ignore[attr-defined]
    sent = client.post(
        f"/api/runs/{run_id}/chat/messages",
        json={"kind": "instruction", "target_run_id": run_id, "node_id": "lead", "content": "Go", "author": "x"},
    )
    assert sent.status_code == 201 and sent.json()["author"] == "runner"  # never a field of the body


def test_export_and_delete_carry_the_chat(make_app: Callable[..., TestClient]) -> None:
    client, run_id = setup(make_app)
    client.post(
        f"/api/runs/{run_id}/chat/messages",
        json={
            "kind": "instruction",
            "target_run_id": run_id,
            "node_id": "lead",
            "content": "Write to jane.doe@example.com",
        },
    )
    full = client.get(f"/api/runs/{run_id}/export").json()
    assert full["chat"][0]["content"] == "Write to jane.doe@example.com" and "client_id" not in full["chat"][0]
    masked = client.get(f"/api/runs/{run_id}/export?mode=sanitized").json()
    assert "jane.doe@example.com" not in json.dumps(masked)
    removed = client.get(f"/api/runs/{run_id}/export?mode=sanitized&contents=true").json()
    assert removed["chat"][0]["content"].startswith("[REMOVED:")
    assert client.post(f"/api/runs/{run_id}/cancel").json() == {"cancelled": True}
    until(lambda: client.get(f"/api/runs/{run_id}").json()["status"] == "cancelled")
    view = client.get(f"/api/runs/{run_id}/chat").json()
    assert view["messages"][0]["status"] == "expired"  # never shown as taken into account
    assert client.delete(f"/api/runs/{run_id}").status_code == 204
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    assert ctx.runs.chat.messages(run_id) == []


def test_the_event_stream_sends_changes(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_run_chat, "POLL_SECONDS", 0.05)
    client, run_id = setup(make_app)
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    with client.stream("GET", f"/api/runs/{run_id}/chat/events") as stream:
        lines = stream.iter_lines()
        assert next(lines) == "event: snapshot"
        snapshot = json.loads(next(lines)[6:])
        assert snapshot["active"] and snapshot["targets"][0]["node_id"] == "lead"
        ctx.runs.chat.submit(
            run_id=run_id, target_run_id=run_id, node_id="lead", kind="instruction", content="From another process"
        )
        for line in lines:
            if line.startswith("data:") and "From another process" in line:
                assert json.loads(line[6:])["status"] == "queued"
                break


def test_conversations_of_another_tenant_are_not_found(server: Any) -> None:  # noqa: F811
    from test_worker import _start

    alice = server.member("acme", "alice", "owner")
    mallory = server.member("globex", "mallory", "owner")
    run_id = _start(alice)
    assert alice.get(f"/api/runs/{run_id}/chat").status_code == 200
    assert mallory.get(f"/api/runs/{run_id}/chat").status_code == 404
    assert mallory.get(f"/api/runs/{run_id}/chat/events").status_code == 404
    body = {"kind": "instruction", "target_run_id": run_id, "node_id": "x", "content": "steal"}
    assert mallory.post(f"/api/runs/{run_id}/chat/messages", json=body).status_code == 404
    viewer = server.member("acme", "victor", "viewer")
    assert viewer.get(f"/api/runs/{run_id}/chat").status_code == 200
    assert viewer.post(f"/api/runs/{run_id}/chat/messages", json=body).status_code == 403
