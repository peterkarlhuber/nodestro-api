"""The runtime of an exported workflow API: the same engine and API layer, without the studio.

An exported project (see :mod:`nodestro_api.exporter`) contains this module together with the
engine and everything it needs, copied byte for byte from Nodestro into
``app/workflow_runtime``. :func:`create_app` builds a FastAPI application from the project
directory:

- ``workflow/release.json``: the immutable release (workflow, sub-workflow versions, tool code);
- ``workflow/knowledge`` and ``workflow/memory``: the data the user chose to include;
- ``DATA_DIR`` (default ``./data``): a SQLite database with runs, approvals, result files,
  uploaded files and memory, and the tool registry.

Configuration comes from environment variables only (see :class:`StandaloneSettings`); API
keys are configured as hashes (``API_KEYS``), never as plain text, and are independent of
Nodestro's keys. The agents' providers read their keys and endpoints from the environment too
(:func:`nodestro_runtime.providers.environment_connection`); the export contains no provider key.
Nothing is sent to Nodestro; usage and cost are recorded with each run in the project's own
database and reported under ``/api/published/<slug>/usage``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from nodestro_runtime.agent_builder import AgentBuilder
from nodestro_runtime.engine import RunEnvironment, RunManager
from nodestro_runtime.input_files import FileCatalog, InputFiles
from nodestro_runtime.knowledge import KnowledgeBase
from nodestro_runtime.memory_spaces import MemorySpaces
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import Connection, environment_connection, provider_of
from nodestro_runtime.release import ReleaseBundle, materialize_tools
from nodestro_runtime.reports import ResultFiles
from nodestro_runtime.schemas import Run, RunSummary, now
from nodestro_runtime.storage import Store
from nodestro_runtime.tool_library import ToolLibrary
from orkexa import StoredFile
from orkexa.dynamic import SubprocessSandbox
from pydantic import BaseModel, Field

from .serving import (
    ApiError,
    ApiLimits,
    KeyIdentity,
    ServedRelease,
    ServingCore,
    add_schemas,
    build_router,
    secret_matches,
)

logger = logging.getLogger(__name__)

#: Numerical libraries start one thread per core; the tool sandbox allows no extra processes.
_TOOL_ENV = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}


class KeyEntry(BaseModel):
    id: str
    secret_hash: str
    scopes: list[str]
    name: str = ""


def parse_keys(text: str) -> list[KeyEntry]:
    """``API_KEYS``: entries ``<id>:<sha256>:<scopes joined by +>[:<name>]``, separated by ``;`` or new lines."""
    entries = []
    for raw in text.replace("\n", ";").split(";"):
        item = raw.strip()
        if not item or item.startswith("#"):
            continue
        parts = item.split(":")
        if len(parts) < 3 or len(parts[1]) != 64:
            raise ValueError("API_KEYS: each entry is <id>:<sha256>:<scopes>[:<name>] (see README)")
        scopes = [scope for scope in parts[2].replace(",", "+").split("+") if scope in ("run", "read", "approve")]
        entries.append(KeyEntry(id=parts[0], secret_hash=parts[1], scopes=scopes, name=":".join(parts[3:])))
    return entries


class StandaloneSettings(BaseModel):
    """Everything an exported API reads from its environment."""

    data_dir: Path = Path("data")
    api_keys: list[KeyEntry] = Field(default_factory=list)
    #: ``local``: /docs and /openapi.json only for requests from this computer; ``on``; ``off``.
    api_docs: Literal["local", "on", "off"] = "local"
    #: Overrides of the limits published with the release (``LIMIT_<NAME>``).
    limits: dict[str, Any] = Field(default_factory=dict)
    #: Delete runs (and their files) older than this many days at start; 0 keeps everything.
    run_retention_days: int = 0
    tool_timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> StandaloneSettings:
        env = dict(os.environ if environ is None else environ)
        limits: dict[str, Any] = {}
        for name in ApiLimits.model_fields:
            value = env.get(f"LIMIT_{name.upper()}")
            if value is None or value == "":
                continue
            limits[name] = None if value.lower() in ("none", "off", "unlimited") else value
        return cls(
            data_dir=Path(env.get("DATA_DIR") or "data"),
            api_keys=parse_keys(env.get("API_KEYS", "")),
            api_docs=env.get("API_DOCS", "local") or "local",  # type: ignore[arg-type]
            limits=limits,
            run_retention_days=int(env.get("RUN_RETENTION_DAYS") or 0),
            tool_timeout_seconds=float(env.get("TOOL_TIMEOUT_SECONDS") or 60),
        )


class Manifest(BaseModel):
    """``workflow/manifest.json``: what was exported, from which release, with which runtime."""

    publication_id: str
    slug: str
    name: str
    description: str = ""
    release_id: str
    release_number: int
    release_digest: str
    published_at: str
    limits: ApiLimits = Field(default_factory=ApiLimits)
    knowledge_included: list[str] = Field(default_factory=list)
    memory_included: list[str] = Field(default_factory=list)
    #: The providers the release's agents use.
    providers: list[str] = Field(default_factory=list)
    #: Base URLs of endpoints without credentials in them (``<PROVIDER>_BASE_URL`` overrides them).
    base_urls: dict[str, str] = Field(default_factory=dict)
    #: Capabilities declared for endpoints in Nodestro's settings, per provider.
    capabilities: dict[str, dict[str, str]] = Field(default_factory=dict)


class StandaloneHost:
    """Serves the one release of an exported project (see :class:`nodestro_api.serving.ServingHost`)."""

    def __init__(
        self,
        project_dir: Path,
        settings: StandaloneSettings,
        *,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        """``client_factory(provider)`` replaces the SDK clients (tests)."""
        self.project_dir = project_dir
        self.settings = settings
        workflow_dir = project_dir / "workflow"
        self.bundle = ReleaseBundle.model_validate_json((workflow_dir / "release.json").read_text(encoding="utf-8"))
        self.manifest = Manifest.model_validate_json((workflow_dir / "manifest.json").read_text(encoding="utf-8"))
        if self.bundle.digest() != self.manifest.release_digest:
            raise RuntimeError("workflow/release.json does not match its manifest (digest differs)")
        data_dir = settings.data_dir if settings.data_dir.is_absolute() else project_dir / settings.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        database = data_dir / "runtime.db"
        self.store = Store(database)
        self.knowledge = KnowledgeBase(database)
        self.memory = MemorySpaces(database)
        sandbox = SubprocessSandbox(
            timeout=settings.tool_timeout_seconds, python=sys.executable, env=_TOOL_ENV, acknowledge_unsafe=True
        )
        tools = ToolLibrary(materialize_tools(self.bundle, data_dir / "tools"), sandbox=sandbox)
        self.builtin = set(tools.builtin)
        builder = AgentBuilder(tools, self.knowledge, self.memory)
        builder.declared = lambda provider: dict(self.manifest.capabilities.get(provider) or {})
        self._client_factory = client_factory
        self.runs = RunManager(
            self.store,
            builder,
            connect=self.connection,
            default_budget=lambda: None,
        )
        self.runs.resolve_workflow = self.bundle.resolver()
        self.runs.tool_marker = self.bundle.tool_marker
        # Uploaded files are input files of their runs, kept below DATA_DIR/inputs (never knowledge).
        self.inputs = InputFiles(self.store, data_dir / "inputs")
        self.runs.inputs = self.inputs
        self.runs.file_names = lambda: FileCatalog(self.inputs, self.knowledge.store)
        self.environment = RunEnvironment(
            builder=builder,
            resolve_workflow=self.bundle.resolver(),
            tool_sandbox=lambda _workflow_id: None,
            tool_marker=self.bundle.tool_marker,
        )
        self.runs.environments = lambda _run: self.environment
        self.files = ResultFiles(self.store)
        self.runs.after_run = self.files.write_outputs
        self.limits = ApiLimits.model_validate({**self.manifest.limits.model_dump(), **settings.limits})
        self._seed(workflow_dir)
        self.served = ServedRelease(
            publication_id=self.manifest.publication_id,
            slug=self.manifest.slug,
            name=self.manifest.name,
            description=self.manifest.description,
            release_id=self.manifest.release_id,
            number=self.manifest.release_number,
            workflow_version=self.bundle.workflow_version,
            workflow=self.bundle.workflow,
            limits=self.limits,
            published_at=self.manifest.published_at,  # type: ignore[arg-type]
            approvals=self.bundle.approvals(),
            output_schema=self.bundle.output_schema,
            missing_resources=[
                f"knowledge:{ref.name}" for ref in self.bundle.knowledge if ref.id not in self.knowledge.store
            ],
        )

    def _seed(self, workflow_dir: Path) -> None:
        """Copy the included knowledge files and memory into the database once (ids are kept)."""
        knowledge = workflow_dir / "knowledge"
        if knowledge.is_dir():
            for folder in sorted(path for path in knowledge.iterdir() if path.is_dir()):
                entry = StoredFile.model_validate_json((folder / "entry.json").read_text(encoding="utf-8"))
                if entry.id in self.knowledge.store:
                    continue
                text_file = folder / "text.md"
                self.knowledge.restore_file(
                    entry,
                    (folder / "data").read_bytes(),
                    text_file.read_text(encoding="utf-8") if text_file.is_file() else "",
                )
        memory = workflow_dir / "memory"
        if memory.is_dir():
            for file in sorted(memory.glob("*.json")):
                seed = json.loads(file.read_text(encoding="utf-8"))
                space = self.memory.open(seed["space"])
                if space.entries():
                    continue  # the project's own memory already has content: never overwritten
                for item in seed.get("files", []):
                    space.write(item["path"], item["content"])

    def close(self) -> None:
        self.knowledge.close()
        self.memory.close()
        self.store.close()

    # ---- ServingHost ----------------------------------------------------------------

    def release(self, slug: str, number: int | None, *, channel: str) -> ServedRelease:
        if slug != self.served.slug:
            raise ApiError(404, "publication.not_found", "No published workflow with this name")
        if number is not None and number != self.served.number:
            raise ApiError(404, "version.not_found", "This project serves one version only", version=number)
        return self.served

    def versions(self, _slug: str) -> list[int]:
        return [self.served.number]

    def release_of_run(self, run: Run) -> ServedRelease | None:
        return self.served

    def verify(self, key_id: str, secret: str) -> KeyIdentity | None:
        for entry in self.settings.api_keys:
            if entry.id == key_id and secret_matches(secret, entry.secret_hash):
                return KeyIdentity(
                    id=entry.id, name=entry.name, scopes=entry.scopes, publications=[self.served.publication_id]
                )
        return None

    def connection(self, provider: str) -> Connection:
        """The connection of a provider from the environment; raises when its key or endpoint is missing."""
        client = self._client_factory(provider) if self._client_factory is not None else None
        return environment_connection(
            provider, base_urls=self.manifest.base_urls, capabilities=self.manifest.capabilities, client=client
        )

    async def prepare_start(self, release: ServedRelease) -> None:
        try:
            for provider in sorted({provider_of(model) for model in self.bundle.models}):
                self.connection(provider)
        except NodestroError as exc:  # MissingApiKeyError among them
            raise ApiError(
                503,
                "model.unavailable",
                f"{exc}; runs cannot start",
                headers={"Retry-After": "300"},
                provider=str(exc.params.get("provider") or ""),
            ) from None

    def add_upload(self, release: ServedRelease, name: str, data: bytes, media_type: str | None) -> str:
        limit = self.limits.max_upload_mb
        return self.inputs.add(data, name=name, media_type=media_type, origin="api", limit_mb=limit).id

    def builtin_tools(self) -> set[str]:
        return self.builtin

    def cleanup(self) -> int:
        """Delete runs older than the retention period (their result files with them)."""
        days = self.settings.run_retention_days
        if days <= 0:
            return 0
        cutoff = now() - timedelta(days=days)
        removed = 0
        for summary in self.store.list("runs", RunSummary, limit=100_000):
            if summary.created_at < cutoff and summary.status not in ("queued", "running", "paused"):
                run = self.store.get("runs", summary.id, Run)
                self.store.delete("runs", summary.id)
                self.files.delete_run(summary.id)
                if run is not None:
                    self.inputs.release(run.file_ids)
                removed += 1
        self.inputs.cleanup()
        return removed


_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def create_app(
    project_dir: Path,
    *,
    settings: StandaloneSettings | None = None,
    client_factory: Callable[[str], Any] | None = None,
) -> FastAPI:
    """The FastAPI application of an exported project; ``client_factory(provider)`` replaces SDK clients."""
    settings = settings or StandaloneSettings.from_env()
    host = StandaloneHost(project_dir, settings, client_factory=client_factory)
    core = ServingCore(host)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        interrupted = host.runs.recover()
        if interrupted:
            logger.warning("%s runs were interrupted by the last stop; they are marked as failed", interrupted)
        removed = host.cleanup()
        if removed:
            logger.info("Deleted %s runs older than %s days", removed, settings.run_retention_days)
        if not settings.api_keys:
            logger.warning("API_KEYS is empty: every request will be refused (see README, 'API keys')")
        yield
        await host.runs.shutdown()
        host.close()

    app = FastAPI(
        title=host.served.name,
        version=str(host.served.number),
        description=host.served.description or f"Exported Nodestro workflow {host.served.slug}",
        lifespan=lifespan,
        docs_url=None if settings.api_docs == "off" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.api_docs == "off" else "/openapi.json",
    )
    app.state.host = host
    app.include_router(build_router(core, channel="network", release=host.served))

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            document = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
            app.openapi_schema = add_schemas(document, host.served)
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    if settings.api_docs == "local":

        @app.middleware("http")
        async def local_docs(request: Request, call_next: Any) -> Any:
            if request.url.path in ("/docs", "/openapi.json", "/docs/oauth2-redirect"):
                client = request.client.host if request.client else ""
                if client not in _LOCAL:
                    return JSONResponse({"error": {"status": 404, "code": "not_found", "message": "Not found"}}, 404)
            return await call_next(request)

    return app
