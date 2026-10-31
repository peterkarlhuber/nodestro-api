"""The Nodestro Assistant: conversations, documentation, attachments, handovers, usage.

Every answer comes from a scripted model (conftest.ScriptedClient); nothing calls the API.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

import anthropic
from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import text, tool_use
from test_runs import agent_node, create, start

ASSISTANT = "You are the Nodestro Assistant"


def chat(client: TestClient) -> str:
    response = client.post("/api/assistant/conversations", json={})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def wait(client: TestClient, conversation_id: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/assistant/conversations/{conversation_id}").json()
        if not detail["streaming"]:
            return detail  # type: ignore[no-any-return]
        time.sleep(0.03)
    raise AssertionError("The answer did not finish")


def send(client: TestClient, conversation_id: str, content: str, language: str = "de") -> dict[str, Any]:
    response = client.post(
        f"/api/assistant/conversations/{conversation_id}/messages", json={"content": content, "language": language}
    )
    assert response.status_code == 201, response.text
    return wait(client, conversation_id)


def requests_of(client: TestClient) -> list[dict[str, Any]]:
    return [call for call in client.fake.messages.calls if ASSISTANT in str(call.get("system"))]  # type: ignore[attr-defined]


def user_text(request: dict[str, Any]) -> str:
    content = request["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if isinstance(block, dict))


def last_user_prompt(client: TestClient) -> str:
    """The text of the last user prompt (not a tool result) the model received."""
    for request in reversed(requests_of(client)):
        for message in reversed(request["messages"]):
            if message["role"] != "user":
                continue
            content = message["content"]
            if isinstance(content, str):
                return content
            texts = [
                block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
            ]
            if texts:
                return "".join(texts)
    raise AssertionError("no user prompt")


# ---- conversations ---------------------------------------------------------------------


def test_conversations_are_created_continued_renamed_and_deleted(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Hallo! Wie kann ich helfen?"), text("Gern, noch etwas?")]})
    conversation_id = chat(client)
    detail = send(client, conversation_id, "Hallo, wer bist du eigentlich?")
    assert [m["role"] for m in detail["history"]] == ["user", "assistant"]
    assert detail["history"][1]["content"] == "Hallo! Wie kann ich helfen?"
    assert detail["history"][1]["status"] == "complete"
    # The title comes from the first message, without another request.
    assert detail["title"] == "Hallo, wer bist du eigentlich?"
    assert len(requests_of(client)) == 1

    detail = send(client, conversation_id, "Danke")
    assert len(detail["history"]) == 4
    # The earlier exchange goes back to the model as history.
    second = requests_of(client)[-1]
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert second["messages"][1]["content"] == "Hallo! Wie kann ich helfen?"

    renamed = client.patch(f"/api/assistant/conversations/{conversation_id}", json={"title": "Begrüßung"}).json()
    assert renamed["title"] == "Begrüßung" and renamed["auto_title"] is False
    listed = client.get("/api/assistant/conversations").json()
    assert [(item["id"], item["title"], item["messages"]) for item in listed] == [(conversation_id, "Begrüßung", 4)]

    other = chat(client)
    assert client.get("/api/assistant/conversations").json()[0]["id"] == other
    assert client.delete(f"/api/assistant/conversations/{conversation_id}").status_code == 204
    assert client.get(f"/api/assistant/conversations/{conversation_id}").status_code == 404
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    with store._lock:
        left = store._connection.execute(
            "SELECT count(*) FROM nodestro_documents WHERE id LIKE ?", (f"{conversation_id}:%",)
        ).fetchone()[0]
    assert left == 0


def test_long_first_messages_get_a_short_title() -> None:
    from nodestro_api.assistant import make_title

    title = make_title(
        "Wie kann ich in Nodestro einen bestehenden Workflow mit der KI verbessern lassen und dabei sparen?", []
    )
    assert len(title) <= 61 and title.endswith("…")
    assert make_title("**Hilfe**\nmehr", []) == "Hilfe"


def test_a_conversation_survives_a_restart_mid_answer(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Antwort")]})
    conversation_id = chat(client)
    send(client, conversation_id, "Frage")
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    from nodestro_api.assistant import ChatMessage

    message = store.list_prefix("assistant_messages", f"{conversation_id}:", ChatMessage)[1]
    message.status = "streaming"  # as if the server had stopped while writing it
    store.put("assistant_messages", f"{conversation_id}:{message.seq:06d}", message)
    detail = client.get(f"/api/assistant/conversations/{conversation_id}").json()
    assert detail["streaming"] is None
    assert detail["history"][1]["status"] == "failed"
    assert detail["history"][1]["error"]["code"] == "assistant.interrupted"
    assert detail["history"][0]["content"] == "Frage"


# ---- streaming, cancel, errors and retry ----------------------------------------------------


class _SlowStream:
    def __init__(self, chunks: int, stop: threading.Event) -> None:
        self.chunks = chunks
        self.stop = stop

    def __enter__(self) -> _SlowStream:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()

    def __iter__(self) -> Iterator[SimpleNamespace]:
        usage = SimpleNamespace(
            input_tokens=1500, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        yield SimpleNamespace(type="message_start", message=SimpleNamespace(model="claude-opus-5-5", usage=usage))
        for index in range(self.chunks):
            time.sleep(0.02)
            yield SimpleNamespace(type="text", text=f"Wort{index} ")

    def get_final_message(self) -> Any:
        return text("".join(f"Wort{index} " for index in range(self.chunks)), model="claude-opus-5-5")


class _SlowMessages:
    def __init__(self) -> None:
        self.closed = threading.Event()
        self.calls: list[dict[str, Any]] = []

    def stream(self, **request: Any) -> _SlowStream:
        self.calls.append(request)
        return _SlowStream(400, self.closed)


def test_answers_stream_and_can_be_cancelled_with_estimated_usage(make_app: Callable[..., TestClient]) -> None:
    messages = _SlowMessages()
    client = make_app(client=SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages)))
    conversation_id = chat(client)
    sent = client.post(
        f"/api/assistant/conversations/{conversation_id}/messages", json={"content": "Erzähl viel", "language": "de"}
    ).json()
    message_id = sent["assistant"]["id"]
    assert sent["assistant"]["status"] == "streaming"
    time.sleep(0.3)
    live = client.get(f"/api/assistant/conversations/{conversation_id}").json()
    assert live["streaming"] == message_id
    assert live["history"][1]["content"].startswith("Wort0 ")
    cancelled = client.post(f"/api/assistant/conversations/{conversation_id}/messages/{message_id}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert messages.closed.wait(5), "the stream to the API is closed"
    detail = wait(client, conversation_id)
    answer = detail["history"][1]
    assert answer["status"] == "cancelled" and answer["content"].startswith("Wort0")
    assert "Wort399" not in answer["content"]
    # The cancelled request is counted once; Orkexa reports no usage before a stream ends, so the input
    # tokens are estimated from the size of the request (four characters per token).
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not client.get("/api/usage").json()["recent"]:
        time.sleep(0.05)
    entry = client.get("/api/usage").json()["recent"][0]
    assert entry["source"] == "assistant" and entry["estimated"] is True
    assert entry["usage"]["input_tokens"] > 100 and entry["usage"]["output_tokens"] > 1
    assert entry["ref"] == conversation_id and entry["request"] == message_id


def test_the_event_stream_delivers_snapshot_deltas_and_end(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Fertig.")]})
    conversation_id = chat(client)
    sent = client.post(f"/api/assistant/conversations/{conversation_id}/messages", json={"content": "Hi"}).json()
    wait(client, conversation_id)
    events = client.get(
        f"/api/assistant/conversations/{conversation_id}/messages/{sent['assistant']['id']}/events"
    ).text
    assert "event: snapshot" in events and "event: end" in events
    assert '"content": "Fertig."' in events
    # Nothing of a finished answer stays in memory, also not after its stream was read. The answer's
    # thread stores the message before it releases the live events, so give it that moment.
    service = client.app.state.nodestro.assistant  # type: ignore[attr-defined]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (service._events or service._subscribers):
        time.sleep(0.01)
    assert service._events == {} and service._subscribers == {}


def _api_error(kind: str) -> Callable[[dict[str, Any]], Any]:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def fail(_request: dict[str, Any]) -> Any:
        if kind == "connection":
            raise anthropic.APIConnectionError(request=request)
        if kind == "auth":
            raise anthropic.AuthenticationError(
                "invalid x-api-key", response=httpx.Response(401, request=request), body=None
            )
        if kind == "long":
            raise anthropic.BadRequestError(
                "prompt is too long: 250000 tokens > 200000 maximum",
                response=httpx.Response(400, request=request),
                body=None,
            )
        raise anthropic.NotFoundError("model not found", response=httpx.Response(404, request=request), body=None)

    return fail


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("connection", "assistant.connection_failed"),
        ("auth", "assistant.auth_failed"),
        ("long", "assistant.context_too_large"),
        ("model", "assistant.model_unavailable"),
    ],
)
def test_failed_answers_keep_the_conversation_and_can_be_retried(
    make_app: Callable[..., TestClient], kind: str, code: str
) -> None:
    client = make_app({ASSISTANT: [text("Erste Antwort"), _api_error(kind), text("Jetzt klappt es")]})
    conversation_id = chat(client)
    send(client, conversation_id, "Eins")
    detail = send(client, conversation_id, "Zwei")
    failed = detail["history"][3]
    assert failed["status"] == "failed" and failed["error"]["code"] == code
    # The conversation is still there, the question too.
    assert [m["content"] for m in detail["history"][:3]] == ["Eins", "Erste Antwort", "Zwei"]
    retried = client.post(
        f"/api/assistant/conversations/{conversation_id}/messages/{failed['id']}/retry", json={"language": "de"}
    )
    assert retried.status_code == 200, retried.text
    detail = wait(client, conversation_id)
    assert detail["history"][3]["status"] == "complete" and detail["history"][3]["content"] == "Jetzt klappt es"
    assert detail["history"][3]["attempts"] == 2
    # The retry sends the same question once more, with the history before it.
    assert user_text(requests_of(client)[-1]) == "Zwei"
    # Two successful requests are in the ledger; the failed one never reached a response.
    report = client.get("/api/usage").json()
    assert report["total"]["requests"] == 2
    assert detail["usage"]["requests"] == 2


def test_a_finished_answer_is_seen_whole_and_not_busy(make_app: Callable[..., TestClient]) -> None:
    """Whoever sees an answer as finished sees its usage and may send or retry at once.

    A reader looks at the conversation exactly while the finished answer is stored; before the
    fix it saw the answer finished while the conversation was still busy (a retry got
    ``assistant.busy``), or the conversation's usage without the answer's request.
    """
    client = make_app({ASSISTANT: [text("Antwort")]})
    service = client.app.state.nodestro.assistant  # type: ignore[attr-defined]
    conversation_id = chat(client)
    put = service.store.put
    seen: list[tuple[str | None, bool, int]] = []
    readers: list[threading.Thread] = []

    def read() -> None:
        detail = service.detail(conversation_id)
        seen.append((detail.streaming, service._busy(conversation_id), detail.usage.requests))

    def watched_put(collection: str, key: str, value: Any) -> None:
        put(collection, key, value)
        if getattr(value, "role", None) == "assistant" and getattr(value, "status", None) == "complete":
            reader = threading.Thread(target=read)
            readers.append(reader)
            reader.start()
            reader.join(timeout=0.5)  # returns at once unless the reader waits for the answer to finish

    service.store.put = watched_put
    try:
        send(client, conversation_id, "Frage")
    finally:
        service.store.put = put
    for reader in readers:
        reader.join(timeout=5)
    assert seen, "the finished answer was never stored"
    for streaming, busy, requests in seen:
        assert streaming is not None or (not busy and requests == 1), (streaming, busy, requests)


def test_only_the_last_failed_answer_can_be_retried(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Antwort")]})
    conversation_id = chat(client)
    detail = send(client, conversation_id, "Frage")
    response = client.post(
        f"/api/assistant/conversations/{conversation_id}/messages/{detail['history'][1]['id']}/retry", json={}
    )
    assert response.status_code == 400 and response.json()["code"] == "assistant.retry_invalid"


def test_without_api_key_nothing_is_sent_or_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from nodestro_api.app import create_app
    from nodestro_api.context import AppConfig

    with TestClient(create_app(AppConfig(data_dir=tmp_path)), base_url="http://127.0.0.1") as client:
        conversation_id = chat(client)
        response = client.post(f"/api/assistant/conversations/{conversation_id}/messages", json={"content": "Hallo"})
        assert response.status_code == 400 and response.json()["code"] == "api_key_missing"
        assert client.get(f"/api/assistant/conversations/{conversation_id}").json()["history"] == []


# ---- documentation ----------------------------------------------------------------------


# ---- attachments ------------------------------------------------------------------------


def _workflow(client: TestClient) -> str:
    return create(
        client,
        [
            {"id": "start", "type": "start", "label": "Start"},
            agent_node(
                "research",
                "Du recherchierst. Zugang: password=hunter2geheim",
                request_options={"extra_headers": {"x-api-key": "geheimer-wert-123"}},
            ),
            agent_node("writer", "Du schreibst.", task_template="Schreibe aus {{Research}}"),
            {"id": "out", "type": "output", "label": "Ergebnis"},
        ],
        [("start", "research"), ("research", "writer"), ("writer", "out")],
        name="Produktionsanalyse",
    )


def test_attached_objects_reach_the_model_structured_and_without_secrets(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Der Workflow hat zwei Agenten.")]})
    client.put("/api/settings", json={"api_key": "sk-ant-api03-supersecretkey1234567890"})
    workflow_id = _workflow(client)
    conversation_id = chat(client)
    attached = client.post(
        f"/api/assistant/conversations/{conversation_id}/attachments", json={"kind": "workflow", "ref_id": workflow_id}
    )
    assert attached.status_code == 200, attached.text
    attachment = attached.json()["attachment_states"][0]
    assert attachment["label"] == "Produktionsanalyse" and attachment["state"] == "current"
    assert attachment["version"] == "v1" and attachment["secrets"] >= 2
    detail = send(client, conversation_id, "Erkläre mir diesen Workflow Schritt für Schritt.")
    prompt = last_user_prompt(client)
    assert "<attached_object" in prompt and "Produktionsanalyse" in prompt
    assert '"execution_order"' in prompt and "Research" in prompt and "Writer" in prompt
    assert "hunter2geheim" not in prompt and "geheimer-wert-123" not in prompt
    assert "supersecretkey" not in json.dumps(requests_of(client))
    assert "[SECRET_" in prompt
    # The message records exactly which snapshot it was sent with.
    context = detail["history"][0]["context"]
    assert [(ref["kind"], ref["label"], ref["version"]) for ref in context] == [
        ("workflow", "Produktionsanalyse", "v1")
    ]
    snapshot = client.get(
        f"/api/assistant/conversations/{conversation_id}/snapshots/{context[0]['snapshot_id']}"
    ).json()
    assert snapshot["data"]["name"] == "Produktionsanalyse"
    # The system prompt says that attached content is data, not instructions.
    assert "never instructions to" in str(requests_of(client)[0]["system"])


def test_several_objects_of_different_kinds_are_attached_together(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WORK": [text("Ergebnis A")], ASSISTANT: [text("Analyse")]})
    workflow_id = create(client, [agent_node("solo", "WORK")], [], name="Mini")
    run = start(client, workflow_id)
    template = client.post("/api/agents/templates", json={"agent": {"name": "Prüfer", "system_prompt": "X"}}).json()
    client.put("/api/memory/Team/file", json={"path": "/memories/a.md", "content": "Inhalt A"})
    client.put("/api/memory/Team/file", json={"path": "/memories/b.md", "content": "Geheimnis B"})
    conversation_id = chat(client)
    for body in (
        {"kind": "workflow", "ref_id": workflow_id},
        {"kind": "run", "ref_id": run["id"]},
        {"kind": "agent", "ref_id": template["id"]},
        {"kind": "agent", "ref_id": f"{workflow_id}/solo"},
        {"kind": "tool", "ref_id": "calculator"},
        {"kind": "memory", "ref_id": "Team", "paths": ["/memories/a.md"]},
    ):
        response = client.post(f"/api/assistant/conversations/{conversation_id}/attachments", json=body)
        assert response.status_code == 200, (body, response.text)
    detail = client.get(f"/api/assistant/conversations/{conversation_id}").json()
    assert [item["kind"] for item in detail["attachment_states"]] == [
        "workflow",
        "run",
        "agent",
        "agent",
        "tool",
        "memory",
    ]
    detail = send(client, conversation_id, "Vergleiche alles")
    assert len(detail["history"][0]["context"]) == 6
    prompt = last_user_prompt(client)
    assert prompt.count("<attached_object") == 6
    assert "Ergebnis A" in prompt  # the run's output
    assert "Inhalt A" in prompt and "Geheimnis B" not in prompt  # only the selected memory file
    assert '"name": "calculator"' in prompt


def test_attaching_does_not_change_or_run_anything(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("ok")]})
    workflow_id = _workflow(client)
    before = client.get(f"/api/workflows/{workflow_id}").json()
    conversation_id = chat(client)
    client.post(
        f"/api/assistant/conversations/{conversation_id}/attachments", json={"kind": "workflow", "ref_id": workflow_id}
    )
    send(client, conversation_id, "Analysiere")
    assert client.get(f"/api/workflows/{workflow_id}").json() == before
    assert len(client.get(f"/api/workflows/{workflow_id}/versions").json()) == 1
    assert client.get("/api/runs").json() == []


def test_changed_objects_are_detected_and_refreshing_keeps_old_messages(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Alt"), text("Neu")]})
    workflow_id = _workflow(client)
    conversation_id = chat(client)
    url = f"/api/assistant/conversations/{conversation_id}"
    client.post(f"{url}/attachments", json={"kind": "workflow", "ref_id": workflow_id})
    first = send(client, conversation_id, "Wie heißt er?")
    old_snapshot = first["history"][0]["context"][0]["snapshot_id"]

    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    workflow["name"] = "Produktionsanalyse 2"
    client.put(f"/api/workflows/{workflow_id}", json=workflow)
    detail = client.get(url).json()
    attachment = detail["attachment_states"][0]
    assert attachment["state"] == "changed"

    refreshed = client.post(f"{url}/attachments/{attachment['id']}/refresh", json={}).json()
    assert refreshed["attachment_states"][0]["state"] == "current"
    assert refreshed["attachment_states"][0]["label"] == "Produktionsanalyse 2"
    assert refreshed["attachment_states"][0]["version"] == "v2"
    detail = send(client, conversation_id, "Und jetzt?")
    # The first message still points at the old snapshot, the new one at the new snapshot.
    assert detail["history"][0]["context"][0]["snapshot_id"] == old_snapshot
    assert detail["history"][0]["context"][0]["label"] == "Produktionsanalyse"
    assert detail["history"][2]["context"][0]["label"] == "Produktionsanalyse 2"
    assert client.get(f"{url}/snapshots/{old_snapshot}").json()["label"] == "Produktionsanalyse"


def test_deleted_objects_cause_no_errors(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("ok")]})
    workflow_id = _workflow(client)
    conversation_id = chat(client)
    url = f"/api/assistant/conversations/{conversation_id}"
    client.post(f"{url}/attachments", json={"kind": "workflow", "ref_id": workflow_id})
    client.delete(f"/api/workflows/{workflow_id}")
    detail = client.get(url).json()
    assert detail["attachment_states"][0]["state"] == "missing"
    # The snapshot is still there: the message can be sent and the refresh is explained.
    assert send(client, conversation_id, "Was war das?")["history"][1]["status"] == "complete"
    refresh = client.post(f"{url}/attachments/{detail['attachment_states'][0]['id']}/refresh", json={})
    assert refresh.status_code == 400 and refresh.json()["code"] == "assistant.object_missing"
    missing = client.post(f"{url}/attachments", json={"kind": "run", "ref_id": "run_gibtsnicht"})
    assert missing.status_code == 400 and missing.json()["code"] == "assistant.object_missing"


def test_unsaved_editor_drafts_are_attached_as_drafts(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Entwurf gesehen")]})
    workflow_id = _workflow(client)
    conversation_id = chat(client)
    url = f"/api/assistant/conversations/{conversation_id}"
    draft = client.get(f"/api/workflows/{workflow_id}").json()
    draft["nodes"].append(agent_node("critic", "Du prüfst kritisch."))
    detail = client.post(
        f"{url}/attachments", json={"kind": "workflow", "ref_id": workflow_id, "variant": "draft", "workflow": draft}
    ).json()
    attachment = detail["attachment_states"][0]
    assert attachment["variant"] == "draft" and attachment["state"] == "draft"
    send(client, conversation_id, "Was hat sich geändert?")
    prompt = last_user_prompt(client)
    assert "Critic" in prompt and "unsaved editor draft" in prompt
    # The saved version is unchanged and can be attached instead (it replaces the draft).
    assert len(client.get(f"/api/workflows/{workflow_id}").json()["nodes"]) == 4
    saved = client.post(f"{url}/attachments/{attachment['id']}/refresh", json={}).json()
    assert saved["attachment_states"][0]["variant"] == "saved"
    no_draft = client.post(f"{url}/attachments", json={"kind": "workflow", "ref_id": workflow_id, "variant": "draft"})
    assert no_draft.status_code == 400 and no_draft.json()["code"] == "assistant.draft_missing"


def test_removed_attachments_are_not_sent_again(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Mit"), text("Ohne")]})
    workflow_id = _workflow(client)
    conversation_id = chat(client)
    url = f"/api/assistant/conversations/{conversation_id}"
    detail = client.post(f"{url}/attachments", json={"kind": "workflow", "ref_id": workflow_id}).json()
    send(client, conversation_id, "Eins")
    client.delete(f"{url}/attachments/{detail['attachment_states'][0]['id']}")
    detail = send(client, conversation_id, "Zwei")
    assert detail["history"][2]["context"] == []
    assert "<attached_object" not in last_user_prompt(client)
    # The first message keeps its context; the history names it without repeating the content.
    assert detail["history"][0]["context"][0]["label"] == "Produktionsanalyse"
    history = requests_of(client)[-1]["messages"]
    assert "Attached objects at that time: workflow 'Produktionsanalyse'" in history[0]["content"]
    assert "execution_order" not in json.dumps(history[:-1])


def test_large_objects_are_shortened_and_marked_partial(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WORK": [text("x" * 200_000)], ASSISTANT: [text("Teilweise")]})
    workflow_id = create(client, [agent_node("solo", "WORK")], [], name="Gross")
    run = start(client, workflow_id)
    conversation_id = chat(client)
    detail = client.post(
        f"/api/assistant/conversations/{conversation_id}/attachments", json={"kind": "run", "ref_id": run["id"]}
    ).json()
    attachment = detail["attachment_states"][0]
    assert attachment["truncated"] is True
    assert any(note["code"] == "assistant.shortened" for note in attachment["notes"])
    detail = send(client, conversation_id, "Warum so lang?")
    assert detail["history"][0]["context"][0]["partial"] is True
    prompt = last_user_prompt(client)
    assert len(prompt) < 80_000 and '"partial": true' in prompt and "[shortened:" in prompt


def test_knowledge_content_is_only_shared_when_chosen(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("a"), text("b")]})
    uploaded = client.post(
        "/api/knowledge",
        files={"files": ("notiz.md", b"# Notiz\nDer Liefertermin ist der 3. Mai.")},
        data={"description": ""},
    ).json()[0]
    conversation_id = chat(client)
    url = f"/api/assistant/conversations/{conversation_id}"
    client.post(f"{url}/attachments", json={"kind": "knowledge", "ref_id": uploaded["id"]})
    send(client, conversation_id, "Was steht drin?")
    assert "notiz.md" in last_user_prompt(client) and "Liefertermin" not in last_user_prompt(client)
    client.post(f"{url}/attachments", json={"kind": "knowledge", "ref_id": uploaded["id"], "include_content": True})
    send(client, conversation_id, "Und jetzt?")
    assert "Liefertermin" in last_user_prompt(client)


# ---- handover -------------------------------------------------------------------------


def test_handover_to_the_workflow_architect_is_only_a_proposal(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Ein Kritiker würde helfen."), None, text("Der Vorschlag liegt bereit.")]})
    workflow_id = _workflow(client)
    before = client.get(f"/api/workflows/{workflow_id}").json()
    conversation_id = chat(client)
    url = f"/api/assistant/conversations/{conversation_id}"
    detail = client.post(f"{url}/attachments", json={"kind": "workflow", "ref_id": workflow_id}).json()
    attachment_id = detail["attachment_states"][0]["id"]
    send(client, conversation_id, "Wie könnte ich den Workflow verbessern?")
    client.fake.messages.script[ASSISTANT][0] = tool_use(  # type: ignore[attr-defined]
        "propose_handover",
        {
            "target": "workflow_architect",
            "instruction": "Füge nach dem Writer einen Kritiker hinzu, der Fakten prüft.",
            "attachment": attachment_id,
        },
    )
    recorded: list[Any] = []
    service = client.app.state.nodestro.assistant  # type: ignore[attr-defined]
    emit = service._emit

    def record(message: Any, kind: str, **options: Any) -> None:
        emit(message, kind, **options)
        recorded.extend(service._events.get(message.id, [])[-1:])

    service._emit = record
    detail = send(client, conversation_id, "Ja, setze das bitte um.")
    answer = detail["history"][3]
    # The live events carry their payload as the interface reads it (``event.data.target``), not wrapped.
    events = {event.type: event.data for event in recorded}
    assert events["handover"]["target"] == "workflow_architect"
    assert events["step"]["kind"] == "generate"
    assert events["usage"]["usage"]["requests"] >= 1
    assert events["finished"]["message"]["id"] == answer["id"]
    assert answer["status"] == "complete"
    assert [step["kind"] for step in answer["steps"]] == ["context", "generate", "handover", "generate"]
    [handover] = answer["handovers"]
    assert handover["target"] == "workflow_architect" and handover["mode"] == "revise"
    assert handover["workflow_id"] == workflow_id and handover["workflow_name"] == "Produktionsanalyse"
    assert "Kritiker" in handover["instruction"]
    # Nothing changed, nothing ran, no architect job was started.
    assert client.get(f"/api/workflows/{workflow_id}").json() == before
    assert client.get("/api/runs").json() == []
    assert client.app.state.nodestro.architect.jobs == {}  # type: ignore[attr-defined]
    # The tool told the model that nothing was changed.
    result = json.dumps(requests_of(client)[-1]["messages"][-1]["content"], ensure_ascii=False)
    assert "Nothing has been changed" in result


def test_handover_checks_its_attachment(make_app: Callable[..., TestClient]) -> None:
    client = make_app(
        {
            ASSISTANT: [
                tool_use("propose_handover", {"target": "agent_designer", "instruction": "Ein Agent"}),
                text("Dafür brauche ich einen Workflow."),
                tool_use("propose_handover", {"target": "tool_builder", "instruction": "Ein Tool für IBAN-Prüfung"}),
                text("Bereit."),
            ]
        }
    )
    conversation_id = chat(client)
    detail = send(client, conversation_id, "Baue einen Agenten")
    assert detail["history"][1]["handovers"] == []
    result = json.dumps(requests_of(client)[1]["messages"][-1]["content"], ensure_ascii=False)
    assert "needs the attachment id of a workflow" in result
    detail = send(client, conversation_id, "Dann ein Tool")
    assert [(h["target"], h["workflow_id"]) for h in detail["history"][3]["handovers"]] == [("tool_builder", None)]


# ---- usage and backup -------------------------------------------------------------------


def test_assistant_usage_is_counted_once_per_request(make_app: Callable[..., TestClient]) -> None:
    client = make_app(
        {
            ASSISTANT: [
                tool_use("search_docs", {"query": "Kosten"}, usage={"input_tokens": 3000, "output_tokens": 50}),
                text("Unter Nutzung.", usage={"input_tokens": 4000, "output_tokens": 300}),
            ]
        }
    )
    conversation_id = chat(client)
    detail = send(client, conversation_id, "Wo sehe ich die API-Kosten eines Workflows?")
    answer = detail["history"][1]
    assert answer["usage"]["requests"] == 2
    assert answer["usage"]["input_tokens"] == 7000 and answer["usage"]["output_tokens"] == 350
    assert answer["usage"]["estimated_cost_usd"] > 0 and answer["model"] == "anthropic:claude-opus-5-5"
    report = client.get("/api/usage").json()
    [bucket] = [item for item in report["by_source"] if item["key"] == "assistant"]
    assert bucket["requests"] == 2 and bucket["operations"] == 1
    assert report["total"]["requests"] == 2
    [entry] = report["recent"]
    assert entry["label"] == detail["title"] and entry["ref"] == conversation_id and entry["request"] == answer["id"]
    assert entry["detail"] == "reply" and entry["estimated"] is False
    assert detail["usage"]["estimated_cost_usd"] == pytest.approx(answer["usage"]["estimated_cost_usd"])


def test_backups_contain_the_conversations(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Gesichert")]})
    conversation_id = chat(client)
    send(client, conversation_id, "Sichere mich")
    backup = client.get("/api/backup")
    assert backup.status_code == 200
    info = client.post("/api/backup/inspect", files={"file": ("b.zip", backup.content)}).json()
    assert info["contents"]["conversations"] == 1
    client.delete(f"/api/assistant/conversations/{conversation_id}")
    client.post("/api/backup/restore", json={"id": info["id"], "confirm": True})
    restored = client.get(f"/api/assistant/conversations/{conversation_id}").json()
    assert [m["content"] for m in restored["history"]] == ["Sichere mich", "Gesichert"]
