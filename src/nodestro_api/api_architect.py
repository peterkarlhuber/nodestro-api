"""The Workflow Architect's API: start, follow (Server-Sent Events), cancel, accept and discard
a job, plus the tool drafts it leaves for the user's approval.

Nothing here runs a workflow. Accepting a ``create`` draft stores a new workflow; accepting a
``revise`` draft only marks it, the editor applies the changes as an undoable step.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from nodestro_runtime.tool_drafts import ToolDraft
from nodestro_runtime.tool_library import ToolInfo
from pydantic import BaseModel, Field

from .api import Ctx, _sse
from .architect import ArchitectJob, ArchitectRequest
from .permissions import EDIT, READ, RUN

router = APIRouter(prefix="/api")


@router.post("/architect/jobs", status_code=201, dependencies=RUN)
async def start_architect(body: ArchitectRequest, ctx: Ctx) -> ArchitectJob:
    """Start designing (``create``) or revising (``revise``) a workflow; returns at once."""
    ctx.connect_model(ctx.settings().assistant_model)  # fail fast without provider settings
    return ctx.architect.start(body, loop=asyncio.get_running_loop())


@router.get("/architect/jobs/{job_id}", dependencies=READ)
def get_architect_job(job_id: str, ctx: Ctx) -> ArchitectJob:
    return ctx.architect.get(job_id)


@router.get("/architect/jobs/{job_id}/events", dependencies=READ)
async def architect_events(job_id: str, request: Request, ctx: Ctx) -> StreamingResponse:
    """Server-Sent Events: a ``snapshot`` of the job first, then every event until ``end``."""
    job = ctx.architect.get(job_id)

    async def stream() -> AsyncIterator[str]:
        if not ctx.architect.is_live(job_id):
            yield _sse("snapshot", job.model_dump(mode="json"))
            yield _sse("end", {"status": job.status})
            return
        snapshot, last, queue = ctx.architect.follow(job_id)
        try:
            yield _sse("snapshot", snapshot.model_dump(mode="json"))
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    yield _sse("end", {"status": ctx.architect.get(job_id).status})
                    return
                if event.seq <= last:
                    continue
                yield _sse("event", event.model_dump(mode="json"))
        finally:
            ctx.architect.unsubscribe(job_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/architect/jobs/{job_id}/cancel", dependencies=RUN)
def cancel_architect(job_id: str, ctx: Ctx) -> ArchitectJob:
    return ctx.architect.cancel(job_id)


class AcceptRequest(BaseModel):
    #: ``create``: the name of the new workflow (the proposed one when empty).
    name: str = Field(default="", max_length=120)


@router.post("/architect/jobs/{job_id}/accept", dependencies=EDIT)
def accept_architect(job_id: str, body: AcceptRequest, ctx: Ctx) -> ArchitectJob:
    """Take a ready draft: a new workflow for ``create``; never starts a run."""
    return ctx.architect.accept(job_id, name=body.name)


@router.post("/architect/jobs/{job_id}/discard", dependencies=EDIT)
def discard_architect(job_id: str, ctx: Ctx) -> ArchitectJob:
    """Throw a draft away, with its tool drafts; existing workflows stay untouched."""
    return ctx.architect.discard(job_id)


# ---- tool drafts --------------------------------------------------------------------


class ToolDraftDecision(BaseModel):
    draft: ToolDraft
    tool: ToolInfo | None = None


@router.get("/tools/drafts", dependencies=READ)
def list_tool_drafts(ctx: Ctx) -> list[ToolDraft]:
    """Tools the Workflow Architect wrote that wait for a decision."""
    return ctx.tool_drafts.pending()


@router.get("/tools/drafts/{draft_id}", dependencies=READ)
def get_tool_draft(draft_id: str, ctx: Ctx) -> ToolDraft:
    return ctx.tool_drafts.get(draft_id)


@router.post("/tools/drafts/{draft_id}/test", dependencies=RUN)
async def test_tool_draft(draft_id: str, ctx: Ctx) -> ToolDraft:
    """Run the draft's tests in the sandbox (the user's explicit request); nothing is registered."""
    return await asyncio.to_thread(ctx.tool_drafts.test, draft_id)


@router.post("/tools/drafts/{draft_id}/approve", dependencies=RUN)
async def approve_tool_draft(draft_id: str, ctx: Ctx) -> ToolDraftDecision:
    """The user's approval: test in the sandbox and register the tool only if everything passes."""
    draft, tool = await asyncio.to_thread(ctx.tool_drafts.approve, draft_id)
    return ToolDraftDecision(draft=draft, tool=tool)


@router.delete("/tools/drafts/{draft_id}", status_code=204, dependencies=EDIT)
def reject_tool_draft(draft_id: str, ctx: Ctx) -> Response:
    ctx.tool_drafts.reject(draft_id)
    return Response(status_code=204)
