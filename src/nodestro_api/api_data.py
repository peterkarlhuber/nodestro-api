"""The API for finding and keeping data: global search, favorites and tags, diagnosis, backups."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import tempfile
from typing import Annotated

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .api import Ctx, request_language
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
    restore_backup,
)
from .diagnostics import DiagnosticCheck, Diagnostics, check_connection, run_diagnostics
from .organize import Kind, Organize, OrganizeState
from .permissions import EDIT, INSTALL, READ
from .search import SearchResult, search

router = APIRouter(prefix="/api")


# ---- search ---------------------------------------------------------------------


@router.get("/search", dependencies=READ)
def global_search(
    request: Request, ctx: Ctx, q: str = Query("", max_length=200), limit: int = Query(40, ge=1, le=100)
) -> list[SearchResult]:
    return search(ctx, q, language=request_language(request), limit=limit)


# ---- favorites and tags ---------------------------------------------------------


@router.get("/organize", dependencies=READ)
def organize_state(ctx: Ctx) -> OrganizeState:
    return ctx.organize.state()


@router.put("/organize/{kind}/{item_id}", dependencies=EDIT)
def organize_item(kind: Kind, item_id: str, body: Organize, ctx: Ctx) -> Organize:
    return ctx.organize.set(kind, item_id, body)


# ---- diagnosis ------------------------------------------------------------------


@router.get("/diagnostics", dependencies=INSTALL)
async def diagnostics(ctx: Ctx) -> Diagnostics:
    """Local checks only: free, no network (the sandbox check runs a tiny test tool)."""
    return await asyncio.to_thread(run_diagnostics, ctx)


@router.post("/diagnostics/connection", dependencies=INSTALL)
async def diagnostics_connection(ctx: Ctx, provider: str | None = None) -> DiagnosticCheck:
    """Reach a provider's API (default: the default model's) by listing its models; no tokens, no cost."""
    return await asyncio.to_thread(check_connection, ctx, provider)


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


@router.post("/backup/restore", dependencies=INSTALL)
async def restore(body: RestoreRequest, ctx: Ctx) -> RestoreResult:
    """Replace all data with a checked backup; the current state is saved as a safety backup first."""
    if not body.confirm:
        raise BackupError("backup.not_confirmed", "Restoring replaces all data and must be confirmed")
    result = await asyncio.to_thread(restore_backup, ctx, body.id)
    # Network access for published APIs is off after a restore (see nodestro_api.backup).
    await ctx.network.stop()
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
