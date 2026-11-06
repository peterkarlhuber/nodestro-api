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

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import dataclasses
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from nodestro_runtime import docker_status
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
from orkexa.dynamic import DockerSandbox, Sandbox, SubprocessSandbox
from pydantic import BaseModel, Field

from .body_limit import BodyLimitMiddleware, BodyLimits
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
    #: ``subprocess``: own tools run as a process of this server (only tools written by a person in the editor,
    #: ``user-authored``); ``docker``: every own tool runs in an isolated container. AI-generated tools and tools
    #: of unknown provenance run only with ``docker`` (ADR-0006), otherwise they are refused.
    tool_sandbox: Literal["subprocess", "docker"] = "subprocess"
    #: Where a copy of every result file is written (``<folder>/<date>_<run id>/<file>``); relative paths are
    #: relative to the project; empty: result files stay in the database only (download through the API).
    output_dir: str = ""
    #: The web interface (``nodestro_api/webui``, if the export has it): ``on`` or ``off``.
    web_ui: Literal["on", "off"] = "on"
    #: The language the web interface starts in.
    ui_language: str = "en"

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
            tool_sandbox=env.get("TOOL_SANDBOX") or "subprocess",  # type: ignore[arg-type]
            output_dir=env.get("OUTPUT_DIR") or "",
            web_ui="off" if (env.get("WEB_UI") or "on").lower() in ("off", "0", "false", "no") else "on",
            ui_language=env.get("UI_LANGUAGE") or "en",
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
    #: Provider settings that are not credentials (Amazon Bedrock: ``region``, ``data_residency``; ``AWS_REGION``
    #: overrides the region, an exported ``eu_only`` stays in force).
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: How the knowledge search ranks when an agent says ``default`` (the studio's setting at export time), and the
    #: embedding model of ``vector``/``hybrid`` searches (an Ollama model; the semantic index is built on first use).
    knowledge_retrieval: Literal["fts", "vector", "hybrid"] = "fts"
    embedding_model: str | None = None


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
        #: The web interface (:mod:`nodestro_api.webui_server`), set by :func:`create_app` when it is served.
        self.web_ui: Any = None
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
        sandbox: Sandbox
        if settings.tool_sandbox == "docker":
            sandbox = self._docker_sandbox(data_dir)
        else:
            sandbox = SubprocessSandbox(
                timeout=settings.tool_timeout_seconds, python=sys.executable, env=_TOOL_ENV, acknowledge_unsafe=True
            )
        tools = ToolLibrary(
            materialize_tools(self.bundle, data_dir / "tools"), sandbox=sandbox, provenance=self.bundle.provenance()
        )
        self.builtin = set(tools.builtin)
        builder = AgentBuilder(tools, self.knowledge, self.memory)
        builder.declared = lambda provider: dict(self.manifest.capabilities.get(provider) or {})
        # Local models: what the model server reports for each model (``/api/show``), as in the studio.
        self._reported: dict[str, tuple[float, dict[str, Any]]] = {}
        self._gpu: bool | None = None
        builder.reported = self.reported_capabilities
        self.knowledge.retrieval = lambda: (self.manifest.knowledge_retrieval, self.manifest.embedding_model)
        self.knowledge.embedding_model = self._embedding_model
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
        self.output_dir, self.output_warning = resolve_output_dir(project_dir, settings.output_dir)
        #: Checks a folder the operator chose on the settings page again before each copy (it may have been replaced
        #: by a link meanwhile); set by :class:`nodestro_api.output_folder.OutputFolder`, ``None`` for the default.
        self.output_check: Callable[[Path], Any] | None = None

        def after_run(run: Run) -> None:
            self.files.write_outputs(run)
            folder, check = self.output_dir, self.output_check
            if folder is None:
                return
            problem = check(folder) if check is not None else None
            if problem is not None:
                logger.warning("Result files of run %s stay in the database: %s (%s)", run.id, folder, problem)
                return
            copy_result_files(self.files, run, folder)

        self.runs.after_run = after_run
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

    def _docker_sandbox(self, data_dir: Path) -> DockerSandbox:
        """``TOOL_SANDBOX=docker``: Orkexa's container sandbox (its ``orkexa-sandbox`` image, which the operator builds
        from Orkexa's digest-pinned ``docker/sandbox.Dockerfile``; the self-test resolves the tag to an image id and
        containers run that id, never the tag). Nodestro's runtime image is not used: building it needs the runtime's
        Python environment manager, which an export does not carry. Untrusted tools run in it only once the daemon
        passed the preflight and the in-container self-test, which a background monitor
        (:mod:`nodestro_runtime.docker_status`) checks; until then they are refused. The containers carry this
        installation's own owner label, so the monitor's reaper (expired containers only) never touches another
        installation's containers on the same daemon."""
        owner = docker_status.installation_owner(data_dir)
        timeout = self.settings.tool_timeout_seconds

        def make() -> DockerSandbox:
            return DockerSandbox(timeout=timeout, owner=owner)

        self.docker_monitor = docker_status.monitor_for(make, owner=owner)
        self.docker_monitor.start_reaper(remove_all_first=False)
        self.docker_monitor.status()  # the first check starts in the background
        return make()

    # ---- local models ---------------------------------------------------------------

    def ollama_url(self) -> str | None:
        """The local model server's address (``OLLAMA_BASE_URL`` without ``/v1``; the launcher of a portable
        package sets it to the Ollama it started), else the exported one."""
        url = os.environ.get("OLLAMA_BASE_URL") or self.manifest.base_urls.get("ollama") or ""
        return url.removesuffix("/").removesuffix("/v1").rstrip("/") or None

    def reported_capabilities(self, model: str) -> dict[str, Any]:
        """What the local model server reports for ``model`` (``/api/show``), cached for ten minutes; ``{}`` while
        it cannot be asked (the agent check then names what is unknown)."""
        import time

        from orkexa.local import OllamaClient, OllamaError

        name = model.removeprefix("ollama:")
        cached = self._reported.get(name)
        if cached is not None and time.monotonic() - cached[0] < 600:
            return cached[1]
        url = self.ollama_url()
        if url is None:
            return {}
        try:
            details = OllamaClient(url, allow_remote=True, timeout=10).show(name)
        except (OllamaError, OSError, ValueError):
            return {}
        overrides = {key: str(value) for key, value in details.capability_overrides().items()}
        if "completion" in details.capabilities:  # as the studio: Ollama reports ``thinking`` for reasoning models
            overrides.setdefault("reasoning", "supported" if "thinking" in details.capabilities else "unsupported")
        self._reported[name] = (time.monotonic(), overrides)
        return overrides

    def reasoning_saving(self) -> bool:
        """The CPU saving mode of local thinking models, as in the studio: on a computer without a GPU they answer
        at effort ``medium`` without thinking (``NODESTRO_LOCAL_REASONING=full`` turns this off)."""
        if os.environ.get("NODESTRO_LOCAL_REASONING", "auto").strip().lower() == "full":
            return False
        if self._gpu is None:
            from nodestro_runtime.local_models import detect_hardware

            try:
                self._gpu = detect_hardware().gpu != "none"
            except Exception:  # unknown hardware never saves
                self._gpu = True
        return not self._gpu

    def _embedding_model(self, name: str) -> Any:
        from orkexa.embeddings import OllamaEmbeddings

        url = self.ollama_url()
        if url is None:
            raise NodestroError("local.not_running", "No local model server is configured (OLLAMA_BASE_URL)")
        return OllamaEmbeddings(name.removeprefix("ollama:"), base_url=url, allow_remote=True)

    def prepare_knowledge_index(self) -> None:
        """Build the semantic index of the included knowledge once (``vector``/``hybrid`` exports): the vectors are
        not part of an export, they are computed here with the same embedding model."""
        if self.manifest.knowledge_retrieval == "fts" or not self.manifest.embedding_model:
            return
        if not self.manifest.knowledge_included:
            return
        try:
            status = self.knowledge.index_status()
            if status.state in ("ready",) or (status.job is not None and status.job.state == "running"):
                return
            self.knowledge.start_reindex(self.manifest.embedding_model)
        except NodestroError as exc:
            logger.warning("The semantic index of the knowledge cannot be built yet: %s", exc)

    # ---- web interface --------------------------------------------------------------

    def session_identity(self, request: Request) -> KeyIdentity | None:
        """The identity of the web interface's session cookie (``None`` without the interface or a session)."""
        ui = getattr(self, "web_ui", None)
        return ui.identity(request) if ui is not None else None

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
        """The connection of a provider from the environment; raises when its key or endpoint is missing (Amazon
        Bedrock: ``provider.region_missing`` without a region; no AWS call is made here)."""
        client = self._client_factory(provider) if self._client_factory is not None else None
        found = environment_connection(
            provider,
            base_urls=self.manifest.base_urls,
            capabilities=self.manifest.capabilities,
            client=client,
            settings=self.manifest.settings,
        )
        if provider == "ollama":  # what the model server reports completes the declared capabilities
            found = dataclasses.replace(
                found, reported=self.reported_capabilities, reasoning_saving=self.reasoning_saving()
            )
        return found

    def providers(self) -> list[str]:
        """The providers of every model the release uses: agents, fallbacks, judges, orchestrators and the embedding
        model of semantic knowledge search (``bundle.models`` of older releases lacks the orchestrators)."""
        from .export_models import model_uses

        found = {provider_of(model) for model in self.bundle.models}
        uses = model_uses(
            self.bundle,
            embedding_model=self.manifest.embedding_model,
            workspace_mode=self.manifest.knowledge_retrieval,
        )
        found.update(use.provider for use in uses)
        return sorted(found)

    async def prepare_start(self, release: ServedRelease) -> None:
        try:
            # An Ollama connection asks the model server (/api/ps): never on the event loop (RT3-04 pattern).
            for provider in self.providers():
                await asyncio.to_thread(self.connection, provider)
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
        # Every run: the list is newest first, so a limit would leave out exactly the oldest ones.
        for summary in self.store.list("runs", RunSummary):
            if summary.created_at < cutoff and summary.status not in ("queued", "running", "paused"):
                run = self.store.get("runs", summary.id, Run)
                self.store.delete("runs", summary.id)
                self.files.delete_run(summary.id)
                self.store.delete_prefix("run_messages", f"{summary.id}:")  # its live chat (LRC-1)
                if run is not None:
                    self.inputs.release(run.file_ids)
                removed += 1
        self.inputs.cleanup()
        return removed


_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}
#: File names Windows reserves in every folder.
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
)


def resolve_output_dir(project_dir: Path, configured: str) -> tuple[Path | None, str | None]:
    """The folder result files are copied to, and a warning when the configured one cannot be used.

    A relative path is relative to the project. When the configured folder cannot be created or written (for
    example an absolute path of the exporting computer that does not exist here), the portable default
    ``results`` next to the project is used and the warning says why; ``(None, None)`` when nothing is configured.
    """
    if not configured.strip():
        return None, None
    wanted = Path(os.path.expandvars(os.path.expanduser(configured.strip())))
    target = wanted if wanted.is_absolute() else project_dir / wanted
    problem = _writable(target)
    if problem is None:
        return target.resolve(), None
    fallback = project_dir / "results"
    if fallback.resolve() != target.resolve() and _writable(fallback) is None:
        logger.warning("The output folder %s cannot be used (%s); result files go to %s", target, problem, fallback)
        return fallback.resolve(), f"{target}: {problem}"
    logger.warning("The output folder %s cannot be used (%s); result files stay in the database", target, problem)
    return None, f"{target}: {problem}"


def _writable(folder: Path) -> str | None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return exc.strerror or type(exc).__name__
    return None


def safe_file_name(name: str, fallback: str = "result") -> str:
    """A file name that is safe on Windows, macOS and Linux: no folders, no control or reserved characters, no
    reserved device names, at most 120 characters (the extension kept)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join("_" if ord(char) < 32 or char in '<>:"|?*' else char for char in base).strip(" .")
    if not base:
        base = fallback
    stem, dot, extension = base.rpartition(".")
    if not dot:
        stem, extension = base, ""
    if stem.split(".")[0].lower() in _RESERVED:
        stem = f"_{stem}"
    extension = extension[:16]
    stem = stem[: 120 - len(extension) - 1] if extension else stem[:120]
    return f"{stem}.{extension}" if extension else stem


def copy_result_files(files: ResultFiles, run: Run, folder: Path) -> list[Path]:
    """Copy a finished run's result files to ``<folder>/<date>_<run id>/``; never overwrites anything."""
    entries = [entry for entry in files.list(run.id) if not entry.error_code]
    if not entries:
        return []
    stamp = (run.finished_at or now()).strftime("%Y-%m-%d_%H%M%S")
    target = folder / f"{stamp}_{run.id}"
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    taken: set[str] = set()
    for entry in entries:
        _, data = files.content(run.id, entry.id)
        name = safe_file_name(entry.name)
        stem, dot, extension = name.rpartition(".")
        if not dot:
            stem, extension = name, ""
        number = 1
        while True:
            candidate = (
                name if number == 1 else (f"{stem} ({number}).{extension}" if extension else f"{stem} ({number})")
            )
            if candidate.lower() not in taken:
                try:
                    with (target / candidate).open("xb") as writer:  # exclusive: never overwrite
                        writer.write(data)
                except FileExistsError:
                    pass
                else:
                    taken.add(candidate.lower())
                    written.append(target / candidate)
                    break
            number += 1
    return written


#: The vendored Swagger UI (``nodestro_api/swagger_ui``, see ``VENDORED.md``; an export carries the files it serves).
_DOCS_DIR = Path(__file__).resolve().parent / "swagger_ui"
_DOCS_ASSETS = {
    "swagger-ui-bundle.js": "text/javascript; charset=utf-8",
    "swagger-ui.css": "text/css; charset=utf-8",
    "favicon-32x32.png": "image/png",
    "nodestro-init.js": "text/javascript; charset=utf-8",
}
#: The reference page's policy: its own files only (Swagger UI sets inline styles), no frames.
_DOCS_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


def _install_docs(app: FastAPI) -> None:
    """``/docs`` with the vendored Swagger UI: no CDN, no inline script, a strict Content-Security-Policy."""
    import html

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(app.title)} - API reference</title>
<link rel="icon" type="image/png" href="/docs/assets/favicon-32x32.png">
<link rel="stylesheet" href="/docs/assets/swagger-ui.css">
</head>
<body>
<div id="swagger-ui" data-openapi-url="/openapi.json"></div>
<script src="/docs/assets/swagger-ui-bundle.js"></script>
<script src="/docs/assets/nodestro-init.js"></script>
</body>
</html>
"""
    headers = {
        "Content-Security-Policy": _DOCS_CSP,
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    @app.get("/docs", include_in_schema=False)
    def docs() -> HTMLResponse:
        return HTMLResponse(page, headers=headers)

    @app.get("/docs/assets/{name}", include_in_schema=False)
    def docs_asset(name: str) -> Any:
        media_type = _DOCS_ASSETS.get(name)
        if media_type is None or not (_DOCS_DIR / name).is_file():
            return JSONResponse({"error": {"status": 404, "code": "not_found", "message": "Not found"}}, 404)
        return FileResponse(_DOCS_DIR / name, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})


def create_app(
    project_dir: Path,
    *,
    settings: StandaloneSettings | None = None,
    client_factory: Callable[[str], Any] | None = None,
    web_ui: Callable[[StandaloneHost, ServingCore], Any] | None = None,
) -> FastAPI:
    """The FastAPI application of an exported project; ``client_factory(provider)`` replaces SDK clients.

    ``web_ui(host, core)`` returns the :class:`~nodestro_api.webui_server.WebUi` to serve (the launcher of a portable
    package passes one with the operator's launch link and the provider setup); without it the interface, if the
    project has one and ``WEB_UI`` is not ``off``, offers sign-in with an API key only."""
    settings = settings or StandaloneSettings.from_env()
    host = StandaloneHost(project_dir, settings, client_factory=client_factory)
    core = ServingCore(host)
    ui = None
    if settings.web_ui == "on" and (Path(__file__).resolve().parent / "webui" / "index.html").is_file():
        from .webui_server import UiOptions, WebUi

        if web_ui is not None:
            ui = web_ui(host, core)
        else:
            ui = WebUi(
                host,
                UiOptions(
                    language=settings.ui_language,
                    output_dir=str(host.output_dir) if host.output_dir else None,
                    output_dir_warning=host.output_warning,
                ),
                auth_failed=core.auth_failed,
            )
        host.web_ui = ui

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        interrupted = host.runs.recover()
        if interrupted:
            logger.warning("%s runs were interrupted by the last stop; they are marked as failed", interrupted)
        removed = host.cleanup()
        if removed:
            logger.info("Deleted %s runs older than %s days", removed, settings.run_retention_days)
        if not settings.api_keys and ui is None:
            logger.warning("API_KEYS is empty: every request will be refused (see README, 'API keys')")
        host.prepare_knowledge_index()
        yield
        await host.runs.shutdown()
        host.close()

    app = FastAPI(
        title=host.served.name,
        version=str(host.served.number),
        description=host.served.description or f"Exported Nodestro workflow {host.served.slug}",
        lifespan=lifespan,
        # FastAPI's own page loads Swagger UI from a CDN into this origin (API2-01, API3-01): the reference is served
        # from the vendored files below instead, or not at all.
        docs_url=None,
        redoc_url=None,
        openapi_url=None if settings.api_docs == "off" else "/openapi.json",
    )
    app.state.host = host
    app.include_router(build_router(core, channel="network", release=host.served))
    # Request bodies are limited like the studio's (API3-02): the published routes that FastAPI parses before the
    # key check and the web interface's own endpoints; starting a run keeps the published APIs' own limit.
    app.add_middleware(BodyLimitMiddleware, limits=BodyLimits.from_env())
    if settings.api_docs != "off" and (_DOCS_DIR / "swagger-ui-bundle.js").is_file():
        _install_docs(app)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if ui is not None:
        from .webui_server import build_router as ui_router

        app.include_router(ui_router(ui))

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            document = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
            app.openapi_schema = add_schemas(document, host.served)
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]

    if settings.api_docs == "local":

        @app.middleware("http")
        async def local_docs(request: Request, call_next: Any) -> Any:
            if request.url.path in ("/docs", "/openapi.json") or request.url.path.startswith("/docs/"):
                client = request.client.host if request.client else ""
                if client not in _LOCAL:
                    return JSONResponse({"error": {"status": 404, "code": "not_found", "message": "Not found"}}, 404)
            return await call_next(request)

    return app
