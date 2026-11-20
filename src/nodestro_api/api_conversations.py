"""The API of conversations with own agents (N-04; runtime ``docs/conversations.md``). Off by default: only while
``NODESTRO_CONVERSATIONS=1``; otherwise every route answers ``404 conversation.disabled`` and nothing is read or
written.

- ``GET /api/conversations`` (``read``), ``POST`` (``edit``): list and create.
- ``GET``, ``PATCH`` (rename, switch agent or model, offline flag, budget) and ``DELETE`` (``edit``) of
  ``/api/conversations/{conversation_id}``.
- ``GET .../export`` (``read``): the conversation as Markdown (a download).
- ``POST .../files`` (``edit``, multipart), ``DELETE .../files/{file_id}`` and ``GET .../files/{file_id}/content``:
  the conversation's files, as for the Nodestro Assistant (:mod:`nodestro_api.api_assistant`).
- ``POST .../messages`` (``run``): store a message and start its answer. Every answer is a run of the engine
  (trigger ``conversation``), so its cost is in the usage ledger and its tool approvals use the existing approval
  routes. ``GET .../messages/{message_id}`` (``read``), ``POST .../messages/{message_id}/cancel`` (``run``: cancels
  the run, which closes the model connection) and ``GET .../messages/{message_id}/events`` (``read``): Server-Sent
  Events in the run's event format (``snapshot`` with the message, then ``event`` with each ``RunEvent``, then
  ``end``).

On a server a conversation belongs to the user who started it: every route with a conversation id answers
``404 conversation.not_found`` for another user's conversation, and the list shows only the user's own ones. Tenants are
separated as everywhere (each tenant's workspace and store); a stored conversation of another tenant is never read.
The answers' runs belong to the conversation too: every run route answers ``404 run.not_found`` for another user's
answer run, and the run lists leave it out (:func:`nodestro_api.api.app_context`, H-1).

With the switch off, the upload route still reads its multipart body before it answers ``404 conversation.disabled``
(FastAPI parses the body before the route runs; the server's request size limit applies). Nothing is stored.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Annotated, TypeVar

from fastapi import APIRouter, File, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from nodestro_runtime.conversations import (
    AgentConversationDetail,
    AgentConversationFile,
    AgentConversationSummary,
    ConversationChange,
    ConversationIn,
    ConversationMessage,
    ConversationNotFound,
    MessageIn,
    SentConversationMessage,
    conversations_enabled,
)
from nodestro_runtime.file_formats import FileFormatError
from nodestro_runtime.problems import NodestroError, Problem
from pydantic import BaseModel

from . import model_readiness
from .api import Ctx, _sse, actor_id, owner_of, uploaded_file_headers
from .api_access import revoke_conversation_grants
from .permissions import EDIT, READ, RUN, StreamAccess
from .serving import ApiError

router = APIRouter(prefix="/api/conversations")

T = TypeVar("T")


class ConversationUpload(BaseModel):
    """One uploaded file: attached (``file``) or refused (``problem``; nothing is stored then)."""

    name: str
    file: AgentConversationFile | None = None
    problem: Problem | None = None


def _owner(request: Request) -> str | None:
    """The switch, then whose conversations this request sees (``None``: the desktop's only person)."""
    if not conversations_enabled():
        raise ApiError(404, "conversation.disabled", "Conversations are not switched on for this application")
    return owner_of(request)


def _found(call: Callable[[], T]) -> T:
    """Unknown ids (also another owner's conversation) answer ``404 conversation.not_found``."""
    try:
        return call()
    except ConversationNotFound as missing:
        name = str(missing.args[0]) if missing.args else ""
        raise ApiError(404, "conversation.not_found", "The conversation or its part does not exist", id=name) from None


@router.get("", dependencies=READ)
def list_conversations(ctx: Ctx, request: Request) -> list[AgentConversationSummary]:
    return ctx.conversations.summaries(_owner(request))


@router.post("", status_code=201, dependencies=EDIT)
def create_conversation(body: ConversationIn, ctx: Ctx, request: Request) -> AgentConversationDetail:
    owner = _owner(request)
    return ctx.conversations.create(body, owner=owner)


@router.get("/{conversation_id}", dependencies=READ)
def get_conversation(conversation_id: str, ctx: Ctx, request: Request) -> AgentConversationDetail:
    owner = _owner(request)
    return _found(lambda: ctx.conversations.detail(conversation_id, owner))


@router.patch("/{conversation_id}", dependencies=EDIT)
def change_conversation(
    conversation_id: str, body: ConversationChange, ctx: Ctx, request: Request
) -> AgentConversationDetail:
    """Rename it or switch its agent, model, offline flag or budget; only the fields sent change."""
    owner = _owner(request)
    return _found(lambda: ctx.conversations.update(conversation_id, owner, body))


@router.delete("/{conversation_id}", status_code=204, dependencies=EDIT)
async def delete_conversation(conversation_id: str, ctx: Ctx, request: Request) -> Response:
    """Delete it with its messages, files and the answers' runs (a running answer is cancelled first), and revoke its
    grants for "this conversation". The usage ledger keeps the cost."""
    owner = _owner(request)
    try:
        await ctx.conversations.delete(conversation_id, owner)
    except ConversationNotFound as missing:
        name = str(missing.args[0]) if missing.args else ""
        raise ApiError(404, "conversation.not_found", "The conversation or its part does not exist", id=name) from None
    revoke_conversation_grants(ctx, request, conversation_id, owner)
    return Response(status_code=204)


@router.get(
    "/{conversation_id}/export",
    dependencies=READ,
    response_class=Response,
    responses={200: {"content": {"text/markdown": {"schema": {"type": "string"}}}}},
)
def export_conversation(conversation_id: str, ctx: Ctx, request: Request) -> Response:
    """The conversation as Markdown, as a download."""
    owner = _owner(request)
    text = _found(lambda: ctx.conversations.export_markdown(conversation_id, owner))
    return Response(
        content=text.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{conversation_id}.md"'},
    )


# ---- files --------------------------------------------------------------------------------------------------------


@router.post("/{conversation_id}/files", status_code=201, dependencies=EDIT)
async def upload_files(
    conversation_id: str, ctx: Ctx, request: Request, files: Annotated[list[UploadFile], File()]
) -> list[ConversationUpload]:
    """Check, read and attach files to the conversation (a message sends them); refused files are reported with the
    reason and never stored."""
    owner = _owner(request)
    _found(lambda: ctx.conversations.get(conversation_id, owner))
    results: list[ConversationUpload] = []
    for upload in files:
        name = upload.filename or "upload"
        data = await upload.read()
        try:
            attached = await asyncio.to_thread(
                _found,
                lambda data=data, name=name, upload=upload: ctx.conversations.add_file(
                    conversation_id,
                    owner,
                    data,
                    name=name,
                    media_type=upload.content_type,
                    uploaded_by=actor_id(request),
                ),
            )
        except (FileFormatError, NodestroError) as error:
            results.append(ConversationUpload(name=name, problem=error.problem))
            continue
        results.append(ConversationUpload(name=name, file=attached))
    return results


@router.delete("/{conversation_id}/files/{file_id}", dependencies=EDIT)
def remove_file(conversation_id: str, file_id: str, ctx: Ctx, request: Request) -> AgentConversationDetail:
    """Remove a file from the conversation and delete it (sent messages keep its name)."""
    owner = _owner(request)
    return _found(lambda: ctx.conversations.remove_file(conversation_id, owner, file_id))


@router.get("/{conversation_id}/files/{file_id}/content", dependencies=READ)
def file_content(conversation_id: str, file_id: str, ctx: Ctx, request: Request) -> Response:
    """The file as uploaded: always a download (never shown inline in the studio's origin)."""
    owner = _owner(request)
    item = _found(lambda: ctx.conversations.file(conversation_id, owner, file_id))
    media_type = ctx.inputs.download_type(item.id)
    return Response(
        content=ctx.inputs.data(item.id),
        media_type=media_type,
        headers=uploaded_file_headers(item.name, media_type, inline=False),
    )


# ---- messages -----------------------------------------------------------------------------------------------------


@router.post("/{conversation_id}/messages", status_code=201, dependencies=RUN)
async def send_message(conversation_id: str, body: MessageIn, ctx: Ctx, request: Request) -> SentConversationMessage:
    """Store the message and start its answer (a run); follow it via its events. A local model that is not ready is
    refused before anything is stored (``409 model.local_unavailable``); no other model is chosen instead."""
    owner = _owner(request)
    conversation = _found(lambda: ctx.conversations.get(conversation_id, owner))
    await model_readiness.require_local(ctx, request, conversation.model, "run")
    try:
        return await ctx.conversations.send(conversation_id, owner, body, actor=actor_id(request))
    except ConversationNotFound as missing:
        name = str(missing.args[0]) if missing.args else ""
        raise ApiError(404, "conversation.not_found", "The conversation or its part does not exist", id=name) from None


@router.get("/{conversation_id}/messages/{message_id}", dependencies=READ)
def get_message(conversation_id: str, message_id: str, ctx: Ctx, request: Request) -> ConversationMessage:
    owner = _owner(request)
    return _found(lambda: ctx.conversations.message(conversation_id, owner, message_id))


@router.post("/{conversation_id}/messages/{message_id}/cancel", dependencies=RUN)
def cancel_message(conversation_id: str, message_id: str, ctx: Ctx, request: Request) -> ConversationMessage:
    """Cancel a running answer: its run is cancelled and the model connection closed."""
    owner = _owner(request)
    return _found(lambda: ctx.conversations.cancel(conversation_id, owner, message_id))


@router.get("/{conversation_id}/messages/{message_id}/events", dependencies=READ)
async def message_events(conversation_id: str, message_id: str, ctx: Ctx, request: Request) -> StreamingResponse:
    """Server-Sent Events: a ``snapshot`` of the message, then the run's events (``event``) until ``end``."""
    owner = _owner(request)
    run_id = _found(lambda: ctx.conversations.run_of(conversation_id, owner, message_id))
    access = StreamAccess(request)

    def snapshot() -> str:
        found = ctx.conversations.message(conversation_id, owner, message_id)
        return _sse("snapshot", found.model_dump(mode="json"))

    def gone() -> bool:
        """The conversation was deleted (or is no longer the caller's) while the stream ran (L-5)."""
        try:
            ctx.conversations.get(conversation_id, owner)
        except ConversationNotFound:
            return True
        return False

    async def stream() -> AsyncIterator[str]:
        if not ctx.runs.is_live(run_id):
            yield snapshot()
            yield _sse("end", {"status": ctx.conversations.message(conversation_id, owner, message_id).status})
            return
        history, queue = ctx.runs.subscribe(run_id)
        try:
            yield snapshot()
            last = history[-1].seq if history else 0
            while True:
                if await request.is_disconnected():
                    return
                if await access.lost():
                    yield _sse("end", {"reason": "access_ended"})
                    return
                if gone():
                    yield _sse("end", {"reason": "deleted"})
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=access.wait)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    if gone():
                        yield _sse("end", {"reason": "deleted"})
                        return
                    found = ctx.conversations.message(conversation_id, owner, message_id)
                    yield _sse("end", {"status": found.status})
                    return
                if event.seq <= last:
                    continue
                yield _sse("event", event.model_dump(mode="json"))
        finally:
            ctx.runs.unsubscribe(run_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
