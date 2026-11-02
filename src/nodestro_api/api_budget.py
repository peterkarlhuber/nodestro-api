"""The API of run budget decisions (BUDGET-1; runtime :mod:`nodestro_runtime.run_budget`).

An attended run whose next model request does not fit its budget pauses before the request is sent and asks for a
decision. These routes show and answer it:

- ``GET /api/runs/{run_id}/budget`` (``read``): the run's budget (a sub-workflow's run answers for its top-level run),
  the open request, earlier requests, and what the caller may decide (``policy``).
- ``POST /api/runs/{run_id}/budget/decisions``: raise the blocking limit of this run (``raise``, ``limit_usd``) or end
  the run (``end``). Raising and continuing are one operation. Needs ``run`` **and** that the caller started the
  run or holds ``admin``; a run without a recorded starter is decided by an admin only. The new limit is at most the
  tenant's ``max_run_budget_usd`` and the operator's ``NODESTRO_MAX_RUN_BUDGET_USD``. Errors: 404 (run or request
  unknown), 409 ``budget.not_pending``, 422 ``budget.limit_not_higher`` / ``budget.limit_invalid`` /
  ``budget.action_invalid``, 403 ``budget.over_policy`` / ``access.forbidden``. The same ``idempotency_key`` with the
  same decision by the same person answers the stored result.
- ``GET /api/budget-requests?status=pending`` (``read``): the tenant's open requests, oldest first (approvals inbox).

The decider is always the request's principal, never a field of the body. Every route works on the request's tenant
only; ids of other tenants are not found. A stored workflow, a release and the default budget never change.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Request
from nodestro_runtime.engine import run_has_unknown_cost, run_spent_usd
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.run_budget import BudgetRequest, BudgetRequests
from nodestro_runtime.schemas import BudgetRaise, Run
from pydantic import BaseModel, Field

from .api import Ctx
from .permissions import READ, RUN, AccessError, Permission, Principal
from .serving import ApiError

router = APIRouter(prefix="/api")

#: HTTP status of each refusal of a decision.
STATUS = {
    "budget.not_pending": 409,
    "budget.limit_not_higher": 422,
    "budget.limit_invalid": 422,
    "budget.action_invalid": 422,
    "budget.over_policy": 403,
}


class BudgetPolicy(BaseModel):
    #: The highest limit a decision may set (tenant and operator maximum); ``None``: no maximum.
    max_limit_usd: Decimal | None = None
    #: Whether the caller may decide this run's budget requests.
    can_decide: bool = False
    #: Why not (an error code), when ``can_decide`` is false.
    reason: str | None = None
    #: How long an attended run waits for a decision.
    decision_hours: int = 24


class RunBudgetOut(BaseModel):
    run_id: str
    status: str
    source: str
    initial_limit_usd: Decimal | None = None
    limit_usd: Decimal | None = None
    spent_usd: Decimal
    remaining_usd: Decimal | None = None
    #: Some requests had no known price: ``spent_usd`` is a lower bound.
    unknown_cost: bool = False
    raises: list[BudgetRaise] = Field(default_factory=list)
    pending: BudgetRequest | None = None
    history: list[BudgetRequest] = Field(default_factory=list)
    policy: BudgetPolicy


class BudgetDecisionIn(BaseModel):
    request_id: str = Field(min_length=1, max_length=100)
    action: Literal["raise", "end"]
    #: The new limit of the blocking budget in USD (a decimal string such as ``"2.50"``), for ``raise``.
    limit_usd: str | None = Field(default=None, max_length=32)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")


def _principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


def _actor(request: Request) -> str:
    principal = _principal(request)
    return principal.id if principal is not None else "local"


def _root(ctx: Ctx, run_id: str) -> Run:
    run = ctx.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    return ctx.runs.root_of(run)


def _may_decide(root: Run, request: Request) -> str | None:
    """``None`` when the caller may decide this run's budget, else the reason (an error code)."""
    principal = _principal(request)
    if principal is None:
        return None  # the desktop without access control: its owner
    if not principal.allows(Permission.RUN):
        return "access.forbidden"
    if principal.allows(Permission.ADMIN):
        return None
    if root.started_by is not None and root.started_by == principal.id:
        return None
    return "budget.not_starter"


def _view(ctx: Ctx, root: Run, request: Request) -> RunBudgetOut:
    cap, hours = ctx.runs.budget_policy()
    requests = BudgetRequests(ctx.store).of_run(root.id)
    budget = root.budget
    pending_id = budget.pending_request_id if budget is not None else None
    pending = next((item for item in requests if item.id == pending_id and item.status == "pending"), None)
    if pending is None:
        pending = next((item for item in requests if item.status == "pending"), None)
    spent = run_spent_usd(root)
    limit = budget.limit_usd if budget is not None else None
    reason = _may_decide(root, request)
    return RunBudgetOut(
        run_id=root.id,
        status=root.status,
        source=budget.source if budget is not None else "none",
        initial_limit_usd=budget.initial_limit_usd if budget is not None else None,
        limit_usd=limit,
        spent_usd=spent,
        remaining_usd=None if limit is None else max(limit - spent, Decimal(0)),
        unknown_cost=run_has_unknown_cost(root),
        raises=list(budget.raises) if budget is not None else [],
        pending=pending,
        history=[item for item in requests if item is not pending],
        policy=BudgetPolicy(max_limit_usd=cap, can_decide=reason is None, reason=reason, decision_hours=hours),
    )


@router.get("/runs/{run_id}/budget", dependencies=READ)
def run_budget(run_id: str, ctx: Ctx, request: Request) -> RunBudgetOut:
    """The budget of a run's top-level run, its open decision and what the caller may decide."""
    return _view(ctx, _root(ctx, run_id), request)


@router.post("/runs/{run_id}/budget/decisions", dependencies=RUN)
async def decide_run_budget(run_id: str, body: BudgetDecisionIn, ctx: Ctx, request: Request) -> RunBudgetOut:
    """Raise exactly the blocking limit of this run and continue it, or end the run."""
    root = _root(ctx, run_id)
    reason = _may_decide(root, request)
    if reason is not None:
        raise AccessError(
            403,
            "access.forbidden",
            "Only the person who started the run or an administrator decides its budget",
            reason=reason,
        )
    cap, _hours = ctx.runs.budget_policy()
    try:
        ctx.runs.decide_budget(
            root.id,
            body.request_id,
            action=body.action,
            by=_actor(request),
            limit_usd=body.limit_usd,
            idempotency_key=body.idempotency_key,
            max_limit_usd=cap,
        )
    except NodestroError as problem:
        if problem.code == "budget.request_not_found":
            raise KeyError(body.request_id) from problem
        status = STATUS.get(problem.code)
        if status is not None:
            params = dict(problem.params)
            if "status" in params:
                params["request_status"] = params.pop("status")
            raise ApiError(status, problem.code, str(problem), **params) from problem
        raise
    return _view(ctx, _root(ctx, root.id), request)


@router.get("/budget-requests", dependencies=READ)
def budget_requests(ctx: Ctx, status: Literal["pending"] = "pending") -> list[BudgetRequest]:
    """The tenant's open budget requests, oldest first (also after a restart)."""
    del status  # only open requests are listed
    return BudgetRequests(ctx.store).pending()
