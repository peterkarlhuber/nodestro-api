"""Recovery mode of the desktop: the permissions of the database are repaired only with the user's confirmation.

TABRION MIG-NOD-9 (decision E-NOD-PERM, Peter 2026-11-14 10:37Z and 10:41Z, option B). An existing ``nodestro.db``
that group or others may write is refused when the workspace opens (``storage.unsafe_file``). When the runtime
reports it as ``fixable`` (every existing file is a regular file of the running user with one name), the desktop API
does not stop: :func:`nodestro_api.app.create_app` starts it in *recovery mode* instead. Nothing else runs then (no
workspace, no store, no workflows, no scheduler); the app answers only

- ``GET /api/recovery/database-permissions``: the dry run. Which files would change, from which mode to which
  (``repair_desktop_database_modes(confirmed=False)``), the files that are left alone and why (symbolic link, hard
  link, another owner), the manual alternative (``chmod 600 …``, quoted for a shell) and ``plan``, a fingerprint of
  the files the user is shown (device, inode, owner, mode and link count of each);
- ``POST /api/recovery/database-permissions/repair`` with ``{"confirm": true, "plan": "…"}``: the repair. Without
  ``confirm`` nothing is changed (``recovery.not_confirmed``); when a file is not the one the dry run showed (it was
  replaced, moved, linked or its mode changed) nothing is changed either (``recovery.plan_changed``). Then the
  runtime narrows the modes (only for regular files of this user with one name, never through a link, the content
  never changes), the result is checked again (the dry run is clean and the database opens read-only), and the
  process ends with :data:`RESTART_EXIT_CODE`: the desktop shell starts the regular backend again.

plus the health check (``/api/health``, which the desktop shell waits for; it answers ``"mode": "recovery"``) and
the built interface, which shows the repair dialog. Every request passes the same guards as the regular API (host and
strict origin check, the launch secret's session cookie, security headers, body limits, contract header) and needs
the local owner's ``install`` permission. The target is always the data directory's own ``nodestro.db`` and its
``-wal``, ``-shm`` and ``-journal`` files; a path never comes from a request. The data directory stays locked
(``DataDirectoryLock``) as long as the recovery app runs, so no other process opens the database during the repair,
and no store of this process has it open.

The same repair is reachable from the diagnosis, where the workspace is open and an in-process repair would drop the
store's SQLite locks: ``POST /api/diagnostics/database-permissions/recovery`` (``{"confirm": true}``, refused while
runs or other background work are active) shuts the regular API down cleanly and ends the process with
:data:`RECOVERY_EXIT_CODE`. The desktop shell then starts the backend again with ``NODESTRO_RECOVERY`` set to
:data:`RECOVERY_VALUE` (:attr:`AppConfig.recovery`), which starts this recovery mode without opening the workspace.
Nothing is ever repaired at start, and nothing without the confirmation in the dialog.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import hashlib
import logging
import os
from pathlib import Path
import shlex
import stat
import threading
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Body, FastAPI
from nodestro_runtime import storage
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.workspace import DATABASE, DataDirectoryLock
from pydantic import BaseModel, Field, StrictBool
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from . import handoff
from .context import AppConfig
from .permissions import INSTALL, PUBLIC
from .serving import ApiError

if TYPE_CHECKING:
    from .context import Nodestro

logger = logging.getLogger(__name__)

#: The exit code that asks the desktop shell to start the regular backend again (after a repair).
RESTART_EXIT_CODE = 75
#: The exit code that asks the desktop shell to start the backend again in recovery mode (from the diagnosis).
RECOVERY_EXIT_CODE = 76
#: Set by the desktop shell for one start after :data:`RECOVERY_EXIT_CODE` (read into :attr:`AppConfig.recovery`).
RECOVERY_ENV = "NODESTRO_RECOVERY"
RECOVERY_VALUE = "database-permissions"

PREFIX = "/api/recovery"
PLAN_PATH = f"{PREFIX}/database-permissions"
REPAIR_PATH = f"{PLAN_PATH}/repair"

Status = Literal["changed", "unchanged", "not_confirmed", "skipped", "failed"]


class ExitRequest:
    """The exit code the process ends with once the server stopped (``None``: an ordinary stop).

    :func:`nodestro_api.cli.serve` binds ``stop`` to the uvicorn server (``should_exit``) and ends the process with
    :attr:`code` after the application's shutdown ran."""

    def __init__(self) -> None:
        self.code: int | None = None
        self._stop: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def bind(self, stop: Callable[[], None] | None) -> None:
        with self._lock:
            self._stop = stop
            self.code = None

    def request(self, code: int) -> None:
        with self._lock:
            self.code = code
            stop = self._stop
        if stop is None:
            logger.warning("Nodestro has to be started again (exit code %s), but it was not started by serve()", code)
            return
        stop()


#: The one exit request of this process.
EXIT = ExitRequest()


def requested(config: AppConfig) -> bool:
    """The desktop shell asked for recovery mode (after the diagnosis' button); desktop profile only."""
    return config.recovery and config.profile_kind == "desktop" and os.name == "posix"


def recoverable(config: AppConfig, error: BaseException) -> bool:
    """A refusal that recovery mode can repair: ``storage.unsafe_file`` with ``fixable`` on a desktop."""
    return (
        isinstance(error, NodestroError)
        and error.code == "storage.unsafe_file"
        and error.params.get("fixable") is True
        and config.profile_kind == "desktop"
        and os.name == "posix"
    )


def database_path(config: AppConfig) -> Path:
    """The only database recovery mode ever repairs: the data directory's own ``nodestro.db``."""
    return config.data_dir / DATABASE


# ---- the dry run and its fingerprint -------------------------------------------------------------------------------


class FileMode(BaseModel):
    """One existing file of the database as the runtime reports it (modes in octal, e.g. ``"0664"``)."""

    name: str
    path: str
    status: Status
    old_mode: str | None = None
    new_mode: str | None = None
    #: Why a file is ``skipped`` (``symlink``, ``hard_link``, ``other_owner``, ``not_a_file``,
    #: ``changed_during_repair``) or ``failed``.
    reason: str | None = None


class RepairPlan(BaseModel):
    """What a confirmed repair would do; nothing was changed."""

    #: ``refused``: the database was refused at start; ``requested``: the user asked for it in the diagnosis.
    origin: Literal["refused", "requested"]
    files: list[FileMode]
    #: ``True`` when at least one file would change and none is left alone.
    repairable: bool
    #: The fingerprint of the files shown; the repair is refused when it no longer matches.
    plan: str
    #: The manual alternative (``chmod 600 <files>``, quoted for a shell); ``None`` when nothing would change.
    command: str | None = None


class RepairRequest(BaseModel):
    #: Must be true: the user confirmed the repair in the dialog.
    confirm: StrictBool = False
    #: ``plan`` of the dry run the user saw.
    plan: str = Field(default="", max_length=128)


class RepairResult(BaseModel):
    repaired: bool
    files: list[FileMode]
    #: The process ends now and the desktop shell starts the regular backend.
    restart: bool


def _octal(mode: int | None) -> str | None:
    return None if mode is None else f"{mode:04o}"


def file_modes(results: list[storage.ModeRepair]) -> list[FileMode]:
    return [
        FileMode(
            name=item.path.name,
            path=str(item.path),
            status=item.status,
            old_mode=_octal(item.old_mode),
            new_mode=_octal(item.new_mode),
            reason=item.reason,
        )
        for item in results
    ]


def candidates(database: Path) -> list[Path]:
    return [database, *(database.with_name(database.name + suffix) for suffix in storage.SIDE_FILES)]


def fingerprint(database: Path, *, modes: bool = True) -> str:
    """The identity of every file of the database as it is now (never following a link): device, inode, owner, type
    and mode (without ``modes``: the type only), link count; a missing file counts too. Equal fingerprints: the same
    files (with the same modes)."""
    digest = hashlib.sha256()
    for path in candidates(database):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            digest.update(f"{path.name}:missing\n".encode())
            continue
        except OSError as exc:
            digest.update(f"{path.name}:error:{exc.errno}\n".encode())
            continue
        mode = info.st_mode if modes else stat.S_IFMT(info.st_mode)
        digest.update(f"{path.name}:{info.st_dev}:{info.st_ino}:{info.st_uid}:{mode:o}:{info.st_nlink}\n".encode())
    return digest.hexdigest()


def manual_command(results: list[storage.ModeRepair]) -> str | None:
    """``chmod 600 <files>`` for the files a repair would change, quoted for a shell as the runtime's message is."""
    files = [str(item.path) for item in results if item.status == "not_confirmed"]
    return f"chmod 600 {shlex.join(files)}" if files else None


def is_repairable(results: list[storage.ModeRepair]) -> bool:
    return any(item.status == "not_confirmed" for item in results) and all(
        item.status in ("not_confirmed", "unchanged") for item in results
    )


def is_private(results: list[storage.ModeRepair]) -> bool:
    """Nothing is left for group or others, and no file is left alone (the dry run is clean)."""
    return all(item.status == "unchanged" for item in results)


def dry_run(database: Path, origin: Literal["refused", "requested"]) -> RepairPlan:
    plan = fingerprint(database)
    results = storage.repair_desktop_database_modes(database, confirmed=False)
    if fingerprint(database) != plan:  # changed while it was looked at: show it again
        plan = fingerprint(database)
        results = storage.repair_desktop_database_modes(database, confirmed=False)
    return RepairPlan(
        origin=origin,
        files=file_modes(results),
        repairable=is_repairable(results),
        plan=plan,
        command=manual_command(results),
    )


def opens(database: Path) -> str | None:
    """``None`` when the database opens as the regular start opens it (read-only here), else the reason."""
    try:
        storage.open_sqlite(database, read_only=True).close()
    except NodestroError as exc:
        return exc.code
    except Exception as exc:
        return type(exc).__name__
    return None


class _Repairer:
    """Serializes repairs: one at a time, and none after a successful one (the process is about to end)."""

    def __init__(self, database: Path, origin: Literal["refused", "requested"]) -> None:
        self.database = database
        self.origin = origin
        self.lock = threading.Lock()
        self.done = False

    def plan(self) -> RepairPlan:
        with self.lock:
            return dry_run(self.database, self.origin)

    def repair(self, body: RepairRequest) -> RepairResult:
        if not body.confirm:
            raise ApiError(400, "recovery.not_confirmed", "Repairing the permissions of the database must be confirmed")
        with self.lock:
            if self.done:
                raise ApiError(409, "recovery.restarting", "The permissions were repaired; Nodestro is restarting")
            # Taken before the comparison: a file swapped after it is caught after the repair, one swapped before by
            # the comparison itself.
            files = fingerprint(self.database, modes=False)
            if not body.plan or fingerprint(self.database) != body.plan:
                raise ApiError(
                    409,
                    "recovery.plan_changed",
                    "The database files changed since they were shown; nothing was changed. Look at them again",
                )
            planned = storage.repair_desktop_database_modes(self.database, confirmed=False)
            if not is_repairable(planned):
                raise ApiError(
                    409,
                    "recovery.not_repairable",
                    "These database files cannot be repaired here; nothing was changed",
                    files=[item.model_dump() for item in file_modes(planned)],
                )
            results = storage.repair_desktop_database_modes(self.database, confirmed=True)
            after = storage.repair_desktop_database_modes(self.database, confirmed=False)
            # The same files as before the repair (nothing was swapped in meanwhile), private now, and it opens.
            problem = None if fingerprint(self.database, modes=False) == files else "files_changed"
            problem = problem or (None if is_private(after) else "not_private")
            problem = problem or opens(self.database)
            if problem is not None or any(item.status not in ("changed", "unchanged") for item in results):
                logger.warning("The permissions of %s could not be repaired completely (%s)", self.database, problem)
                raise ApiError(
                    500,
                    "recovery.repair_failed",
                    "The permissions of the database could not be repaired completely",
                    files=[item.model_dump() for item in file_modes(results)],
                    command=manual_command(after) or "",
                )
            self.done = True
            return RepairResult(repaired=True, files=file_modes(results), restart=True)


# ---- the recovery app ----------------------------------------------------------------------------------------------


def build_router(database: Path, origin: Literal["refused", "requested"]) -> APIRouter:
    repairer = _Repairer(database, origin)
    router = APIRouter()

    @router.get("/api/health", dependencies=PUBLIC)
    async def health() -> dict[str, str]:
        """The desktop shell waits for it; the interface reads ``mode`` and shows the repair dialog."""
        return {"status": "ok", "mode": "recovery"}

    @router.get(PLAN_PATH, dependencies=INSTALL)
    async def database_permissions() -> RepairPlan:
        """The dry run: what a confirmed repair would change. Nothing is changed."""
        return await asyncio.to_thread(repairer.plan)

    @router.post(REPAIR_PATH, dependencies=INSTALL)
    async def repair_database_permissions(body: Annotated[RepairRequest, Body()]) -> JSONResponse:
        """Narrow the modes of the files the dry run showed (``confirm`` and its ``plan`` required); then the process
        ends with :data:`RESTART_EXIT_CODE` and the desktop shell starts the regular backend."""
        result = await asyncio.to_thread(repairer.repair, body)
        return JSONResponse(result.model_dump(), background=BackgroundTask(EXIT.request, RESTART_EXIT_CODE))

    return router


def recovery_lifespan(config: AppConfig) -> Callable[[FastAPI], Any]:
    """Holds the data directory lock while the recovery app runs: no other Nodestro process opens the database."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        lock = DataDirectoryLock(config.data_dir)
        try:
            yield
        finally:
            lock.release()

    return lifespan


def log_start(error: BaseException | None) -> None:
    logger.warning(
        "Nodestro starts in recovery mode: only the repair of the database's permissions is offered (%s). Nothing is "
        "changed without the user's confirmation",
        "requested from the diagnosis" if error is None else error,
    )


# ---- from the diagnosis (the workspace is open: never repaired in this process) ------------------------------------


def shell_restarts() -> bool:
    """The desktop shell started this process (its handoff): it starts the backend again on :data:`RECOVERY_EXIT_CODE`
    and :data:`RESTART_EXIT_CODE`. A backend started from a terminal ends on them and is started again by hand."""
    return handoff.received() is not None


def restart_into_recovery(nodestro: Nodestro, confirm: bool) -> JSONResponse:
    """Stop the regular API cleanly and end the process with :data:`RECOVERY_EXIT_CODE`, so that the desktop shell
    starts the backend in recovery mode, where the user confirms the repair itself. Refused without ``confirm``,
    outside the desktop app, and while runs or other background work are active."""
    if not confirm:
        raise ApiError(400, "recovery.not_confirmed", "Restarting into the repair of the database must be confirmed")
    if nodestro.config.profile_kind != "desktop" or os.name != "posix" or not shell_restarts():
        raise ApiError(
            409,
            "recovery.unavailable",
            "Only the desktop app can restart into the repair; restrict the permissions by hand instead",
        )
    busy = nodestro.background_work()
    if busy:
        raise ApiError(
            409,
            "recovery.busy",
            "Runs or other work are active; restart into the repair when they have finished",
            count=len(busy),
        )
    logger.warning("Nodestro stops to start again in recovery mode (repair of the database's permissions)")
    return JSONResponse({"restart": True}, background=BackgroundTask(EXIT.request, RECOVERY_EXIT_CODE))
