"""The server's start-up container check: Orkexa's preflight, not the image build or the self-test."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nodestro_runtime.problems import NodestroError
from orkexa.dynamic import SandboxProbe
from orkexa.dynamic.docker import DockerProbeError
import pytest

from nodestro_api import server as command


def _patch(monkeypatch: pytest.MonkeyPatch, *, probe: SandboxProbe | None = None, error: str = "") -> list[str]:
    import orkexa.dynamic.docker as docker

    calls: list[str] = []

    def inspect_host(cli: str = "docker", **_kwargs: Any) -> object:
        calls.append(cli)
        if error:
            raise DockerProbeError(error, "Cannot connect to the Docker daemon")
        return object()

    monkeypatch.setattr(docker, "inspect_host", inspect_host)
    monkeypatch.setattr(docker, "preflight", lambda _host: probe)
    return calls


def test_a_daemon_that_passes_the_preflight_starts_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch(monkeypatch, probe=SandboxProbe("docker", True))
    command._require_containers(SimpleNamespace(docker="/usr/bin/docker"))  # type: ignore[arg-type]
    assert calls == ["/usr/bin/docker"]  # no image build and no self-test: the Docker monitor runs those


def test_an_unreachable_daemon_refuses_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, error="unreachable")
    with pytest.raises(NodestroError) as caught:
        command._require_containers(SimpleNamespace(docker="docker"))  # type: ignore[arg-type]
    assert caught.value.code == "sandbox.unavailable" and caught.value.params["reason"] == "docker-unreachable"


@pytest.mark.parametrize("reason", ["limits", "seccomp", "rootless-cgroup", "os-type"])
def test_a_daemon_that_cannot_isolate_refuses_the_start(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    _patch(monkeypatch, probe=SandboxProbe("docker", False, reason, "refused"))
    with pytest.raises(NodestroError) as caught:
        command._require_containers(SimpleNamespace(docker="docker"))  # type: ignore[arg-type]
    assert caught.value.code == "sandbox.unavailable" and caught.value.params["reason"] == f"docker-{reason}"
