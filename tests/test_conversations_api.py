"""The API of conversations with own agents (N-04): the switch, the routes, the run behind each answer, its events in
the run's format, cancel, export, files and owners. Tenants on a server: ``test_conversations_api_server``. The
runtime's own tests cover the model rules, cancelling a local model request and images in detail."""

from __future__ import annotations

from collections.abc import Callable
import json
import struct
import threading
import time
from typing import Any
import zlib

from fastapi.testclient import TestClient
from nodestro_runtime.conversations import CONVERSATIONS_ENV
import pytest

from nodestro_api.permissions import ALL_PERMISSIONS, Principal

from conftest import text, tool_use

BASE = "/api/conversations"
CLOUD = "anthropic:claude-sonnet-5-5"


def png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def chatting(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    return make_app({"AGENT": [tool_use("calculator", {"expression": "6 * 7"}), text("It is 42."), text("Bye.")]})


def template(client: TestClient, **agent: Any) -> str:
    created = client.post(
        "/api/agents/templates", json={"agent": {"name": "Helper", "system_prompt": "AGENT", **agent}}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def conversation(client: TestClient, **fields: Any) -> dict[str, Any]:
    body = {"agent": {"template_id": template(client, tools=["calculator"])}, "model": CLOUD, **fields}
    created = client.post(BASE, json=body)
    assert created.status_code == 201, created.text
    return created.json()  # type: ignore[no-any-return]


def wait(client: TestClient, run_id: str) -> None:
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    client.portal.call(ctx.wait, run_id)  # type: ignore[union-attr]


def events(body: str) -> list[tuple[str, Any]]:
    found = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in lines:
            found.append((lines["event"], json.loads(lines["data"])))
    return found


def _as(client: TestClient, principal: Principal) -> None:
    client.app.state.principal_resolver = lambda _connection: principal  # type: ignore[attr-defined]


def test_without_the_switch_every_route_is_not_found(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    for method, path in [
        ("get", BASE),
        ("post", BASE),
        ("get", f"{BASE}/conv_x"),
        ("patch", f"{BASE}/conv_x"),
        ("delete", f"{BASE}/conv_x"),
        ("get", f"{BASE}/conv_x/export"),
        ("post", f"{BASE}/conv_x/messages"),
        ("get", f"{BASE}/conv_x/messages/msg_x"),
        ("post", f"{BASE}/conv_x/messages/msg_x/cancel"),
        ("get", f"{BASE}/conv_x/messages/msg_x/events"),
        ("delete", f"{BASE}/conv_x/files/in_x"),
        ("get", f"{BASE}/conv_x/files/in_x/content"),
    ]:
        body = {"agent": {"template_id": "tpl_x"}} if path == BASE else {}
        kwargs: dict[str, Any] = {"json": body} if method in ("post", "patch") else {}
        response = getattr(client, method)(path, **kwargs)
        assert (response.status_code, response.json()["code"]) == (404, "conversation.disabled"), (method, path)
    uploaded = client.post(f"{BASE}/conv_x/files", files=[("files", ("a.txt", b"x", "text/plain"))])
    assert (uploaded.status_code, uploaded.json()["code"]) == (404, "conversation.disabled")


def test_create_send_follow_rename_export_and_delete(chatting: TestClient) -> None:
    client = chatting
    created = conversation(client)
    conversation_id = created["id"]
    assert (created["model"], created["offline"], created["provenance"], created["messages"]) == (
        CLOUD,
        False,
        "unchecked",
        [],
    )
    sent = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "What is 6 times 7?"})
    assert sent.status_code == 201, sent.text
    answer = sent.json()["assistant"]
    wait(client, answer["run_id"])

    # The answer is a run of the engine, with the conversation as its trigger.
    run = client.get(f"/api/runs/{answer['run_id']}").json()
    assert (run["trigger"], run["trigger_ref"], run["status"]) == ("conversation", conversation_id, "succeeded")
    message = client.get(f"{BASE}/{conversation_id}/messages/{answer['id']}").json()
    assert (message["status"], message["content"]) == ("succeeded", "It is 42.")
    [call] = message["tool_calls"]
    assert call["name"] == "calculator" and call["arguments"] == {"expression": "6 * 7"}
    assert "42" in call["result"] and call["duration_ms"] is not None
    assert message["cost_usd"] == run["usage"]["estimated_cost_usd"]

    # The events of a finished answer: its snapshot, then the end.
    stream = client.get(f"{BASE}/{conversation_id}/messages/{answer['id']}/events")
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert [kind for kind, _ in events(stream.text)] == ["snapshot", "end"]
    assert events(stream.text)[1][1] == {"status": "succeeded"}

    detail = client.get(f"{BASE}/{conversation_id}").json()
    assert detail["title"] == "What is 6 times 7?" and detail["usage"]["estimated_cost_usd"] == pytest.approx(
        run["usage"]["estimated_cost_usd"]
    )
    renamed = client.patch(f"{BASE}/{conversation_id}", json={"title": "Arithmetic"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "Arithmetic"
    assert [item["title"] for item in client.get(BASE).json()] == ["Arithmetic"]

    exported = client.get(f"{BASE}/{conversation_id}/export")
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.text.startswith("# Arithmetic\n") and "It is 42." in exported.text

    assert client.delete(f"{BASE}/{conversation_id}").status_code == 204
    missing = client.get(f"{BASE}/{conversation_id}")
    assert (missing.status_code, missing.json()["code"]) == (404, "conversation.not_found")
    # The answer's run went with it (H-1): it held the conversation.
    assert client.get(f"/api/runs/{answer['run_id']}").status_code == 404


def test_a_live_answer_streams_the_run_events_and_can_be_cancelled(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    gate = threading.Event()

    def held(_request: dict[str, Any]) -> Any:
        assert gate.wait(10)
        return text("Late.")

    client = make_app({"AGENT": [held, held]})
    conversation_id = conversation(client)["id"]

    # A live answer: the stream sends its snapshot, then the run's events in their format, then the end.
    sent = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "Hi"}).json()["assistant"]
    received: list[str] = []

    def follow() -> None:
        with client.stream("GET", f"{BASE}/{conversation_id}/messages/{sent['id']}/events") as stream:
            received.append(stream.read().decode())

    reader = threading.Thread(target=follow)
    reader.start()
    time.sleep(0.5)
    gate.set()
    reader.join(15)
    found = events(received[0])
    assert found[0][0] == "snapshot" and found[0][1]["id"] == sent["id"]
    assert found[-1] == ("end", {"status": "succeeded"})
    streamed = [data for kind, data in found if kind == "event"]
    assert streamed and all(item["run_id"] == sent["run_id"] for item in streamed)
    assert "node_finished" in {item["type"] for item in streamed}

    # A running answer is cancelled through its run.
    gate.clear()
    second = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "Again"}).json()["assistant"]
    busy = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "Third"})
    assert (busy.status_code, busy.json()["code"]) == (409, "conversation.answer_running")
    time.sleep(0.3)
    assert client.post(f"{BASE}/{conversation_id}/messages/{second['id']}/cancel").status_code == 200
    gate.set()
    wait(client, second["run_id"])
    ended = client.get(f"{BASE}/{conversation_id}/messages/{second['id']}").json()
    assert ended["status"] == "cancelled"
    assert client.get(f"/api/runs/{second['run_id']}").json()["status"] == "cancelled"


def test_refusals_of_a_message(chatting: TestClient) -> None:
    client = chatting
    conversation_id = conversation(client, budget_usd=0)["id"]
    empty = client.post(f"{BASE}/{conversation_id}/messages", json={"content": " "})
    assert (empty.status_code, empty.json()["code"]) == (400, "conversation.empty_message")
    spent = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "Hi"})
    assert (spent.status_code, spent.json()["code"]) == (409, "conversation.budget_exhausted")
    unknown = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "Hi", "file_ids": ["in_000000000000"]})
    assert (unknown.status_code, unknown.json()["code"]) == (404, "conversation.not_found")
    assert client.get(f"{BASE}/{conversation_id}").json()["messages"] == []
    agent = client.post(BASE, json={"agent": {"template_id": "tpl_missing"}, "model": CLOUD})
    assert (agent.status_code, agent.json()["code"]) == (404, "conversation.agent_not_found")
    offline = client.post(BASE, json={"agent": {"template_id": template(client)}, "model": CLOUD, "offline": True})
    assert (offline.status_code, offline.json()["code"]) == (422, "conversation.offline_cloud_model")


def test_no_cloud_model_is_chosen_without_being_asked(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    cloud_defaults = make_app()  # the cloud defaults of an existing installation
    refused = cloud_defaults.post(BASE, json={"agent": {"template_id": template(cloud_defaults, model=CLOUD)}})
    assert (refused.status_code, refused.json()["code"]) == (422, "conversation.model_required")
    local_defaults = make_app(local_defaults=True)
    created = local_defaults.post(BASE, json={"agent": {"template_id": template(local_defaults, model=CLOUD)}})
    assert created.status_code == 201 and created.json()["model"].startswith("ollama:")


def test_switching_agent_and_model(chatting: TestClient) -> None:
    client = chatting
    created = conversation(client)
    other = template(client, name="Other")
    switched = client.patch(
        f"{BASE}/{created['id']}", json={"agent": {"template_id": other}, "model": "anthropic:claude-opus-5-5"}
    )
    assert switched.status_code == 200
    assert (switched.json()["agent"], switched.json()["model"]) == (
        {"template_id": other, "version": 1},
        "anthropic:claude-opus-5-5",
    )
    assert switched.json()["title"] == created["title"]


def test_the_history_strategy_of_a_conversation(chatting: TestClient) -> None:
    """N-05: ``history`` (``trim`` by default, or ``summarize``) and the helper ``summary_model`` are set on create and
    change; an offline conversation refuses a cloud helper. An answer whose history fits has no ``summary``."""
    client = chatting
    created = conversation(client)
    assert (created["history"], created["summary_model"]) == ("trim", None)
    changed = client.patch(f"{BASE}/{created['id']}", json={"history": "summarize", "summary_model": CLOUD})
    assert changed.status_code == 200, changed.text
    assert (changed.json()["history"], changed.json()["summary_model"]) == ("summarize", CLOUD)
    cleared = client.patch(f"{BASE}/{created['id']}", json={"summary_model": ""})
    assert cleared.json()["summary_model"] is None and cleared.json()["history"] == "summarize"
    assert client.patch(f"{BASE}/{created['id']}", json={"history": "forget"}).status_code == 422
    refused = client.post(
        BASE,
        json={
            "agent": {"template_id": template(client)},
            "model": "ollama:qwen3.5:4b",
            "offline": True,
            "summary_model": CLOUD,
        },
    )
    assert refused.status_code >= 400 and refused.json()["code"] == "conversation.offline_cloud_model"
    sent = client.post(f"{BASE}/{created['id']}/messages", json={"content": "What is 6 * 7?"})
    assert sent.status_code in (200, 201, 202), sent.text
    wait(client, sent.json()["assistant"]["run_id"])
    answer = client.get(f"{BASE}/{created['id']}").json()["messages"][-1]
    assert answer["status"] == "succeeded" and answer["summary"] is None


def test_files_of_a_conversation(chatting: TestClient) -> None:
    client = chatting
    conversation_id = conversation(client)["id"]
    uploaded = client.post(
        f"{BASE}/{conversation_id}/files",
        files=[("files", ("dot.png", png(), "image/png")), ("files", ("empty.txt", b"", "text/plain"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    first, refused = uploaded.json()
    assert first["file"]["name"] == "dot.png" and refused["problem"]["code"].startswith("input.")
    file_id = first["file"]["id"]
    content = client.get(f"{BASE}/{conversation_id}/files/{file_id}/content")
    assert content.status_code == 200 and content.content == png()
    assert "attachment" in content.headers["content-disposition"]
    assert client.get(f"/api/inputs/{file_id}").json()["origin"] == "conversation"
    removed = client.delete(f"{BASE}/{conversation_id}/files/{file_id}")
    assert removed.status_code == 200 and removed.json()["files"] == []
    again = client.delete(f"{BASE}/{conversation_id}/files/{file_id}")
    assert (again.status_code, again.json()["code"]) == (404, "conversation.not_found")


def test_another_users_conversation_and_files_do_not_exist(chatting: TestClient) -> None:
    """On a server a conversation is its owner's: another member gets 404 on every route, its files included."""
    client = chatting
    alice = Principal(id="usr_alice", role="owner", permissions=ALL_PERMISSIONS)
    bob = Principal(id="usr_bob", role="owner", permissions=ALL_PERMISSIONS)
    _as(client, alice)
    conversation_id = conversation(client)["id"]
    uploaded = client.post(f"{BASE}/{conversation_id}/files", files=[("files", ("a.txt", b"hello", "text/plain"))])
    file_id = uploaded.json()[0]["file"]["id"]
    _as(client, bob)
    assert client.get(BASE).json() == []
    for method, path in [
        ("get", f"{BASE}/{conversation_id}"),
        ("patch", f"{BASE}/{conversation_id}"),
        ("delete", f"{BASE}/{conversation_id}"),
        ("get", f"{BASE}/{conversation_id}/export"),
        ("post", f"{BASE}/{conversation_id}/messages"),
        ("get", f"{BASE}/{conversation_id}/files/{file_id}/content"),
    ]:
        kwargs: dict[str, Any] = (
            {"json": {"content": "x"} if method == "post" else {}} if method in ("post", "patch") else {}
        )
        response = getattr(client, method)(path, **kwargs)
        assert (response.status_code, response.json()["code"]) == (404, "conversation.not_found"), (method, path)
    # Nor is the file reachable as an input file, nor usable as another person's run input.
    assert client.get(f"/api/inputs/{file_id}").status_code == 404
    _as(client, alice)
    assert client.get(f"/api/inputs/{file_id}").status_code == 200
    assert client.get(f"{BASE}/{conversation_id}").status_code == 200


# ---- review fixes (independent review of N-04) ---------------------------------------------------------------------


def _run_routes(client: TestClient) -> list[tuple[str, str]]:
    """Every studio route with a run id in its path (not the published APIs, not benchmark runs)."""
    from nodestro_api.permissions import check_routes

    return sorted(
        {
            (item.method, item.path)
            for item in check_routes(client.app)  # type: ignore[arg-type]
            if "{run_id}" in item.path and not item.path.startswith(("/api/published", "/api/benchmark-runs"))
        }
    )


def test_h1_another_users_answer_run_does_not_exist_on_any_run_route(chatting: TestClient) -> None:
    """H-1: an answer's run holds the conversation (input), the answer (output) and its files. Another member gets
    404 on every route with a run id, and the run is left out of the run list and of the assistant's attachments."""
    client = chatting
    alice = Principal(id="usr_alice", role="owner", permissions=ALL_PERMISSIONS)
    bob = Principal(id="usr_bob", role="owner", permissions=ALL_PERMISSIONS)
    _as(client, alice)
    conversation_id = conversation(client)["id"]
    sent = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "What is 6 times 7?"}).json()
    run_id = sent["assistant"]["run_id"]
    wait(client, run_id)
    assert client.get(f"/api/runs/{run_id}").status_code == 200
    assert run_id in {item["id"] for item in client.get("/api/runs").json()}

    _as(client, bob)
    routes = _run_routes(client)
    assert len(routes) >= 20  # runs, export, files, cancel, resume, approvals, tool approvals, budget, chat, events
    for method, path in routes:
        url = path.format(run_id=run_id, file_id="f_1", node_id="agent", request_id="tap_1", message_id="m_1")
        response = client.request(method, url, json={})
        assert response.status_code == 404, (method, path, response.status_code)
        assert response.json()["code"] == "run.not_found", (method, path)
    assert run_id not in {item["id"] for item in client.get("/api/runs").json()}
    assert client.get("/api/runs", params={"trigger_ref": conversation_id}).json() == []
    assistant = client.post("/api/assistant/conversations", json={}).json()
    attached = client.post(
        f"/api/assistant/conversations/{assistant['id']}/attachments", json={"kind": "run", "ref_id": run_id}
    )
    assert (attached.status_code, attached.json()["code"]) == (404, "run.not_found")

    _as(client, alice)  # nothing was changed by the other member
    run = client.get(f"/api/runs/{run_id}")
    assert run.status_code == 200 and run.json()["status"] == "succeeded"


def test_n1_another_users_answer_run_is_no_preview_context(chatting: TestClient) -> None:
    """N-1: the context preview reads a run's input and outputs. Another member's answer run is not found there, by
    its id or as the latest run of the conversation id."""
    client = chatting
    alice = Principal(id="usr_alice", role="owner", permissions=ALL_PERMISSIONS)
    bob = Principal(id="usr_bob", role="owner", permissions=ALL_PERMISSIONS)
    _as(client, alice)
    conversation_id = conversation(client)["id"]
    sent = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "What is 6 times 7?"}).json()
    run_id = sent["assistant"]["run_id"]
    wait(client, run_id)
    workflow = {
        "name": "W",
        "nodes": [
            {"id": "agent", "type": "agent", "agent": {"name": "A", "system_prompt": "S", "task": "{{run.input}}"}}
        ],
        "edges": [],
    }

    def preview(**body: Any) -> Any:
        return client.post("/api/workflows/preview", json={"workflow": workflow, "node_id": "agent", **body})

    own = preview(run_id=run_id)
    assert own.status_code == 200, own.text
    assert own.json()["run_id"] == run_id
    assert preview(workflow_id=conversation_id).json()["run_id"] == run_id

    _as(client, bob)
    other = preview(run_id=run_id)
    assert (other.status_code, other.json()["code"]) == (404, "run.not_found")
    latest = preview(workflow_id=conversation_id)
    assert latest.status_code == 200 and latest.json()["run_id"] is None
    assert "6 times 7" not in latest.text


def test_n2_listing_runs_reads_no_conversation_for_a_run_that_is_no_answer(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-2: the run list checks each run's own trigger first. Only an answer's run costs a read of its
    conversation."""
    from test_runs import agent_node, create, start

    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    client = make_app({"R": [text("one"), text("two"), text("three")]})
    workflow_id = create(client, [agent_node("research", "R")], [])
    for word in ("a", "b", "c"):
        start(client, workflow_id, word)
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    reads: list[str] = []
    real = type(ctx.store).get

    def counting(store: Any, collection: str, *args: Any, **kwargs: Any) -> Any:
        reads.append(collection)
        return real(store, collection, *args, **kwargs)

    monkeypatch.setattr(type(ctx.store), "get", counting)
    assert len(client.get("/api/runs").json()) == 3
    assert "conversations" not in reads
    assert reads.count("runs") == 0


def test_l2_deleting_a_conversation_revokes_its_grants(chatting: TestClient) -> None:
    from nodestro_runtime.tool_approvals import AccessRules, Person
    from nodestro_runtime.tool_policy import AccessResource, ToolFacts

    client = chatting
    conversation_id = conversation(client)["id"]
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    info = ctx.tools.get("calculator")
    granted = AccessRules(ctx.store).grant(
        resource=AccessResource(type="tool", id="calculator"),
        tool=ToolFacts.of(info.capability, version=info.version),
        actions=["execute"],
        duration="conversation",
        conversation_id=conversation_id,
        by=Person("local"),
    )
    assert client.delete(f"{BASE}/{conversation_id}").status_code == 204
    [stored] = [item for item in AccessRules(ctx.store).grants() if item.id == granted.id]
    assert (stored.status, stored.revoked_by) == ("revoked", "local")


def test_l5_the_answer_stream_ends_when_the_conversation_is_deleted(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    gate = threading.Event()

    def held(_request: dict[str, Any]) -> Any:
        assert gate.wait(10)
        return text("Late.")

    client = make_app({"AGENT": [held]})
    conversation_id = conversation(client)["id"]
    sent = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "Hi"}).json()["assistant"]
    received: list[str] = []

    def follow() -> None:
        with client.stream("GET", f"{BASE}/{conversation_id}/messages/{sent['id']}/events") as stream:
            received.append(stream.read().decode())

    reader = threading.Thread(target=follow)
    reader.start()
    time.sleep(0.3)
    deleting = threading.Thread(target=lambda: client.delete(f"{BASE}/{conversation_id}"))
    deleting.start()
    time.sleep(0.3)
    gate.set()
    deleting.join(20)
    reader.join(20)
    found = events(received[0])
    assert found[0][0] == "snapshot"
    assert found[-1] == ("end", {"reason": "deleted"})


# ---- RT-089 follow-ups ---------------------------------------------------------------------------------------------


def test_meta_names_conversations_only_while_they_are_served(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The studio shows its conversation area only when ``/api/meta`` lists the feature (off by default)."""
    from fastapi import FastAPI

    from nodestro_api.api import served_features

    client = make_app()
    assert client.get("/api/meta").json()["features"] == []
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    assert client.get("/api/meta").json()["features"] == ["conversations"]
    assert served_features(FastAPI()) == []  # an app without the router group does not claim it


def test_rt089_earlier_files_reach_later_answers_of_the_owner_only(chatting: TestClient) -> None:
    """A file sent with an earlier message is offered to the next answer (its run's reference files); another member
    reaches neither the file nor the run, and cannot send it in a conversation of their own."""
    client = chatting
    alice = Principal(id="usr_alice", role="owner", permissions=ALL_PERMISSIONS)
    bob = Principal(id="usr_bob", role="owner", permissions=ALL_PERMISSIONS)
    _as(client, alice)
    conversation_id = conversation(client)["id"]
    uploaded = client.post(f"{BASE}/{conversation_id}/files", files=[("files", ("a.txt", b"hello", "text/plain"))])
    file_id = uploaded.json()[0]["file"]["id"]
    first = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "Keep it", "file_ids": [file_id]})
    wait(client, first.json()["assistant"]["run_id"])
    second = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "And now?"})
    assert second.status_code == 201, second.text
    run_id = second.json()["assistant"]["run_id"]
    wait(client, run_id)
    run = client.get(f"/api/runs/{run_id}").json()
    assert (run["file_ids"], run["reference_file_ids"]) == ([], [file_id])
    assert [(item["file_id"], item["mode"]) for item in run["nodes"]["agent"]["files"]] == [(file_id, "overview")]

    _as(client, bob)
    assert client.get(f"/api/runs/{run_id}").status_code == 404
    assert client.get(f"/api/inputs/{file_id}").status_code == 404
    assert client.get(f"{BASE}/{conversation_id}/files/{file_id}/content").status_code == 404
    own = conversation(client)["id"]
    refused = client.post(f"{BASE}/{own}/messages", json={"content": "x", "file_ids": [file_id]})
    assert (refused.status_code, refused.json()["code"]) == (404, "conversation.not_found")


# ---- W3-P4a review M-1: an imported trace never runs and never reaches another member's file ----------------------

SECRET = "ALICE-SECRET-4f1d"


@pytest.fixture
def imported(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str, str]:
    """Alice's conversation file, and Bob's import of a trace paused at an approval that names it as a reference file
    and a run file (the reviewer's proof of concept, W3-P4a M-1)."""
    from test_flow import edge, save
    from test_flow import wait as wait_run
    from test_runs import agent_node

    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    client = make_app({"AGENT": [text("ok")], "READER": []})
    alice = Principal(id="usr_alice", role="owner", permissions=ALL_PERMISSIONS)
    bob = Principal(id="usr_bob", role="owner", permissions=ALL_PERMISSIONS)

    _as(client, alice)
    conversation_id = client.post(BASE, json={"agent": {"template_id": template(client)}, "model": CLOUD}).json()["id"]
    uploaded = client.post(
        f"{BASE}/{conversation_id}/files", files=[("files", ("plan.txt", SECRET.encode(), "text/plain"))]
    )
    file_id = uploaded.json()[0]["file"]["id"]
    sent = client.post(f"{BASE}/{conversation_id}/messages", json={"content": "keep", "file_ids": [file_id]})
    wait(client, sent.json()["assistant"]["run_id"])

    _as(client, bob)
    assert client.get(f"/api/inputs/{file_id}").status_code == 404  # Bob cannot reach the file directly
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            {"id": "check", "type": "approval", "label": "Review"},
            agent_node("reader", "READER"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "check"), edge("check", "reader", "approved"), edge("reader", "out")],
    )
    started = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "hi"})
    paused = wait_run(client, started.json()["id"])
    assert paused["status"] == "paused"
    trace = client.get(f"/api/runs/{paused['id']}").json()
    trace.update(reference_file_ids=[file_id], file_ids=[file_id], input="hi again")
    response = client.post("/api/runs/import", content=json.dumps(trace))
    assert response.status_code == 201, response.text
    script = client.fake.messages.script["READER"]  # type: ignore[attr-defined]
    script += [tool_use("input_files", {"command": "read", "file_id": file_id}), text("done")]
    client.calls_before = len(client.fake.messages.calls)  # type: ignore[attr-defined]  # Alice's own answer read it
    return client, file_id, response.json()["run"]["id"]


def _bobs_calls(client: TestClient) -> str:
    """The model requests since Bob's import."""
    calls = client.fake.messages.calls[client.calls_before :]  # type: ignore[attr-defined]
    return json.dumps(calls, default=str)


def test_w3p4a_m1_an_imported_trace_is_stopped_names_no_files_and_is_never_executed(
    imported: tuple[TestClient, str, str],
) -> None:
    client, _file_id, run_id = imported
    stored = client.get(f"/api/runs/{run_id}").json()
    assert stored["status"] == "cancelled" and stored["imported_at"] is not None
    assert stored["file_ids"] == [] and stored["reference_file_ids"] == []
    decided = client.post(f"/api/runs/{run_id}/approvals/check", json={"decision": "approved"})
    assert decided.status_code == 400, decided.text
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "cancelled"
    assert SECRET not in _bobs_calls(client)


def test_w3p4a_m1_an_imported_run_stored_paused_is_never_executed_and_reads_no_file(
    imported: tuple[TestClient, str, str],
) -> None:
    """An imported run stored as paused (an import of an earlier version) is refused with ``run.imported``: its
    approval is not recorded, the run does not continue, and no model reads the file it names."""
    from nodestro_runtime.schemas import Run

    client, file_id, run_id = imported
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    stored = ctx.runs.get(run_id)
    source = ctx.runs.get(stored.source_run_id)
    legacy = source.model_copy(
        update={"id": "run_legacy_import", "imported_at": stored.imported_at, "reference_file_ids": [file_id]}
    )
    ctx.store.put("runs", legacy.id, legacy)
    decided = client.post(f"/api/runs/{legacy.id}/approvals/check", json={"decision": "approved"})
    assert (decided.status_code, decided.json()["code"]) == (400, "run.imported")
    kept = ctx.store.get("runs", legacy.id, Run)
    assert kept.status == "paused" and kept.nodes["check"].approval is None
    time.sleep(0.2)
    assert SECRET not in _bobs_calls(client)
