"""Test helpers: the API with scripted model answers (nodestro_runtime.testing), never calling a provider."""

from __future__ import annotations

from collections.abc import Callable
import os
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
    config.addinivalue_line("markers", "real_identity_check: nodestro-server check reads the identity provider")
    _own_postgres_database_per_xdist_worker()


#: The database this pytest-xdist worker created for itself (dropped again when the worker ends).
_XDIST_DATABASE: dict[str, str] = {}


def _own_postgres_database_per_xdist_worker() -> None:
    """Under pytest-xdist every worker process gets its own PostgreSQL database (CI-FAST AP6).

    Each test already migrates into its own schema, but PostgreSQL advisory locks belong to the whole database: the
    scheduler leader lock, the export lock and the price-sync lock. Two workers sharing one database would take each
    other's locks (a test that expects to lead would see "another worker leads"). A worker's own database keeps the
    locks apart, exactly as in a serial run. ``NODESTRO_TEST_POSTGRES`` is rewritten before any test module reads it,
    so tests and the child processes they start use the worker's database.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    dsn = os.environ.get("NODESTRO_TEST_POSTGRES")
    if not worker or not dsn:
        return
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"{conninfo_to_dict(dsn).get('dbname') or 'postgres'}_xdist_{worker}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    _XDIST_DATABASE.update(admin=dsn, name=name)
    os.environ["NODESTRO_TEST_POSTGRES"] = make_conninfo(dsn, dbname=name)


def pytest_unconfigure(config: pytest.Config) -> None:
    if not _XDIST_DATABASE:
        return
    import psycopg
    from psycopg import sql

    os.environ["NODESTRO_TEST_POSTGRES"] = _XDIST_DATABASE["admin"]
    with psycopg.connect(_XDIST_DATABASE["admin"], autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(_XDIST_DATABASE["name"])))


@pytest.fixture(autouse=True)
def _no_identity_provider_reads(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """``nodestro-server check`` reads the identity provider's discovery document (DX-31); the tests' providers are
    not reachable, so it passes unless a test asks for the real check."""
    if request.node.get_closest_marker("real_identity_check") is None:
        from nodestro_api import server

        monkeypatch.setattr(server, "check_identity_provider", lambda _server, _http=None: None)


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
        local_defaults: bool = False,
    ) -> TestClient:
        """``local_defaults``: keep the local default models of a new installation; otherwise the app uses the cloud
        defaults of an existing one, which the scripted (Anthropic-shaped) client answers."""
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
        if not local_defaults:
            from nodestro_runtime.schemas import DEFAULT_AGENT_MODEL, DEFAULT_ASSISTANT_MODEL, SettingsIn

            app.state.nodestro.update_settings(
                SettingsIn(default_model=DEFAULT_AGENT_MODEL, assistant_model=DEFAULT_ASSISTANT_MODEL)
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
