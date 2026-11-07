"""The liveness signal of a server worker (API-011): the heartbeat file and ``nodestro-server worker-health``."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import pytest

from nodestro_api import server as command
from nodestro_api.worker_health import HEARTBEAT_ENV, Heartbeat, check, heartbeat_path, write


def _beat(path: Path, **changes: Any) -> None:
    beat = {
        "worker": "host:1:abc",
        "pid": 1,
        "time": "2026-11-07T00:00:00+00:00",
        "unix": time.time(),
        "state": "serving",
        "database": "ok",
        "duties": {"worker-runs": "running", "worker-reaper": "running"},
    }
    beat.update(changes)
    write(path, beat)


def test_the_check_reads_the_heartbeat_file(tmp_path: Path) -> None:
    path = tmp_path / "beat.json"
    missing = check(path)
    assert not missing.alive and "no heartbeat file" in missing.reason
    path.write_text("{half", encoding="utf-8")
    assert check(path).reason == "the heartbeat file cannot be read"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert not check(path).alive
    path.write_text(json.dumps({"unix": "soon"}), encoding="utf-8")
    assert not check(path).alive

    _beat(path)
    alive = check(path)
    assert alive.alive and alive.reason.startswith("worker host:1:abc is serving")
    _beat(path, unix=time.time() - 61)
    stale = check(path)
    assert not stale.alive and "more than 60 s" in stale.reason
    assert check(path, max_age=120).alive
    assert not check(path, max_age=30, clock=lambda: time.time() + 100).alive
    _beat(path, database="unreachable")
    assert check(path).reason == "the worker could not reach its database at the last heartbeat"
    _beat(path, duties={"worker-runs": "failed", "worker-reaper": "stopped", "worker-docker": "running"})
    assert check(path).reason == "duties of the worker ended: worker-reaper, worker-runs"
    # Draining after SIGTERM: duties end, the worker stays alive while it finishes its runs.
    _beat(path, state="stopping", duties={"worker-runs": "ended", "worker-reaper": "stopped"})
    assert check(path).alive and "is stopping" in check(path).reason
    assert [item.name for item in tmp_path.iterdir()] == ["beat.json"]  # no temporary files are left


def test_the_heartbeat_reports_duties_and_the_database() -> None:
    async def scenario(tmp: Path) -> list[dict[str, Any]]:
        async def forever() -> None:
            await asyncio.Event().wait()

        async def broken() -> None:
            raise RuntimeError("duty crashed")

        tasks = {
            "worker-runs": asyncio.create_task(forever()),
            "worker-reaper": asyncio.create_task(broken()),
            "worker-docker": asyncio.create_task(forever()),
        }
        tasks["worker-docker"].cancel()
        await asyncio.sleep(0.01)
        reachable = [True, False]

        async def database() -> bool:
            return reachable.pop(0)

        stopping = [False]
        beats = Heartbeat(
            tmp / "beat.json",
            name="w1",
            duties=lambda: tasks,
            database=database,
            stopping=lambda: stopping[0],
            every=0.01,
        )
        written = [await beats.beat()]
        stopping[0] = True
        written.append(await beats.beat())
        tasks["worker-runs"].cancel()
        return written

    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        first, second = asyncio.run(scenario(Path(folder)))
        stored = json.loads((Path(folder) / "beat.json").read_text(encoding="utf-8"))
    assert first["duties"] == {"worker-runs": "running", "worker-reaper": "failed", "worker-docker": "stopped"}
    assert (first["state"], first["database"], first["worker"], first["pid"]) == ("serving", "ok", "w1", os.getpid())
    assert second["duties"] == {"worker-runs": "running", "worker-reaper": "ended", "worker-docker": "ended"}
    assert (second["state"], second["database"]) == ("stopping", "unreachable")
    assert stored == second


def test_a_failing_write_is_logged_and_retried(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a folder", encoding="utf-8")
    calls: list[int] = []

    async def database() -> bool:
        calls.append(1)
        if len(calls) == 3:
            done.set()
        return True

    async def scenario() -> None:
        beats = Heartbeat(
            blocked / "beat.json", name="w", duties=dict, database=database, stopping=lambda: False, every=0.01
        )
        await beats.run(done)

    done = asyncio.Event()
    asyncio.run(scenario())
    assert len(calls) == 3
    assert "Writing the worker heartbeat failed" in caplog.text


def test_the_command_exits_with_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It needs no database or other setting: only the file."""
    for name in list(os.environ):
        if name.startswith("NODESTRO_"):
            monkeypatch.delenv(name)
    path = tmp_path / "beat.json"

    def code(*argv: str) -> int:
        with pytest.raises(SystemExit) as ended:
            command.main(["worker-health", *argv])
        return int(ended.value.code or 0)

    assert heartbeat_path() is None
    assert code() == 2 and HEARTBEAT_ENV in capsys.readouterr().err
    assert code("--file", str(path)) == 1 and "no heartbeat file yet" in capsys.readouterr().err
    _beat(path)
    assert code("--file", str(path)) == 0 and "is serving" in capsys.readouterr().out
    monkeypatch.setenv(HEARTBEAT_ENV, str(path))
    assert heartbeat_path() == path
    assert code() == 0
    _beat(path, unix=time.time() - 30)
    assert code("--max-age", "10") == 1
    assert code("--max-age", "0") == 2 and "--max-age" in capsys.readouterr().err
    # As the container's health check runs it: its own process.
    done = subprocess.run(
        [sys.executable, "-m", "nodestro_api.server", "worker-health", "--file", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={key: value for key, value in os.environ.items() if not key.startswith("NODESTRO_")},
    )
    assert done.returncode == 0, done.stderr
