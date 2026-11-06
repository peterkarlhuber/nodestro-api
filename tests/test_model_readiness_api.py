"""Missing local models at the API: interactive actions are refused with the cause, the allowed actions and the ready
alternatives before anything starts; another ready model can be used once; downloads attach instead of starting
twice; unattended runs only report the missing prerequisite."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import sys
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.launcher import launch
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import RunIn, SettingsIn, Workflow
import pytest
from test_local_models_api import FAKE_OLLAMA

from nodestro_api.permissions import ALL_PERMISSIONS, Permission, Principal

MakeApp = Callable[..., TestClient]
MISSING = "ollama:qwen3.5:9b"
INSTALLED = "ollama:qwen3.5:4b"


def _ctx(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


@pytest.fixture
def local(make_app: MakeApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A desktop app whose managed Ollama (a fake) has ``qwen3.5:4b``; the assistant model ``qwen3.5:9b`` is missing."""
    if sys.platform == "win32":
        pytest.skip("the fake Ollama binary is a shebang script")
    binary = tmp_path / "bin" / "ollama"
    binary.parent.mkdir()
    binary.write_text(FAKE_OLLAMA.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("NODESTRO_OLLAMA_BINARY", str(binary))
    monkeypatch.setattr("nodestro_runtime.local_readiness._free_bytes", lambda folder: 10**12)
    client = make_app()
    ctx = _ctx(client)
    # Ollama is the managed (fake) server here, not an injected client (no cloud provider is configured then).
    ctx._client_factory = None
    ctx.update_settings(SettingsIn(assistant_model=MISSING, default_model=INSTALLED))
    service = ctx.local_models
    service._hardware = service.hardware().model_copy(update={"ram_bytes": 64 * 10**9})
    return client


def _problem(response: Any) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "model.local_unavailable", body
    return body["params"]  # type: ignore[no-any-return]


def test_the_architect_is_refused_before_it_starts_with_what_can_be_done(local: TestClient) -> None:
    params = _problem(local.post("/api/architect/jobs", json={"description": "Sort invoices", "name": "Invoices"}))
    assert params["model"] == MISSING and params["cause"] == "model_missing"
    problem = params["problem"]
    assert problem["actions"] == ["download", "choose_model"] and problem["location"] == "this_computer"
    assert problem["download_bytes"] > 0 and problem["free_bytes"] == 10**12 and problem["fits"] is True
    assert INSTALLED in [item["model"] for item in params["alternatives"]]
    assert not local.fake.messages.calls  # type: ignore[attr-defined]  # nothing was sent or charged


def test_another_ready_model_is_used_once_and_never_any_model(local: TestClient) -> None:
    started = local.post("/api/architect/jobs", json={"description": "Sort invoices", "model": INSTALLED})
    assert started.status_code == 201, started.text
    assert _ctx(local).settings().assistant_model == MISSING  # the settings stay as they are
    deadline = time.monotonic() + 20
    while local.get(f"/api/architect/jobs/{started.json()['id']}").json()["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    refused = local.post("/api/architect/jobs", json={"description": "Sort invoices", "model": "ollama:other:70b"})
    assert refused.status_code == 409 and refused.json()["code"] == "model.choice_invalid"


def test_the_assistant_keeps_the_unsent_message_and_can_answer_with_another_model(local: TestClient) -> None:
    conversation = local.post("/api/assistant/conversations", json={}).json()["id"]
    url = f"/api/assistant/conversations/{conversation}/messages"
    _problem(local.post(url, json={"content": "Hello"}))
    assert local.get(f"/api/assistant/conversations/{conversation}").json()["history"] == []  # nothing stored
    sent = local.post(url, json={"content": "Hello", "model": INSTALLED})
    assert sent.status_code == 201, sent.text
    assert sent.json()["assistant"]["model"] == INSTALLED
    deadline = time.monotonic() + 20  # the fake server cannot answer: the answer fails, and ends
    while local.get(f"/api/assistant/conversations/{conversation}").json()["streaming"]:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    other = local.post(url, json={"content": "Hello", "model": "anthropic:claude-unknown"})
    assert other.status_code == 409 and other.json()["code"] == "model.choice_invalid"


def test_agent_design_test_runs_and_the_knowledge_index_name_the_same_problem(local: TestClient) -> None:
    design = _problem(local.post("/api/agents/design", json={"description": "Summarizes text"}))
    assert "choose_model" not in design["problem"]["actions"]  # designing has no per-request model
    agent = {"name": "Writer", "model": MISSING, "task": "Write"}
    tested = _problem(local.post("/api/agents/test", json={"agent": agent, "input": "x"}))
    assert tested["problem"]["action"] == "agent_test" and "choose_model" in tested["problem"]["actions"]
    workflow = {
        "name": "Local",
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "a", "type": "agent", "agent": agent},
            {"id": "o", "type": "output"},
        ],
        "edges": [{"id": "e1", "source": "s", "target": "a"}, {"id": "e2", "source": "a", "target": "o"}],
    }
    created = local.post("/api/workflows", json=workflow)
    assert created.status_code == 201, created.text
    run = _problem(local.post(f"/api/workflows/{created.json()['id']}/runs", json={"input": "x"}))
    assert run["models"] == [MISSING] and run["problem"]["actions"] == ["download"]  # a run changes no agent
    index = _problem(local.post("/api/knowledge/index/rebuild", json={"model": "qwen3-embedding:0.6b"}))
    assert index["problem"]["action"] == "knowledge_index"


def test_without_install_permission_only_an_administrator_can_help(local: TestClient) -> None:
    member = Principal(id="m", permissions=ALL_PERMISSIONS - {Permission.INSTALL})
    local.app.state.principal_resolver = lambda _connection: member  # type: ignore[attr-defined]
    problem = _problem(local.post("/api/architect/jobs", json={"description": "x"}))["problem"]
    assert problem["blocked"] == "permission" and problem["actions"] == ["choose_model", "ask_admin"]
    assert local.post("/api/local-models/pull", json={"model": "qwen3.5:9b"}).status_code == 403


def test_the_strict_offline_mode_offers_no_download(local: TestClient) -> None:
    local.put("/api/settings", json={"strict_offline": True})
    found = local.post("/api/local-models/readiness", json={"model": MISSING, "action": "architect"}).json()
    assert found["ready"] is False and found["problem"]["blocked"] == "offline"
    assert "download" not in found["problem"]["actions"]


def test_the_readiness_check_and_a_download_that_attaches(local: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = local.post("/api/local-models/readiness", json={"model": INSTALLED, "action": "assistant"}).json()
    assert ready == {"model": INSTALLED, "ready": True, "problem": None, "alternatives": []}
    cloud = local.post("/api/local-models/readiness", json={"model": "anthropic:claude-sonnet-4-5"}).json()
    assert cloud["ready"] is True
    asked: list[dict[str, Any]] = []
    service = _ctx(local).local_models

    def pull(name: str, *, attach: bool = False) -> Any:
        asked.append({"name": name, "attach": attach})
        raise NodestroError("local.offline_download", "stop here")

    monkeypatch.setattr(service, "pull", pull)
    local.post("/api/local-models/pull", json={"model": "qwen3.5:9b", "attach": True})
    local.post("/api/local-models/pull", json={"model": "qwen3.5:9b"})
    assert asked == [{"name": "qwen3.5:9b", "attach": True}, {"name": "qwen3.5:9b", "attach": False}]
    invalid = local.post("/api/local-models/readiness", json={"model": MISSING, "capabilities": ["flying"]})
    assert invalid.status_code == 422


def test_unattended_runs_never_download_and_report_the_prerequisite(local: TestClient) -> None:
    ctx = _ctx(local)
    workflow = Workflow.model_validate(
        {
            "id": "w",
            "name": "Local",
            "nodes": [
                {"id": "s", "type": "start"},
                {"id": "a", "type": "agent", "agent": {"name": "a", "model": MISSING, "task": "x"}},
                {"id": "o", "type": "output"},
            ],
            "edges": [{"id": "e1", "source": "s", "target": "a"}, {"id": "e2", "source": "a", "target": "o"}],
        }
    )
    ctx.local_models.start()
    with pytest.raises(NodestroError) as refused:
        asyncio.run(launch(ctx, workflow, RunIn(input="x"), trigger="schedule", trigger_ref="sched_1"))
    assert refused.value.code == "workflow.blocked"
    assert [item["code"] for item in refused.value.params["problems"]] == ["check.local_model_missing"]
    assert [job for job in ctx.local_models.jobs() if job.kind == "pull"] == []
