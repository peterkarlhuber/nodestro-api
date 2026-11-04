"""``nodestro-api`` says "running" only once it listens, and stops with one sentence on a taken port (DX B2)."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from nodestro_api import cli


def _listening() -> socket.socket:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    return server


def test_a_taken_port_is_named() -> None:
    with _listening() as taken:
        port = taken.getsockname()[1]
        problem = cli.port_problem("127.0.0.1", port)
        assert problem is not None and f"Port {port} on 127.0.0.1 is already in use" in problem
        assert "--port" in problem
    assert cli.port_problem("127.0.0.1", port) is None


def test_main_stops_before_saying_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    for key in ("NODESTRO_PROFILE", "NODESTRO_HOST"):
        monkeypatch.delenv(key, raising=False)
    import uvicorn

    served: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: served.append(args))
    monkeypatch.setattr(uvicorn, "Server", lambda *args, **kwargs: served.append(args))
    with _listening() as taken, pytest.raises(SystemExit) as stopped:
        cli.main(["--port", str(taken.getsockname()[1])])
    assert stopped.value.code == 2
    out = capsys.readouterr()
    assert "running" not in out.out and "already in use" in out.err
    assert served == []


def test_a_network_address_is_refused_as_such_before_the_port_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The port check comes after the refusals: an address that cannot be bound still gets the login refusal."""
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    for key in ("NODESTRO_PROFILE", "NODESTRO_HOST", "NODESTRO_ALLOWED_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(cli, "require_free_port", lambda args: pytest.fail("port checked before the refusal"))
    with pytest.raises(SystemExit, match="no login"):
        cli.main(["--host", "192.168.1.20"])


def test_running_is_printed_after_the_server_listens(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {**os.environ, "NODESTRO_DATA_DIR": str(tmp_path / "data")}
    env.pop("NODESTRO_PROFILE", None)
    # Through a pipe Python buffers its output unless told otherwise: the line must arrive without that help.
    env.pop("PYTHONUNBUFFERED", None)
    process = subprocess.Popen(
        [sys.executable, "-c", f"from nodestro_api.cli import main; main(['--port', '{port}'])"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        lines: queue.Queue[str] = queue.Queue()
        stdout = process.stdout
        threading.Thread(target=lambda: [lines.put(line) for line in stdout], daemon=True).start()
        deadline = time.monotonic() + 90
        line = ""
        while time.monotonic() < deadline and "running at" not in line:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
        assert "Nodestro API is running at" in line
        # It listens by the time it says so.
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    finally:
        process.terminate()
        process.wait(timeout=30)
