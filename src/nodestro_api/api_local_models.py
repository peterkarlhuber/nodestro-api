"""The API of local models (Ollama) and of the semantic knowledge index.

The runtime does the work (:mod:`nodestro_runtime.local_service`, :mod:`nodestro_runtime.local_models`,
:class:`nodestro_runtime.knowledge.KnowledgeBase`); these routes expose it:

- ``GET /api/local-models``: the server's state, the catalog with what is installed, this computer's hardware and a
  recommendation per task.
- Jobs (``202`` with the job; progress at ``GET /api/local-models/jobs/{id}``, ``POST .../cancel``): install the
  runtime (from a local archive or by download), download a model, import an offline model package or a GGUF file,
  build a catalog model from local files, measure a model's speed, move the model folder.
- ``/api/knowledge/index``: the semantic index (state, rebuild, cancel, remove).

Downloads need the internet and are refused in the strict offline mode. Paths name files on the computer that runs
Nodestro (in Nodestro Local Docker: inside its container, e.g. the mounted import folder). On a server the local
model server belongs to its operator: tenants read its state, every change answers 403.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Response
from nodestro_runtime.knowledge import KnowledgeIndexStatus
from nodestro_runtime.local_models import CATALOG, TASKS, Hardware, LocalModelInfo, Recommendation, recommend_all
from nodestro_runtime.local_service import LocalAdvice, LocalJob, LocalStatus
from pydantic import BaseModel, Field

from .api import Ctx
from .permissions import EDIT, INSTALL, READ
from .serving import ApiError

router = APIRouter(prefix="/api")


class CatalogEntry(LocalModelInfo):
    installed: bool = False
    #: The installed model has the catalog's digest (the verified build).
    verified: bool = False


class LocalOverview(BaseModel):
    status: LocalStatus
    catalog: list[CatalogEntry]
    hardware: Hardware
    recommendations: list[Recommendation]
    #: Hints for this computer: small context window, no GPU, slow or too large models, the CPU saving mode.
    advice: list[LocalAdvice] = Field(default_factory=list)
    #: Whether this installation may change the local model server (``False`` on a server's tenants).
    manageable: bool


class RuntimeInstallIn(BaseModel):
    #: A release archive on this computer (offline); ``download`` fetches it from the Ollama release page instead.
    archive: str | None = None
    download: bool = False


class PullIn(BaseModel):
    model: str = Field(min_length=1, max_length=200)


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


@router.get("/local-models", dependencies=READ)
async def local_overview(ctx: Ctx) -> LocalOverview:
    service = ctx.local_models
    status = await asyncio.to_thread(service.status)
    hardware = await asyncio.to_thread(service.hardware)
    installed = {model.name: model for model in status.models}
    catalog = [
        CatalogEntry(
            **entry.model_dump(),
            installed=entry.name in installed,
            verified=entry.name in installed and installed[entry.name].verified,
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
    return LocalOverview(
        status=status,
        catalog=catalog,
        hardware=hardware,
        recommendations=recommendations,
        advice=service.advice(status, reasoning_saving=saving),
        manageable=service.profile != "server",
    )


@router.get("/local-models/recommend", dependencies=READ)
async def local_recommend(ctx: Ctx, task: str) -> Recommendation:
    if task not in TASKS:
        raise ApiError(400, "local.task_unknown", f"Unknown task {task!r}", task=task)
    return await asyncio.to_thread(ctx.local_models.recommend, task)


@router.post("/local-models/start", dependencies=INSTALL)
async def local_start(ctx: Ctx) -> LocalStatus:
    _manageable(ctx)
    await asyncio.to_thread(ctx.local_models.start)
    return await asyncio.to_thread(ctx.local_models.status)


@router.post("/local-models/stop", dependencies=INSTALL)
async def local_stop(ctx: Ctx) -> LocalStatus:
    _manageable(ctx)
    await asyncio.to_thread(ctx.local_models.stop)
    return await asyncio.to_thread(ctx.local_models.status)


@router.put("/local-models/autostart", dependencies=INSTALL)
def local_autostart(body: AutostartIn, ctx: Ctx) -> LocalStatus:
    _manageable(ctx)
    ctx.local_models.set_autostart(body.enabled)
    return ctx.local_models.status()


@router.post("/local-models/runtime", status_code=202, dependencies=INSTALL)
def local_install_runtime(body: RuntimeInstallIn, ctx: Ctx) -> LocalJob:
    _manageable(ctx)
    return ctx.local_models.install_runtime(archive=body.archive, download=body.download)


@router.post("/local-models/pull", status_code=202, dependencies=INSTALL)
async def local_pull(body: PullIn, ctx: Ctx) -> LocalJob:
    _manageable(ctx)
    return await asyncio.to_thread(ctx.local_models.pull, body.model)


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
    return await asyncio.to_thread(ctx.local_models.import_package, body.path, body.models)


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
async def knowledge_reindex(body: ReindexIn, ctx: Ctx) -> KnowledgeIndexStatus:
    return await asyncio.to_thread(ctx.knowledge.start_reindex, body.model)


@router.post("/knowledge/index/cancel", dependencies=EDIT)
def knowledge_cancel_reindex(ctx: Ctx) -> KnowledgeIndexStatus:
    return ctx.knowledge.cancel_reindex()


@router.delete("/knowledge/index", dependencies=EDIT)
async def knowledge_drop_vectors(ctx: Ctx) -> KnowledgeIndexStatus:
    return await asyncio.to_thread(ctx.knowledge.drop_vectors)
