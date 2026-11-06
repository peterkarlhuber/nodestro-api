"""The API of local models (Ollama) and of the semantic knowledge index.

The runtime does the work (:mod:`nodestro_runtime.local_service`, :mod:`nodestro_runtime.local_models`,
:class:`nodestro_runtime.knowledge.KnowledgeBase`); these routes expose it:

- ``GET /api/local-models``: the server's state, the catalog with what is installed, this computer's hardware and a
  recommendation per task.
- Jobs (``202`` with the job; progress at ``GET /api/local-models/jobs/{id}``, ``POST .../cancel``): set up the
  server step by step (``/setup``: check, prerequisites, download, verify, install, configure, start, reachability,
  hardware; steps that are not needed are skipped with the reason), install the runtime (the same job), download a
  model (Ollama's steps with measured bytes, rate and remaining time), import an offline model package or a GGUF file,
  build a catalog model from local files, measure a model's speed, move the model folder.
- ``/api/knowledge/index``: the semantic index (state, rebuild, cancel, remove).

Downloads need the internet and are refused in the strict offline mode. Paths name files on the computer that runs
Nodestro (in Nodestro Local Docker: inside its container, e.g. the mounted import folder). On a server the local
model server belongs to its operator: tenants read its state, every change answers 403.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Request, Response
from nodestro_runtime.knowledge import KnowledgeIndexStatus
from nodestro_runtime.local_models import CATALOG, TASKS, Hardware, LocalModelInfo, Recommendation, fits, recommend_all
from nodestro_runtime.local_readiness import Readiness
from nodestro_runtime.local_service import BuildInput, LocalAdvice, LocalJob, LocalStatus, build_inputs
from orkexa.core.capabilities import Capability as CapabilityName
from orkexa.local import OllamaError
from pydantic import BaseModel, Field

from . import model_readiness
from .api import Ctx
from .model_readiness import ReadinessAction
from .permissions import EDIT, INSTALL, READ, RUN, Permission, Principal
from .serving import ApiError

router = APIRouter(prefix="/api")


class CatalogEntry(LocalModelInfo):
    installed: bool = False
    #: The installed model has the catalog's digest (the verified build).
    verified: bool = False
    #: Whether the model can run on this computer (GPU memory, or memory and fast enough on a CPU).
    fits: bool = True
    #: ``source="build"``: the pinned upstream files an offline build needs (role, URL, SHA-256, size).
    build_inputs: list[BuildInput] = Field(default_factory=list)


class LocalOverview(BaseModel):
    status: LocalStatus
    catalog: list[CatalogEntry]
    #: This computer's hardware; ``None`` for a server's tenants (the server's machine belongs to its operator).
    hardware: Hardware | None
    recommendations: list[Recommendation]
    #: Hints for this computer: small context window, no GPU, slow or too large models, the CPU saving mode.
    advice: list[LocalAdvice] = Field(default_factory=list)
    #: Whether this installation may change the local model server (``False`` on a server's tenants).
    manageable: bool


class RuntimeInstallIn(BaseModel):
    #: A release archive on this computer (offline); ``download`` fetches it from the Ollama release page instead.
    archive: str | None = None
    download: bool = False


class SetupIn(BaseModel):
    #: Allow downloading the pinned runtime when it is missing (refused in the strict offline mode).
    download: bool = False
    #: A release archive on this computer to install the runtime from instead.
    archive: str | None = Field(default=None, max_length=4096)


class PullIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    #: Follow a download of the same model that runs already instead of refusing a second one
    #: (``local.job_running``): a second click or another dialog never starts the download twice.
    attach: bool = False


class ReadinessIn(BaseModel):
    #: The model the action uses (``ollama:<tag>``; any other model is ready as far as this check goes).
    model: str = Field(min_length=1, max_length=200)
    #: What the model is needed for: decides what it must be able to do and whether another model may be chosen.
    action: ReadinessAction = "check"
    #: Capabilities the action needs beyond its own (Orkexa names, e.g. ``tools`` for an agent with tools).
    capabilities: list[CapabilityName] = Field(default_factory=list, max_length=20)


class BenchmarkIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ImportIn(BaseModel):
    kind: Literal["package", "gguf", "build"]
    #: The package folder, the GGUF file, or (build) the upstream weights file of a catalog model built from files.
    path: str = Field(min_length=1, max_length=4096)
    #: GGUF: the model name to create (e.g. ``my-model:q4``); package: which of its models (default: all).
    name: str | None = Field(default=None, max_length=200)
    models: list[str] | None = None
    #: GGUF: the vision projector file (``mmproj``), optional.
    projector: str | None = Field(default=None, max_length=4096)
    #: Build: the template GGUF file (or its header) of the catalog model's recipe, e.g. for ``mdenseon:307m``.
    template: str | None = Field(default=None, max_length=4096)


class StorageIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    #: ``use`` the folder as it is (an existing Ollama model folder or an empty one), or ``copy`` the models there.
    transfer: Literal["use", "copy"] = "copy"


class AutostartIn(BaseModel):
    enabled: bool


class ReindexIn(BaseModel):
    #: The embedding model (default: the one chosen in the settings).
    model: str | None = Field(default=None, max_length=200)


def _manageable(ctx: Ctx) -> None:
    if ctx.local_models.profile == "server":
        raise ApiError(403, "local.server_managed", "The local model server of a server belongs to its operator")


def _operator_view(ctx: Ctx, request: Request) -> bool:
    """Whether the request may see the machine behind the local model server: everywhere but on a server, where
    only a principal with ``install`` may (no tenant role holds it)."""
    if ctx.local_models.profile != "server":
        return True
    principal: Principal | None = getattr(request.state, "principal", None)
    return principal is not None and principal.allows(Permission.INSTALL)


#: Advice and recommendation reasons that describe the machine itself (memory, CPU count, GPU, model speed or size
#: against the hardware): a server's tenants do not get them (API3-08 follow-up, UX handover B-1).
_MACHINE_ADVICE = {"local.cpu_only", "local.memory_low", "local.model_slow", "local.model_too_large"}
_MACHINE_REASONS = {"no_gpu", "hardware_limit"}


def _without_machine_reasons(recommendation: Recommendation) -> Recommendation:
    reasons = [reason for reason in recommendation.reasons if reason not in _MACHINE_REASONS]
    return recommendation.model_copy(update={"reasons": reasons})


#: What a server's tenants do not see of the local model server's state (API3-08): its folders, executable and
#: addresses on the server's machine.
_MACHINE_FIELDS = {
    "base_url": None,
    "runtime_path": None,
    "models_dir": None,
    "free_bytes": None,
    "detected_external": None,
}


@router.get("/local-models", dependencies=READ)
async def local_overview(ctx: Ctx, request: Request) -> LocalOverview:
    """The local model server's state, the catalog, this computer's hardware and the recommendations.

    For a server's tenants the server's machine stays hidden: ``hardware`` is ``null``, the status has no
    ``base_url``, ``runtime_path``, ``models_dir``, ``free_bytes`` or ``detected_external``, and advice and
    recommendation reasons that describe the machine (memory, CPU, GPU, model speed or size) are left out."""
    service = ctx.local_models
    status = await asyncio.to_thread(service.status)
    hardware = await asyncio.to_thread(service.hardware)
    installed = {model.name: model for model in status.models}
    catalog = [
        CatalogEntry(
            **entry.model_dump(),
            installed=entry.name in installed,
            verified=entry.name in installed and installed[entry.name].verified,
            fits=fits(entry, hardware),
            build_inputs=build_inputs(entry.name),
        )
        for entry in CATALOG
    ]
    recommendations = recommend_all(
        hardware,
        list(installed),
        capabilities={name: model.capabilities for name, model in installed.items()},
        preferences=service.preferences(),
    )
    saving = await asyncio.to_thread(ctx.reasoning_saving)
    operator = _operator_view(ctx, request)
    advice = service.advice(status, reasoning_saving=saving)
    return LocalOverview(
        status=status if operator else status.model_copy(update=_MACHINE_FIELDS),
        catalog=catalog,
        hardware=hardware if operator else None,
        recommendations=recommendations if operator else [_without_machine_reasons(r) for r in recommendations],
        advice=advice if operator else [hint for hint in advice if hint.code not in _MACHINE_ADVICE],
        manageable=service.profile != "server",
    )


@router.get("/local-models/recommend", dependencies=READ)
async def local_recommend(ctx: Ctx, request: Request, task: str) -> Recommendation:
    if task not in TASKS:
        raise ApiError(400, "local.task_unknown", f"Unknown task {task!r}", task=task)
    found = await asyncio.to_thread(ctx.local_models.recommend, task)
    return found if _operator_view(ctx, request) else _without_machine_reasons(found)


@router.post("/local-models/start", dependencies=INSTALL)
async def local_start(ctx: Ctx) -> LocalStatus:
    _manageable(ctx)
    await asyncio.to_thread(ctx.local_models.start)
    return await asyncio.to_thread(ctx.local_models.status)


@router.post("/local-models/stop", dependencies=INSTALL)
async def local_stop(ctx: Ctx) -> LocalStatus:
    """Stop the server Nodestro started (desktop). Local Docker's container and a server the user runs are never
    stopped from here (``local.not_managed``)."""
    _manageable(ctx)
    if ctx.local_models.mode != "managed":
        raise ApiError(409, "local.not_managed", "Nodestro does not manage this local model server")
    await asyncio.to_thread(ctx.local_models.stop)
    return await asyncio.to_thread(ctx.local_models.status)


@router.put("/local-models/autostart", dependencies=INSTALL)
def local_autostart(body: AutostartIn, ctx: Ctx) -> LocalStatus:
    _manageable(ctx)
    ctx.local_models.set_autostart(body.enabled)
    return ctx.local_models.status()


@router.post("/local-models/setup", status_code=202, dependencies=INSTALL)
def local_setup(body: SetupIn, ctx: Ctx) -> LocalJob:
    """Set up the local model server as one job with steps (see the module documentation); one at a time
    (``local.job_running``). For Local Docker and the user's own server it only checks the server and the hardware:
    such a server is never started, stopped or changed."""
    _manageable(ctx)
    return ctx.local_models.setup(archive=body.archive, download=body.download)


@router.post("/local-models/runtime", status_code=202, dependencies=INSTALL)
def local_install_runtime(body: RuntimeInstallIn, ctx: Ctx) -> LocalJob:
    _manageable(ctx)
    return ctx.local_models.install_runtime(archive=body.archive, download=body.download)


@router.post("/local-models/pull", status_code=202, dependencies=INSTALL)
async def local_pull(body: PullIn, ctx: Ctx) -> LocalJob:
    _manageable(ctx)
    return await asyncio.to_thread(ctx.local_models.pull, body.model, attach=body.attach)


@router.post("/local-models/readiness", dependencies=RUN)
async def local_readiness(body: ReadinessIn, ctx: Ctx, request: Request) -> Readiness:
    """Whether a local model can be used for an action now; if not, why (``problem.cause``: not installed, no
    runtime, server not started or unreachable, invalid name, too little space, missing capability, hardware) and
    what this person may do (``problem.actions``, from their permission, the operating mode and the strict offline
    mode), plus the ready models that can do the same (``alternatives``). Interactive actions refuse such a model
    with ``409 model.local_unavailable`` carrying the same answer; the studio checks again here after a download.
    May start the managed server of the desktop (as the action would); never downloads anything."""
    return await asyncio.to_thread(model_readiness.check, ctx, request, body.model, body.action, body.capabilities)


@router.post("/local-models/benchmark", status_code=202, dependencies=INSTALL)
async def local_benchmark(body: BenchmarkIn, ctx: Ctx) -> LocalJob:
    """Measure how fast an installed chat model answers here (a short request; the result is stored with the
    model). Shown instead of any estimate; loads the model like an agent's first request."""
    _manageable(ctx)
    return await asyncio.to_thread(ctx.local_models.benchmark, body.model)


@router.post("/local-models/import", status_code=202, dependencies=INSTALL)
async def local_import(body: ImportIn, ctx: Ctx) -> LocalJob:
    _manageable(ctx)
    if body.kind == "build":
        if not body.name or not body.template:
            raise ApiError(
                400, "local.name_missing", "Name the catalog model and choose its weights and template files"
            )
        return await asyncio.to_thread(ctx.local_models.build, body.name, weights=body.path, template=body.template)
    if body.kind == "gguf":
        if not body.name:
            raise ApiError(400, "local.name_missing", "Name the model to create from the GGUF file")
        return await asyncio.to_thread(ctx.local_models.import_gguf, body.name, body.path, projector=body.projector)
    try:
        return await asyncio.to_thread(ctx.local_models.import_package, body.path, body.models)
    except OllamaError as exc:
        # The package's structure is checked before the job starts (no manifest, unknown format, a bad entry): a
        # code the studio translates instead of agent.OllamaError (UX handover B3).
        if exc.code == "unreachable" or exc.code == "timeout":
            raise
        raise ApiError(400, "local.invalid_package", str(exc), reason=exc.code, path=body.path) from None


@router.delete("/local-models/models/{name:path}", status_code=204, dependencies=INSTALL)
async def local_delete(name: str, ctx: Ctx) -> Response:
    _manageable(ctx)
    await asyncio.to_thread(ctx.local_models.delete, name)
    return Response(status_code=204)


@router.put("/local-models/storage", dependencies=INSTALL)
async def local_storage(body: StorageIn, ctx: Ctx, response: Response) -> LocalJob | LocalStatus:
    _manageable(ctx)
    job = await asyncio.to_thread(ctx.local_models.set_models_dir, body.path, transfer=body.transfer)
    if job is not None:
        response.status_code = 202
        return job
    return await asyncio.to_thread(ctx.local_models.status)


@router.get("/local-models/jobs", dependencies=READ)
def local_jobs(ctx: Ctx) -> list[LocalJob]:
    return ctx.local_models.jobs()


@router.get("/local-models/jobs/{job_id}", dependencies=READ)
def local_job(job_id: str, ctx: Ctx) -> LocalJob:
    return ctx.local_models.job(job_id)


@router.post("/local-models/jobs/{job_id}/cancel", dependencies=INSTALL)
def local_cancel(job_id: str, ctx: Ctx) -> LocalJob:
    _manageable(ctx)
    return ctx.local_models.cancel(job_id)


# ---- the semantic knowledge index --------------------------------------------------------------------------------


@router.get("/knowledge/index", dependencies=READ)
async def knowledge_index(ctx: Ctx) -> KnowledgeIndexStatus:
    return await asyncio.to_thread(ctx.knowledge.index_status)


@router.post("/knowledge/index/rebuild", status_code=202, dependencies=EDIT)
async def knowledge_reindex(body: ReindexIn, ctx: Ctx, request: Request) -> KnowledgeIndexStatus:
    """Build the semantic index; an embedding model the local model server does not have is refused first
    (``409 model.local_unavailable``, with what can be done), instead of a job that fails at its first chunk."""
    name = body.model or ctx.knowledge.retrieval()[1]
    if name:
        await model_readiness.require_local(ctx, request, f"ollama:{name.removeprefix('ollama:')}", "knowledge_index")
    return await asyncio.to_thread(ctx.knowledge.start_reindex, body.model)


@router.post("/knowledge/index/cancel", dependencies=EDIT)
def knowledge_cancel_reindex(ctx: Ctx) -> KnowledgeIndexStatus:
    return ctx.knowledge.cancel_reindex()


@router.delete("/knowledge/index", dependencies=EDIT)
async def knowledge_drop_vectors(ctx: Ctx) -> KnowledgeIndexStatus:
    return await asyncio.to_thread(ctx.knowledge.drop_vectors)
