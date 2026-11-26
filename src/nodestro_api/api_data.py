"""The API for finding and keeping data: global search, favorites and tags, diagnosis, backups."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import tempfile
from typing import Annotated, Any

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from nodestro_runtime.problems import NodestroError
from pydantic import BaseModel, Field, StrictBool
from starlette.background import BackgroundTask

from . import access_audit
from .api import Ctx, owner_of, request_language
from .backup import (
    BackupError,
    BackupInfo,
    LocalBackup,
    RestoreResult,
    backup_file_name,
    create_backup,
    inspect_backup,
    local_backup_path,
    local_backups,
    profile_of,
    restore_backup,
)
from .data_lifecycle import DeleteResult, DeleteStarted, delete_all, export_all, export_file_name
from .diagnostics import DiagnosticCheck, Diagnostics, check_connection, run_diagnostics
from .organize import Kind, Organize, OrganizeState
from .permissions import EDIT, INSTALL, READ
from .recovery import restart_into_recovery
from .search import SearchResult, search

router = APIRouter(prefix="/api")


# ---- search ---------------------------------------------------------------------


@router.get("/search", dependencies=READ)
def global_search(
    request: Request, ctx: Ctx, q: str = Query("", max_length=200), limit: int = Query(40, ge=1, le=100)
) -> list[SearchResult]:
    return search(ctx, q, owner=owner_of(request), language=request_language(request), limit=limit)


# ---- favorites and tags ---------------------------------------------------------


@router.get("/organize", dependencies=READ)
def organize_state(ctx: Ctx) -> OrganizeState:
    return ctx.organize.state()


@router.put("/organize/{kind}/{item_id}", dependencies=EDIT)
def organize_item(kind: Kind, item_id: str, body: Organize, ctx: Ctx) -> Organize:
    """Favorite and tags of an existing workflow, agent template or tool; an unknown one is ``404`` (API2-07)."""
    if kind == "tool":
        ctx.tools.get(item_id)  # raises KeyError for an unknown tool
    elif ctx.store.get_raw("workflows" if kind == "workflow" else "templates", item_id) is None:
        raise KeyError(item_id)
    return ctx.organize.set(kind, item_id, body)


# ---- diagnosis ------------------------------------------------------------------


@router.get("/diagnostics", dependencies=INSTALL)
async def diagnostics(ctx: Ctx) -> Diagnostics:
    """Local checks only: free, no network (the sandbox check runs a tiny test tool)."""
    return await asyncio.to_thread(run_diagnostics, ctx)


@router.post("/diagnostics/connection", dependencies=INSTALL)
async def diagnostics_connection(ctx: Ctx, provider: str | None = None) -> DiagnosticCheck:
    """Reach a provider's API (default: the default model's) by listing its models; no tokens, no cost.

    Amazon Bedrock: runs the settings verification instead (free AWS calls) and, as a side effect, stores its result
    in the provider's ``verification`` (rate limit shared with ``POST /api/settings/providers/bedrock/verify``)."""
    return await asyncio.to_thread(check_connection, ctx, provider)


class RecoveryRequest(BaseModel):
    #: Must be true: the user confirmed that Nodestro restarts into the repair of the database's permissions.
    confirm: StrictBool


@router.post("/diagnostics/database-permissions/recovery", dependencies=INSTALL)
def diagnostics_recovery(body: RecoveryRequest, ctx: Ctx) -> JSONResponse:
    """Restart into recovery mode, where the user confirms the repair of the database's permissions (TABRION
    MIG-NOD-9, :mod:`nodestro_api.recovery`); nothing is repaired here. Refused while runs are active."""
    return restart_into_recovery(ctx, body.confirm)


# ---- backups --------------------------------------------------------------------


class RestoreRequest(BaseModel):
    id: str
    #: Must be true: the user confirmed that the current data is replaced.
    confirm: bool


def _remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


@router.get("/backup", dependencies=INSTALL)
async def download_backup(ctx: Ctx) -> FileResponse:
    """A complete backup as a ZIP file (without the API key)."""
    directory = Path(tempfile.mkdtemp(prefix="nodestro-export-"))
    name = backup_file_name()
    await asyncio.to_thread(create_backup, ctx, directory / name)
    return FileResponse(
        directory / name, media_type="application/zip", filename=name, background=BackgroundTask(_remove, directory)
    )


@router.post("/backup/inspect", dependencies=INSTALL)
async def inspect(ctx: Ctx, file: Annotated[UploadFile, File()]) -> BackupInfo:
    """Check an uploaded backup completely; nothing is changed yet."""
    directory = Path(tempfile.mkdtemp(prefix="nodestro-upload-"))
    try:
        path = directory / "backup.zip"
        with path.open("wb") as handle:
            await asyncio.to_thread(shutil.copyfileobj, file.file, handle)
        return await asyncio.to_thread(inspect_backup, ctx, path, file.filename or "backup.zip")
    finally:
        _remove(directory)


def _audit(ctx: Ctx, request: Request, kind: access_audit.AccessKind, **details: Any) -> None:
    """N-15: who restored, exported or deleted the installation's data, and when (counts and names only)."""
    principal = getattr(request.state, "principal", None)
    access_audit.record(
        ctx.store,
        access_audit.AccessEvent(
            kind=kind,
            actor=principal.id if principal is not None else "local",
            target_type="data",
            target_id=profile_of(ctx).product,
            details=details,
        ),
    )


@router.post("/backup/restore", dependencies=INSTALL)
async def restore(body: RestoreRequest, request: Request, ctx: Ctx) -> RestoreResult:
    """Replace all data with a checked backup; the current state is saved as a safety backup first.

    ``secrets_to_reenter`` names the secrets the backup left out that this computer does not have."""
    if not body.confirm:
        raise BackupError("backup.not_confirmed", "Restoring replaces all data and must be confirmed")
    result = await asyncio.to_thread(restore_backup, ctx, body.id)
    # Network access for published APIs is off after a restore (see nodestro_api.backup).
    await ctx.network.stop()
    _audit(ctx, request, "backup_restored", safety_backup=result.safety_backup, workflows=result.contents.workflows)
    return result


@router.get("/backup/local", dependencies=INSTALL)
def list_local_backups(ctx: Ctx) -> list[LocalBackup]:
    return local_backups(ctx)


@router.get("/backup/local/{name}", dependencies=INSTALL)
def download_local_backup(name: str, ctx: Ctx) -> FileResponse:
    return FileResponse(local_backup_path(ctx, name), media_type="application/zip", filename=name)


@router.post("/backup/local/{name}/inspect", dependencies=INSTALL)
async def inspect_local(name: str, ctx: Ctx) -> BackupInfo:
    """Check a backup kept in the data directory, e.g. the safety copy of a restore."""
    path = local_backup_path(ctx, name)
    return await asyncio.to_thread(inspect_backup, ctx, path, name)


# ---- export and delete everything (N-15) -------------------------------------------------


#: The value ``confirm`` must have: deleting everything is never one mistaken ``true`` away.
DELETE_ALL_CONFIRMATION = "delete all data"


class DeleteAllRequest(BaseModel):
    #: Must be exactly ``"delete all data"``: the user confirmed that every piece of data is deleted.
    confirm: str = Field(max_length=100)


@router.get("/data/export", dependencies=INSTALL)
async def export_everything(request: Request, ctx: Ctx) -> FileResponse:
    """Everything the workspace holds as readable files in a ZIP file (``nodestro-export``), without any secret."""
    directory = Path(tempfile.mkdtemp(prefix="nodestro-export-"))
    name = export_file_name(profile_of(ctx).product)
    try:
        info = await asyncio.to_thread(export_all, ctx, directory / name)
    except BaseException:
        _remove(directory)  # with a partial file in it
        raise
    _audit(ctx, request, "data_exported", file_name=name, workflows=info.contents.workflows)
    return FileResponse(
        directory / name, media_type="application/zip", filename=name, background=BackgroundTask(_remove, directory)
    )


@router.post("/data/delete-all", dependencies=INSTALL)
async def delete_everything(body: DeleteAllRequest, request: Request, ctx: Ctx) -> DeleteResult:
    """Delete every piece of data in the data directory: the database is emptied and rebuilt, every other file and
    folder is removed (see :mod:`nodestro_api.data_lifecycle`). Needs ``confirm`` set to ``"delete all data"``;
    refused while runs or other background work are active. Cannot be undone: no safety backup is kept."""
    if body.confirm != DELETE_ALL_CONFIRMATION:
        raise NodestroError(
            "data.not_confirmed",
            "Deleting all data cannot be undone and must be confirmed",
            expected=DELETE_ALL_CONFIRMATION,
        )
    try:
        result = await asyncio.to_thread(delete_all, ctx)
    except DeleteStarted as exc:
        # Deleting began and failed: what happened is recorded all the same, then the cause is answered.
        await ctx.network.stop()
        cause = exc.__cause__
        _audit(ctx, request, "data_deleted", complete=False, error=getattr(cause, "code", type(cause).__name__))
        raise cause if cause is not None else exc from None
    # Nothing is published any more: network access for published APIs is switched off.
    await ctx.network.stop()
    _audit(
        ctx,
        request,
        "data_deleted",
        removed_entries=result.removed_entries,
        workflows=result.contents.workflows,
        complete=result.complete,
        left=result.left[:50],
    )
    return result
