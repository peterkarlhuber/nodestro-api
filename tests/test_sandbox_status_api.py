"""The Docker sandbox's status in the API: ``GET /api/sandbox/status`` and ``POST /api/sandbox/self-test`` (plan A1).

Both answer the runtime's background check (``Workspace.docker_status``); neither waits for ``docker info`` unless a
bounded wait is asked for. The monitors here are stand-ins (``StaticDockerMonitor``) or the real monitor with a
daemon that does not answer."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timezone
import sys
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime import docker_status
from nodestro_runtime.docker_status import DockerMonitor, DockerStatus, SandboxCheck
from nodestro_runtime.testing import StaticDockerMonitor
from orkexa.dynamic.docker import DockerHost
import pytest

from nodestro_api import api
from nodestro_api.permissions import Permission, Principal


@pytest.fixture(autouse=True)
def _fresh_throttle() -> Iterator[None]:
    api._sandbox_checks.clear()
    yield
    api._sandbox_checks.clear()


class RecordingMonitor(StaticDockerMonitor):
    """A static monitor that records each call and turns approved once a refresh was asked for."""

    def __init__(self, status: DockerStatus | None = None) -> None:
        super().__init__(status or docker_status.CHECKING)
        self.calls: list[tuple[bool, float]] = []

    def status(self, *, refresh: bool = False, wait: float = 0.0) -> DockerStatus:
        self.calls.append((refresh, wait))
        if refresh:
            self.refreshes += 1
            # A forced check runs the self-test again: a new time.
            self.current = replace(docker_status.approved_status(), tested_at=datetime.now(timezone.utc))
        return self.current


def _monitor(client: TestClient, monitor: Any) -> Any:
    client.app.state.nodestro.docker_monitor = monitor  # type: ignore[attr-defined]
    return monitor


def _as(client: TestClient, *permissions: Permission) -> None:
    principal = Principal(id="someone", permissions=frozenset(permissions))
    client.app.state.principal_resolver = lambda _connection: principal  # type: ignore[attr-defined]


def test_the_status_is_the_settings_docker_row(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    found = DockerStatus(
        "unavailable",
        "docker-limits",
        "The daemon does not enforce process limits",
        checks=(SandboxCheck("cli", True, "/usr/bin/docker"), SandboxCheck("preflight", False, "no pids limit")),
    )
    _monitor(client, StaticDockerMonitor(found))
    answer = client.get("/api/sandbox/status")
    assert answer.status_code == 200
    body = answer.json()
    assert (body["name"], body["state"], body["reason"], body["available"]) == (
        "docker",
        "unavailable",
        "docker-limits",
        False,
    )
    assert body["checks"] == [
        {"name": "cli", "ok": True, "detail": "/usr/bin/docker"},
        {"name": "preflight", "ok": False, "detail": "no pids limit"},
    ]
    assert {"engine", "rootless", "cgroup_version", "tested_at", "isolation", "notes"} <= set(body)
    docker = next(row for row in client.get("/api/settings").json()["sandbox_backends"] if row["name"] == "docker")
    assert docker == body


def test_refresh_starts_a_check_and_answers_at_once(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    monitor = _monitor(client, RecordingMonitor())
    assert client.get("/api/sandbox/status").json()["state"] == "checking"
    assert monitor.calls == [(False, 0.0)]  # never waits unless asked
    assert client.get("/api/sandbox/status?refresh=1").json()["state"] == "approved"
    assert monitor.calls[-1] == (True, 0.0) and monitor.refreshes == 1
    # A second refresh right after starts no new check (one monitor serves every tenant of a server).
    client.get("/api/sandbox/status?refresh=true")
    assert monitor.refreshes == 1 and monitor.calls[-1] == (False, 0.0)
    client.get("/api/sandbox/status?wait=2.5")
    assert monitor.calls[-1] == (False, 2.5)


def test_the_wait_is_capped(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    _monitor(client, RecordingMonitor())
    assert client.get("/api/sandbox/status?wait=6").status_code == 422
    assert client.get("/api/sandbox/status?wait=-1").status_code == 422
    assert client.post("/api/sandbox/self-test?timeout=61").status_code == 422


@pytest.mark.real_docker_status
def test_neither_route_waits_for_a_daemon_that_does_not_answer(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    gate = threading.Event()

    def inspect(docker: str, *, env: Any = None) -> DockerHost:
        gate.wait(20)
        raise docker_status.DockerProbeError("unreachable", "Cannot connect to the Docker daemon")

    monitor = DockerMonitor(lambda: None, docker=sys.executable, environ={"PATH": "/usr/bin"}, inspect=inspect)  # type: ignore[arg-type,return-value]
    _monitor(client, monitor)
    try:
        began = time.monotonic()
        assert client.get("/api/settings").status_code == 200
        assert client.get("/api/sandbox/status").json()["state"] == "checking"
        assert client.get("/api/sandbox/status?refresh=1").json()["reason"] == "docker-checking"
        assert client.post("/api/sandbox/self-test?timeout=0").json()["state"] == "checking"
        assert time.monotonic() - began < 5
    finally:
        gate.set()
        monitor.wait(10)
        monitor.close()
    status = client.get("/api/sandbox/status").json()
    assert (status["state"], status["reason"]) == ("unavailable", "docker-unreachable")


def test_the_self_test_runs_the_check_again_and_waits_for_it(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    monitor = _monitor(client, RecordingMonitor(docker_status.unavailable_status("docker-selftest", "no seccomp")))
    answer = client.post("/api/sandbox/self-test?timeout=12")
    assert answer.status_code == 200
    assert answer.json()["state"] == "approved" and answer.json()["available"] is True
    assert monitor.refreshes == 1
    assert (True, 12.0) in monitor.calls
    # Within the interval no new check starts; the answer is the current status.
    assert client.post("/api/sandbox/self-test").json()["state"] == "approved"
    assert monitor.refreshes == 1 and monitor.calls[-1] == (False, 30.0)


def test_a_check_already_running_is_followed_by_the_forced_one(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    stale = DockerStatus("approved", "", "passed earlier", checks=(SandboxCheck("self-test", True),), tested_at=None)

    class Running(StaticDockerMonitor):
        """The first forced status still answers the earlier check's result."""

        def status(self, *, refresh: bool = False, wait: float = 0.0) -> DockerStatus:
            if refresh:
                self.refreshes += 1
                if self.refreshes == 2:
                    self.current = DockerStatus(
                        "approved",
                        "",
                        "passed now",
                        checks=(SandboxCheck("self-test", True),),
                        tested_at=datetime.now(timezone.utc),
                    )
            return self.current

    monitor = _monitor(client, Running(stale))
    body = client.post("/api/sandbox/self-test?timeout=5").json()
    assert monitor.refreshes == 2 and body["detail"] == "passed now" and body["tested_at"]


def test_reading_needs_read_and_the_self_test_admin(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    monitor = _monitor(client, RecordingMonitor())
    _as(client, Permission.READ)
    assert client.get("/api/sandbox/status?refresh=1").status_code == 200
    refused = client.post("/api/sandbox/self-test")
    assert refused.status_code == 403
    assert refused.json()["code"] == "access.forbidden" and refused.json()["params"] == {"permission": "admin"}
    assert monitor.refreshes == 1  # only the status' refresh
    api._sandbox_checks.clear()
    _as(client, Permission.READ, Permission.ADMIN)
    assert client.post("/api/sandbox/self-test").status_code == 200 and monitor.refreshes == 2
    _as(client)
    assert client.get("/api/sandbox/status").status_code == 403


def test_the_diagnosis_reports_the_docker_checks(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    found = DockerStatus(
        "unavailable",
        "docker-seccomp",
        "No seccomp filter",
        checks=(SandboxCheck("preflight", False, "No seccomp filter"),),
    )
    _monitor(client, StaticDockerMonitor(found))
    checks = {item["id"]: item for item in client.get("/api/diagnostics").json()["checks"]}
    docker = checks["docker"]
    assert docker["code"] == "unavailable" and docker["message"] == "No seccomp filter"
    assert docker["params"]["reason"] == "docker-seccomp" and docker["params"]["state"] == "unavailable"
    assert docker["params"]["checks"] == [{"name": "preflight", "ok": False, "detail": "No seccomp filter"}]
    _monitor(client, StaticDockerMonitor(docker_status.approved_status()))
    checks = {item["id"]: item for item in client.get("/api/diagnostics").json()["checks"]}
    assert checks["docker"]["code"] == "available" and checks["docker"]["status"] == "ok"
    _monitor(client, StaticDockerMonitor())
    checks = {item["id"]: item for item in client.get("/api/diagnostics").json()["checks"]}
    assert checks["docker"]["code"] == "not_installed"
