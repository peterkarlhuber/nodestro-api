"""The API for files as workflow inputs and for taking content over into knowledge.

Files a user attached to a Nodestro Assistant conversation are input files too (``origin="assistant"``). On a server
they are private to the user who uploaded them: these routes (and the adoption into knowledge) answer 404 for another
user's assistant file, so a file id from someone else's conversation reveals nothing.

A knowledge collection travels as ``nodestro-collection/1`` (N-14, :mod:`nodestro_runtime.exchange`): ``GET
/api/knowledge/collections/export`` (``read``) writes its documents with the retrieval settings and embedding identity;
``POST /api/knowledge/collections/import`` (``edit``) checks such a file completely, previews it (``dry_run``) or
stores its documents into a new collection, indexed again by this workspace.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from nodestro_runtime import exchange
from nodestro_runtime.adoption import AdoptCheck, AdoptConfirm, AdoptOutcome, AdoptRequest, adopt, check
from nodestro_runtime.file_delivery import INLINE_FILE_CHARS, INLINE_TOTAL_CHARS
from nodestro_runtime.file_formats import (
    DEFAULT_FIELD_MB,
    MAX_ARCHIVE_ENTRIES,
    MAX_ARCHIVE_UNPACKED,
    MAX_UPLOAD_MB,
    FileFormatError,
    accepted,
    formats_overview,
    table_markdown,
)
from nodestro_runtime.input_files import CleanupResult, InputFileOut, StorageInfo, is_input_id
from nodestro_runtime.knowledge import CollectionInfo
from nodestro_runtime.problems import Problem
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .api import Ctx, actor_id, owner_of, uploaded_file_headers
from .conversation_owners import chat_file_visible
from .permissions import EDIT, READ
from .serving import ApiError

router = APIRouter(prefix="/api")
_PREVIEW_CHARS = 20_000


class FormatsInfo(BaseModel):
    formats: list[dict[str, Any]]
    limits: dict[str, int]


class UploadResult(BaseModel):
    """One uploaded file: stored (``file``) or refused (``problem``)."""

    name: str
    file: InputFileOut | None = None
    problem: Problem | None = None


class FileText(BaseModel):
    name: str
    format: str | None = None
    text: str
    truncated: bool = False
    tables: list[dict[str, Any]] = Field(default_factory=list)


class RunInput(BaseModel):
    id: str
    #: ``input`` uploaded for the run, ``knowledge`` a knowledge file.
    source: str
    available: bool
    name: str | None = None
    file: InputFileOut | None = None
    #: The form field it was given in (``None``: the run's extra files).
    field: str | None = None


class CleanupRequest(BaseModel):
    #: Also remove the input files of finished runs older than this many days.
    older_than_days: int | None = Field(default=None, ge=0, le=3650)


@router.get("/inputs/formats", dependencies=READ)
def input_formats() -> FormatsInfo:
    return FormatsInfo(
        formats=formats_overview(),
        limits={
            "max_upload_mb": MAX_UPLOAD_MB,
            "default_field_mb": DEFAULT_FIELD_MB,
            "archive_entries": MAX_ARCHIVE_ENTRIES,
            "archive_unpacked_mb": MAX_ARCHIVE_UNPACKED // (1024 * 1024),
            "inline_file_chars": INLINE_FILE_CHARS,
            "inline_total_chars": INLINE_TOTAL_CHARS,
        },
    )


@router.post("/inputs", status_code=201, dependencies=EDIT)
async def upload_inputs(
    ctx: Ctx,
    request: Request,
    files: Annotated[list[UploadFile], File()],
    accept: Annotated[str, Form()] = "",
    max_size_mb: Annotated[int | None, Form()] = None,
    origin: Annotated[str, Form()] = "studio",
) -> list[UploadResult]:
    """Check and store files for a run; refused files are reported, never stored."""
    allowed = accepted([item for item in accept.split(",") if item.strip()]) if accept.strip() else None
    results: list[UploadResult] = []
    for upload in files:
        name = upload.filename or "upload"
        data = await upload.read()
        try:
            if allowed is not None:
                from nodestro_runtime.file_formats import FORMATS, format_of_name

                spec = format_of_name(name)
                if spec is None or spec.key not in allowed:
                    raise FileFormatError(
                        "input.type_not_accepted",
                        f"{name}: this field does not accept this file type",
                        name=name,
                        accepted=", ".join(sorted(FORMATS[key].extensions[0] for key in allowed)),
                    )
            stored = await asyncio.to_thread(
                ctx.inputs.add,
                data,
                name=name,
                media_type=upload.content_type,
                origin=origin if origin in ("studio", "test", "schedule", "batch") else "studio",  # type: ignore[arg-type]
                limit_mb=max_size_mb or DEFAULT_FIELD_MB,
                uploaded_by=actor_id(request),
            )
        except FileFormatError as error:
            results.append(UploadResult(name=name, problem=error.problem))
            continue
        results.append(UploadResult(name=name, file=InputFileOut.of(stored)))
    return results


@router.get("/inputs", dependencies=READ)
def input_storage(ctx: Ctx) -> StorageInfo:
    return ctx.inputs.storage()


class InputCleanupResult(CleanupResult):
    #: Files of assistant conversations removed because they were older than the retention period an
    #: administrator set (``/api/assistant/files/retention``, API-056); 0 without one.
    assistant_files_expired: int = 0


@router.post("/inputs/cleanup", dependencies=EDIT)
def cleanup_inputs(body: CleanupRequest, ctx: Ctx) -> InputCleanupResult:
    """Remove input files nobody uses, with ``older_than_days`` also those of older finished runs, and the
    assistant conversation files past their retention period (when an administrator set one)."""
    expired = ctx.assistant.expire_files()
    result = ctx.inputs.cleanup(older_than_days=body.older_than_days)
    return InputCleanupResult(**result.model_dump(), assistant_files_expired=expired)


def visible(ctx: Ctx, request: Request, file_id: str) -> None:
    """Another user's file of an assistant conversation or of a conversation with an own agent (N-04) does not exist
    for this request (``KeyError``, answered with 404); nor does a file of a conversation without an owner on a
    server, nor any such file for a request without an owner identity (W3-S1,
    :func:`nodestro_api.conversation_owners.chat_file_visible`)."""
    if not is_input_id(file_id):
        return
    item = ctx.inputs.find(file_id)
    if item is None or item.origin not in ("assistant", "conversation"):
        return
    if item.uploaded_by != actor_id(request) or not chat_file_visible(ctx.store, item, owner_of(request)):
        raise KeyError(file_id)


@router.get("/inputs/{file_id}", dependencies=READ)
def input_file(file_id: str, ctx: Ctx, request: Request) -> InputFileOut:
    visible(ctx, request, file_id)
    return InputFileOut.of(ctx.inputs.get(file_id))


@router.get("/inputs/{file_id}/content", dependencies=READ)
def input_content(file_id: str, ctx: Ctx, request: Request) -> Response:
    visible(ctx, request, file_id)
    item = ctx.inputs.get(file_id)
    # A type the file storage keeps as an inert attachment (NODESTRO_FILES_BACKEND=axentra) is served as opaque bytes.
    media_type = ctx.inputs.download_type(file_id)
    return Response(
        content=ctx.inputs.data(file_id),
        media_type=media_type,
        headers=uploaded_file_headers(item.name, media_type, inline=False),
    )


@router.get("/inputs/{file_id}/text", dependencies=READ)
def input_text(file_id: str, ctx: Ctx, request: Request, path: str = "") -> FileText:
    """What Nodestro read from a file (or one file of an archive): the text the agents can get."""
    visible(ctx, request, file_id)
    return read_text(file_id, ctx, path)


def read_text(file_id: str, ctx: Ctx, path: str = "") -> FileText:
    """What Nodestro read from a file (or one file of an archive); also the assistant's file preview."""
    item = ctx.inputs.get(file_id)
    if path:
        entry, extracted, _ = ctx.inputs.member(file_id, path)
        name = f"{item.name}/{entry.path}"
    else:
        extracted, name = ctx.inputs.extracted(file_id), item.name
    tables = [
        {
            "name": table.name,
            "columns": table.columns,
            "rows": table.total_rows,
            "preview": table_markdown(table, limit=20),
        }
        for table in extracted.tables
    ]
    text = extracted.text or extracted.outline
    return FileText(
        name=name,
        format=extracted.format,
        text=text[:_PREVIEW_CHARS],
        truncated=len(text) > _PREVIEW_CHARS,
        tables=tables,
    )


@router.delete("/inputs/{file_id}", status_code=204, dependencies=EDIT)
def delete_input(file_id: str, ctx: Ctx, request: Request) -> Response:
    visible(ctx, request, file_id)
    ctx.inputs.delete(file_id)
    return Response(status_code=204)


@router.get("/runs/{run_id}/inputs", dependencies=READ)
def run_inputs(run_id: str, ctx: Ctx) -> list[RunInput]:
    """The files a run received, and whether they are still stored."""
    run = ctx.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    fields: dict[str, str] = {}
    for key, value in run.values.items():
        if isinstance(value, list):
            for item in value:
                fields.setdefault(str(item), key)
    found = []
    for file_id in run.file_ids:
        if is_input_id(file_id):
            item = ctx.inputs.find(file_id)
            found.append(
                RunInput(
                    id=file_id,
                    source="input",
                    available=item is not None,
                    name=item.name if item else None,
                    file=InputFileOut.of(item) if item else None,
                    field=fields.get(file_id),
                )
            )
        else:
            known = file_id in ctx.knowledge.store
            found.append(
                RunInput(
                    id=file_id,
                    source="knowledge",
                    available=known,
                    name=ctx.knowledge.store.info(file_id).name if known else None,
                    field=fields.get(file_id),
                )
            )
    return found


# ---- knowledge: collections and adoption -----------------------------------------------


@router.get("/knowledge/collections", dependencies=READ)
def knowledge_collections(ctx: Ctx) -> list[CollectionInfo]:
    return ctx.knowledge.collections()


@router.get(
    "/knowledge/collections/export",
    dependencies=READ,
    response_class=FileResponse,
    responses={200: {"content": {"application/zip": {}}, "description": "The ``nodestro-collection/1`` file"}},
)
async def export_collection(ctx: Ctx, name: Annotated[str, Query(min_length=1, max_length=120)]) -> FileResponse:
    """The collection ``name`` as ``nodestro-collection/1`` (a ZIP file): its documents' stored bytes, the retrieval
    settings and the embedding identity of the index (never vectors or provenance). 404
    ``exchange.collection_not_found`` for a name without files."""
    handle, path = tempfile.mkstemp(prefix="nodestro-collection-", suffix=".zip")
    try:
        with os.fdopen(handle, "wb") as target:
            await asyncio.to_thread(exchange.export_collection, ctx.knowledge, name, target)
    except BaseException:
        os.unlink(path)
        raise
    file_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-") or "collection"
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{file_name}.nodestro-collection.zip",
        background=BackgroundTask(os.unlink, path),
    )


@router.post("/knowledge/collections/import", dependencies=EDIT)
async def import_collection(
    ctx: Ctx,
    request: Request,
    file: Annotated[UploadFile, File()],
    dry_run: Annotated[bool, Form()] = False,
    collection: Annotated[str | None, Form(max_length=120)] = None,
) -> exchange.ImportReport:
    """Preview (``dry_run``) or apply a ``nodestro-collection/1`` file: size, entries, paths, ZIP bombs, format and
    version (``exchange.version_newer``), the manifest's content and every document's checksum are checked first.
    Applying stores the documents into a new collection (``collection``, else the file's name; one with files already
    is refused, ``exchange.collection_exists``) with the rules of an upload, all of them or none; a document whose
    content this workspace has already is left as it is (``exists``). The workspace's settings are never changed."""
    return await asyncio.to_thread(
        exchange.import_collection,
        ctx.knowledge,
        file.file,
        dry_run=dry_run,
        collection=collection,
        actor=actor_id(request),
    )


def _adoptable(ctx: Ctx, request: Request, body: AdoptRequest) -> None:
    """Every item's sources exist for this request: its input file (:func:`visible`) and its run. The run of another
    user's conversation with an own agent (N-04 H-1) is not found like a missing run (W3-S1): its step outputs,
    proposals and result files are the conversation's answers."""
    owner = owner_of(request)
    for item in body.items:
        visible(ctx, request, item.input_id or "")
        if item.run_id and not ctx.conversations.run_visible(item.run_id, owner):
            raise ApiError(404, "run.not_found", f"Run {item.run_id!r} does not exist", id=item.run_id, kind="run")


@router.post("/knowledge/adopt/check", dependencies=READ)
def adopt_check(body: AdoptRequest, ctx: Ctx, request: Request) -> list[AdoptCheck]:
    """What the chosen items would become in knowledge; stores nothing."""
    _adoptable(ctx, request, body)
    return check(ctx, body)


@router.post("/knowledge/adopt", dependencies=EDIT)
def adopt_confirm(body: AdoptConfirm, ctx: Ctx, request: Request) -> AdoptOutcome:
    """Store the confirmed items in knowledge (only with the tokens of the check)."""
    _adoptable(ctx, request, body)
    return adopt(ctx, body)
