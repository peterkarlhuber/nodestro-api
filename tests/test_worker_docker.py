"""The worker keeps the Docker sandbox's proof warm (:class:`nodestro_api.worker.DockerWatch`), fail-closed.

These tests use the runtime's real :class:`~nodestro_runtime.docker_status.DockerMonitor` with a stand-in for
``docker info`` and the self-test (no daemon, no container); the worker's ``serve`` with the watch is tested in
``test_worker.py`` (PostgreSQL)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

from nodestro_runtime import docker_status, sandbox_selection
from nodestro_runtime.docker_status import DockerMonitor
from nodestro_runtime.sandbox_policy import SandboxPolicy
from orkexa.dynamic import SandboxProbe
from orkexa.dynamic.docker import DockerHost, DockerProbeError
import pytest

from nodestro_api.worker import DockerWatch, uses_docker

DOCKER = sys.executable  # an absolute path that exists: the CLI check passes, nothing is run with it
ENVIRON = {"PATH": "/usr/bin"}
HOST = DockerHost(
    engine="docker",
    os_type="linux",
    arch="x86_64",
    rootless=False,
    cgroup_version="2",
    cgroup_driver="systemd",
    memory_limit=True,
    pids_limit=True,
    cpu_cfs_quota=True,
    cpu_cfs_period=True,
    seccomp_profile="builtin",
    apparmor=True,
    selinux=False,
    daemon_id="daemon-1",
    server_version="27.4.0",
)
#: What a daemon behind a ``DOCKER_HOST`` with credentials could say; none of it may reach the worker's log.
SECRET_DETAIL = "Cannot connect to tcp://operator:s3cret@10.0.0.7:2376 (/run/user/1000/docker.sock)"


class FakeSandbox:
    """Stands in for the runtime-image sandbox: counts self-tests."""

    def __init__(self) -> None:
        self.self_tests = 0

    def self_test(self) -> SandboxProbe:
        self.self_tests += 1
        return SandboxProbe("docker", True, facts=(("image_id", "sha256:" + "ab" * 32),))


class Daemon:
    """``docker info`` of a stand-in daemon: answers ``host`` (or raises it), optionally only once ``gate`` opens;
    counts calls and the most that ran at once."""

    def __init__(self, host: DockerHost | Exception = HOST, gate: threading.Event | None = None) -> None:
        self.host = host
        self.gate = gate
        self.calls = 0
        self.running = 0
        self.most_at_once = 0
        self._lock = threading.Lock()

    def __call__(self, docker: str, *, env: Any = None) -> DockerHost:
        with self._lock:
            self.calls += 1
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)
        try:
            if self.gate is not None:
                self.gate.wait(10)
            if isinstance(self.host, Exception):
                raise self.host
            return self.host
        finally:
            with self._lock:
                self.running -= 1


def make_monitor(sandbox: FakeSandbox, daemon: Daemon, **options: Any) -> DockerMonitor:
    return DockerMonitor(
        lambda: sandbox,  # type: ignore[arg-type,return-value]
        docker=DOCKER,
        environ=ENVIRON,
        inspect=daemon,
        **options,
    )


def until(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def refused(monitor: DockerMonitor) -> bool:
    """Whether a server tenant's tool code is refused now (the selection and the policy's daemon proof)."""
    selection = sandbox_selection.select(SandboxPolicy("server"), "docker", monitor.status())
    return selection.blocked and not docker_status.daemon_proven(DOCKER, ENVIRON)


def test_the_watch_starts_the_check_at_once_and_tool_code_waits_for_the_proof() -> None:
    gate = threading.Event()
    sandbox, daemon = FakeSandbox(), Daemon(gate=gate)
    monitor = make_monitor(sandbox, daemon)
    # Two workspaces report the same monitor (one per process): it is read once per pass.
    watch = DockerWatch(lambda: [monitor, monitor])

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(watch.run(stop, every=60.0, checking_every=0.01))
        try:
            # The first pass (the warm-up) starts the check, without waiting for the daemon.
            assert await asyncio.to_thread(until, lambda: daemon.calls == 1)
            await asyncio.sleep(0.3)  # about 30 passes while the daemon does not answer
            assert daemon.calls == 1 and daemon.most_at_once == 1  # no second check, none in parallel
            assert monitor.status().state == "checking"
            assert refused(monitor)  # fail-closed: nothing runs before the proof
            gate.set()
            assert await asyncio.to_thread(until, lambda: monitor.status().approved)
            assert not refused(monitor)
            assert daemon.calls == 1 and sandbox.self_tests == 1
        finally:
            gate.set()
            stop.set()
            await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_the_watch_keeps_the_proof_fresh_without_repeating_the_self_test(monkeypatch: pytest.MonkeyPatch) -> None:
    # Scaled down: the proof counts 0.6 s, the preflight 0.05 s; the watch reads at most every PROOF_TTL / 2.
    monkeypatch.setattr(docker_status, "PROOF_TTL", 0.6)
    sandbox, daemon = FakeSandbox(), Daemon()
    monitor = make_monitor(sandbox, daemon, preflight_ttl=0.05)
    monitor.status()
    assert until(lambda: monitor.status().approved) and docker_status.daemon_proven(DOCKER, ENVIRON)

    async def watched(seconds: float) -> list[bool]:
        stop = asyncio.Event()
        task = asyncio.create_task(DockerWatch(lambda: [monitor]).run(stop, every=60.0, checking_every=0.01))
        proven: list[bool] = []
        began = time.monotonic()
        while time.monotonic() - began < seconds:
            # Asked like a tool call does (daemon_proven); its own re-check only starts when the proof is stale.
            proven.append(docker_status.daemon_proven(DOCKER, ENVIRON))
            await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, 2)
        return proven

    assert all(asyncio.run(watched(1.5)))  # more than twice the proof's lifetime, never refused
    assert sandbox.self_tests == 1  # the self-test is not repeated; only the cheap preflight is
    assert 1 < daemon.calls < 20 and daemon.most_at_once == 1
    # Without the watch the proof expires while nothing asks (the gap this closes).
    time.sleep(0.7)
    assert not docker_status.daemon_proven(DOCKER, ENVIRON)


def test_an_unavailable_daemon_is_reported_once_per_change_without_host_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = [1000.0]
    daemon = Daemon(host=DockerProbeError("unreachable", SECRET_DETAIL))
    monitor = make_monitor(FakeSandbox(), daemon, clock=lambda: clock[0])
    watch = DockerWatch(lambda: [monitor])
    caplog.set_level(logging.INFO, logger="nodestro_api.worker")

    def passes(count: int) -> None:
        for _ in range(count):
            clock[0] += 10  # past the preflight's TTL: every pass starts a new check
            watch.poll()
            monitor.wait(5)
        watch.poll()

    passes(4)
    assert daemon.calls >= 4
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    message = warnings[0].getMessage()
    assert "docker-unreachable" in message and "The Docker daemon cannot be reached" in message
    assert "refused" in message
    text = caplog.text
    assert "s3cret" not in text and "tcp://" not in text and "docker.sock" not in text
    # The daemon comes back: reported once; it goes away again: reported again.
    daemon.host = HOST
    passes(3)
    daemon.host = DockerProbeError("unreachable", SECRET_DETAIL)
    passes(3)
    messages = [record.getMessage() for record in caplog.records]
    assert sum("proven" in message for message in messages) == 1
    assert sum("unavailable" in message for message in messages) == 2
    assert "s3cret" not in caplog.text


def test_a_failing_status_read_does_not_end_the_watch(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[int] = []

    def monitors() -> list[Any]:
        calls.append(1)
        raise RuntimeError("broken")

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(DockerWatch(monitors).run(stop, every=0.01))
        await asyncio.to_thread(until, lambda: len(calls) >= 3)
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert len(calls) >= 3 and "Reading the Docker sandbox status failed" in caplog.text


def test_the_watch_reads_only_monitors_of_workspaces_that_use_docker() -> None:
    def context(profile: str, mode: str | None) -> Any:
        return SimpleNamespace(sandbox_policy=SandboxPolicy(profile), tools=SimpleNamespace(sandbox_mode=mode))  # type: ignore[arg-type]

    assert uses_docker(context("server", None))
    assert uses_docker(context("local-container", None))
    assert uses_docker(context("desktop", "docker")) and uses_docker(context("desktop", "auto"))
    assert not uses_docker(context("desktop", "linux")) and not uses_docker(context("desktop", "subprocess"))
