"""Missing local models at the API: one check before every interactive action that needs a local model.

The runtime diagnoses (:mod:`nodestro_runtime.local_readiness`); this module adds what only a request knows, namely
whether the person may install anything (``install``, never a server's tenants), and turns a model that is not
ready into ``409 model.local_unavailable`` with the problem and the ready alternatives as parameters, before anything
is stored, sent or charged. The studio shows one dialog for it (download, set up, choose another model, ask an
administrator) and repeats the action with the same inputs afterwards.

Unattended starts (schedules, batches, published APIs) never come here: their runs are refused by the workflow
check (``check.local_model_missing``) and nothing is downloaded for them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Request
from nodestro_runtime.local_readiness import (
    UNAVAILABLE,
    ModelNeeds,
    Readiness,
    check_override,
    is_local,
    readiness,
)
from nodestro_runtime.problems import NodestroError

from .permissions import Permission, Principal
from .serving import ApiError

if TYPE_CHECKING:
    from nodestro_runtime.workspace import Workspace

ReadinessAction = Literal["architect", "assistant", "agent_design", "agent_test", "run", "knowledge_index", "check"]

#: What each action needs of its model, and whether the person may use another ready model for it (once; the
#: saved settings and agents stay as they are). A run uses the models of its agents: changing them is an edit of the
#: workflow, so a run start offers no other model; the knowledge index changes its stored model on a rebuild.
ACTIONS: dict[str, tuple[ModelNeeds, bool]] = {
    "architect": (ModelNeeds(capabilities=["structured_output"]), True),
    "assistant": (ModelNeeds(capabilities=["tools"]), True),
    "agent_design": (ModelNeeds(capabilities=["structured_output"]), False),
    "agent_test": (ModelNeeds(), True),
    "run": (ModelNeeds(), False),
    "knowledge_index": (ModelNeeds(kind="embedding"), False),
    "check": (ModelNeeds(), False),
}


def can_install(ctx: Workspace, request: Request) -> bool:
    """Whether this person may download models or set up the local model server here: the permission ``install``,
    and never on a server (its model server belongs to the operator)."""
    if ctx.local_models.profile == "server":
        return False
    principal: Principal | None = getattr(request.state, "principal", None)
    return principal is not None and principal.allows(Permission.INSTALL)


def needs_of(action: str, capabilities: Iterable[str] | None = None) -> ModelNeeds:
    needs, _ = ACTIONS.get(action, ACTIONS["check"])
    if capabilities:
        needs = needs.model_copy(update={"capabilities": sorted({*needs.capabilities, *capabilities})})
    return needs


def check(
    ctx: Workspace, request: Request, model: str, action: str, capabilities: Iterable[str] | None = None
) -> Readiness:
    """The readiness of ``model`` for ``action`` as this person sees it (blocking: run it off the event loop)."""
    _, switchable = ACTIONS.get(action, ACTIONS["check"])
    return readiness(
        ctx,
        model,
        action=action,
        needs=needs_of(action, capabilities),
        can_install=can_install(ctx, request),
        switchable=switchable,
    )


def _refusal(found: Readiness, *, models: list[str] | None = None) -> ApiError:
    problem = found.problem
    assert problem is not None
    params: dict[str, Any] = {
        "model": problem.model,
        "cause": problem.cause,
        "problem": problem.model_dump(mode="json"),
        "alternatives": [item.model_dump(mode="json") for item in found.alternatives],
    }
    if models:
        params["models"] = models
    return ApiError(409, UNAVAILABLE, f"{problem.model} cannot be used now ({problem.cause})", **params)


async def require_local(
    ctx: Workspace, request: Request, model: str, action: str, capabilities: Iterable[str] | None = None
) -> None:
    """Refuse an interactive action whose local model is not ready (``409 model.local_unavailable``); other models
    pass (their providers are checked by the action itself)."""
    if not is_local(model):
        return
    found = await asyncio.to_thread(check, ctx, request, model, action, capabilities)
    if found.problem is not None:
        raise _refusal(found)


def require_local_now(
    ctx: Workspace, request: Request, model: str, action: str, capabilities: Iterable[str] | None = None
) -> None:
    """:func:`require_local` for routes that already run in a worker thread."""
    if not is_local(model):
        return
    found = check(ctx, request, model, action, capabilities)
    if found.problem is not None:
        raise _refusal(found)


async def require_all_local(ctx: Workspace, request: Request, models: Iterable[str], action: str) -> None:
    """The same for several models (a run's agents, judges, fallbacks and orchestrators): the first one that is not
    ready is refused, ``models`` names all of them."""
    local = sorted({model for model in models if is_local(model)})
    if not local:
        return

    def first() -> tuple[Readiness | None, list[str]]:
        problems = [found for found in (check(ctx, request, model, action) for model in local) if found.problem]
        return (problems[0] if problems else None, [found.model for found in problems])

    found, missing = await asyncio.to_thread(first)
    if found is not None:
        raise _refusal(found, models=missing)


async def chosen_model(ctx: Workspace, chosen: str | None, configured: str, action: str) -> str:
    """The model an action uses: the configured one, or one the person chose for this action from the alternatives
    (``model.choice_invalid`` otherwise). The choice is never stored."""
    if not chosen or chosen == configured:
        return configured
    needs = needs_of(action)
    try:
        return await asyncio.to_thread(check_override, ctx, chosen, configured, needs=needs)
    except NodestroError as exc:
        raise ApiError(409, exc.code, str(exc), **exc.params) from None


__all__ = [
    "ACTIONS",
    "ReadinessAction",
    "can_install",
    "check",
    "chosen_model",
    "require_all_local",
    "require_local",
    "require_local_now",
]
