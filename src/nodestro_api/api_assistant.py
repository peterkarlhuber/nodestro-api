"""The Nodestro Assistant's API: conversations, attachments, files, messages and live answers.

Nothing here changes a workflow, an agent, a tool, memory or knowledge, and nothing runs:
attaching only reads an object, and a handover is a proposal the interface shows. Uploaded files
belong to their conversation (:mod:`nodestro_api.assistant_files`); they become knowledge only
through the confirmed adoption (``/api/knowledge/adopt``).

On a server, a conversation belongs to the user who started it: every route with a conversation
id answers 404 for another user's conversation, and the list shows only the user's own ones
(and conversations from before owners were recorded). Tenants are separated as everywhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, TypeVar

from fastapi import APIRouter, File, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from nodestro_runtime.file_formats import FileFormatError
from nodestro_runtime.problems import NodestroError, Problem
from nodestro_runtime.schemas import Language, WorkflowIn
from pydantic import BaseModel, Field

from . import model_readiness
from .api import Ctx, _sse, actor_id, uploaded_file_headers
from .api_inputs import FileText, read_text
from .assistant import (
    ChatMessage,
    ConversationDetail,
    ConversationSummary,
    FileRetention,
    RetryRequest,
    SendRequest,
    SentMessage,
)
from .assistant_context import AttachRequest, Snapshot
from .assistant_files import FileSupport, FileView
from .docs_index import DocsIndex
from .permissions import ADMIN, EDIT, READ, RUN, StreamAccess

_Body = TypeVar("_Body", SendRequest, RetryRequest)

router = APIRouter(prefix="/api/assistant")


class NewConversation(BaseModel):
    title: str = Field(default="", max_length=120)


class Rename(BaseModel):
    title: str = Field(max_length=120)


class Refresh(BaseModel):
    #: The editor's current, unsaved workflow (refreshes the attachment as a draft).
    workflow: WorkflowIn | None = None


class DocHitOut(BaseModel):
    ref: str
    title: str
    chapter_title: str
    link: str
    snippet: str


class FileUpload(BaseModel):
    """One uploaded file: attached (``file``) or refused (``problem``, nothing is stored then)."""

    name: str
    file: FileView | None = None
    problem: Problem | None = None


def owner_of(request: Request) -> str | None:
    """The server user a conversation belongs to; ``None`` for the desktop's (or Local Docker's) only person."""
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.role is None:
        return None
    return str(principal.id)


def _own(ctx: Ctx, request: Request, conversation_id: str) -> None:
    """Another user's conversation is answered like a missing one (404), so ids reveal nothing."""
    ctx.assistant.check_owner(conversation_id, owner_of(request))


@router.get("/conversations", dependencies=READ)
def list_conversations(ctx: Ctx, request: Request) -> list[ConversationSummary]:
    return ctx.assistant.conversations(owner_of(request))


@router.post("/conversations", status_code=201, dependencies=EDIT)
def create_conversation(body: NewConversation, ctx: Ctx, request: Request) -> ConversationDetail:
    return ctx.assistant.create(body.title, owner=owner_of(request))


@router.get("/conversations/{conversation_id}", dependencies=READ)
def get_conversation(conversation_id: str, ctx: Ctx, request: Request) -> ConversationDetail:
    _own(ctx, request, conversation_id)
    return ctx.assistant.detail(conversation_id)


@router.patch("/conversations/{conversation_id}", dependencies=EDIT)
def rename_conversation(conversation_id: str, body: Rename, ctx: Ctx, request: Request) -> ConversationDetail:
    _own(ctx, request, conversation_id)
    return ctx.assistant.rename(conversation_id, body.title)


@router.delete("/conversations/{conversation_id}", status_code=204, dependencies=EDIT)
def delete_conversation(conversation_id: str, ctx: Ctx, request: Request) -> Response:
    """Delete the conversation with its messages, snapshots and files."""
    _own(ctx, request, conversation_id)
    ctx.assistant.delete(conversation_id)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/attachments", dependencies=EDIT)
def attach(conversation_id: str, body: AttachRequest, ctx: Ctx, request: Request) -> ConversationDetail:
    """The user's explicit choice: take a snapshot of the object for the following messages."""
    _own(ctx, request, conversation_id)
    return ctx.assistant.attach(conversation_id, body)


@router.post("/conversations/{conversation_id}/attachments/{attachment_id}/refresh", dependencies=EDIT)
def refresh_attachment(
    conversation_id: str, attachment_id: str, body: Refresh, ctx: Ctx, request: Request
) -> ConversationDetail:
    _own(ctx, request, conversation_id)
    return ctx.assistant.refresh(conversation_id, attachment_id, body.workflow)


@router.delete("/conversations/{conversation_id}/attachments/{attachment_id}", dependencies=EDIT)
def detach(conversation_id: str, attachment_id: str, ctx: Ctx, request: Request) -> ConversationDetail:
    _own(ctx, request, conversation_id)
    return ctx.assistant.detach(conversation_id, attachment_id)


@router.get("/conversations/{conversation_id}/snapshots/{snapshot_id}", dependencies=READ)
def get_snapshot(conversation_id: str, snapshot_id: str, ctx: Ctx, request: Request) -> Snapshot:
    """Exactly what the assistant received of an object (for "what was sent?")."""
    _own(ctx, request, conversation_id)
    return ctx.assistant.snapshot(conversation_id, snapshot_id)


# ---- files --------------------------------------------------------------------------------


@router.get("/files/support", dependencies=READ)
def file_support(ctx: Ctx) -> FileSupport:
    """The formats and limits of files, and whether the assistant's current model reads images and PDFs natively."""
    return ctx.assistant.file_support()


@router.get("/files/retention", dependencies=READ)
def file_retention(ctx: Ctx) -> FileRetention:
    """How long conversation files are kept (API-056); ``retention_days: null`` keeps them with their conversation."""
    return ctx.assistant.file_retention()


@router.put("/files/retention", dependencies=ADMIN)
def set_file_retention(body: FileRetention, ctx: Ctx) -> FileRetention:
    """Set how long conversation files are kept (administrators). The input file clean-up
    (``POST /api/inputs/cleanup``) removes the older ones; ``null`` keeps them until the conversation is deleted."""
    return ctx.assistant.set_file_retention(body)


@router.post("/conversations/{conversation_id}/files", status_code=201, dependencies=EDIT)
async def upload_files(
    conversation_id: str, ctx: Ctx, request: Request, files: Annotated[list[UploadFile], File()]
) -> list[FileUpload]:
    """Check, read and attach files to the conversation (they wait for the next message); refused files are
    reported with the reason and never stored."""
    _own(ctx, request, conversation_id)
    results: list[FileUpload] = []
    for upload in files:
        name = upload.filename or "upload"
        data = await upload.read()
        try:
            view = await asyncio.to_thread(
                ctx.assistant.add_file,
                conversation_id,
                data,
                name=name,
                media_type=upload.content_type,
                uploaded_by=actor_id(request),
            )
        except (FileFormatError, NodestroError) as error:
            results.append(FileUpload(name=name, problem=error.problem))
            continue
        results.append(FileUpload(name=name, file=view))
    return results


@router.delete("/conversations/{conversation_id}/files/{file_id}", dependencies=EDIT)
def remove_file(conversation_id: str, file_id: str, ctx: Ctx, request: Request) -> ConversationDetail:
    """Remove a file from the conversation and delete it (sent messages keep only its name)."""
    _own(ctx, request, conversation_id)
    return ctx.assistant.remove_file(conversation_id, file_id)


@router.get("/conversations/{conversation_id}/files/{file_id}/content", dependencies=READ)
def file_content(conversation_id: str, file_id: str, ctx: Ctx, request: Request) -> Response:
    """The file as uploaded: always a download (never shown inline in the studio's origin)."""
    _own(ctx, request, conversation_id)
    item = ctx.assistant.file(conversation_id, file_id)
    media_type = ctx.inputs.download_type(item.id)
    return Response(
        content=ctx.inputs.data(item.id),
        media_type=media_type,
        headers=uploaded_file_headers(item.name, media_type, inline=False),
    )


@router.get("/conversations/{conversation_id}/files/{file_id}/text", dependencies=READ)
def file_text(conversation_id: str, file_id: str, ctx: Ctx, request: Request, path: str = "") -> FileText:
    """What Nodestro read from the file (the text a model without native support gets)."""
    _own(ctx, request, conversation_id)
    item = ctx.assistant.file(conversation_id, file_id)
    return read_text(item.id, ctx, path)


# ---- messages -----------------------------------------------------------------------------


@router.post("/conversations/{conversation_id}/messages", status_code=201, dependencies=RUN)
async def send_message(conversation_id: str, body: SendRequest, ctx: Ctx, request: Request) -> SentMessage:
    """Store the message and start the answer; follow it via its events."""
    _own(ctx, request, conversation_id)
    body = await _ready(ctx, request, body)
    # send() checks the model's connection (a local model server is asked): off the event loop (RT3-04 pattern).
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(ctx.assistant.send, conversation_id, body, loop=loop)


@router.post("/conversations/{conversation_id}/messages/{message_id}/retry", dependencies=RUN)
async def retry_message(
    conversation_id: str, message_id: str, body: RetryRequest, ctx: Ctx, request: Request
) -> ChatMessage:
    _own(ctx, request, conversation_id)
    body = await _ready(ctx, request, body)
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(ctx.assistant.retry, conversation_id, message_id, body, loop=loop)


async def _ready(ctx: Ctx, request: Request, body: _Body) -> _Body:
    """The answer's model: the assistant model, or another ready one chosen for it; a local model that is not
    ready is refused before the message is stored (``409 model.local_unavailable``), so its text and files stay in
    the composer."""
    model = await model_readiness.chosen_model(ctx, body.model, ctx.settings().assistant_model, "assistant")
    await model_readiness.require_local(ctx, request, model, "assistant")
    return body.model_copy(update={"model": model if body.model else None})


@router.post("/conversations/{conversation_id}/messages/{message_id}/cancel", dependencies=RUN)
def cancel_message(conversation_id: str, message_id: str, ctx: Ctx, request: Request) -> ChatMessage:
    _own(ctx, request, conversation_id)
    return ctx.assistant.cancel(conversation_id, message_id)


@router.get("/conversations/{conversation_id}/messages/{message_id}/events", dependencies=READ)
async def message_events(conversation_id: str, message_id: str, request: Request, ctx: Ctx) -> StreamingResponse:
    """Server-Sent Events: a ``snapshot`` of the answer so far, then ``event``s until ``end``."""
    _own(ctx, request, conversation_id)
    service = ctx.assistant

    access = StreamAccess(request)

    async def stream() -> AsyncIterator[str]:
        snapshot, last, queue = service.subscribe(message_id)
        try:
            if snapshot is not None and snapshot.conversation_id != conversation_id:
                # Another conversation's answer (perhaps another user's): nothing of it is shown here.
                yield _sse("end", {"status": "missing"})
                return
            if snapshot is None:
                stored = next((item for item in service.messages(conversation_id) if item.id == message_id), None)
                if stored is None:
                    yield _sse("end", {"status": "missing"})
                    return
                yield _sse("snapshot", stored.model_dump(mode="json"))
                yield _sse("end", {"status": stored.status})
                return
            yield _sse("snapshot", snapshot.model_dump(mode="json"))
            while True:
                if await request.is_disconnected():
                    return
                if await access.lost():
                    yield _sse("end", {"reason": "access_ended"})
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=access.wait)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    yield _sse("end", {})
                    return
                if event.seq <= last:
                    continue
                yield _sse("event", event.model_dump(mode="json"))
        finally:
            service.unsubscribe(message_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/docs/search", dependencies=READ)
def search_docs(ctx: Ctx, q: str, language: Language = "en") -> list[DocHitOut]:
    """The documentation search the assistant uses (free, no AI request)."""
    docs: DocsIndex = ctx.assistant.docs
    return [
        DocHitOut(
            ref=hit.section.ref,
            title=hit.section.title,
            chapter_title=hit.chapter_title,
            link=hit.section.link,
            snippet=hit.snippet,
        )
        for hit in docs.search(q, language)
    ]
