"""The Nodestro Assistant's API: conversations, attachments, messages and live answers.

Nothing here changes a workflow, an agent, a tool, memory or knowledge, and nothing runs:
attaching only reads an object, and a handover is a proposal the interface shows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from nodestro_runtime.schemas import Language, WorkflowIn
from pydantic import BaseModel, Field

from .api import Ctx, _sse
from .assistant import (
    ChatMessage,
    ConversationDetail,
    ConversationSummary,
    RetryRequest,
    SendRequest,
    SentMessage,
)
from .assistant_context import AttachRequest, Snapshot
from .docs_index import DocsIndex
from .permissions import EDIT, READ, RUN, StreamAccess

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


@router.get("/conversations", dependencies=READ)
def list_conversations(ctx: Ctx) -> list[ConversationSummary]:
    return ctx.assistant.conversations()


@router.post("/conversations", status_code=201, dependencies=EDIT)
def create_conversation(body: NewConversation, ctx: Ctx) -> ConversationDetail:
    return ctx.assistant.create(body.title)


@router.get("/conversations/{conversation_id}", dependencies=READ)
def get_conversation(conversation_id: str, ctx: Ctx) -> ConversationDetail:
    return ctx.assistant.detail(conversation_id)


@router.patch("/conversations/{conversation_id}", dependencies=EDIT)
def rename_conversation(conversation_id: str, body: Rename, ctx: Ctx) -> ConversationDetail:
    return ctx.assistant.rename(conversation_id, body.title)


@router.delete("/conversations/{conversation_id}", status_code=204, dependencies=EDIT)
def delete_conversation(conversation_id: str, ctx: Ctx) -> Response:
    ctx.assistant.delete(conversation_id)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/attachments", dependencies=EDIT)
def attach(conversation_id: str, body: AttachRequest, ctx: Ctx) -> ConversationDetail:
    """The user's explicit choice: take a snapshot of the object for the following messages."""
    return ctx.assistant.attach(conversation_id, body)


@router.post("/conversations/{conversation_id}/attachments/{attachment_id}/refresh", dependencies=EDIT)
def refresh_attachment(conversation_id: str, attachment_id: str, body: Refresh, ctx: Ctx) -> ConversationDetail:
    return ctx.assistant.refresh(conversation_id, attachment_id, body.workflow)


@router.delete("/conversations/{conversation_id}/attachments/{attachment_id}", dependencies=EDIT)
def detach(conversation_id: str, attachment_id: str, ctx: Ctx) -> ConversationDetail:
    return ctx.assistant.detach(conversation_id, attachment_id)


@router.get("/conversations/{conversation_id}/snapshots/{snapshot_id}", dependencies=READ)
def get_snapshot(conversation_id: str, snapshot_id: str, ctx: Ctx) -> Snapshot:
    """Exactly what the assistant received of an object (for "what was sent?")."""
    return ctx.assistant.snapshot(conversation_id, snapshot_id)


@router.post("/conversations/{conversation_id}/messages", status_code=201, dependencies=RUN)
async def send_message(conversation_id: str, body: SendRequest, ctx: Ctx) -> SentMessage:
    """Store the message and start the answer; follow it via its events."""
    return ctx.assistant.send(conversation_id, body, loop=asyncio.get_running_loop())


@router.post("/conversations/{conversation_id}/messages/{message_id}/retry", dependencies=RUN)
async def retry_message(conversation_id: str, message_id: str, body: RetryRequest, ctx: Ctx) -> ChatMessage:
    return ctx.assistant.retry(conversation_id, message_id, body, loop=asyncio.get_running_loop())


@router.post("/conversations/{conversation_id}/messages/{message_id}/cancel", dependencies=RUN)
def cancel_message(conversation_id: str, message_id: str, ctx: Ctx) -> ChatMessage:
    return ctx.assistant.cancel(conversation_id, message_id)


@router.get("/conversations/{conversation_id}/messages/{message_id}/events", dependencies=READ)
async def message_events(conversation_id: str, message_id: str, request: Request, ctx: Ctx) -> StreamingResponse:
    """Server-Sent Events: a ``snapshot`` of the answer so far, then ``event``s until ``end``."""
    service = ctx.assistant

    access = StreamAccess(request)

    async def stream() -> AsyncIterator[str]:
        snapshot, last, queue = service.subscribe(message_id)
        try:
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
                except asyncio.TimeoutError:
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
