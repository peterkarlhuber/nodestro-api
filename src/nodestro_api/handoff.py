"""Secrets the desktop app hands to the backend through a pipe, never through the environment or the command line.

Any process of the same OS user can read another process's initial command line and environment: on macOS with
``sysctl`` ``KERN_PROCARGS2`` (also from inside the App Sandbox), on Linux in ``/proc/<pid>/environ`` and
``/proc/<pid>/cmdline``. Removing a variable from ``os.environ`` later does not change what they show. So the desktop
app (``nodestro-desktop``) does not put the per-launch secret or the MCP token vault key into the backend's
environment (independent review MS-1). It starts the backend with ``--secrets-fd 0``, makes the backend's standard
input a pipe, writes one JSON object and closes its end:

    {"launch_secret": "<64 hex characters>", "vault_key": "<base64>" | null}

:func:`receive` is the first thing the command line does (:func:`nodestro_api.cli.prepare`): it reads the descriptor
to its end (at most :data:`MAX_BYTES`), makes it non-inheritable and closes it; descriptor 0 is replaced by the null
device, so no process the backend starts (uv, OCR, sandboxed tools, MCP servers) inherits the pipe. The values stay
in this process's memory only and are never written to ``os.environ``. Standard input works the same on Linux, macOS
and Windows; another descriptor number (3 or higher, inherited from the parent) is accepted on POSIX systems.

Operators can still configure the backend with ``NODESTRO_LAUNCH_SECRET`` (:func:`nodestro_api.launch
.launch_secret_from_env`) and ``NODESTRO_VAULT_KEY`` (:func:`vault_key`): both are taken out of ``os.environ`` on the
first read, so no child process inherits them either; such a value is still visible in the backend's own initial
environment. After a handoff both variables are removed and ignored.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import json
import os

from nodestro_runtime.problems import NodestroError

#: The command line option that names the descriptor.
OPTION = "--secrets-fd"
#: The operator's variable for the MCP token vault key (``nodestro_runtime.mcp.vault``).
VAULT_KEY_ENV = "NODESTRO_VAULT_KEY"
#: A handoff is a few hundred bytes; anything much larger is refused.
MAX_BYTES = 64 * 1024
_KEYS = frozenset({"launch_secret", "vault_key"})


@dataclass(frozen=True)
class Handoff:
    """The values of one handoff; an empty or missing value is ``None``."""

    launch_secret: str | None = field(default=None, repr=False)
    vault_key: str | None = field(default=None, repr=False)


_received: Handoff | None = None
_UNSET = object()
_vault_from_env: object | str | None = _UNSET


def _invalid(reason: str) -> NodestroError:
    # The reason never quotes the payload: it holds the secrets.
    return NodestroError("access.handoff_invalid", f"The secrets on {OPTION} are not usable: {reason}", reason=reason)


def parse(payload: bytes) -> Handoff:
    """The handoff in ``payload`` (one JSON object with the keys ``launch_secret`` and ``vault_key``)."""
    try:
        found = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _invalid("not a JSON object") from None
    if not isinstance(found, dict):
        raise _invalid("not a JSON object")
    unknown = sorted(set(found) - _KEYS)
    if unknown:
        raise _invalid(f"unknown keys {', '.join(unknown)}")
    values: dict[str, str | None] = {}
    for key in sorted(_KEYS):
        value = found.get(key)
        if value is not None and not isinstance(value, str):
            raise _invalid(f"{key} must be a string")
        values[key] = (value.strip() or None) if isinstance(value, str) else None
    return Handoff(**values)


def read(fd: int) -> Handoff:
    """Read and close descriptor ``fd`` and parse what it held; descriptor 0 is replaced by the null device.

    The descriptor is made non-inheritable before anything else, so a process started while it is read cannot get it
    either. It is closed also when what it held is refused."""
    if fd in (1, 2) or fd < 0:
        raise _invalid("the descriptor must be 0 (standard input) or 3 and higher")
    try:
        os.set_inheritable(fd, False)
    except OSError as error:
        raise _invalid(f"descriptor {fd} is not open ({error.strerror})") from None
    chunks: list[bytes] = []
    size = 0
    try:
        while True:
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise _invalid(f"more than {MAX_BYTES} bytes")
            chunks.append(chunk)
    except OSError as error:
        raise _invalid(f"descriptor {fd} cannot be read ({error.strerror})") from None
    finally:
        _release(fd)
    return parse(b"".join(chunks))


def _release(fd: int) -> None:
    if fd == 0:
        # Standard input stays a valid descriptor (the null device) for everything that expects one.
        null = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(null, 0)
        finally:
            os.close(null)
        return
    with contextlib.suppress(OSError):
        os.close(fd)


def receive(fd: int | None) -> Handoff | None:
    """Read the handoff on ``fd`` once (``None``: no handoff) and keep it in memory; also removes
    ``NODESTRO_LAUNCH_SECRET`` and ``NODESTRO_VAULT_KEY`` from ``os.environ``, which a handoff replaces."""
    global _received
    if fd is None:
        return None
    if _received is not None:
        raise _invalid("the secrets were already received")
    from .launch import ENV_VAR

    _received = read(fd)
    os.environ.pop(ENV_VAR, None)
    os.environ.pop(VAULT_KEY_ENV, None)
    return _received


def received() -> Handoff | None:
    """The handoff this process received; ``None`` without one."""
    return _received


def vault_key() -> str | None:
    """The MCP token vault key: the handoff's, otherwise ``NODESTRO_VAULT_KEY`` (the operator's), which is taken out of
    ``os.environ`` on the first call so that no process the backend starts inherits it."""
    global _vault_from_env
    if _vault_from_env is _UNSET:
        _vault_from_env = os.environ.pop(VAULT_KEY_ENV, "").strip() or None
    if _received is not None:
        os.environ.pop(VAULT_KEY_ENV, None)
        return _received.vault_key
    return _vault_from_env  # type: ignore[return-value]


def reset() -> None:
    """Forget the handoff and the cached variable (tests)."""
    global _received, _vault_from_env
    _received = None
    _vault_from_env = _UNSET
