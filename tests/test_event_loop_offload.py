"""Checks that may ask a local model server never run on the event loop (the RT3-04 pattern in the API).

An Ollama connection asks the server (``/api/show``, ``/api/ps``) with a 30 s timeout; done inside an ``async def``
handler, one unanswered server stalls every request of the process (on a server: every tenant). The probes below
replace the check with one that notes whether it ran on the event loop and stops the request."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
import pytest
from test_published_api import published  # noqa: F401  (fixture)

from nodestro_api.benchmarks import Benchmarks

from test_automation import form_workflow


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _probe(calls: list[bool]) -> Callable[..., Any]:
    def check(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(_on_loop())
        raise NodestroError("probe.stopped", "stopped by the test")

    return check


def _ctx(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


def _stopped(answer: Any) -> None:
    assert answer.status_code == 400, answer.text
    assert answer.json()["code"] == "probe.stopped"


def test_studio_checks_run_off_the_event_loop(make_app: Callable[..., TestClient], monkeypatch: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    calls: list[bool] = []
    monkeypatch.setattr(ctx, "require_providers", _probe(calls))
    monkeypatch.setattr(ctx, "connect_model", _probe(calls))
    # Agent test: the agent's check and its providers.
    agent = {"name": "Tester", "system_prompt": "Answer briefly."}
    _stopped(client.post("/api/agents/test", json={"agent": agent, "input": "x"}))
    # Workflow architect and assistant (message and retry check the assistant model first).
    _stopped(client.post("/api/architect/jobs", json={"description": "A blog article", "language": "en"}))
    conversation = client.post("/api/assistant/conversations", json={}).json()["id"]
    sent = client.post(
        f"/api/assistant/conversations/{conversation}/messages", json={"content": "Hello", "language": "en"}
    )
    _stopped(sent)
    assert calls == [False, False, False]


def test_a_test_series_checks_its_models_off_the_event_loop(
    make_app: Callable[..., TestClient], monkeypatch: Any
) -> None:
    client = make_app()
    calls: list[bool] = []
    monkeypatch.setattr(Benchmarks, "_models", lambda self, workflow, start: _probe(calls)())
    workflow_id = form_workflow(client)
    suite = client.post(
        "/api/benchmarks",
        json={"name": "Series", "workflow_id": workflow_id, "cases": [{"name": "A", "values": {"kunde": "A"}}]},
    )
    assert suite.status_code == 201, suite.text
    _stopped(client.post(f"/api/benchmarks/{suite.json()['id']}/runs", json={}))
    assert calls == [False]


def test_a_published_run_checks_its_providers_off_the_event_loop(
    request: pytest.FixtureRequest, monkeypatch: Any
) -> None:
    client, publication, _token = request.getfixturevalue("published")()
    calls: list[bool] = []
    monkeypatch.setattr(_ctx(client), "require_providers", _probe(calls))
    _stopped(client.post(f"/api/publications/{publication['id']}/test", json={"body": {"values": {"customer": "A"}}}))
    assert calls == [False]
