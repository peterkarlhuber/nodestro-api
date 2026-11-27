"""Workflows as tools of an agent (N-20) through the API: a saved version as a tool (the binding with its effect), and a
conversation whose agent calls it: the workflow's approval step is a card of the answer, decided with the run route,
and the version stays fixed. The runtime's tests cover the effect, the confirmation before a changing workflow, files
and cancelling in detail (``nodestro-runtime/tests/test_workflow_tools.py``)."""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.conversations import CONVERSATIONS_ENV
from nodestro_runtime.workflow_tools import WORKFLOW_TOOLS_ENV
import pytest

from conftest import text, tool_use

CLOUD = "anthropic:claude-sonnet-5-5"


@pytest.fixture
def client(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    monkeypatch.setenv(WORKFLOW_TOOLS_ENV, "1")
    return make_app(
        {
            "AGENT": [tool_use("workflow_check", {"customer": "ACME"}), text("ACME checked.")],
            "CHECKER": [text("ACME is fine")],
        }
    )


def checker() -> dict[str, Any]:
    """A workflow that only reads: a start form, an agent without tools, an approval step and a result (no files)."""
    return {
        "name": "Check",
        "description": "Checks a customer.",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "start": {"fields": [{"name": "customer", "label": "Customer", "required": True}]},
            },
            {
                "id": "a",
                "type": "agent",
                "agent": {"name": "Checker", "system_prompt": "CHECKER", "task_template": "{{form.customer}}"},
            },
            {"id": "ok", "type": "approval", "approval": {"instructions": "Is the check right?"}},
            {"id": "out", "type": "output"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "a"},
            {"id": "e2", "source": "a", "target": "ok"},
            {"id": "e3", "source": "ok", "target": "out", "source_handle": "approved"},
        ],
    }


def test_a_saved_version_as_a_tool_and_a_conversation_that_calls_it(client: TestClient) -> None:
    created = client.post("/api/workflows", json=checker())
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    assert client.get(f"/api/workflows/{workflow_id}/versions/9/tool").status_code == 404
    assert client.get(f"/api/workflows/{workflow_id}/versions/1/tool", params={"name": "bad name"}).status_code == 422
    found = client.get(f"/api/workflows/{workflow_id}/versions/1/tool", params={"name": "workflow_check"})
    assert found.status_code == 200, found.text
    info = found.json()
    binding = info["binding"]
    assert (binding["version"], binding["name"], info["latest_version"]) == (1, "workflow_check", 1)
    # Reads only, but its agent's cloud model reaches outside (review M2).
    assert (binding["effect"]["access"], binding["effect"]["reach"], binding["effect"]["approvals"]) == (
        "read",
        "external",
        1,
    )
    assert info["input_schema"]["required"] == ["customer"]

    template = client.post(
        "/api/agents/templates",
        json={"agent": {"name": "Helper", "system_prompt": "AGENT", "workflow_tools": [binding]}},
    )
    assert template.status_code == 201, template.text
    conversation = client.post(
        "/api/conversations", json={"agent": {"template_id": template.json()["id"]}, "model": CLOUD}
    )
    assert conversation.status_code == 201, conversation.text
    base = f"/api/conversations/{conversation.json()['id']}"
    sent = client.post(f"{base}/messages", json={"content": "Check ACME"})
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["assistant"]["id"]

    deadline = time.monotonic() + 15
    while True:
        answer = client.get(f"{base}/messages/{message_id}").json()
        calls = answer["tool_calls"]
        if calls and calls[0]["workflow"] and calls[0]["workflow"]["approvals"]:
            break
        assert time.monotonic() < deadline, answer
        time.sleep(0.05)
    view = calls[0]["workflow"]
    assert view["version"] == 1 and answer["status"] == "running"
    [card] = view["approvals"]
    assert card["instructions"] == "Is the check right?"
    decided = client.post(f"/api/runs/{card['run_id']}/approvals/{card['node_id']}", json={"decision": "approved"})
    assert decided.status_code == 200, decided.text

    deadline = time.monotonic() + 15
    while answer["status"] not in ("succeeded", "failed", "cancelled"):
        assert time.monotonic() < deadline, answer
        time.sleep(0.05)
        answer = client.get(f"{base}/messages/{message_id}").json()
    assert answer["status"] == "succeeded" and answer["content"] == "ACME checked."
    assert answer["tool_calls"][0]["workflow"]["status"] == "succeeded"
    assert "ACME is fine" in answer["tool_calls"][0]["result"]
    assert client.get(base).json()["workflow_tool_updates"] == []
