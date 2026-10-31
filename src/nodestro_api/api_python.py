"""The API of the Python dependency management (``/api/python``).

The flow is always the same: a *status* says what a workflow or tool needs, a *plan* resolves
the versions for the approval overview (nothing is installed), and only *approving* a plan
installs the packages. Removing an environment keeps the approved definition.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.python_envs import (
    DependencyStatus,
    ExportedDefinition,
    Plan,
    PythonEnvironment,
    PythonStatus,
    ScopeKind,
    scope_of,
)
from nodestro_runtime.python_tools import ToolEnvironment
from nodestro_runtime.schemas import WorkflowIn
from pydantic import BaseModel

from .api import Ctx, _workflow
from .permissions import ADMIN, EDIT, READ

router = APIRouter(prefix="/api/python")


class WorkflowDependencyRequest(BaseModel):
    #: The editor's current (possibly unsaved) workflow; the stored one when omitted.
    workflow: WorkflowIn | None = None


class ToolDependencyRequest(BaseModel):
    code: str
    tests: str = ""


@router.get("", dependencies=READ)
def python_status(ctx: Ctx) -> PythonStatus:
    """uv, the environments on disk (with owner and size) and the download cache."""
    return ctx.python.python_status()


def _workflow_needed(ctx: Ctx, workflow_id: str, body: WorkflowDependencyRequest | None) -> tuple[WorkflowIn, str]:
    stored = _workflow(ctx, workflow_id)
    return (body.workflow if body is not None and body.workflow is not None else stored), stored.name


@router.post("/workflows/{workflow_id}/status", dependencies=READ)
def workflow_status(workflow_id: str, ctx: Ctx, body: WorkflowDependencyRequest | None = None) -> DependencyStatus:
    workflow, name = _workflow_needed(ctx, workflow_id, body)
    return ctx.python.status("workflow", workflow_id, ctx.python.needed_for_workflow(workflow), label=name)


@router.post("/workflows/{workflow_id}/plan", dependencies=EDIT)
async def workflow_plan(workflow_id: str, ctx: Ctx, body: WorkflowDependencyRequest | None = None) -> Plan:
    """The approval overview: resolves all versions (may download package metadata), installs nothing."""
    workflow, name = _workflow_needed(ctx, workflow_id, body)
    needed = ctx.python.needed_for_workflow(workflow)
    return await asyncio.to_thread(ctx.python.plan, "workflow", workflow_id, needed, label=name)


@router.get("/workflows/{workflow_id}/export", dependencies=READ)
def workflow_export(workflow_id: str, ctx: Ctx) -> ExportedDefinition | None:
    """The dependency definition that goes into an exported workflow (without approvals)."""
    _workflow(ctx, workflow_id)
    return ctx.python.export(scope_of("workflow", workflow_id))


@router.post("/tools/{name}/status", dependencies=READ)
def tool_status(name: str, body: ToolDependencyRequest, ctx: Ctx) -> DependencyStatus:
    return ToolEnvironment(ctx.python, name, body.code, body.tests).status


@router.post("/tools/{name}/plan", dependencies=EDIT)
async def tool_plan(name: str, body: ToolDependencyRequest, ctx: Ctx) -> Plan:
    needed = ctx.python.needed_for_sources([(name, body.code, body.tests)])
    return await asyncio.to_thread(ctx.python.plan, "tool", name, needed, label=name)


@router.post("/plans/{plan_id}/approve", dependencies=ADMIN)
def approve_plan(plan_id: str, ctx: Ctx) -> PythonEnvironment:
    """The user's approval: store lock and approvals, then install in the background."""
    return ctx.python.approve(plan_id)


@router.get("/environments/{kind}/{ref}", dependencies=READ)
def get_environment(kind: ScopeKind, ref: str, ctx: Ctx) -> PythonEnvironment:
    env = ctx.python.get(scope_of(kind, ref))
    if env is None:
        raise KeyError(ref)
    return env


@router.post("/environments/{kind}/{ref}/install", dependencies=ADMIN)
def reinstall_environment(kind: ScopeKind, ref: str, ctx: Ctx) -> PythonEnvironment:
    """Rebuild the environment from the approved lock (same versions, no new approval)."""
    scope = scope_of(kind, ref)
    modules: set[str] = set()
    if kind == "workflow" and ctx.store.get_raw("workflows", ref) is not None:
        modules = ctx.python.needed_for_workflow(_workflow(ctx, ref)).all_modules()
    return ctx.python.install(scope, modules, wait=False)


@router.delete("/environments/{kind}/{ref}", status_code=204, dependencies=ADMIN)
def remove_environment(kind: ScopeKind, ref: str, ctx: Ctx, forget: bool = False) -> Response:
    """Delete the installed environment; ``forget`` also deletes the definition and approvals."""
    scope = scope_of(kind, ref)
    if kind == "workflow" and ref in ctx.runs.live_workflow_ids():
        raise NodestroError("deps.run_active", "The workflow is running; try again when it has finished")
    if forget:
        ctx.python.forget(scope)
    else:
        ctx.python.remove_environment(scope)
    return Response(status_code=204)


class CleanupResult(BaseModel):
    removed: list[str]


@router.post("/cleanup", dependencies=ADMIN)
async def cleanup(ctx: Ctx) -> CleanupResult:
    """Remove environments no workflow or tool owns any more and prune the download cache."""
    return CleanupResult(removed=await asyncio.to_thread(ctx.python.cleanup))
