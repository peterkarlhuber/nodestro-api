"""The API for files as workflow inputs and for taking content over into knowledge."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import Response
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

from .api import Ctx, uploaded_file_headers
from .permissions import EDIT, READ

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
            )
        except FileFormatError as error:
            results.append(UploadResult(name=name, problem=error.problem))
            continue
        results.append(UploadResult(name=name, file=InputFileOut.of(stored)))
    return results


@router.get("/inputs", dependencies=READ)
def input_storage(ctx: Ctx) -> StorageInfo:
    return ctx.inputs.storage()


@router.post("/inputs/cleanup", dependencies=EDIT)
def cleanup_inputs(body: CleanupRequest, ctx: Ctx) -> CleanupResult:
    return ctx.inputs.cleanup(older_than_days=body.older_than_days)


@router.get("/inputs/{file_id}", dependencies=READ)
def input_file(file_id: str, ctx: Ctx) -> InputFileOut:
    return InputFileOut.of(ctx.inputs.get(file_id))


@router.get("/inputs/{file_id}/content", dependencies=READ)
def input_content(file_id: str, ctx: Ctx) -> Response:
    item = ctx.inputs.get(file_id)
    return Response(
        content=ctx.inputs.data(file_id),
        media_type=item.media_type,
        headers=uploaded_file_headers(item.name, item.media_type, inline=False),
    )


@router.get("/inputs/{file_id}/text", dependencies=READ)
def input_text(file_id: str, ctx: Ctx, path: str = "") -> FileText:
    """What Nodestro read from a file (or one file of an archive): the text the agents can get."""
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
def delete_input(file_id: str, ctx: Ctx) -> Response:
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


@router.post("/knowledge/adopt/check", dependencies=READ)
def adopt_check(body: AdoptRequest, ctx: Ctx) -> list[AdoptCheck]:
    """What the chosen items would become in knowledge; stores nothing."""
    return check(ctx, body)


@router.post("/knowledge/adopt", dependencies=EDIT)
def adopt_confirm(body: AdoptConfirm, ctx: Ctx) -> AdoptOutcome:
    """Store the confirmed items in knowledge (only with the tokens of the check)."""
    return adopt(ctx, body)
