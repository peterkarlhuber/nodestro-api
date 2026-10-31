"""The API of per-call tool approvals (S-2b, decision D10; runtime :mod:`nodestro_runtime.tool_approvals`).

- ``GET /api/tool-approvals``: the tenant's open questions of all runs (the approval inbox), oldest first.
- ``GET /api/runs/{run_id}/tool-approvals``: the questions of one run, decided ones included.
- ``POST /api/runs/{run_id}/tool-approvals/{request_id}``: approve or reject one call. Needs ``run``; the decision
  names the arguments hash the person was shown, so a decision about other arguments or another tool definition is
  refused (409 ``tool.approval_stale``), and so is a late or repeated one (409 ``tool.approval_expired`` /
  ``tool.approval_decided``). The decider is the request's principal, never a field of the body.
- ``GET``/``POST /api/tool-pre-grants`` and ``DELETE /api/tool-pre-grants/{scope}/{scope_id}/{grant_id}``: advance
  approvals for one schedule, batch or test series (reading needs ``read``, granting and revoking ``admin``).
- ``GET /api/tool-approvals/audit`` (``admin``): who decided, granted or revoked what, and every refused decision.
- ``GET``/``POST /api/tool-approvals/legacy``: the activation report, and the activation step that stamps every
  existing binding of workflows, workflow versions and published releases as a visible legacy exemption
  (``admin``). The step does not switch approvals on: that stays ``NODESTRO_TOOL_APPROVALS=1`` after the security
  acceptance.

Every route works on the request's tenant only (its workspace and store); ids of other tenants are not found.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now
from nodestro_runtime.tool_approvals import (
    ToolApprovalEvent,
    ToolApprovalRequest,
    ToolApprovals,
    ToolPreGrants,
    approvals_enabled,
    audit_events,
)
from nodestro_runtime.tool_legacy import LegacyReport
from nodestro_runtime.tool_policy import PreGrantScope, ToolPreGrant
from pydantic import BaseModel, Field

from .api import Ctx
from .permissions import ADMIN, READ, RUN, Principal
from .serving import ApiError

router = APIRouter(prefix="/api")

#: Problem codes of a decision that came too late or about something else (answered with 409).
CONFLICTS = frozenset({"tool.approval_stale", "tool.approval_expired", "tool.approval_decided"})


def _actor(request: Request) -> str:
    principal: Principal = request.state.principal
    return principal.id


def _require_run(ctx: Ctx, run_id: str) -> None:
    if ctx.runs.get(run_id) is None:
        raise KeyError(run_id)


class ToolApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    #: The ``arguments_hash`` of the request as it was shown (binds the decision to the call and the definition).
    arguments_hash: str = Field(min_length=1, max_length=128)
    comment: str = Field(default="", max_length=4000)


@router.get("/tool-approvals", dependencies=READ)
def open_tool_approvals(ctx: Ctx) -> list[ToolApprovalRequest]:
    """Every question that waits for a person, oldest first (also after a restart)."""
    return ToolApprovals(ctx.store).open()


@router.get("/runs/{run_id}/tool-approvals", dependencies=READ)
def run_tool_approvals(run_id: str, ctx: Ctx) -> list[ToolApprovalRequest]:
    _require_run(ctx, run_id)
    return ToolApprovals(ctx.store).of_run(run_id)


@router.post("/runs/{run_id}/tool-approvals/{request_id}", dependencies=RUN)
def decide_tool_approval(
    run_id: str, request_id: str, body: ToolApprovalDecision, ctx: Ctx, request: Request
) -> ToolApprovalRequest:
    """Approve or reject one tool call; the waiting run continues at its next look (within about a second)."""
    _require_run(ctx, run_id)
    try:
        return ToolApprovals(ctx.store).decide(
            run_id,
            request_id,
            approved=body.decision == "approved",
            by=_actor(request),
            arguments=body.arguments_hash,
            comment=body.comment or None,
        )
    except NodestroError as problem:
        if problem.code == "tool.approval_not_found":
            raise KeyError(request_id) from problem
        if problem.code in CONFLICTS:
            params = dict(problem.params)
            if "status" in params:  # the request's status (``status`` is the HTTP status of the answer)
                params["request_status"] = params.pop("status")
            raise ApiError(409, problem.code, str(problem), **params) from problem
        raise


# ---- pre-grants -------------------------------------------------------------------------------------------------


class PreGrantIn(BaseModel):
    tool: str = Field(min_length=1, max_length=200)
    scope: PreGrantScope
    scope_id: str = Field(min_length=1, max_length=200)
    expires_at: datetime | None = None


def _require_scope(ctx: Ctx, scope: PreGrantScope, scope_id: str) -> None:
    if scope == "schedule":
        ctx.schedules.get(scope_id)
    else:  # batches and test series (benchmark runs are batches of their own kind)
        ctx.batches.get(scope_id)


@router.get("/tool-pre-grants", dependencies=READ)
def list_pre_grants(ctx: Ctx) -> list[ToolPreGrant]:
    """Every pre-grant of the tenant, newest first, revoked and expired ones included."""
    return ToolPreGrants(ctx.store).all()


@router.post("/tool-pre-grants", status_code=201, dependencies=ADMIN)
def create_pre_grant(body: PreGrantIn, ctx: Ctx, request: Request) -> ToolPreGrant:
    """Approve one tool version in advance for one schedule, batch or test series (never MCP or destructive tools)."""
    _require_scope(ctx, body.scope, body.scope_id)
    info = ctx.tools.get(body.tool)
    return ToolPreGrants(ctx.store).grant(
        tool=info.name,
        kind=info.kind,
        capability=info.capability,
        version=info.version,
        scope=body.scope,
        scope_id=body.scope_id,
        by=_actor(request),
        expires_at=body.expires_at,
    )


@router.delete("/tool-pre-grants/{scope}/{scope_id}/{grant_id}", dependencies=ADMIN)
def revoke_pre_grant(scope: PreGrantScope, scope_id: str, grant_id: str, ctx: Ctx, request: Request) -> ToolPreGrant:
    """Revoke a pre-grant; it no longer applies from the next call on (revoking twice changes nothing)."""
    try:
        return ToolPreGrants(ctx.store).revoke(scope, scope_id, grant_id, by=_actor(request))
    except NodestroError as problem:
        if problem.code == "tool.pre_grant_not_found":
            raise KeyError(grant_id) from problem
        raise


# ---- audit and activation -----------------------------------------------------------------------------------------


@router.get("/tool-approvals/audit", dependencies=ADMIN)
def tool_approval_audit(ctx: Ctx, limit: int = 200) -> list[ToolApprovalEvent]:
    return audit_events(ctx.store, limit=max(1, min(limit, 1000)))


class ToolApprovalsState(BaseModel):
    #: Whether this process asks before tool calls (``NODESTRO_TOOL_APPROVALS=1``; off until the security acceptance).
    enabled: bool
    #: What the activation step stamped (``None``: not run yet).
    legacy: LegacyReport | None = None


@router.get("/tool-approvals/legacy", dependencies=READ)
def tool_approvals_state(ctx: Ctx) -> ToolApprovalsState:
    return ToolApprovalsState(enabled=approvals_enabled(), legacy=ctx.legacy_tool_bindings())


@router.post("/tool-approvals/legacy", dependencies=ADMIN)
def stamp_legacy_bindings(ctx: Ctx, request: Request) -> ToolApprovalsState:
    """The activation step: stamp existing bindings of workflows, workflow versions and published releases as legacy
    exemptions (visible, bound to the tool version they run with). It runs once: a repeated call stamps nothing, so
    an assignment or release made after the first call never becomes an exemption. It does not switch approvals on;
    run it, review the report, and only then set ``NODESTRO_TOOL_APPROVALS=1``."""

    def version_of(name: str) -> int | None:
        try:
            return ctx.tools.get(name).version
        except KeyError:
            return None

    ctx.stamp_legacy_tool_bindings(by=_actor(request))
    report = ctx.legacy_tool_bindings()
    assert report is not None  # written by the call above
    if report.publications_stamped_at is None:
        # Once, and only releases that existed when the workspace was stamped (also after an interrupted first call).
        released = ctx.publications.stamp_legacy_tool_bindings(version_of, created_before=report.stamped_at)
        report = ctx.record_legacy_tool_bindings(LegacyReport(bindings=released), publications_stamped_at=now())
    return ToolApprovalsState(enabled=approvals_enabled(), legacy=report)


__all__ = ["router"]
