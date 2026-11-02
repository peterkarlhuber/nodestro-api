"""What the Docker sandbox routes show and when they may start a check (security review DS-6, DS-8, DS-12).

- **Who may start a check (DS-6).** Only administrators start a forced check (``refresh=1`` on ``GET
  /api/sandbox/status``, ``POST /api/sandbox/self-test``); a reader's ``refresh`` is ignored and the answer says so
  (``refresh_ignored``). The two admin paths have separate windows of :data:`CHECK_INTERVAL` seconds per Docker
  monitor, so polling the status with ``refresh`` never starves the self-test. An answer that did not start the check
  it asked for says ``throttled`` with ``retry_after``.
- **What a reader sees (DS-8).** One Docker monitor serves every tenant of a server; its details (CLI path, engine
  and version, image ID, the daemon's error output with socket paths or ``tcp://`` addresses) are the host's. Callers
  without ``admin`` get state, reason, check names and ``ok`` flags with fixed English texts instead
  (:func:`redact_backend`); on a server also no ``rootless`` and ``cgroup_version``.
- **Who may wait (DS-12).** A ``wait`` blocks a worker thread; at most :data:`MAX_WAITERS` requests wait at once,
  the others answer at once with the current status (``wait_skipped``)."""

from __future__ import annotations

from collections.abc import Callable
import threading
import time

from nodestro_runtime.docker_status import CHECKING
from nodestro_runtime.schemas import SandboxBackendStatus, SandboxCheck, Settings

#: A new forced check starts at most this often per Docker monitor and path (seconds).
CHECK_INTERVAL = 10.0
#: How many status requests may wait for a running check at once (per process; the self-test has its own).
MAX_WAITERS = 4
#: How many self-tests may wait for their check at once (per process).
MAX_SELF_TEST_WAITERS = 2

#: The window of each path: ``refresh`` (``GET /api/sandbox/status``) and ``self-test`` (``POST``).
_checks: dict[tuple[int, str], float] = {}
_checks_lock = threading.Lock()
_waiters = threading.BoundedSemaphore(MAX_WAITERS)
_self_test_waiters = threading.BoundedSemaphore(MAX_SELF_TEST_WAITERS)

#: What a caller without ``admin`` reads as ``detail``, per reason (no host detail in it).
PUBLIC_DETAILS: dict[str, str] = {
    "": "Preflight and self-test passed",
    "docker-missing": "Docker is not installed",
    "docker-unreachable": "The Docker daemon cannot be reached",
    "docker-limits": "The Docker daemon does not enforce the sandbox's resource limits",
    "docker-seccomp": "The Docker daemon applies no seccomp filter",
    "docker-rootless-cgroup": "Rootless Docker on cgroup v1 cannot enforce the sandbox's limits",
    "docker-os-type": "The Docker daemon does not run Linux containers",
    "docker-image": "The sandbox image cannot be found or built",
    "docker-selftest": "A container of the sandbox's shape showed a missing restriction",
    "docker-checking": CHECKING.detail,
}
#: Appended to a redacted ``detail``.
ADMIN_HINT = " (an administrator sees the details)"


class SandboxStatus(SandboxBackendStatus):
    """The Docker row of the settings' ``sandbox_backends``, and what this request did about it."""

    #: Whether this request started a forced check (``refresh`` or the self-test). ``false`` with ``throttled`` or
    #: ``refresh_ignored``: the answer is the running or last check, not a new one.
    check_started: bool = False
    #: A forced check was asked for, but one started on this path less than 10 s ago; ``retry_after`` (seconds) says
    #: when the next may start.
    throttled: bool = False
    retry_after: float | None = None
    #: ``refresh`` was asked for without the permission ``admin`` and ignored (only administrators start checks).
    refresh_ignored: bool = False
    #: ``wait`` (or the self-test's ``timeout``) was not waited because too many requests wait already.
    wait_skipped: bool = False
    #: Host details were left out (the caller lacks ``admin``): ``detail`` is a fixed text, the checks' ``detail`` and
    #: ``engine`` are empty, and on a server ``rootless`` and ``cgroup_version`` are ``null``.
    redacted: bool = False


def try_start(monitor: object, path: str) -> float | None:
    """Claim the window of ``path`` on ``monitor``: ``None`` when a forced check may start now, otherwise the
    seconds until it may."""
    key = (id(monitor), path)
    moment = time.monotonic()
    with _checks_lock:
        last = _checks.get(key)
        if last is not None and moment - last < CHECK_INTERVAL:
            return round(CHECK_INTERVAL - (moment - last), 1)
        _checks[key] = moment
        return None


def bounded_wait(
    read: Callable[[float], SandboxBackendStatus], wait: float, *, self_test: bool = False
) -> tuple[SandboxBackendStatus, bool]:
    """``read(wait)`` when a waiter slot is free, otherwise ``read(0)`` at once; and whether the wait was skipped."""
    if wait <= 0:
        return read(0.0), False
    slots = _self_test_waiters if self_test else _waiters
    if not slots.acquire(blocking=False):
        return read(0.0), True
    try:
        return read(wait), False
    finally:
        slots.release()


def public_detail(row: SandboxBackendStatus) -> str:
    """The ``detail`` a caller without ``admin`` sees for ``row``."""
    text = PUBLIC_DETAILS.get(row.reason) if row.state != "approved" else PUBLIC_DETAILS[""]
    if text is None:
        text = "The Docker sandbox is not available"
    return text if row.reason == "docker-checking" else text + ADMIN_HINT


def redact_backend(row: SandboxBackendStatus, *, server: bool) -> SandboxBackendStatus:
    """``row`` without host details (DS-8): state, reason, check names and ``ok`` flags stay."""
    update: dict[str, object] = {
        "detail": public_detail(row),
        "engine": "",
        "checks": [SandboxCheck(name=check.name, ok=check.ok) for check in row.checks],
    }
    if server:
        update.update(rootless=None, cgroup_version=None)
    return row.model_copy(update=update)


def redact_settings(settings: Settings, *, server: bool) -> Settings:
    """The settings without the Docker row's host details, also where the sandbox selection quotes them."""
    rows = list(settings.sandbox_backends)
    selection = settings.sandbox_selection
    for index, row in enumerate(rows):
        if row.name != "docker":
            continue
        hidden = redact_backend(row, server=server)
        rows[index] = hidden
        if selection is not None and row.detail and row.detail in selection.detail:
            selection = selection.model_copy(update={"detail": selection.detail.replace(row.detail, hidden.detail)})
    return settings.model_copy(update={"sandbox_backends": rows, "sandbox_selection": selection})


def reset() -> None:
    """Forget every window (tests)."""
    with _checks_lock:
        _checks.clear()


__all__ = [
    "CHECK_INTERVAL",
    "MAX_SELF_TEST_WAITERS",
    "MAX_WAITERS",
    "PUBLIC_DETAILS",
    "SandboxStatus",
    "bounded_wait",
    "public_detail",
    "redact_backend",
    "redact_settings",
    "reset",
    "try_start",
]
