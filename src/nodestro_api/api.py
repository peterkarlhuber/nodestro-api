"""The REST API (``/api``) and the Server-Sent Events stream of a run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
import nodestro_examples as library
from nodestro_examples import samples as library_samples
from nodestro_runtime.agent_builder import ConfigIssue
from nodestro_runtime.agent_test import AgentTestIn, AgentTestResult
from nodestro_runtime.catalog import SERVER_TOOLS, ModelInfo, ServerToolInfo, models
from nodestro_runtime.knowledge import KnowledgeFile
from nodestro_runtime.launcher import launch
from nodestro_runtime.memory_spaces import MemoryFile, MemorySpaceInfo, check_space_name
from nodestro_runtime.preflight import WorkflowCheck, preflight
from nodestro_runtime.preview import ContextPreview, PreviewRequest, build_preview, latest_run
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import CAPABILITY_NAMES, PROVIDERS, ProviderInfo, qualify
from nodestro_runtime.python_envs import ExportedDefinition, scope_of
from nodestro_runtime.python_tools import check_with_dependencies, save_with_dependencies, tool_run_sandbox
from nodestro_runtime.reports import FileRequest, ResultFile
from nodestro_runtime.resume import ResumePlan
from nodestro_runtime.resume import plan as plan_resume
from nodestro_runtime.resume import resume as resume_from
from nodestro_runtime.run_views import PendingApproval, models_of_run, pending_approvals
from nodestro_runtime.sanitize import sanitize_run
from nodestro_runtime.schemas import (
    AgentConfig,
    AgentTemplate,
    AgentTemplateIn,
    Language,
    Run,
    RunIn,
    RunSummary,
    Settings,
    SettingsIn,
    Workflow,
    WorkflowIn,
    WorkflowSummary,
    now,
)
from nodestro_runtime.tool_library import (
    CUSTOM_TESTS_TEMPLATE,
    CUSTOM_TOOL_TEMPLATE,
    CheckResult,
    DependencyChoice,
    ToolInfo,
    ToolJob,
)
from nodestro_runtime.traces import MAX_TRACE_BYTES, TraceImportError, import_trace
from nodestro_runtime.usage_log import USAGE_SOURCES, UsageEntry
from nodestro_runtime.versions import VersionSummary, WorkflowVersion
from orkexa import OrkexaError, ProviderError, UsageTracker
from orkexa import __version__ as library_version
from pydantic import BaseModel, Field

from . import __version__
from .assistants import AgentDraft, MemorySeed
from .benchmarks import SuiteIn, TestCase
from .context import Nodestro
from .permissions import ADMIN, EDIT, PUBLIC, READ, RUN, AccessError
from .serving import ApiError

router = APIRouter(prefix="/api")


def app_context(request: Request) -> Nodestro:
    """The context a route works on: the desktop's one context, or the tenant of the request's principal.

    On a server (``app.state.tenants``) the tenant comes only from the principal that the route's access
    requirement resolved, so a route can never reach another tenant's data."""
    tenants = getattr(request.app.state, "tenants", None)
    if tenants is None:
        return request.app.state.nodestro  # type: ignore[no-any-return]
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise AccessError(401, "access.login_required", "Log in to use this part of Nodestro")
    return tenants.get(principal.tenant_id)  # type: ignore[no-any-return]


Ctx = Annotated[Nodestro, Depends(app_context)]


def _error(status_code: int, code: str, detail: str, **params: Any) -> JSONResponse:
    """An error response: ``detail`` is the technical text, ``code``/``params`` the UI message."""
    return JSONResponse({"detail": detail, "code": code, "params": params}, status_code=status_code)


def install_error_handlers(app: FastAPI) -> None:
    async def nodestro_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, NodestroError)
        return _error(400, exc.code, str(exc), **exc.params)

    async def bad_request(_: Request, exc: Exception) -> JSONResponse:
        return _error(400, "bad_request", str(exc))

    async def not_found(_: Request, exc: Exception) -> JSONResponse:
        name = str(exc.args[0]) if exc.args else ""
        return _error(404, "not_found", f"Not found: {name}", name=name)

    async def upstream(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ProviderError)
        return _error(
            502, "provider_api", f"{exc.provider} API: {exc}", status=exc.status_code or "", provider=exc.provider
        )

    async def agent_error(_: Request, exc: Exception) -> JSONResponse:
        kind = type(exc).__name__
        return _error(422, f"agent.{kind}", f"{kind}: {exc}", type=kind)

    async def api_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return _error(exc.status, exc.code, exc.message, **exc.params)

    async def access_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, AccessError)
        return _error(exc.status, exc.code, str(exc), **exc.params)

    app.add_exception_handler(NodestroError, nodestro_error)
    app.add_exception_handler(ApiError, api_error)
    app.add_exception_handler(AccessError, access_error)
    app.add_exception_handler(ValueError, bad_request)
    app.add_exception_handler(PermissionError, bad_request)
    app.add_exception_handler(KeyError, not_found)
    app.add_exception_handler(ProviderError, upstream)
    app.add_exception_handler(OrkexaError, agent_error)


# ---- meta and settings ----------------------------------------------------------


class Meta(BaseModel):
    version: str
    library_version: str
    #: The providers Nodestro offers (what each needs); their settings are part of ``/settings``.
    providers: list[ProviderInfo]
    #: Capabilities that can be declared for OpenAI-compatible endpoints.
    capabilities: list[str]
    models: list[ModelInfo]
    server_tools: list[ServerToolInfo]
    efforts: list[str] = ["low", "medium", "high", "xhigh", "max"]


@router.get("/health", dependencies=PUBLIC)
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/meta", dependencies=READ)
def meta() -> Meta:
    return Meta(
        version=__version__,
        library_version=library_version,
        providers=list(PROVIDERS.values()),
        capabilities=list(CAPABILITY_NAMES),
        models=models(),
        server_tools=list(SERVER_TOOLS.values()),
    )


@router.get("/settings", dependencies=READ)
def get_settings(ctx: Ctx) -> Settings:
    return ctx.settings()


@router.put("/settings", dependencies=ADMIN)
def put_settings(update: SettingsIn, ctx: Ctx) -> Settings:
    return ctx.update_settings(update)


# ---- workflows ------------------------------------------------------------------


def _workflow(ctx: Nodestro, workflow_id: str) -> Workflow:
    workflow = ctx.store.get("workflows", workflow_id, Workflow)
    if workflow is None:
        raise KeyError(workflow_id)
    return workflow


def request_language(request: Request) -> str:
    """The interface language the frontend sends (``Accept-Language``), for names it creates."""
    header = request.headers.get("accept-language", "")
    for part in header.split(","):
        tag = part.split(";")[0].strip()
        if tag in ("pt-BR", "pt-br", "pt"):
            return "pt-BR"
        if tag[:2] in ("de", "en", "es", "fr", "it"):
            return tag[:2]
    return "en"


@router.get("/workflows", dependencies=READ)
def list_workflows(ctx: Ctx) -> list[WorkflowSummary]:
    last_status: dict[str, Any] = {}
    for run in ctx.store.list("runs", RunSummary, limit=500):
        if run.imported_at is None:
            last_status.setdefault(run.workflow_id, run.status)
    return [
        WorkflowSummary(
            id=w.id,
            name=w.name,
            description=w.description,
            agents=sum(node.type == "agent" for node in w.nodes),
            updated_at=w.updated_at,
            last_run_status=last_status.get(w.id),
        )
        for w in ctx.store.list("workflows", Workflow)
    ]


@router.post("/workflows", status_code=201, dependencies=EDIT)
def create_workflow(body: WorkflowIn, ctx: Ctx) -> Workflow:
    return ctx.save_workflow(body)


class WorkflowImport(WorkflowIn):
    #: The Python libraries of an exported workflow (no approvals: they are given again here).
    python: ExportedDefinition | None = None


@router.post("/workflows/import", status_code=201, dependencies=EDIT)
def import_workflow(body: WorkflowImport, ctx: Ctx) -> Workflow:
    """Import an exported workflow as a new copy (new id)."""
    workflow = create_workflow(WorkflowIn(**body.model_dump(exclude={"python"})), ctx)
    if body.python is not None:
        ctx.python.adopt("workflow", workflow.id, body.python, label=workflow.name, change="imported")
    return workflow


@router.get("/workflows/{workflow_id}", dependencies=READ)
def get_workflow(workflow_id: str, ctx: Ctx) -> Workflow:
    return _workflow(ctx, workflow_id)


@router.put("/workflows/{workflow_id}", dependencies=EDIT)
def update_workflow(workflow_id: str, body: WorkflowIn, ctx: Ctx, note: str = Query("", max_length=200)) -> Workflow:
    """Save a workflow; a changed workflow becomes the next version (``note`` describes it)."""
    current = _workflow(ctx, workflow_id)
    workflow = Workflow(**body.model_dump(), id=current.id, created_at=current.created_at, updated_at=now())
    ctx.store.put("workflows", workflow.id, workflow)
    ctx.versions.record(workflow, description=note, previous=current)
    return workflow


@router.delete("/workflows/{workflow_id}", status_code=204, dependencies=EDIT)
def delete_workflow(workflow_id: str, ctx: Ctx) -> Response:
    if not ctx.store.delete("workflows", workflow_id):
        raise KeyError(workflow_id)
    ctx.python.forget(scope_of("workflow", workflow_id))
    ctx.versions.delete_all(workflow_id)
    ctx.organize.forget("workflow", workflow_id)
    # Schedules and test series of the workflow cannot run any more.
    for schedule in ctx.schedules.list():
        if schedule.workflow_id == workflow_id:
            ctx.schedules.delete(schedule.id)
    for suite in ctx.benchmarks.suites(workflow_id):
        ctx.benchmarks.delete_suite(suite.id)
    # Its published APIs end with it; keys keep working for their other APIs.
    for publication_id in ctx.publications.forget_workflow(workflow_id):
        ctx.api_keys.forget_publication(publication_id)
    return Response(status_code=204)


# ---- workflow versions ----------------------------------------------------------


class VersionNote(BaseModel):
    description: str = Field(default="", max_length=200)


@router.get("/workflows/{workflow_id}/versions", dependencies=READ)
def list_versions(workflow_id: str, ctx: Ctx) -> list[VersionSummary]:
    return ctx.versions.summaries(_workflow(ctx, workflow_id))


@router.get("/workflows/{workflow_id}/versions/{number}", dependencies=READ)
def get_version(workflow_id: str, number: int, ctx: Ctx) -> WorkflowVersion:
    _workflow(ctx, workflow_id)
    return ctx.versions.get(workflow_id, number)


@router.patch("/workflows/{workflow_id}/versions/{number}", dependencies=EDIT)
def describe_version(workflow_id: str, number: int, body: VersionNote, ctx: Ctx) -> WorkflowVersion:
    _workflow(ctx, workflow_id)
    return ctx.versions.describe(workflow_id, number, body.description)


@router.post("/workflows/{workflow_id}/versions/{number}/restore", dependencies=EDIT)
def restore_version(workflow_id: str, number: int, ctx: Ctx) -> Workflow:
    """Make a saved version current again; it is saved as a new version, nothing is lost."""
    current = _workflow(ctx, workflow_id)
    version = ctx.versions.get(workflow_id, number)
    workflow = Workflow(**version.snapshot.model_dump(), id=current.id, created_at=current.created_at, updated_at=now())
    ctx.store.put("workflows", workflow.id, workflow)
    ctx.versions.record(workflow, restored_from=number, previous=current)
    return workflow


_COPY = {"de": "Kopie", "en": "copy", "es": "copia", "fr": "copie", "pt-BR": "cópia", "it": "copia"}


@router.post("/workflows/{workflow_id}/duplicate", status_code=201, dependencies=EDIT)
def duplicate_workflow(workflow_id: str, request: Request, ctx: Ctx) -> Workflow:
    source = _workflow(ctx, workflow_id)
    body = WorkflowIn(**source.model_dump(exclude={"id", "created_at", "updated_at"}))
    body.name = f"{source.name} ({_COPY[request_language(request)]})"
    workflow = create_workflow(body, ctx)
    definition = ctx.python.export(scope_of("workflow", source.id))
    if definition is not None:
        ctx.python.adopt("workflow", workflow.id, definition, label=workflow.name, change="copied")
    return workflow


# ---- example library ------------------------------------------------------------


@router.get("/library", dependencies=READ)
def list_library(request: Request, language: Language | None = None) -> list[library.TemplateSummary]:
    """The example templates in one language (default: the interface language); nothing is stored."""
    return library.summaries(language or request_language(request))


@router.get("/library/{template_id}", dependencies=READ)
def get_library_template(
    template_id: str, request: Request, language: Language | None = None
) -> library.TemplateDetail:
    """One template with guide, agents and the workflow a copy would get (a preview, never a run)."""
    return library.detail(template_id, language or request_language(request))


@router.get("/library/{template_id}/samples/{sample_id}", dependencies=READ)
def download_library_sample(
    template_id: str, sample_id: str, request: Request, language: Language | None = None
) -> Response:
    """A synthetic sample file of a template for one of its file fields (built locally, nothing is stored)."""
    name, media_type, data = library_samples.render(template_id, sample_id, language or request_language(request))
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(name)}"},
    )


class LibraryCopy(BaseModel):
    #: The language of the copy; the interface language when empty.
    language: Language | None = None
    #: Also create the template's test cases as a test series of the copy (nothing is run).
    tests: bool = False


@router.post("/library/{template_id}/copy", status_code=201, dependencies=EDIT)
def copy_library_template(template_id: str, body: LibraryCopy, request: Request, ctx: Ctx) -> Workflow:
    """Create an ordinary, independent workflow from a template; the template itself never changes.

    Parts the template runs as sub-workflows are copied first, as workflows of their own; the
    copy's sub-workflow nodes are pinned to their first version. With ``tests`` the template's
    test cases become a test series of the copy; their sample files are stored as input files.
    """
    language = body.language or request_language(request)
    workflow = library.build(template_id, language)
    sensitive = library.structure(template_id).sensitive
    copies: dict[str, tuple[str, int | None]] = {}
    for part_id in library.part_ids(template_id):
        child = create_workflow(library.build_part(part_id, language, sensitive=sensitive), ctx)
        copies[part_id] = (child.id, ctx.current_version(child))
    copy = create_workflow(library.link_parts(workflow, copies), ctx)
    if body.tests and library.structure(template_id).tests:

        def store(name: str, media_type: str, data: bytes) -> str:
            return ctx.inputs.add(data, name=name, media_type=media_type, origin="test").id

        text = library.texts(template_id, language)
        ctx.benchmarks.save_suite(
            SuiteIn(
                name=text.name,
                description=text.description,
                workflow_id=copy.id,
                cases=[TestCase(**case) for case in library_samples.suite_cases(template_id, language, copy, store)],
            )
        )
    return copy


@router.post("/workflows/check", dependencies=READ)
def check(body: WorkflowIn, ctx: Ctx, workflow_id: str | None = None) -> WorkflowCheck:
    """Everything that blocks or affects a run; free, no request to the API.

    ``workflow_id`` names the stored workflow the body belongs to (for its Python environment).
    """
    workflow = Workflow(**body.model_dump())
    if workflow_id:
        workflow.id = workflow_id
    return preflight(ctx, workflow)


@router.post("/workflows/preview", dependencies=READ)
def preview(body: PreviewRequest, ctx: Ctx) -> ContextPreview:
    """What an agent would receive: prompt with filled placeholders, inputs and context."""
    workflow = Workflow(**body.workflow.model_dump(), id=body.workflow_id or "preview")
    run = None
    if body.run_id:
        run = ctx.runs.get(body.run_id)
        if run is None:
            raise KeyError(body.run_id)
    elif body.workflow_id:
        run = latest_run(ctx, body.workflow_id)
    return build_preview(ctx, workflow, body.node_id, given_input=body.input, run=run, given_values=body.values)


# ---- runs -----------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/runs", status_code=201, dependencies=RUN)
async def start_run(workflow_id: str, body: RunIn, ctx: Ctx) -> Run:
    return await launch(ctx, _workflow(ctx, workflow_id), body)


@router.get("/runs", dependencies=READ)
def list_runs(
    ctx: Ctx,
    workflow_id: str | None = None,
    limit: int = Query(100, le=500),
    include_children: bool = False,
    trigger_ref: str | None = None,
) -> list[RunSummary]:
    """Stored runs, newest first; sub-workflow runs only with ``include_children`` (they belong to a parent)."""
    filtered = workflow_id is not None or not include_children or trigger_ref is not None
    runs = ctx.store.list("runs", RunSummary, limit=2000 if filtered else limit)
    if not include_children:
        runs = [run for run in runs if run.parent_run_id is None]
    if workflow_id is not None:
        runs = [run for run in runs if run.workflow_id == workflow_id]
    if trigger_ref is not None:
        runs = [run for run in runs if run.trigger_ref == trigger_ref]
    return sorted(runs, key=lambda run: run.created_at, reverse=True)[:limit]


class TraceImport(BaseModel):
    run: RunSummary
    duplicate: bool


@router.post("/runs/import", dependencies=EDIT)
async def import_run(request: Request, response: Response, ctx: Ctx) -> TraceImport:
    """Store an exported trace (the JSON of ``GET /runs/{id}``) for viewing; nothing runs."""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_TRACE_BYTES:
        raise TraceImportError("trace.too_large", "The file is too large: a trace may have at most 50 MB.", limit_mb=50)
    run, duplicate = import_trace(ctx.store, await request.body())
    response.status_code = 200 if duplicate else 201
    return TraceImport(run=RunSummary.model_validate(run.model_dump()), duplicate=duplicate)


@router.get("/runs/{run_id}", dependencies=READ)
def get_run(run_id: str, ctx: Ctx) -> Run:
    run = ctx.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    return run


@router.get("/runs/{run_id}/export", dependencies=READ)
def export_run(
    run_id: str,
    ctx: Ctx,
    mode: str = Query("full", pattern="^(full|sanitized)$"),
    personal_data: bool = True,
    contents: bool = False,
) -> JSONResponse:
    """The run as a trace file; ``sanitized`` removes credentials and more in the copy only."""
    run = ctx.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    data = run.model_dump(mode="json")
    if mode == "sanitized":
        data = sanitize_run(data, personal_data=personal_data, contents=contents)
    suffix = "_sanitized" if mode == "sanitized" else ""
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="run_{run.id}{suffix}.json"'})


@router.get("/runs/{run_id}/files", dependencies=READ)
def list_result_files(run_id: str, ctx: Ctx) -> list[ResultFile]:
    """The result files of a run (written by its result nodes or created later)."""
    if ctx.runs.get(run_id) is None:
        raise KeyError(run_id)
    return ctx.files.list(run_id)


@router.post("/runs/{run_id}/files", status_code=201, dependencies=EDIT)
async def create_result_file(run_id: str, body: FileRequest, ctx: Ctx) -> ResultFile:
    """Write the run's result (or one result node's) as a file; the run must have finished."""
    run = ctx.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)
    if ctx.runs.is_live(run_id) or run.status in ("queued", "running", "paused"):
        raise NodestroError("report.run_not_finished", "Files can be written once the run has ended")
    return await asyncio.to_thread(ctx.files.create, run, body)


@router.get("/runs/{run_id}/files/{file_id}", dependencies=READ)
def download_result_file(run_id: str, file_id: str, ctx: Ctx, inline: bool = False) -> Response:
    entry, data = ctx.files.content(run_id, file_id)
    disposition = "inline" if inline and entry.format in ("html", "pdf", "md") else "attachment"
    headers = {"Content-Disposition": f"{disposition}; filename*=UTF-8''{_quote(entry.name)}", **_NOSNIFF}
    if entry.format == "html":
        # A generated page is shown without scripts or remote content.
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return Response(content=data, media_type=entry.media_type, headers=headers)


@router.delete("/runs/{run_id}/files/{file_id}", status_code=204, dependencies=EDIT)
def delete_result_file(run_id: str, file_id: str, ctx: Ctx) -> Response:
    ctx.files.delete(run_id, file_id)
    return Response(status_code=204)


@router.post("/runs/{run_id}/cancel", dependencies=RUN)
async def cancel_run(run_id: str, ctx: Ctx) -> dict[str, bool]:
    return {"cancelled": ctx.runs.cancel(run_id)}


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=4000)


class ResumeRequest(BaseModel):
    #: ``continue`` reuses what can be reused; ``restart`` starts the workflow anew with the same input.
    mode: Literal["continue", "restart"] = "continue"
    #: Steps that run again had called tools before; the user agreed that they may be called again.
    confirm_side_effects: bool = False


@router.get("/runs/{run_id}/resume", dependencies=READ)
def resume_plan(run_id: str, ctx: Ctx) -> ResumePlan:
    """What continuing a failed run would reuse and run again; nothing starts."""
    return plan_resume(ctx, run_id)


@router.post("/runs/{run_id}/resume", status_code=201, dependencies=RUN)
async def resume_run(run_id: str, body: ResumeRequest, ctx: Ctx) -> Run:
    """A new run linked to the failed one, reusing its safe results (the failed run stays as it was)."""
    return await resume_from(ctx, run_id, mode=body.mode, confirm=body.confirm_side_effects)


@router.get("/approvals", dependencies=READ)
def list_approvals(ctx: Ctx) -> list[PendingApproval]:
    """Every approval that waits for a person, oldest first (also after a restart)."""
    return pending_approvals(ctx)


@router.post("/runs/{run_id}/approvals/{node_id}", dependencies=RUN)
async def decide_approval(run_id: str, node_id: str, body: ApprovalDecision, ctx: Ctx) -> Run:
    """Approve or reject at an approval node; the run (its top-level run) continues right away."""
    return ctx.runs.decide(run_id, node_id, body.decision, body.comment)


@router.delete("/runs/{run_id}", status_code=204, dependencies=EDIT)
def delete_run(run_id: str, ctx: Ctx) -> Response:
    if ctx.runs.is_live(run_id) or ctx.runs.get(run_id) in ctx.runs.live_runs():
        raise NodestroError("run.delete_running", "A running run cannot be deleted")
    run = ctx.runs.get(run_id)
    if run is None or not ctx.store.delete("runs", run_id):
        raise KeyError(run_id)
    # The runs of its sub-workflows hold copies of its inputs and results: they go with it.
    tree = [run, *_child_runs(ctx, run)]
    for item in tree:
        ctx.store.delete("runs", item.id)
        ctx.files.delete_run(item.id)
    # Input files only these runs used go with them (knowledge files are never touched).
    ctx.inputs.release([file_id for item in tree for file_id in item.file_ids])
    return Response(status_code=204)


def _child_runs(ctx: Nodestro, run: Run) -> list[Run]:
    """The runs of a run's sub-workflows, at any depth (not the earlier runs a continued run reuses)."""
    found: list[Run] = []
    queue = [run]
    seen = {run.id}
    while queue:
        parent = queue.pop()
        for state in parent.nodes.values():
            if not state.child_run_id or state.child_run_id in seen:
                continue
            child = ctx.store.get("runs", state.child_run_id, Run)
            if child is not None and child.parent_run_id == parent.id:
                seen.add(child.id)
                found.append(child)
                queue.append(child)
    return found


def _sse(kind: str, data: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/runs/{run_id}/events", dependencies=READ)
async def run_events(run_id: str, request: Request, ctx: Ctx) -> StreamingResponse:
    """Server-Sent Events: a ``snapshot`` first, then every event until ``run_finished``."""
    run = ctx.runs.get(run_id)
    if run is None:
        raise KeyError(run_id)

    async def stream() -> AsyncIterator[str]:
        if not ctx.runs.is_live(run_id):
            yield _sse("snapshot", run.model_dump(mode="json"))
            yield _sse("end", {"status": run.status})
            return
        history, queue = ctx.runs.subscribe(run_id)
        try:
            yield _sse("snapshot", ctx.runs.get(run_id).model_dump(mode="json"))  # type: ignore[union-attr]
            last = history[-1].seq if history else 0
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
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
            ctx.runs.unsubscribe(run_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class UsageBucket(BaseModel):
    key: str
    label: str
    runs: int = 0
    #: AI operations outside workflow runs (agent tests, AI assistants, tool generation).
    operations: int = 0
    requests: int = 0
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    #: Requests whose model has no known price (their cost is missing from the sum).
    unpriced: int = 0


class UsageReport(BaseModel):
    total: UsageBucket
    by_day: list[UsageBucket]
    by_workflow: list[UsageBucket]
    by_model: list[UsageBucket]
    by_source: list[UsageBucket]
    #: The latest AI operations outside workflow runs, newest first.
    recent: list[UsageEntry]


def _add(bucket: UsageBucket, usage: Any, *, run: bool = False) -> None:
    if run:
        bucket.runs += 1
    else:
        bucket.operations += 1
    bucket.requests += usage.requests
    bucket.total_tokens += usage.total_tokens
    bucket.input_tokens += usage.input_tokens
    bucket.output_tokens += usage.output_tokens
    if usage.estimated_cost_usd is None:
        bucket.unpriced += usage.requests
    else:
        bucket.estimated_cost_usd += usage.estimated_cost_usd


@router.get("/usage", dependencies=READ)
def usage(ctx: Ctx, days: int = Query(30, ge=1, le=365)) -> UsageReport:
    """Tokens and cost of every AI request Nodestro made: workflow runs plus everything else.

    Workflow runs are counted from the stored runs (imported traces excluded); agent tests,
    the AI assistants and tool generation from the usage ledger. Neither contains the other,
    so nothing is counted twice.
    """
    total = UsageBucket(key="total", label="Total")
    by_day: dict[str, UsageBucket] = {}
    by_workflow: dict[str, UsageBucket] = {}
    by_model: dict[str, UsageBucket] = {}
    by_source: dict[str, UsageBucket] = {
        key: UsageBucket(key=key, label=key) for key in ("workflow_run", "api_run", *USAGE_SOURCES)
    }
    cutoff = now().timestamp() - days * 86400

    def day_bucket(day: str) -> UsageBucket:
        return by_day.setdefault(day, UsageBucket(key=day, label=day))

    def model_bucket(model: str) -> UsageBucket:
        # Bare Claude ids and qualified names land in the same bucket.
        try:
            key = qualify(model)
        except NodestroError:
            key = model
        return by_model.setdefault(key, UsageBucket(key=key, label=key))

    for run in ctx.store.list("runs", Run, limit=2000):
        # Imported traces were paid elsewhere; a sub-workflow's run is counted in its parent run.
        if run.created_at.timestamp() < cutoff or run.imported_at is not None or run.parent_run_id is not None:
            continue
        day = run.created_at.date().isoformat()
        buckets = [
            total,
            day_bucket(day),
            by_workflow.setdefault(run.workflow_id, UsageBucket(key=run.workflow_id, label=run.workflow_name)),
            # Runs of published APIs are shown on their own (they are workflow runs all the same).
            by_source["api_run" if run.trigger == "api" else "workflow_run"],
        ]
        for bucket in buckets:
            _add(bucket, run.usage, run=True)
        for model, model_usage in models_of_run(ctx, run).items():
            _add(model_bucket(model), model_usage, run=True)
    since = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    entries = ctx.usage_log.entries(since)
    for entry in entries:
        for bucket in (
            total,
            day_bucket(entry.at.date().isoformat()),
            by_source[entry.source],
            model_bucket(entry.model),
        ):
            _add(bucket, entry.usage)
    return UsageReport(
        total=total,
        by_day=sorted(by_day.values(), key=lambda b: b.key),
        by_workflow=sorted(by_workflow.values(), key=lambda b: b.estimated_cost_usd, reverse=True),
        by_model=sorted(by_model.values(), key=lambda b: b.estimated_cost_usd, reverse=True),
        by_source=[bucket for bucket in by_source.values() if bucket.runs or bucket.operations],
        recent=entries[:25],
    )


# ---- agents ---------------------------------------------------------------------


@router.post("/agents/validate", dependencies=READ)
def validate_agent(config: AgentConfig, ctx: Ctx) -> list[ConfigIssue]:
    return ctx.builder.validate(config)


class DesignRequest(BaseModel):
    description: str = Field(min_length=1)
    existing_agents: list[str] = Field(default_factory=list)
    #: The interface language: the default language of the generated texts.
    language: Language = "en"


@router.post("/agents/design", dependencies=RUN)
def design_agent(body: DesignRequest, ctx: Ctx) -> AgentDraft:
    settings = ctx.settings()
    connection = ctx.connect_model(settings.assistant_model)
    tracker = UsageTracker()
    try:
        return ctx.assistants.design_agent(
            body.description,
            connection=connection,
            model=settings.assistant_model,
            allowed_models=ctx.allowed_models(),
            existing_agents=body.existing_agents,
            default_model=settings.default_model,
            language=body.language,
            tracker=tracker,
        )
    finally:
        ctx.usage_log.record("agent_design", tracker.records, label=body.description)


@router.post("/agents/test", dependencies=RUN)
async def test_agent(body: AgentTestIn, ctx: Ctx) -> AgentTestResult:
    """Run one agent with its current configuration on a test task; not a workflow run.

    The result is returned, never stored as a run; its usage goes to the ledger.
    """
    blocking = [issue for issue in ctx.builder.validate(body.agent) if issue.level == "error"]
    if blocking:
        raise NodestroError(
            "run.agents_invalid",
            "; ".join(issue.message for issue in blocking),
            agents=body.agent.name,
        )
    models = {body.agent.model}
    if body.agent.quality.enabled:
        models.add(body.agent.quality.judge_model)
    ctx.require_providers(models)  # fail fast: agent and judge need their providers
    result, tracker = await asyncio.to_thread(ctx.tester.run, body)
    ctx.usage_log.record("agent_test", tracker.records, label=body.agent.name, ref=result.id)
    return result


@router.get("/agents/templates", dependencies=READ)
def list_templates(ctx: Ctx) -> list[AgentTemplate]:
    return ctx.store.list("templates", AgentTemplate)


@router.post("/agents/templates", status_code=201, dependencies=EDIT)
def create_template(body: AgentTemplateIn, ctx: Ctx) -> AgentTemplate:
    template = AgentTemplate(agent=body.agent)
    ctx.store.put("templates", template.id, template)
    return template


@router.put("/agents/templates/{template_id}", dependencies=EDIT)
def update_template(template_id: str, body: AgentTemplateIn, ctx: Ctx) -> AgentTemplate:
    current = ctx.store.get("templates", template_id, AgentTemplate)
    if current is None:
        raise KeyError(template_id)
    template = AgentTemplate(agent=body.agent, id=template_id, created_at=current.created_at, updated_at=now())
    ctx.store.put("templates", template_id, template)
    return template


@router.delete("/agents/templates/{template_id}", status_code=204, dependencies=EDIT)
def delete_template(template_id: str, ctx: Ctx) -> Response:
    if not ctx.store.delete("templates", template_id):
        raise KeyError(template_id)
    ctx.organize.forget("template", template_id)
    return Response(status_code=204)


# ---- tools ----------------------------------------------------------------------


class ToolSource(BaseModel):
    function_name: str = Field(min_length=1)
    code: str
    tests: str


class SaveResult(BaseModel):
    check: CheckResult
    tool: ToolInfo | None


class ToolRunRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class ToolRunResult(BaseModel):
    output: str
    is_error: bool


class JobRequest(BaseModel):
    requirement: str = Field(min_length=1)


class Decision(BaseModel):
    approved: bool


@router.get("/tools", dependencies=READ)
def list_tools(ctx: Ctx) -> list[ToolInfo]:
    return ctx.tools.all()


@router.get("/tools/template", dependencies=READ)
def tool_template() -> ToolSource:
    return ToolSource(function_name="celsius_to_fahrenheit", code=CUSTOM_TOOL_TEMPLATE, tests=CUSTOM_TESTS_TEMPLATE)


@router.post("/tools/check", dependencies=RUN)
async def check_tool(body: ToolSource, ctx: Ctx) -> CheckResult:
    return await asyncio.to_thread(
        check_with_dependencies, ctx.tools, ctx.python, body.code, body.tests, body.function_name
    )


@router.post("/tools", dependencies=RUN)
async def save_tool(body: ToolSource, ctx: Ctx) -> SaveResult:
    result, tool = await asyncio.to_thread(
        save_with_dependencies, ctx.tools, ctx.python, body.code, body.tests, body.function_name
    )
    return SaveResult(check=result, tool=tool)


@router.get("/tools/jobs", dependencies=READ)
def list_jobs(ctx: Ctx) -> list[ToolJob]:
    return ctx.tools.list_jobs()


@router.post("/tools/jobs", status_code=201, dependencies=RUN)
def start_job(body: JobRequest, ctx: Ctx) -> ToolJob:
    model = ctx.settings().assistant_model
    connection = ctx.connect_model(model)  # fail fast without provider settings

    def record(job: ToolJob, tracker: UsageTracker) -> None:
        ctx.usage_log.record("tool_generation", tracker.records, label=job.requirement, ref=job.id)

    return ctx.tools.start_job(body.requirement, connection=connection, model=model, on_finished=record)


@router.get("/tools/jobs/{job_id}", dependencies=READ)
def get_job(job_id: str, ctx: Ctx) -> ToolJob:
    job = ctx.tools.jobs.get(job_id)
    if job is None:
        raise KeyError(job_id)
    return job


@router.post("/tools/jobs/{job_id}/decision", dependencies=EDIT)
def decide_job(job_id: str, body: Decision, ctx: Ctx) -> ToolJob:
    return ctx.tools.decide(job_id, body.approved)


class DependencyDecision(BaseModel):
    #: ``approve`` installs the libraries, ``without`` asks for a standard-library version.
    choice: DependencyChoice


@router.post("/tools/jobs/{job_id}/dependencies", dependencies=ADMIN)
def decide_job_dependencies(job_id: str, body: DependencyDecision, ctx: Ctx) -> ToolJob:
    return ctx.tools.decide_dependencies(job_id, body.choice)


@router.get("/tools/{name}", dependencies=READ)
def get_tool(name: str, ctx: Ctx) -> ToolInfo:
    return ctx.tools.get(name)


@router.delete("/tools/{name}", status_code=204, dependencies=EDIT)
def delete_tool(name: str, ctx: Ctx) -> Response:
    ctx.tools.delete(name)
    ctx.organize.forget("tool", name)
    ctx.python.forget(scope_of("tool", name))
    return Response(status_code=204)


@router.post("/tools/{name}/run", dependencies=RUN)
def run_tool(name: str, body: ToolRunRequest, ctx: Ctx) -> ToolRunResult:
    output, is_error = ctx.tools.run(name, body.input, sandbox=tool_run_sandbox(ctx.tools, ctx.python, name))
    return ToolRunResult(output=output, is_error=is_error)


# ---- knowledge ------------------------------------------------------------------


class KnowledgeUpdate(BaseModel):
    description: str
    #: Move the file to this collection (empty: no collection); ``None`` keeps it.
    collection: str | None = None


class SearchHit(BaseModel):
    file_id: str | None
    source: str
    title: str | None
    text: str
    score: float


@router.get("/knowledge", dependencies=READ)
def list_knowledge(ctx: Ctx, q: str | None = None) -> list[KnowledgeFile]:
    return ctx.knowledge.files(q)


@router.post("/knowledge", status_code=201, dependencies=EDIT)
async def upload_knowledge(
    ctx: Ctx,
    files: Annotated[list[UploadFile], File()],
    description: Annotated[str, Form()] = "",
    collection: Annotated[str, Form()] = "",
) -> list[KnowledgeFile]:
    added: list[KnowledgeFile] = []
    try:
        for upload in files:
            data = await upload.read()
            name = upload.filename or "upload"
            media_type = upload.content_type if upload.content_type not in (None, "application/octet-stream") else None
            added.append(
                await asyncio.to_thread(
                    ctx.knowledge.add,
                    data,
                    name=name,
                    description=description,
                    media_type=media_type,
                    collection=collection,
                )
            )
    except BaseException:
        # One upload is one decision: when a file is refused, the files before it are not kept either.
        for item in added:
            ctx.knowledge.delete(item.id)
        raise
    return added


@router.get("/knowledge/search", dependencies=READ)
def search_knowledge(ctx: Ctx, q: str, file_ids: str | None = None, k: int = 8) -> list[SearchHit]:
    ids = [item for item in (file_ids or "").split(",") if item] or None
    return [
        SearchHit(file_id=hit.file_id, source=hit.source, title=hit.title, text=hit.text, score=hit.score)
        for hit in ctx.knowledge.search(q, ids, k)
    ]


@router.patch("/knowledge/{file_id}", dependencies=EDIT)
def update_knowledge(file_id: str, body: KnowledgeUpdate, ctx: Ctx) -> KnowledgeFile:
    return ctx.knowledge.update(file_id, description=body.description, collection=body.collection)


@router.delete("/knowledge/{file_id}", status_code=204, dependencies=EDIT)
def delete_knowledge(file_id: str, ctx: Ctx) -> Response:
    ctx.knowledge.delete(file_id)
    return Response(status_code=204)


@router.post("/knowledge/{file_id}/ocr", dependencies=EDIT)
async def recognise_knowledge(file_id: str, ctx: Ctx) -> KnowledgeFile:
    """Recognise the text of a scanned PDF with local OCR (tesseract), now."""
    return await asyncio.to_thread(ctx.knowledge.recognise, file_id)


@router.get("/knowledge/{file_id}/content", dependencies=READ)
def knowledge_content(file_id: str, ctx: Ctx) -> Response:
    # Documents and images open in the browser; active content (HTML, SVG, XML) is downloaded.
    entry = ctx.knowledge.store.info(file_id)
    data = ctx.knowledge.store.get_bytes(file_id)
    return Response(
        content=data, media_type=entry.media_type, headers=uploaded_file_headers(entry.name, entry.media_type)
    )


#: The browser must not guess a type: an uploaded "text file" may hold HTML with scripts.
_NOSNIFF = {"X-Content-Type-Options": "nosniff"}
#: Types a browser would execute in the studio's own origin (stored cross-site scripting).
_ACTIVE_TYPES = ("html", "xml", "svg", "javascript", "ecmascript", "xhtml")


def uploaded_file_headers(name: str, media_type: str | None, *, inline: bool = True) -> dict[str, str]:
    """Headers for a file that a user or API client uploaded: shown inline only when it cannot run scripts.

    The studio API has no keys, so a script running in its origin could use all of it; HTML, SVG
    and XML are therefore always downloaded, and every answer forbids scripts and plugins.
    """
    kind = (media_type or "").lower()
    active = not kind or any(item in kind for item in _ACTIVE_TYPES)
    disposition = "inline" if inline and not active else "attachment"
    headers = {"Content-Disposition": f"{disposition}; filename*=UTF-8''{_quote(name)}", **_NOSNIFF}
    if "pdf" not in kind:
        # Browsers render PDF with a plugin that this policy would block; PDF viewers run no page scripts.
        headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
        )
    return headers


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name)


# ---- memory ---------------------------------------------------------------------


class MemoryText(BaseModel):
    path: str
    content: str


class MemoryProposalRequest(BaseModel):
    role: str = ""
    wishes: str = Field(min_length=1)
    language: Language = "en"


class MemoryProposal(BaseModel):
    seed: MemorySeed
    cost_usd: float | None


@router.get("/memory", dependencies=READ)
def list_spaces(ctx: Ctx) -> list[MemorySpaceInfo]:
    return ctx.memory.all()


@router.post("/memory/propose", dependencies=RUN)
def propose_memory(body: MemoryProposalRequest, ctx: Ctx) -> MemoryProposal:
    model = ctx.settings().assistant_model
    connection = ctx.connect_model(model)
    tracker = UsageTracker()
    try:
        seed, cost = ctx.assistants.propose_memory(
            role=body.role,
            wishes=body.wishes,
            connection=connection,
            model=model,
            language=body.language,
            tracker=tracker,
        )
    finally:
        ctx.usage_log.record("memory_proposal", tracker.records, label=body.role or body.wishes)
    return MemoryProposal(seed=seed, cost_usd=cost)


@router.get("/memory/{space}", dependencies=READ)
def list_memory(space: str, ctx: Ctx) -> list[MemoryFile]:
    return ctx.memory.open(check_space_name(space)).entries()


@router.get("/memory/{space}/file", dependencies=READ)
def read_memory(space: str, path: str, ctx: Ctx) -> MemoryText:
    return MemoryText(path=path, content=ctx.memory.open(space).read(path))


@router.put("/memory/{space}/file", dependencies=EDIT)
def write_memory(space: str, body: MemoryText, ctx: Ctx) -> MemoryText:
    ctx.memory.open(space).write(body.path, body.content)
    return body


@router.delete("/memory/{space}/file", status_code=204, dependencies=EDIT)
def delete_memory(space: str, path: str, ctx: Ctx) -> Response:
    ctx.memory.open(space).remove(path)
    return Response(status_code=204)


@router.delete("/memory/{space}", status_code=204, dependencies=EDIT)
def clear_memory(space: str, ctx: Ctx) -> Response:
    ctx.memory.open(space).clear()
    return Response(status_code=204)
