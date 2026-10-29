"""Test helpers: the API with scripted model answers (nodestro_runtime.testing), never calling a provider."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.testing import (
    FakeUv,
    Parsed,
    ScriptedClient,
    ScriptedMessages,
    _Stream,
    message,
    parsed,
    text,
    tool_use,
)
import pytest

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig


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
        test_client = TestClient(app)
        test_client.__enter__()
        test_client.fake = client  # type: ignore[attr-defined]
        clients.append(test_client)
        return test_client

    yield factory  # type: ignore[misc]
    for test_client in clients:
        test_client.__exit__(None, None, None)


__all__ = ["FakeUv", "Parsed", "ScriptedClient", "ScriptedMessages", "_Stream", "message", "parsed", "text", "tool_use"]
