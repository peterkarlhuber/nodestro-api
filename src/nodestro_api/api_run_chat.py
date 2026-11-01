"""The API of the live run chat (LRC-1; runtime :mod:`nodestro_runtime.run_chat`).

- ``GET /api/runs/{run_id}/chat`` (``read``): the conversation, the orchestrators one can talk to (with what they
  and their teams do now) and the limits.
- ``POST /api/runs/{run_id}/chat/messages`` (``run``): a question (answered by a separate model call from the
  stored run state; it changes nothing) or an instruction (handed to the orchestrator at its next decision).
  ``client_id`` makes sending idempotent: 201 for a new message, 200 for the one that already exists.
- ``GET /api/runs/{run_id}/chat/messages/{message_id}`` (``read``): one message.
- ``GET /api/runs/{run_id}/chat/events`` (``read``): Server-Sent Events: ``snapshot`` (the whole view) on every
  connect, then ``message`` (a new or changed message) and ``targets`` (what the orchestrators do now); ``end``
  when the run ended and no answer is pending, or when access ended. A reconnecting client gets a new snapshot.

``run_id`` may be the top-level run or any run below it (a team's run): the conversation belongs to the top-level
run. Every route works on the request's tenant only; ids of other tenants are not found (404). The sender is the
request's principal, never a field of the body.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import logging
import threading
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.run_chat import (
    ACTIVE_RUN,
    MAX_CONTENT_CHARS,
    ChatLimits,
    ChatTarget,
    RunMessage,
    answer_question,
    chat_limits,
    find_target,
    steering_allowed,
    targets_of,
)
from nodestro_runtime.schemas import Run
from pydantic import BaseModel, Field

from .api import Ctx
from .permissions import READ, RUN, Permission, Principal, StreamAccess
from .serving import ApiError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

#: Answers one API process works on at the same time (each is a model call in a thread).
MAX_ANSWERS_AT_ONCE = 4
_answers = threading.BoundedSemaphore(MAX_ANSWERS_AT_ONCE)
#: How often an open event stream looks at the stored conversation (seconds).
POLL_SECONDS = 1.0

_NOT_FOUND = frozenset({"run_chat.unknown_target"})
_CONFLICT = frozenset(
    {
        "run_chat.run_not_active",
        "run_chat.imported_run",
        "run_chat.not_an_orchestrator",
        "run_chat.steering_off",
        "run_chat.steering_unattended",
        "run_chat.orchestrator_finished",
        "run_chat.client_id_reused",
    }
)
_TOO_MANY = frozenset(
    {
        "run_chat.too_many_messages",
        "run_chat.too_many_instructions",
        "run_chat.too_many_questions",
        "run_chat.busy",
        "run_chat.rate_limited",
        "run_chat.budget_exhausted",
    }
)


class RunChatView(BaseModel):
    run_id: str
    run_status: str
    #: Whether the run still takes messages.
    active: bool
    targets: list[ChatTarget]
    messages: list[RunMessage]
    limits: ChatLimits
    max_chars: int = MAX_CONTENT_CHARS
    #: The chat budget left in USD (known cost only; an answer whose cost is unknown is limited by the count).
    remaining_budget_usd: float
    questions_left: int
    #: The model that answers questions (the workspace's assistant model).
    answer_model: str


class ChatMessageIn(BaseModel):
    kind: Literal["question", "instruction"]
    target_run_id: str = Field(min_length=1, max_length=100)
    node_id: str = Field(min_length=1, max_length=100)
    #: Capped here as well as in the runtime (a request body is never larger than this needs).
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS * 2)
    client_id: str | None = Field(default=None, max_length=100)


def _http(exc: NodestroError) -> Exception:
    """A ``run_chat.*`` refusal with its HTTP status (others keep the default handling)."""
    code = exc.code
    if code in _NOT_FOUND:
        return ApiError(404, code, str(exc), **exc.params)
    if code in _CONFLICT:
        return ApiError(409, code, str(exc), **exc.params)
    if code in _TOO_MANY:
        return ApiError(429, code, str(exc), **exc.params)
    return exc


def _root(ctx: Ctx, run_id: str) -> Run:
    run = ctx.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    return ctx.runs.root_of(run)


def _view(ctx: Ctx, root: Run) -> RunChatView:
    chat = ctx.runs.chat
    chat.reconcile(root.id, ctx.runs.get)
    limits = chat_limits()
    messages = chat.messages(root.id)
    questions = sum(item.kind == "question" for item in messages)
    return RunChatView(
        run_id=root.id,
        run_status=root.status,
        active=root.status in ACTIVE_RUN and root.imported_at is None,
        targets=targets_of(ctx.runs.get, root),
        messages=[_public(item) for item in messages],
        limits=limits,
        remaining_budget_usd=chat.remaining_budget(root.id, limits),
        questions_left=max(limits.max_questions - questions, 0),
        answer_model=ctx.settings().assistant_model,
    )


def _public(message: RunMessage) -> RunMessage:
    """A message as the API shows it: without the client's idempotency key."""
    return message.model_copy(update={"client_id": None})


@router.get("/runs/{run_id}/chat", dependencies=READ)
def run_chat_view(run_id: str, ctx: Ctx) -> RunChatView:
    """The live chat of a run: messages, the orchestrators one can talk to and the limits."""
    return _view(ctx, _root(ctx, run_id))


@router.get("/runs/{run_id}/chat/messages/{message_id}", dependencies=READ)
def run_chat_message(run_id: str, message_id: str, ctx: Ctx) -> RunMessage:
    root = _root(ctx, run_id)
    ctx.runs.chat.reconcile(root.id, ctx.runs.get)
    found = ctx.runs.chat.get(root.id, message_id)
    if found is None:
        raise KeyError(message_id)
    return _public(found)


@router.post("/runs/{run_id}/chat/messages", status_code=201, dependencies=RUN)
async def send_run_chat_message(
    run_id: str, body: ChatMessageIn, ctx: Ctx, request: Request, response: Response
) -> RunMessage:
    """Ask a question about the run, or give its orchestrator an instruction for the next decision."""
    root = _root(ctx, run_id)
    located = find_target(ctx.runs.get, root, body.target_run_id, body.node_id)
    if located is None:
        raise ApiError(404, "run_chat.unknown_target", "This run has no such orchestrator")
    principal: Principal = request.state.principal
    model = ctx.settings().assistant_model
    if body.kind == "question":
        ctx.connect_model(model)  # no key for the answer model: refused before anything is stored

    def check() -> str | None:
        # Read again inside the conversation's transaction: the run may have ended since the request came in.
        current_root = ctx.runs.get(root.id)
        target = ctx.runs.get(body.target_run_id)
        if current_root is None or target is None:
            return "run_chat.unknown_target"
        node = next(item for item in target.workflow.nodes if item.id == body.node_id)
        if body.kind == "instruction":
            return steering_allowed(current_root, node, target.nodes.get(body.node_id), target)
        if current_root.imported_at is not None:
            return "run_chat.imported_run"
        if current_root.status not in ACTIVE_RUN:
            return "run_chat.run_not_active"
        return None

    try:
        message, new = await asyncio.to_thread(
            ctx.runs.chat.submit,
            run_id=root.id,
            target_run_id=body.target_run_id,
            node_id=body.node_id,
            kind=body.kind,
            content=body.content,
            author=principal.id,
            author_name=_display_name(request, principal),
            client_id=body.client_id,
            check=check,
        )
    except NodestroError as exc:
        raise _http(exc) from None
    if not new:
        response.status_code = 200
        return _public(message)
    if message.kind == "question":
        if not _answers.acquire(blocking=False):
            ctx.runs.chat.fail_question(message, "run_chat.busy")
            raise ApiError(429, "run_chat.busy", "Too many answers are being written right now; ask again shortly")
        threading.Thread(target=_answer, args=(ctx, message, model), name=f"run-chat-{message.id}", daemon=True).start()
    return _public(message)


def _display_name(request: Request, principal: Principal) -> str:
    name = getattr(request.state, "display_name", None)
    return str(name)[:200] if name else ("" if principal.id == "local" else principal.id)


def _answer(ctx: Ctx, question: RunMessage, model: str) -> None:
    try:
        answer_question(ctx.runs, ctx.runs.chat, ctx.usage_log, question, model=model)
    except Exception:  # the question fails visibly instead of waiting forever
        logger.exception("Answering a live chat question of run %s failed", question.run_id)
        ctx.runs.chat.fail_question(question, "run_chat.answer_failed")
    finally:
        _answers.release()


def _sse(kind: str, data: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/runs/{run_id}/chat/events", dependencies=READ)
async def run_chat_events(run_id: str, request: Request, ctx: Ctx) -> StreamingResponse:
    """Server-Sent Events of the live chat (see the module docs); every process sees every change (the stream
    reads the stored conversation, written by API processes and by the worker that executes the run)."""
    root = _root(ctx, run_id)
    access = StreamAccess(request, Permission.READ)

    async def stream() -> AsyncIterator[str]:
        view = await asyncio.to_thread(_view, ctx, root)
        yield _sse("snapshot", view.model_dump(mode="json"))
        seen = {item.id: (item.status, item.status_at) for item in view.messages}
        shown_targets = [item.model_dump(mode="json") for item in view.targets]
        quiet = 0.0
        while True:
            if not view.active and not any(item.status == "pending" for item in view.messages):
                yield _sse("end", {"status": view.run_status})
                return
            await asyncio.sleep(POLL_SECONDS)
            if await request.is_disconnected():
                return
            if await access.lost():
                yield _sse("end", {"reason": "access_ended"})
                return
            current = ctx.runs.get(root.id)
            if current is None:
                yield _sse("end", {"reason": "deleted"})
                return
            view = await asyncio.to_thread(_view, ctx, current)
            changed = False
            for item in view.messages:
                if seen.get(item.id) != (item.status, item.status_at):
                    seen[item.id] = (item.status, item.status_at)
                    yield _sse("message", item.model_dump(mode="json"))
                    changed = True
            targets = [item.model_dump(mode="json") for item in view.targets]
            if targets != shown_targets:
                shown_targets = targets
                yield _sse("targets", {"targets": targets, "run_status": view.run_status, "active": view.active})
                changed = True
            quiet = 0.0 if changed else quiet + POLL_SECONDS
            if quiet >= access.wait:
                quiet = 0.0
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
