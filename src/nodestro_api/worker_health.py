"""The liveness signal of a worker process (API-011): a heartbeat file and ``nodestro-server worker-health``.

A worker has no HTTP listener, so ``nodestro-server check`` was its only health check, and that checks the
configuration and the database, not the worker. With ``NODESTRO_WORKER_HEARTBEAT_FILE`` set, the worker writes a
small JSON file there every :attr:`~nodestro_api.worker.WorkerSettings.heartbeat_every` seconds (10 by default) from
its event loop:

``{"worker": ..., "pid": ..., "time": <ISO time>, "unix": <seconds>, "state": "serving" | "stopping",
"database": "ok" | "unreachable", "duties": {"worker-runs": "running", ...}}``

``nodestro-server worker-health`` (:func:`check`) reads it and exits ``0`` when the worker is alive, ``1`` otherwise:
the file is missing or unreadable, older than ``--max-age`` seconds (default 60: the event loop is blocked or the
process hangs), the worker could not reach its database at the last beat, or one of its duties (the run, reindex and
export loops, the reaper, the scheduler ...) ended while the worker was serving. A worker that drains after SIGTERM
reports ``stopping`` and stays healthy while it finishes its runs. The file holds no tenant data, host details or
secrets: names of duties, the worker name (host, process id and a random part, as in the job table) and times.

The file is written to a temporary file next to it and moved into place, so a reader never sees half a file. It is
meant for a path inside the worker's own container or host (``/tmp/nodestro-worker.json``), not the shared data
volume: each worker needs its own file. Without the variable no file is written and the command fails with exit code
``2`` (nothing to check).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Where the worker writes its heartbeat (unset: no heartbeat).
HEARTBEAT_ENV = "NODESTRO_WORKER_HEARTBEAT_FILE"
#: How old the heartbeat may be before ``worker-health`` fails (seconds).
DEFAULT_MAX_AGE = 60.0


def heartbeat_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """The heartbeat file named by :data:`HEARTBEAT_ENV` (``None``: unset)."""
    raw = (os.environ if environ is None else environ).get(HEARTBEAT_ENV, "").strip()
    return Path(raw) if raw else None


def write(path: Path, beat: Mapping[str, Any]) -> None:
    """Write ``beat`` to ``path`` atomically (a temporary file in the same folder, then a rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(dict(beat), stream, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


class Heartbeat:
    """Writes the worker's heartbeat (see the module documentation) until :meth:`run` is told to end.

    ``duties`` returns the worker's duty tasks by name; ``database`` answers whether the database is reachable
    (:class:`nodestro_api.server_app.DatabaseHealth`); ``stopping`` whether the worker was asked to stop."""

    def __init__(
        self,
        path: Path,
        *,
        name: str,
        duties: Callable[[], Mapping[str, asyncio.Task[Any]]],
        database: Callable[[], Awaitable[bool]],
        stopping: Callable[[], bool],
        every: float = 10.0,
    ) -> None:
        self.path = path
        self.name = name
        self.duties = duties
        self.database = database
        self.stopping = stopping
        self.every = every

    async def beat(self) -> dict[str, Any]:
        """Write one heartbeat; returns what was written."""
        stopping = self.stopping()
        duties: dict[str, str] = {}
        for name, task in self.duties().items():
            if not task.done():
                duties[name] = "running"
            elif stopping:
                duties[name] = "ended"
            else:  # a duty loop never ends by itself while the worker serves
                duties[name] = "failed" if not task.cancelled() and task.exception() is not None else "stopped"
        reachable = await self.database()
        moment = time.time()
        found = {
            "worker": self.name,
            "pid": os.getpid(),
            "time": datetime.fromtimestamp(moment, UTC).isoformat(timespec="seconds"),
            "unix": moment,
            "state": "stopping" if stopping else "serving",
            "database": "ok" if reachable else "unreachable",
            "duties": duties,
        }
        await asyncio.to_thread(write, self.path, found)
        return found

    async def run(self, done: asyncio.Event) -> None:
        """Beat at once and then every :attr:`every` seconds until ``done`` is set; a failed write is logged and
        tried again at the next beat (the file then grows old, which the check reports)."""
        while not done.is_set():
            try:
                await self.beat()
            except Exception:
                logger.exception("Writing the worker heartbeat failed")
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(done.wait(), timeout=self.every)


@dataclass(frozen=True)
class Health:
    """The answer of :func:`check`."""

    alive: bool
    reason: str


def check(path: Path, *, max_age: float = DEFAULT_MAX_AGE, clock: Callable[[], float] = time.time) -> Health:
    """Whether the worker that writes ``path`` is alive (see the module documentation)."""
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        written = float(beat["unix"])
    except FileNotFoundError:
        return Health(False, "no heartbeat file yet: the worker has not started or does not write one")
    except (OSError, ValueError, TypeError, KeyError):
        return Health(False, "the heartbeat file cannot be read")
    if not isinstance(beat, dict):
        return Health(False, "the heartbeat file cannot be read")
    age = clock() - written
    if age > max_age:
        return Health(False, f"the last heartbeat is {age:.0f} s old (more than {max_age:.0f} s)")
    if beat.get("database") != "ok":
        return Health(False, "the worker could not reach its database at the last heartbeat")
    listed = beat.get("duties")
    duties: dict[str, Any] = listed if isinstance(listed, dict) else {}
    ended = sorted(str(name) for name, state in duties.items() if state in ("failed", "stopped"))
    if ended and beat.get("state") != "stopping":
        return Health(False, f"duties of the worker ended: {', '.join(ended)}")
    state = "stopping" if beat.get("state") == "stopping" else "serving"
    return Health(True, f"worker {beat.get('worker', '?')} is {state}; last heartbeat {max(age, 0):.0f} s ago")


__all__ = ["DEFAULT_MAX_AGE", "HEARTBEAT_ENV", "Health", "Heartbeat", "check", "heartbeat_path", "write"]
