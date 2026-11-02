"""The Docker sandbox's status in the API: ``GET /api/sandbox/status`` and ``POST /api/sandbox/self-test`` (plan A1).

Both answer the runtime's background check (``Workspace.docker_status``); neither waits for ``docker info`` unless a
bounded wait is asked for. The monitors here are stand-ins (``StaticDockerMonitor``) or the real monitor with a
daemon that does not answer."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
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

from nodestro_api import sandbox_status
from nodestro_api.permissions import Permission, Principal


@pytest.fixture(autouse=True)
def _fresh_throttle() -> Iterator[None]:
    sandbox_status.reset()
    yield
    sandbox_status.reset()


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
            self.current = replace(docker_status.approved_status(), tested_at=datetime.now(UTC))
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
    assert (body["check_started"], body["throttled"], body["retry_after"], body["redacted"]) == (
        False,
        False,
        None,
        False,
    )
    docker = next(row for row in client.get("/api/settings").json()["sandbox_backends"] if row["name"] == "docker")
    assert docker == {key: value for key, value in body.items() if key in docker}
    assert set(body) - set(docker) == {
        "check_started",
        "throttled",
        "retry_after",
        "refresh_ignored",
        "wait_skipped",
        "redacted",
    }


def test_refresh_starts_a_check_and_answers_at_once(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    monitor = _monitor(client, RecordingMonitor())
    assert client.get("/api/sandbox/status").json()["state"] == "checking"
    assert monitor.calls == [(False, 0.0)]  # never waits unless asked
    body = client.get("/api/sandbox/status?refresh=1").json()
    assert body["state"] == "approved" and body["check_started"] is True and body["throttled"] is False
    assert monitor.calls[-1] == (True, 0.0) and monitor.refreshes == 1
    # A second refresh right after starts no new check (one monitor serves every tenant of a server), and says so.
    body = client.get("/api/sandbox/status?refresh=true").json()
    assert monitor.refreshes == 1 and monitor.calls[-1] == (False, 0.0)
    assert body["throttled"] is True and body["check_started"] is False
    assert 0 < body["retry_after"] <= sandbox_status.CHECK_INTERVAL
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
    assert answer.json()["check_started"] is True and answer.json()["throttled"] is False
    # Within the interval no new check starts; the answer is the current status and says it is not a new one.
    again = client.post("/api/sandbox/self-test").json()
    assert again["state"] == "approved" and again["throttled"] is True and again["check_started"] is False
    assert again["retry_after"] > 0
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
                        tested_at=datetime.now(UTC),
                    )
            return self.current

    monitor = _monitor(client, Running(stale))
    body = client.post("/api/sandbox/self-test?timeout=5").json()
    assert monitor.refreshes == 2 and body["detail"] == "passed now" and body["tested_at"]


def test_reading_needs_read_and_the_self_test_admin(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    monitor = _monitor(client, RecordingMonitor())
    _as(client, Permission.READ)
    answer = client.get("/api/sandbox/status?refresh=1")
    assert answer.status_code == 200
    # A reader's refresh starts nothing (DS-6) and the answer says so.
    assert answer.json()["refresh_ignored"] is True and answer.json()["check_started"] is False
    assert monitor.refreshes == 0 and monitor.calls[-1] == (False, 0.0)
    refused = client.post("/api/sandbox/self-test")
    assert refused.status_code == 403
    assert refused.json()["code"] == "access.forbidden" and refused.json()["params"] == {"permission": "admin"}
    assert monitor.refreshes == 0
    _as(client, Permission.READ, Permission.ADMIN)
    assert client.post("/api/sandbox/self-test").status_code == 200 and monitor.refreshes == 1
    _as(client)
    assert client.get("/api/sandbox/status").status_code == 403


def test_readers_never_starve_the_admin_self_test(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    monitor = _monitor(client, RecordingMonitor())
    _as(client, Permission.READ)
    for _ in range(5):
        client.get("/api/sandbox/status?refresh=1")
    assert monitor.refreshes == 0
    _as(client, Permission.READ, Permission.ADMIN)
    # An admin's status refresh has a window of its own: the self-test right after it still runs.
    assert client.get("/api/sandbox/status?refresh=1").json()["check_started"] is True
    body = client.post("/api/sandbox/self-test?timeout=1").json()
    assert body["check_started"] is True and body["throttled"] is False and monitor.refreshes == 2


#: A failed check with the host details DS-8 is about.
_LEAKY = DockerStatus(
    "unavailable",
    "docker-unreachable",
    "Cannot connect to the Docker daemon at unix:///run/user/1000/docker.sock or tcp://10.0.0.7:2376",
    host=DockerHost(
        engine="podman",
        os_type="linux",
        arch="x86_64",
        rootless=True,
        cgroup_version="2",
        cgroup_driver="systemd",
        memory_limit=True,
        pids_limit=True,
        cpu_cfs_quota=True,
        cpu_cfs_period=True,
        seccomp_profile="default",
        apparmor=False,
        selinux=False,
        daemon_id="DAEMON-ID-SECRET",
        server_version="5.2.1",
    ),
    checks=(
        SandboxCheck("cli", True, "/opt/secret/bin/docker"),
        SandboxCheck("daemon", False, "error during connect: tcp://10.0.0.7:2376"),
        SandboxCheck("image", False, "sha256:0123456789abcdef"),
    ),
)


def _leaks(text: str) -> list[str]:
    return [
        secret
        for secret in ("docker.sock", "tcp://", "/opt/secret", "sha256:", "podman", "5.2.1", "DAEMON-ID")
        if secret in text
    ]


def test_readers_see_no_host_details(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    _monitor(client, StaticDockerMonitor(_LEAKY))
    full = client.get("/api/sandbox/status").json()  # the local owner is an admin
    assert full["redacted"] is False and full["engine"] == "podman" and full["checks"][0]["detail"]
    _as(client, Permission.READ)
    answer = client.get("/api/sandbox/status")
    body = answer.json()
    assert _leaks(answer.text) == []
    assert (body["state"], body["reason"], body["redacted"]) == ("unavailable", "docker-unreachable", True)
    assert body["checks"] == [
        {"name": "cli", "ok": True, "detail": ""},
        {"name": "daemon", "ok": False, "detail": ""},
        {"name": "image", "ok": False, "detail": ""},
    ]
    assert body["detail"] == "The Docker daemon cannot be reached" + sandbox_status.ADMIN_HINT
    assert body["engine"] == "" and body["rootless"] is True  # not a server: rootless and cgroup stay
    settings = client.get("/api/settings")
    assert settings.status_code == 200 and _leaks(settings.text) == []
    docker = next(row for row in settings.json()["sandbox_backends"] if row["name"] == "docker")
    assert docker["checks"] == body["checks"] and docker["detail"] == body["detail"]


def test_the_settings_selection_quotes_no_host_details(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    _monitor(client, StaticDockerMonitor(_LEAKY))
    client.put("/api/settings", json={"sandbox": "docker"})
    admin = client.get("/api/settings").json()["sandbox_selection"]
    assert "tcp://10.0.0.7" in admin["detail"]
    _as(client, Permission.READ)
    selection = client.get("/api/settings").json()["sandbox_selection"]
    assert selection["reason"] == "docker-unreachable" and _leaks(selection["detail"]) == []
    assert "The Docker daemon cannot be reached" in selection["detail"]


def test_a_server_reader_sees_no_rootless_or_cgroup(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app()
    _monitor(client, StaticDockerMonitor(_LEAKY))
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    monkeypatch.setattr(nodestro, "profile", replace(nodestro.profile, kind="server"))
    _as(client, Permission.READ)
    body = client.get("/api/sandbox/status").json()
    assert body["rootless"] is None and body["cgroup_version"] is None and body["redacted"] is True
    _as(client, Permission.READ, Permission.ADMIN)
    body = client.get("/api/sandbox/status").json()
    assert body["rootless"] is True and body["cgroup_version"] == "2" and body["redacted"] is False


def test_an_approved_status_tells_readers_only_that_it_passed() -> None:
    row = docker_status.approved_status("Preflight and self-test passed (docker 27.1, cgroup v2)").backend()
    hidden = sandbox_status.redact_backend(row, server=True)
    assert hidden.detail == "Preflight and self-test passed" + sandbox_status.ADMIN_HINT
    assert sandbox_status.public_detail(docker_status.CHECKING.backend()) == docker_status.CHECKING.detail
    unknown = row.model_copy(update={"state": "unavailable", "reason": "docker-new"})
    assert sandbox_status.public_detail(unknown).startswith("The Docker sandbox is not available")


def test_too_many_waiters_answer_at_once(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    release = threading.Event()
    entered = threading.Semaphore(0)

    class Slow(RecordingMonitor):
        def status(self, *, refresh: bool = False, wait: float = 0.0) -> DockerStatus:
            found = super().status(refresh=refresh, wait=wait)
            if wait > 0:
                entered.release()
                release.wait(10)
            return found

    monitor = _monitor(client, Slow())
    answers: list[dict[str, Any]] = []
    workers = [
        threading.Thread(target=lambda: answers.append(client.get("/api/sandbox/status?wait=5").json()))
        for _ in range(sandbox_status.MAX_WAITERS)
    ]
    for worker in workers:
        worker.start()
    try:
        for _ in workers:
            assert entered.acquire(timeout=10)
        began = time.monotonic()
        extra = client.get("/api/sandbox/status?wait=5").json()
        assert time.monotonic() - began < 2
        assert extra["wait_skipped"] is True and monitor.calls[-1] == (False, 0.0)
    finally:
        release.set()
        for worker in workers:
            worker.join(10)
    assert len(answers) == sandbox_status.MAX_WAITERS and not any(item["wait_skipped"] for item in answers)
    # The slots are free again.
    assert client.get("/api/sandbox/status?wait=0.1").json()["wait_skipped"] is False


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
