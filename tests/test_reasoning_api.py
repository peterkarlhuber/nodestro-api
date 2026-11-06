"""Live reasoning through the API: the assistant's reply events, the architect's job events, the run chat's live
answers and the model's reasoning declaration. The scripted client streams Claude-shaped thinking blocks
(:func:`nodestro_runtime.testing.thinking`), so the whole chain (stream -> Orkexa -> service -> Server-Sent Events)
runs; text only ever comes from the provider's stream.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.testing import thinking
from test_architect import start, wait_job
from test_assistant import ASSISTANT, chat, wait

from architect_script import AGENT, ANALYST, CODER, agent_spec, analysis, create_script, thinking_first, tool_proposal


def _sse(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_the_assistant_streams_reasoning_before_the_answer_and_keeps_it(make_app: Callable[..., TestClient]) -> None:
    entered, release = threading.Event(), threading.Event()

    def answer(_request: dict[str, Any]) -> Any:
        entered.set()
        assert release.wait(10)
        return thinking("The user asks about runs. A run is one execution of a workflow.", "Ein Lauf.")

    client = make_app({ASSISTANT: [answer]})
    conversation_id = chat(client)
    sent = client.post(
        f"/api/assistant/conversations/{conversation_id}/messages", json={"content": "Was ist ein Lauf?"}
    ).json()
    message_id = sent["assistant"]["id"]
    assert entered.wait(5)
    url = f"/api/assistant/conversations/{conversation_id}/messages/{message_id}/events"
    body: list[str] = []
    reader = threading.Thread(target=lambda: body.append(client.get(url).text))
    reader.start()
    time.sleep(0.3)  # the stream is open before the model answers
    release.set()
    reader.join(10)
    events = _sse(body[0])
    assert events[0][0] == "snapshot" and events[-1][0] == "end"
    live = [data for kind, data in events if kind == "event"]
    kinds = [item["type"] for item in live if item["type"] in ("thinking", "thinking_status", "delta")]
    first_delta = kinds.index("delta")
    assert kinds[0] == "thinking_status" and "thinking" in kinds[:first_delta]
    assert kinds[first_delta - 1] == "thinking_status"  # the phase ended before the answer's text
    status = [item["data"] for item in live if item["type"] == "thinking_status"]
    assert status == [
        {"state": "started", "reasoning_kind": "summary", "redacted": False},
        {"state": "finished", "reasoning_kind": "summary", "redacted": False},
    ]
    streamed = "".join(item["text"] for item in live if item["type"] == "thinking")
    assert streamed == "The user asks about runs. A run is one execution of a workflow."
    assert {item["data"]["reasoning_kind"] for item in live if item["type"] == "thinking"} == {"summary"}
    stored = wait(client, conversation_id)["history"][1]
    assert stored["content"] == "Ein Lauf."
    assert stored["reasoning"] == {"text": streamed, "kind": "summary", "phases": 1, "redacted": False, "active": False}


def test_a_reconnecting_subscriber_never_gets_reasoning_twice(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [thinking("Eins. Zwei. Drei. Vier. Fünf. Sechs. Sieben. Acht.", "Ok.")]})
    service = client.app.state.nodestro.assistant  # type: ignore[attr-defined]
    seen: list[tuple[str, str]] = []
    emit = service._emit

    def record(message: Any, kind: str, **options: Any) -> None:
        emit(message, kind, **options)
        if kind == "thinking":
            # A snapshot taken right after the event holds exactly the text published so far.
            seen.append((service._live[message.id].reasoning.text, options["text"]))

    service._emit = record
    conversation_id = chat(client)
    client.post(f"/api/assistant/conversations/{conversation_id}/messages", json={"content": "x"})
    wait(client, conversation_id)
    joined = ""
    for snapshot, delta in seen:
        joined += delta
        assert snapshot == joined


def test_redacted_reasoning_is_a_status_without_text(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [thinking(None, "Ok.")]})
    conversation_id = chat(client)
    client.post(f"/api/assistant/conversations/{conversation_id}/messages", json={"content": "x"})
    stored = wait(client, conversation_id)["history"][1]
    assert stored["reasoning"] == {"text": "", "kind": None, "phases": 1, "redacted": True, "active": False}


def test_an_answer_without_reasoning_has_none(make_app: Callable[..., TestClient]) -> None:
    from conftest import text

    client = make_app({ASSISTANT: [text("Ok.")]})
    conversation_id = chat(client)
    client.post(f"/api/assistant/conversations/{conversation_id}/messages", json={"content": "x"})
    assert wait(client, conversation_id)["history"][1]["reasoning"] is None


def test_architect_reasoning_streams_per_step_and_stays_on_the_job(make_app: Callable[..., TestClient]) -> None:
    script = create_script()
    script[ANALYST] = [thinking_first(analysis(), "Inputs: a topic. Output: an article.")]
    client = make_app(script)
    architect = client.app.state.nodestro.architect  # type: ignore[attr-defined]
    recorded: list[Any] = []
    emit = architect.emit

    def record(job: Any, kind: str, **fields: Any) -> None:
        emit(job, kind, **fields)
        if kind in ("thinking", "thinking_status", "phase"):
            recorded.append((kind, fields))

    architect.emit = record
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready", job["error"]
    thinking_events = [fields for kind, fields in recorded if kind == "thinking"]
    assert "".join(item["message"] for item in thinking_events) == "Inputs: a topic. Output: an article."
    assert {item["data"]["step"] for item in thinking_events} == {"analysis"}
    states = [fields["data"]["state"] for kind, fields in recorded if kind == "thinking_status"]
    assert states == ["started", "finished"]
    assert job["reasoning"] == [
        {
            "text": "Inputs: a topic. Output: an article.",
            "kind": "summary",
            "phases": 1,
            "redacted": False,
            "active": False,
            "step": "analysis",
            "subject": None,
        }
    ]
    # Live-only events are not kept in the job's event list (a long reasoning cannot crowd out the log).
    assert not [event for event in job["events"] if event["type"].startswith("thinking")]


def test_the_sub_requests_stream_their_reasoning_too(make_app: Callable[..., TestClient]) -> None:
    """RT-063: the Agent Architect (one request per agent, in parallel) and the Coding Agent stream their reasoning
    like the architect's own steps, each into an entry of its own named by step and subject."""
    script = create_script()
    script[AGENT] = [thinking_first(agent_spec(), "Which tools does it need?"), agent_spec(), agent_spec()]
    script[CODER] = [thinking_first(tool_proposal(), "Count the keyword, divide by the words.")]
    client = make_app(script)
    architect = client.app.state.nodestro.architect  # type: ignore[attr-defined]
    recorded: list[Any] = []
    emit = architect.emit

    def record(job: Any, kind: str, **fields: Any) -> None:
        emit(job, kind, **fields)
        if kind == "thinking":
            recorded.append(fields)

    architect.emit = record
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready", job["error"]
    by_step: dict[tuple[str, str], str] = {}
    for item in recorded:  # the events are deltas
        key = (item["data"]["step"], item["data"]["subject"])
        by_step[key] = by_step.get(key, "") + item["message"]
    agent = next(key for key in by_step if key[0] == "agent")
    assert by_step[agent] == "Which tools does it need?" and agent[1]
    assert by_step[("tool", "keyword_density")] == "Count the keyword, divide by the words."
    entries = {(entry["step"], entry["subject"]): entry for entry in job["reasoning"]}
    assert entries[("tool", "keyword_density")]["text"] == "Count the keyword, divide by the words."
    assert entries[agent]["text"] == "Which tools does it need?"
    for item in recorded:  # every event points at its own entry
        entry = job["reasoning"][item["data"]["index"]]
        assert (entry["step"], entry["subject"]) == (item["data"]["step"], item["data"]["subject"])


def test_model_capabilities_declare_what_the_stream_transmits(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    claude = client.get("/api/models/capabilities", params={"model": "anthropic:claude-sonnet-5-5"}).json()
    assert claude["reasoning"]["text"] == "summary" and claude["reasoning"]["request_option"] == "thinking_display"
    openai = client.get("/api/models/capabilities", params={"model": "openai:gpt-5.5"}).json()["reasoning"]
    assert (openai["text"], openai["requested"]) == ("summary", False)
    classic = client.get("/api/models/capabilities", params={"model": "openai:gpt-4o"}).json()["reasoning"]
    assert classic["text"] == "none" and classic["reason"]
    assert client.put("/api/settings", json={"reasoning_summaries": True}).status_code == 200
    openai = client.get("/api/models/capabilities", params={"model": "openai:gpt-5.5"}).json()["reasoning"]
    assert openai["requested"] is True
    assert client.get("/api/settings").json()["reasoning_summaries"] is True
