"""Test helpers: the API with scripted model answers (nodestro_runtime.testing), never calling a provider."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.testing import (
    ContainerDouble,
    FakeUv,
    Parsed,
    ScriptedClient,
    ScriptedMessages,
    StaticDockerMonitor,
    _Stream,
    message,
    parsed,
    text,
    tool_use,
)
import pytest

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "real_docker_status: use the computer's real Docker status (background check)")


@pytest.fixture(autouse=True)
def _static_docker_status(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspaces see a fixed "Docker missing" status (no background thread, no docker CLI, no reaper), so a CI
    runner's Docker changes no test; a test passes its own ``StaticDockerMonitor`` where it needs another status."""
    from nodestro_runtime import docker_status

    docker_status.reset_monitors()
    if request.node.get_closest_marker("real_docker_status") is None:
        monkeypatch.setattr(docker_status, "monitor_for", lambda *args, **kwargs: StaticDockerMonitor())


@pytest.fixture(autouse=True)
def _no_automatic_docker(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The desktop ``auto`` sandbox mode would run untrusted tools in Docker wherever it runs (CI runners have
    Docker). Tests see a computer without that fallback, so "no isolation" stays testable; a test that wants it
    sets ``workspace.tools.container_fallback`` itself."""
    from nodestro_runtime.workspace import Workspace

    monkeypatch.setattr(Workspace, "_auto_container", lambda self: None)


@pytest.fixture
def make_app(tmp_path: Path) -> Callable[..., TestClient]:
    """``make_app(script)`` -> a TestClient whose agents answer from ``script``."""
    clients: list[TestClient] = []

    def factory(
        script: dict[str, list[Any]] | None = None,
        *,
        client: Any = None,
        python_runner: Any = None,
        ocr_runner: Any = None,
        integrations: list[Any] | None = None,
    ) -> TestClient:
        client = client or ScriptedClient(script or {})
        app = create_app(
            AppConfig(data_dir=tmp_path / f"data{len(clients)}"),
            client_factory=lambda _provider: client,
            python_runner=python_runner,
            ocr_runner=ocr_runner,
            # No installed integration changes a test: they are tested explicitly.
            integrations=integrations or [],
            scheduler=False,
        )
        test_client = TestClient(app, base_url="http://127.0.0.1")
        test_client.__enter__()
        test_client.fake = client  # type: ignore[attr-defined]
        clients.append(test_client)
        return test_client

    yield factory  # type: ignore[misc]
    for test_client in clients:
        test_client.__exit__(None, None, None)


def isolate(client: TestClient) -> ContainerDouble:
    """Let ``client``'s tools run in a stand-in for Docker (ADR-0006: untrusted code needs isolation).

    The double counts as isolating for the policy but runs on the host: only for tests of flows that need Docker."""
    double = ContainerDouble()
    client.app.state.nodestro.tools.set_sandbox(double)  # type: ignore[attr-defined]
    return double


__all__ = [
    "ContainerDouble",
    "FakeUv",
    "Parsed",
    "ScriptedClient",
    "ScriptedMessages",
    "_Stream",
    "message",
    "parsed",
    "text",
    "tool_use",
]
