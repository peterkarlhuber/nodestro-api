"""The REST API (``/api``) and the Server-Sent Events stream of a run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
import nodestro_examples as library
from nodestro_examples import samples as library_samples
from nodestro_runtime.agent_builder import ConfigIssue
from nodestro_runtime.agent_test import AgentTestIn, AgentTestResult
from nodestro_runtime.catalog import SERVER_TOOLS, ModelInfo, ServerToolInfo, models
from nodestro_runtime.contracts import contract_of
from nodestro_runtime.engine import check_workflow
from nodestro_runtime.knowledge import KnowledgeFile
from nodestro_runtime.launcher import launch
from nodestro_runtime.memory_spaces import MemoryFile, MemorySpaceInfo, check_space_name
from nodestro_runtime.preflight import WorkflowCheck, preflight
from nodestro_runtime.preview import ContextPreview, PreviewRequest, build_preview, latest_run
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import ProfileKind
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
    OrchestratorConfig,
    Run,
    RunIn,
    RunSummary,
    Settings,
    SettingsIn,
    Usage,
    Workflow,
    WorkflowIn,
    WorkflowSummary,
    now,
)
from nodestro_runtime.tool_legacy import keep_only_inherited
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
from .permissions import ADMIN, EDIT, PUBLIC, READ, RUN, AccessError, Permission, Principal, StreamAccess
from .publishing import PUBLICATIONS, RELEASES
from .references import Reference, ReferenceKind, references
from .search import readable_documents, stored_workflows
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


def actor_id(request: Request) -> str:
    """Who a record names as the person acting: the request's principal (``local``: the desktop's owner)."""
    principal = getattr(request.state, "principal", None)
    return principal.id if principal is not None else "local"


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
    #: The product this API serves (AD-05): ``desktop`` (one person, this computer), ``local-container``
    #: (Nodestro Local Docker: one person, a container on this computer) or ``server`` (an organization's server).
    profile: ProfileKind = "desktop"


# Whether this API process serves; on a server also whether it reaches its database (``503`` if not, D-10).
# (A comment, not a docstring: the OpenAPI contract of this route stays as it was.)
@router.get("/health", dependencies=PUBLIC)
async def health(request: Request, response: Response) -> dict[str, str]:
    check = getattr(request.app.state, "health_check", None)
    if check is None:
        return {"status": "ok"}
    if not await check():
        response.status_code = 503
        return {"status": "unavailable", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


@router.get("/meta", dependencies=READ)
def meta(ctx: Ctx) -> Meta:
    return Meta(
        version=__version__,
        library_version=library_version,
        providers=list(PROVIDERS.values()),
        capabilities=list(CAPABILITY_NAMES),
        # The suggested models with the workspace's prices and their status (PRICING-1): overrides and synced
        # prices show in the model picker, and a model without a known price says so.
        models=models(ctx.pricing),
        server_tools=list(SERVER_TOOLS.values()),
        profile=ctx.profile.kind,
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
    recent = readable_documents(ctx.store, "runs", RunSummary, newest_first=lambda run: run.created_at, limit=500)
    for run in recent:
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
            capabilities=w.capabilities,
            contract=[field.name for field in contract_of(w)],
        )
        for w in stored_workflows(ctx.store)
    ]


#: Findings of the workflow check that make a graph unusable: a save or import with one is refused (AD-08), with
#: the code and params ``/workflows/check`` reports for it. Stored workflows are not checked when they are read.
_REFUSED_ON_SAVE = ("workflow.edge_dangling", "workflow.node_duplicate")

#: The name of a workflow created without one, in the interface language.
_NEW_NAME = {
    "de": "Neuer Workflow",
    "en": "New workflow",
    "es": "Nuevo flujo de trabajo",
    "fr": "Nouveau workflow",
    "pt-BR": "Novo fluxo de trabalho",
    "it": "Nuovo workflow",
}


def _checked(body: WorkflowIn, request: Request | None = None) -> WorkflowIn:
    """``body`` if its graph is consistent (else ``NodestroError`` with the check's code); a missing name is
    given in the request's language."""
    for issue in check_workflow(Workflow(**body.model_dump())):
        if issue.code in _REFUSED_ON_SAVE:
            raise NodestroError(issue.code, issue.message, **issue.params)
    if request is not None and "name" not in body.model_fields_set:
        body = body.model_copy(update={"name": _NEW_NAME[request_language(request)]})
    return body


@router.post("/workflows", status_code=201, dependencies=EDIT)
def create_workflow(body: WorkflowIn, request: Request, ctx: Ctx) -> Workflow:
    """Create a workflow. A connection to a node that does not exist is refused (400 ``workflow.edge_dangling``),
    as are two nodes with one id (``workflow.node_duplicate``)."""
    return ctx.save_workflow(_checked(body, request))


class WorkflowImport(WorkflowIn):
    #: The Python libraries of an exported workflow (no approvals: they are given again here).
    python: ExportedDefinition | None = None


@router.post("/workflows/import", status_code=201, dependencies=EDIT)
def import_workflow(body: WorkflowImport, request: Request, ctx: Ctx) -> Workflow:
    """Import an exported workflow as a new copy (new id).

    A file that is not a workflow export (no ``nodes`` list) is refused with 400 ``workflow.import_invalid``; a
    graph the check finds broken with its code (``workflow.edge_dangling``, ``workflow.node_duplicate``)."""
    if "nodes" not in body.model_fields_set:
        raise NodestroError(
            "workflow.import_invalid",
            "This file is not a Nodestro workflow export: it has no nodes",
            missing=["nodes"],
        )
    workflow = ctx.save_workflow(_checked(WorkflowIn(**body.model_dump(exclude={"python"})), request))
    if body.python is not None:
        ctx.python.adopt("workflow", workflow.id, body.python, label=workflow.name, change="imported")
    return workflow


@router.get("/workflows/{workflow_id}", dependencies=READ)
def get_workflow(workflow_id: str, ctx: Ctx) -> Workflow:
    return _workflow(ctx, workflow_id)


@router.put(
    "/workflows/{workflow_id}",
    dependencies=EDIT,
    responses={409: {"description": "``workflow.conflict``: the workflow changed since the revision in If-Match"}},
)
def update_workflow(
    workflow_id: str,
    body: WorkflowIn,
    ctx: Ctx,
    note: str = Query("", max_length=200),
    if_match: Annotated[
        str | None,
        Header(
            description="The revision the edit is based on: the workflow's ``updated_at`` as read (quotes "
            "optional). A save based on an older revision is refused with 409 ``workflow.conflict``; without the "
            "header the save always applies.",
        ),
    ] = None,
) -> Workflow:
    """Save a workflow; a changed workflow becomes the next version (``note`` describes it).

    The revision of a workflow is its ``updated_at`` (AD-07). A client that sends it as ``If-Match`` saves only
    when nobody saved in between; otherwise the answer is 409 ``workflow.conflict`` with the current revision in
    ``params.updated_at``."""
    current = _workflow(ctx, workflow_id)
    expected = _revision(if_match)
    _checked(body)
    workflow = Workflow(**body.model_dump(), id=current.id, created_at=current.created_at, updated_at=now())
    return _replace_workflow(
        ctx,
        workflow,
        lambda saved, previous: ctx.versions.record(saved, description=note, previous=previous),
        expected=expected,
    )


def _revision(if_match: str | None) -> datetime | None:
    """The revision an ``If-Match`` header names (``None``: none, or ``*``, which matches any revision)."""
    if if_match is None:
        return None
    text = if_match.strip()
    if text.startswith("W/"):
        text = text[2:]
    text = text.strip().strip('"').strip()
    if text in ("", "*"):
        return None
    revision = _time(text)
    if revision is None:
        raise ApiError(
            400, "workflow.revision_invalid", "If-Match must be the workflow's updated_at", revision=if_match
        )
    return revision


def _time(text: object) -> datetime | None:
    """An ISO time as stored or sent (``None``: not one); a time without a zone is UTC."""
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _conflict(current: Workflow) -> ApiError:
    return ApiError(
        409,
        "workflow.conflict",
        "The workflow was changed since it was opened; reload it or save it as a new version",
        updated_at=current.updated_at.isoformat().replace("+00:00", "Z"),
        name=current.name,
    )


def _replace_workflow(
    ctx: Nodestro,
    workflow: Workflow,
    record: Callable[[Workflow, Workflow], object],
    *,
    expected: datetime | None = None,
) -> Workflow:
    """Store a changed workflow and its version in one transaction under the workflow's lock; the stored workflow.

    - Only while the workflow still exists (C-16): a save that races a delete answers 404 instead of bringing the
      deleted workflow back (without its schedules, versions and published APIs).
    - With ``expected`` (the revision the edit is based on, AD-07), only while the stored ``updated_at`` is still
      that revision; otherwise 409 ``workflow.conflict``. The check and the write are one step under the lock that
      :meth:`Workspace.save_workflow` takes too, so two saves based on the same revision never both win.
    - Legacy tool exemptions come only from the activation step, never from a save (as in ``save_workflow``).

    ``record(saved, previous)`` stores the version; ``previous`` is the state read under the lock."""
    with ctx.store.transaction():
        ctx.store.lock("workflows", workflow.id)
        previous = ctx.store.get("workflows", workflow.id, Workflow)
        if previous is None:
            raise KeyError(workflow.id)
        if expected is not None and expected != previous.updated_at:
            raise _conflict(previous)
        saved = keep_only_inherited(workflow, previous)
        # Conditional as a second guard: a writer that skips the lock still cannot bring back a deleted workflow.
        if not ctx.store.replace_if("workflows", saved.id, saved, field="id", expected=saved.id):
            raise KeyError(saved.id)
        record(saved, previous)
    return saved


#: Run states in which a run still executes (or waits to) and may need the workflow's Python environment.
_EXECUTING = ("queued", "running")


@router.delete("/workflows/{workflow_id}", status_code=204, dependencies=EDIT)
def delete_workflow(workflow_id: str, ctx: Ctx) -> Response:
    """Delete a workflow with its versions, schedules, test series and published APIs.

    Refused with 409 ``workflow.runs_active`` while runs of the workflow are queued or running."""
    # They may still need its Python environment, which the delete removes. The documents go in one store
    # transaction, so a crash never leaves half a workflow (C-16); the files (Python environment, release
    # directories) are removed after it.
    _workflow(ctx, workflow_id)
    executing = [
        run.id for run in ctx.store.find("runs", "workflow_id", [workflow_id], RunSummary) if run.status in _EXECUTING
    ]
    if executing or workflow_id in ctx.runs.live_workflow_ids():
        raise ApiError(
            409,
            "workflow.runs_active",
            "Runs of this workflow are still running; delete it when they have finished or were cancelled",
            runs=max(len(executing), 1),
        )
    # Only store reads and writes inside the transaction: a lock taken in it could deadlock with a thread that
    # holds that lock and waits for the store.
    with ctx.store.transaction():
        if not ctx.store.delete("workflows", workflow_id):
            raise KeyError(workflow_id)
        ctx.versions.delete_all(workflow_id)
        ctx.organize.forget("workflow", workflow_id)
        # Schedules and test series of the workflow cannot run any more.
        for schedule in ctx.schedules.list():
            if schedule.workflow_id == workflow_id:
                ctx.schedules.delete(schedule.id)
        for suite in ctx.benchmarks.suites(workflow_id):
            ctx.benchmarks.delete_suite(suite.id)
        # Its published APIs end with it.
        publications = [item for item in ctx.publications.all() if item.workflow_id == workflow_id]
        releases = [release.id for item in publications for release in ctx.publications.releases(item.id)]
        for publication in publications:
            ctx.store.delete_prefix(RELEASES, f"{publication.id}:")
            ctx.store.delete(PUBLICATIONS, publication.id)
    for release_id in releases:
        ctx.publications.forget_environment(release_id)
    ctx.python.forget(scope_of("workflow", workflow_id))
    # Keys keep working for their other APIs.
    for publication in publications:
        ctx.api_keys.forget_publication(publication.id)
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
    return _replace_workflow(
        ctx, workflow, lambda saved, previous: ctx.versions.record(saved, restored_from=number, previous=previous)
    )


_COPY = {"de": "Kopie", "en": "copy", "es": "copia", "fr": "copie", "pt-BR": "cópia", "it": "copia"}


@router.post("/workflows/{workflow_id}/duplicate", status_code=201, dependencies=EDIT)
def duplicate_workflow(workflow_id: str, request: Request, ctx: Ctx) -> Workflow:
    source = _workflow(ctx, workflow_id)
    body = WorkflowIn(**source.model_dump(exclude={"id", "created_at", "updated_at"}))
    body.name = f"{source.name} ({_COPY[request_language(request)]})"
    workflow = ctx.save_workflow(body)  # an exact copy: not checked, like the source
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
    #: Also create the template's test cases as a test series of the copy (nothing is run). Not together with
    #: connected integrations: test series run without a person, and MCP tools are refused in such runs.
    tests: bool = False
    #: The template's integrations to connect (integration id -> MCP server id). Every tool an integration needs
    #: must be approved on that server as read-only; integrations not named here run in their offline mode.
    integrations: dict[str, str] = Field(default_factory=dict)


#: Metadata of a knowledge file stored by a copy: the SHA-256 of the file as the library rendered it, so a second
#: copy of the same template in the same language reuses the file instead of storing it again.
_LIBRARY_DIGEST = "library_sha256"


class _LibraryHost:
    """Stores what a copy of a template needs in the workspace (:class:`nodestro_examples.CopyHost`).

    It remembers what it created, so a copy that fails half-way leaves nothing behind (:meth:`undo`)."""

    def __init__(self, ctx: Nodestro) -> None:
        self.ctx = ctx
        self.workflows: list[str] = []
        self.knowledge: list[str] = []
        self.inputs: list[str] = []

    def save_workflow(self, workflow: WorkflowIn) -> tuple[str, int | None]:
        saved = self.ctx.save_workflow(_checked(workflow))
        self.workflows.append(saved.id)
        return saved.id, self.ctx.current_version(saved)

    def add_knowledge(self, data: bytes, *, name: str, media_type: str, description: str, collection: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        for entry in self.ctx.knowledge.store.list():
            if entry.metadata.get(_LIBRARY_DIGEST) == digest and entry.name == name:
                return entry.id
        added = self.ctx.knowledge.add(
            data,
            name=name,
            media_type=media_type,
            description=description,
            collection=collection,
            extra={_LIBRARY_DIGEST: digest},
        )
        self.knowledge.append(added.id)
        return added.id

    def add_input(self, name: str, media_type: str, data: bytes) -> str:
        file_id = self.ctx.inputs.add(data, name=name, media_type=media_type, origin="test").id
        self.inputs.append(file_id)
        return file_id

    def undo(self) -> None:
        """Remove what this copy created (the error that stopped the copy is what the caller reports)."""
        for workflow_id in reversed(self.workflows):
            with suppress(Exception):
                for suite in self.ctx.benchmarks.suites(workflow_id):
                    self.ctx.benchmarks.delete_suite(suite.id)
                self.ctx.store.delete("workflows", workflow_id)
                self.ctx.versions.delete_all(workflow_id)
        for file_id in self.knowledge:
            with suppress(Exception):
                self.ctx.knowledge.delete(file_id)
        for file_id in self.inputs:
            with suppress(Exception):
                self.ctx.inputs.delete(file_id, force=True)


def _library_connections(ctx: Nodestro, template_id: str, chosen: dict[str, str]) -> dict[str, dict[str, str]]:
    """The agent tool names of each chosen integration (:func:`nodestro_examples.connect`); stores nothing."""
    known = {item.id for item in library.structure(template_id).integrations}
    unknown = sorted(set(chosen) - known)
    if unknown:
        raise ApiError(
            422,
            "library.integration_unknown",
            f"The template {template_id} has no integration {', '.join(unknown)}",
            integration=", ".join(unknown),
        )
    connections: dict[str, dict[str, str]] = {}
    for integration_id, server_id in chosen.items():
        try:
            ctx.mcp.server(server_id)
        except NodestroError as exc:
            if exc.code == "mcp.server_not_found":
                raise KeyError(server_id) from None
            raise
        offered = {
            tool.name: library.ServerTool(
                agent_name=tool.tool_name,
                # Approved means: the current definition is the one an administrator approved.
                approved=tool.status == "approved" and tool.approved_hash == tool.hash,
                access=tool.access,
            )
            for tool in ctx.mcp.tools(server_id)
            if tool.status != "removed"
        }
        try:
            connections[integration_id] = library.connect(template_id, integration_id, offered)
        except library.IntegrationProblem as problem:
            raise ApiError(
                409,
                "library.integration_unavailable",
                str(problem),
                integration=integration_id,
                server=server_id,
                missing=", ".join(problem.missing),
                not_read_only=", ".join(problem.not_read_only),
            ) from problem
    return connections


@router.post("/library/{template_id}/copy", status_code=201, dependencies=EDIT)
def copy_library_template(template_id: str, body: LibraryCopy, request: Request, ctx: Ctx) -> Workflow:
    """Create an ordinary, independent workflow from a template; the template itself never changes.

    Parts the template runs as sub-workflows or orchestrator teams are copied first, as workflows of their own;
    every reference is pinned to their first version. Knowledge files are stored in the knowledge base (collection
    "Examples: <name>" in the copy's language; identical files of an earlier copy are reused). Agents get model
    tiers mapped to the workspace's provider. Integrations named in ``integrations`` are connected to the chosen MCP
    server (only approved read-only tools); the others run in their labelled offline mode. With ``tests`` the
    template's test cases become a test series of the copy; their sample files are stored as input files.

    Everything is checked before anything is stored, and a copy that fails half-way leaves nothing behind.
    """
    language = body.language or request_language(request)
    spec = library.structure(template_id)
    if body.tests and body.integrations:
        raise ApiError(
            422,
            "library.tests_need_offline",
            "A test series runs without a person and cannot use MCP tools; copy the template without integrations to "
            "take over its test cases",
        )
    connections = _library_connections(ctx, template_id, body.integrations)
    settings = ctx.settings()
    models = library.model_choice(settings.default_model, settings.assistant_model)
    host = _LibraryHost(ctx)
    try:
        made = library.instantiate(template_id, language, host, models=models, connections=connections)
        copy = ctx.save_workflow(_checked(made.workflow))
        host.workflows.append(copy.id)
        if body.tests and spec.tests:
            text = library.texts(template_id, language)
            cases = library_samples.suite_cases(template_id, language, copy, host.add_input)
            ctx.benchmarks.save_suite(
                SuiteIn(
                    name=text.name,
                    description=text.description,
                    workflow_id=copy.id,
                    cases=[TestCase(**case) for case in cases],
                )
            )
    except Exception:
        host.undo()
        raise
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
    runs: list[RunSummary]
    if workflow_id is not None:
        # Every run of the workflow, however many other runs were stored since (C-04).
        runs = ctx.store.find("runs", "workflow_id", [workflow_id], RunSummary)
    elif trigger_ref is not None:
        runs = ctx.store.find("runs", "trigger_ref", [trigger_ref], RunSummary)
    elif include_children:
        runs = ctx.store.list("runs", RunSummary, limit=limit)
    else:
        runs = _top_level_runs(ctx, limit)
    if not include_children:
        runs = [run for run in runs if run.parent_run_id is None]
    if workflow_id is not None:
        runs = [run for run in runs if run.workflow_id == workflow_id]
    if trigger_ref is not None:
        runs = [run for run in runs if run.trigger_ref == trigger_ref]
    return sorted(runs, key=lambda run: run.created_at, reverse=True)[:limit]


#: Runs read at least for the top-level run list: runs are listed by creation, the store answers by update.
_RUN_WINDOW = 2000


def _top_level_runs(ctx: Nodestro, limit: int) -> list[RunSummary]:
    """The most recently updated runs, enough of them to hold ``limit`` top-level runs (or all runs).

    Sub-workflow and team runs can outnumber their parents by far; the window grows until it holds enough
    top-level runs instead of stopping at a fixed number of runs."""
    window = _RUN_WINDOW
    while True:
        runs = ctx.store.list("runs", RunSummary, limit=window)
        if len(runs) < window or sum(run.parent_run_id is None for run in runs) >= limit:
            return runs
        window *= 4


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
async def decide_approval(run_id: str, node_id: str, body: ApprovalDecision, request: Request, ctx: Ctx) -> Run:
    """Approve or reject at an approval node; the run (its top-level run) continues right away."""
    return ctx.runs.decide(run_id, node_id, body.decision, body.comment, decided_by=actor_id(request))


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
        # A run paused at a tool call (M6b): its questions close and its checkpoints go with it.
        ctx.runs.discard_tool_waits(item.id, because="run.deleted")
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

    access = StreamAccess(request)

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
    #: The sum of the known costs (a lower bound when ``cost_state`` is ``partial``); kept for older clients.
    estimated_cost_usd: float = 0.0
    #: Requests whose cost is unknown or incomplete (their cost is missing from the sum).
    unpriced: int = 0
    #: The same sum as ``estimated_cost_usd``, under the name of the price references (PRICING-1 N6).
    known_cost_usd: float = 0.0
    #: ``known``: every request was priced; ``partial``: some costs are missing (the sum is a lower bound);
    #: ``unknown``: no request could be priced; ``none``: no requests.
    cost_state: Literal["known", "partial", "unknown", "none"] = "none"
    #: Requests priced with a price that had not been checked for longer than the warning threshold.
    stale_price_requests: int = 0
    #: Cost components that could not be priced (e.g. ``cache_write_1h``), over all requests.
    missing_cost_components: list[str] = Field(default_factory=list)
    #: The price snapshot versions, sources and the lowest quality the costs were calculated with.
    pricing_versions: list[str] = Field(default_factory=list)
    pricing_sources: list[str] = Field(default_factory=list)
    pricing_quality: str | None = None


_QUALITY_ORDER = ("estimated", "unverified", "contract", "verified")


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
    known = getattr(usage, "known_cost_usd", None)
    if known is not None:  # priced with references (PRICING-1): the known part and what is missing
        bucket.known_cost_usd += known
        bucket.unpriced += usage.unpriced_requests
    elif usage.estimated_cost_usd is None:  # stored before PRICING-1 without a known price
        bucket.unpriced += usage.requests
    else:
        bucket.known_cost_usd += usage.estimated_cost_usd
    bucket.estimated_cost_usd = bucket.known_cost_usd
    bucket.stale_price_requests += getattr(usage, "stale_price_requests", 0) or 0
    for target, values in (
        (bucket.missing_cost_components, getattr(usage, "missing_cost_components", None) or []),
        (bucket.pricing_versions, getattr(usage, "pricing_versions", None) or []),
        (bucket.pricing_sources, getattr(usage, "pricing_sources", None) or []),
    ):
        target.extend(value for value in values if value not in target)
    quality = getattr(usage, "pricing_quality", None)
    if quality in _QUALITY_ORDER and (
        bucket.pricing_quality not in _QUALITY_ORDER
        or _QUALITY_ORDER.index(quality) < _QUALITY_ORDER.index(bucket.pricing_quality or "")
    ):
        bucket.pricing_quality = quality
    if not bucket.requests:
        bucket.cost_state = "none"
    elif not bucket.unpriced:
        bucket.cost_state = "known"
    else:
        bucket.cost_state = "unknown" if bucket.unpriced >= bucket.requests and not bucket.known_cost_usd else "partial"


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

    for run in _runs_since(ctx, cutoff):
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
        # The lean run has every field models_of_run reads (sub-workflow runs it reads in full itself).
        for model, model_usage in models_of_run(ctx, cast(Run, run)).items():
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


# The usage report reads every run of the period. Validating full run documents (with the workflow snapshot, every
# step's messages and outputs) costs far more than the report needs, so it reads them with these lean models: only
# the fields the report and ``models_of_run`` use, with the same names and defaults as the full models.


class _UsageAgent(BaseModel):
    model: str = AgentConfig.model_fields["model"].default


class _UsageOrchestrator(BaseModel):
    model: str = OrchestratorConfig.model_fields["model"].default


class _UsageNode(BaseModel):
    id: str
    type: str = "agent"
    agent: _UsageAgent | None = None
    orchestrator: _UsageOrchestrator | None = None


class _UsageWorkflow(BaseModel):
    nodes: list[_UsageNode] = Field(default_factory=list)


class _UsageNodeRun(BaseModel):
    usage: Usage = Field(default_factory=Usage)
    child_run_id: str | None = None
    child_run_ids: list[str] = Field(default_factory=list)
    decision_usage: Usage | None = None


class _UsageRun(BaseModel):
    id: str
    workflow_id: str
    workflow_name: str
    workflow: _UsageWorkflow
    nodes: dict[str, _UsageNodeRun] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    created_at: datetime
    imported_at: datetime | None = None
    trigger: str = "manual"
    parent_run_id: str | None = None


def _runs_since(ctx: Nodestro, cutoff: float) -> list[_UsageRun]:
    """Every top-level, not imported run created at ``cutoff`` (a timestamp) or later, newest first (C-04).

    All of them, however many runs are stored: the store has no query by time, so this reads every run (with the
    lean model above, which keeps that affordable)."""
    counted = [
        run
        for run in ctx.store.list("runs", _UsageRun)
        if run.created_at.timestamp() >= cutoff and run.imported_at is None and run.parent_run_id is None
    ]
    return sorted(counted, key=lambda run: run.created_at, reverse=True)


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
    tracker = ctx.tracker()
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
        ctx.usage_log.record("agent_design", tracker.records, label=body.description, tracker=tracker)


def _is_saved_agent(ctx: Nodestro, body: AgentTestIn) -> bool:
    """Whether the tested configuration is exactly one that somebody with ``edit`` saved: the agent of a node of the
    named workflow, or an agent template. Its tools are then an authorized assignment, not a free choice."""
    wanted = body.agent.model_dump(mode="json")
    if body.workflow_id is not None:
        workflow = ctx.store.get("workflows", body.workflow_id, Workflow)
        if workflow is not None and any(
            node.agent is not None and node.agent.model_dump(mode="json") == wanted for node in workflow.nodes
        ):
            return True
    return any(
        template.agent.model_dump(mode="json") == wanted for template in ctx.store.list("templates", AgentTemplate)
    )


@router.post("/agents/test", dependencies=RUN)
async def test_agent(body: AgentTestIn, ctx: Ctx, request: Request) -> AgentTestResult:
    """Run one agent with its current configuration on a test task; not a workflow run.

    The result is returned, never stored as a run; its usage goes to the ledger. Testing costs money (``run``); a
    configuration that is not exactly a saved one (a node of ``workflow_id`` or an agent template) is a free choice of
    prompt and tools and also needs ``edit``, like saving it would. Every tool call follows the same per-call rules as
    in a workflow run (the runtime's agent test refuses calls that would ask a person).
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is not None and not principal.allows(Permission.EDIT) and not _is_saved_agent(ctx, body):
        raise AccessError(
            403,
            "access.forbidden",
            "Testing a changed agent configuration needs the permission 'edit'",
            permission=Permission.EDIT.value,
        )
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
    ctx.usage_log.record("agent_test", tracker.records, label=body.agent.name, ref=result.id, tracker=tracker)
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
        ctx.usage_log.record("tool_generation", tracker.records, label=job.requirement, ref=job.id, tracker=tracker)

    return ctx.tools.start_job(
        body.requirement, connection=connection, model=model, usage_tracker=ctx.tracker(), on_finished=record
    )


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


@router.post("/tools/{name}/run", dependencies=EDIT)
def run_tool(name: str, body: ToolRunRequest, ctx: Ctx, request: Request) -> ToolRunResult:
    """The tool editor's test panel: run one tool with free input, outside any run and without asking anybody.

    Needs ``edit`` (it belongs to editing the tool) and ``run`` (it executes code). MCP tools that change data never
    run here (``mcp.write_not_supported``); they run only in workflow runs that ask before every call."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is not None and not principal.allows(Permission.RUN):
        raise AccessError(403, "access.forbidden", "This needs the permission 'run'", permission=Permission.RUN.value)
    output, is_error = ctx.tools.run(name, body.input, sandbox=tool_run_sandbox(ctx.tools, ctx.python, name))
    return ToolRunResult(output=output, is_error=is_error)


# ---- references -----------------------------------------------------------------


@router.get("/used-by", dependencies=READ)
def used_by(ctx: Ctx, kind: ReferenceKind, ref: str | None = None) -> list[Reference]:
    """Where knowledge files, tools or memory spaces are used: saved workflows, library agents and published APIs.

    Only objects with at least one user are listed; with ``ref``, only that object. Read only."""
    return references(ctx, kind, ref)


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
    tracker = ctx.tracker()
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
        ctx.usage_log.record("memory_proposal", tracker.records, label=body.role or body.wishes, tracker=tracker)
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
