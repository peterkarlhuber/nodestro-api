"""The application context: the runtime workspace plus the services of the HTTP API.

:class:`Nodestro` is a :class:`~nodestro_runtime.workspace.Workspace` (settings, storage,
providers and the one workflow engine) extended by what only the API offers: the Workflow
Architect and the Nodestro Assistant, automation (batches, test series, schedules),
organisation and search, publishing with its keys and the network listener.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any, Literal

from nodestro_runtime.extensions import Integration
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import ProfileKind, RuntimeProfile
from nodestro_runtime.schemas import Run
from nodestro_runtime.workspace import Workspace, WorkspaceStorage

from . import exporter
from .api_keys import ApiKeys
from .architect import WorkflowArchitect
from .assistant import Assistant
from .assistants import Assistants
from .batches import Batches
from .benchmarks import Benchmarks
from .docs_index import DocsIndex
from .launch import launch_secret_from_env
from .local_container import access_key_from_env, browser_host_from_env, host_port_from_env, session_hours_from_env
from .mcp_server import McpServerEndpoint
from .network import NetworkListener
from .organize import Organizer
from .pricing_setup import PriceSyncConfig
from .publishing import Publications, StudioHost
from .schedules import Schedules
from .serving import ApiError, KeyIdentity, ResumeIn, ServedRelease, ServingCore

#: The AWS SDK's loggers: at DEBUG, botocore logs signed request headers (``Authorization``, temporary
#: ``X-Amz-Security-Token``s of a tenant's assumed role) and response bodies. A process-wide DEBUG logging setting
#: (``logging.basicConfig(level=DEBUG)``) must not write AWS credentials into the logs, so they start at WARNING
#: (an operator who needs botocore's debug output sets it on these loggers explicitly).
AWS_LOGGERS = ("botocore", "boto3", "s3transfer")


def quiet_aws_loggers() -> None:
    for name in AWS_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET:  # a level set explicitly before stays
            logger.setLevel(logging.WARNING)


quiet_aws_loggers()


@dataclass
class AppConfig:
    data_dir: Path
    #: The built interface to serve next to the API (the studio passes its own; ``None``: API only).
    static_dir: Path | None = None
    #: The documentation chapters the assistant reads (the studio passes its own).
    docs_dir: Path | None = None
    #: The interface the server listens on (``NODESTRO_HOST``); with ``0.0.0.0`` every IP address
    #: and this computer's name are answered.
    host: str = "127.0.0.1"
    #: Host names answered besides this computer's (``NODESTRO_ALLOWED_HOSTS``; ``*``: all), see
    #: :mod:`nodestro_api.local_access`.
    allowed_hosts: list[str] = field(default_factory=list)
    #: The operating profile (``NODESTRO_PROFILE``): ``desktop`` (default), ``local-container`` (Nodestro Local
    #: Docker, :mod:`nodestro_api.local_container`) or ``server``, see :attr:`profile`.
    profile_kind: ProfileKind = "desktop"
    #: The desktop app's per-launch secret (``NODESTRO_LAUNCH_SECRET``); with it, only the window that
    #: opened ``/api/launch`` can use the studio API, see :mod:`nodestro_api.launch`. ``None``: not required.
    launch_secret: str | None = field(default=None, repr=False)
    #: Nodestro Local Docker's persistent access key (``NODESTRO_ACCESS_KEY_FILE``); required in the
    #: ``local-container`` profile, see :mod:`nodestro_api.local_container`.
    access_key: str | None = field(default=None, repr=False)
    #: Nodestro Local Docker's browser host name (``nodestro-<random>.localhost``, or ``NODESTRO_BROWSER_HOST``):
    #: browser sessions exist only under this name. Required in the ``local-container`` profile.
    browser_host: str | None = field(default=None, repr=False)
    #: The port Docker publishes on the host (``NODESTRO_HOST_PORT``); Nodestro Local Docker answers only it and
    #: the container's own port.
    host_port: int = 8765
    #: Hours a browser session lasts without a request, and at most (Nodestro Local Docker).
    session_idle_hours: float = 8.0
    session_max_hours: float = 24.0
    #: Where reference prices come from and whether they are synced in the background (``NODESTRO_PRICE_*``,
    #: see :mod:`nodestro_api.pricing_setup`). Off unless configured: tests and embedded uses never go online.
    price_sync: PriceSyncConfig = field(default_factory=lambda: PriceSyncConfig(enabled=False))

    @property
    def profile(self) -> RuntimeProfile:
        """The operating profile on :attr:`data_dir` (built on each access, so the two never disagree).

        Raises ``profile.invalid`` for an unknown :attr:`profile_kind`; :meth:`RuntimeProfile.validate` tells
        whether it can be started."""
        return RuntimeProfile(kind=self.profile_kind, data_dir=self.data_dir)

    @classmethod
    def from_env(cls) -> AppConfig:
        """The configuration from the environment; ``NODESTRO_PROFILE`` and ``NODESTRO_DATA_DIR`` follow
        :meth:`RuntimeProfile.from_env` (unknown values are refused with ``profile.invalid``)."""
        profile = RuntimeProfile.from_env(os.environ)
        static = os.environ.get("NODESTRO_STATIC_DIR")
        docs = os.environ.get("NODESTRO_DOCS_DIR")
        allowed = os.environ.get("NODESTRO_ALLOWED_HOSTS", "")
        local = profile.kind == "local-container"
        idle, longest = session_hours_from_env() if local else (8.0, 24.0)
        return cls(
            data_dir=profile.data_dir,
            static_dir=Path(static) if static else None,
            docs_dir=Path(docs) if docs else None,
            host=os.environ.get("NODESTRO_HOST", "127.0.0.1"),
            allowed_hosts=[item.strip() for item in allowed.split(",") if item.strip()],
            profile_kind=profile.kind,
            launch_secret=launch_secret_from_env(),
            access_key=access_key_from_env(),
            browser_host=browser_host_from_env() if local else None,
            host_port=host_port_from_env() if local else 8765,
            session_idle_hours=idle,
            session_max_hours=longest,
            price_sync=PriceSyncConfig.from_env(),
        )


class Nodestro(Workspace):
    """Owns every service; one instance per running app."""

    def __init__(
        self,
        config: AppConfig,
        *,
        client_factory: Callable[[str], Any] | None = None,
        python_runner: Any | None = None,
        ocr_runner: Any | None = None,
        integrations: Iterable[Integration] | None = None,
        storage: WorkspaceStorage | None = None,
    ) -> None:
        """``client_factory(provider)`` replaces the SDK clients, ``python_runner`` uv, ``ocr_runner`` the
        OCR (tests); ``integrations`` replaces the installed integrations. ``storage`` runs the context on the
        stores of one tenant (:mod:`nodestro_api.server_app`) instead of the data directory's database."""
        self.config = config
        #: The context runs on a server tenant's handed-in storage, which other processes share: batches and test
        #: series that look ``running`` may execute in another process right now, so opening the context never
        #: marks them as interrupted (C-07; the engine skips its run recovery on such storage as well).
        self.shared_storage = storage is not None
        super().__init__(
            profile=config.profile,
            client_factory=client_factory,
            python_runner=python_runner,
            ocr_runner=ocr_runner,
            integrations=integrations,
            storage=storage,
        )

    def compose(self) -> None:
        self.organize = Organizer(self.store)
        self.assistants = Assistants(self.tools, capabilities=self.builder.capabilities)
        self.batches = Batches(self)
        self.benchmarks = Benchmarks(self)
        self.schedules = Schedules(self)
        self.architect = WorkflowArchitect(self)
        self.assistant = Assistant(self, DocsIndex(self.config.docs_dir))
        # Published APIs: releases run with their own frozen environment on the same engine.
        self.publications = Publications(self)
        self._normalize_published()
        self.api_keys = ApiKeys(self.store)
        self.runs.environments = lambda run: self.publications.environment(run.release_id) if run.release_id else None
        self.serving = ServingCore(StudioHost(self))
        # Nodestro's MCP server (WP-11): off unless the operator and an administrator switch it on.
        self.mcp_server = McpServerEndpoint(self.serving, self._mcp_catalog)
        self.network = NetworkListener(
            self.store,
            self.serving,
            unavailable="Nodestro Local Docker offers published APIs only on this computer (its port is on the "
            "host's loopback); network access is not available"
            if self.config.profile_kind == "local-container"
            else None,
        )
        self.published = _PublishedRuns(self)
        #: Syncs the reference prices; ``None`` on a server, whose worker syncs them (see :mod:`.pricing_setup`).
        self.price_sync = self.config.price_sync.service(self.pricing) if self.pricing.reference_writable else None

    def _mcp_catalog(self) -> list[ServedRelease]:
        """The active releases of the publications an administrator offered as MCP tools."""
        found = []
        for publication in self.publications.all():
            if publication.mcp_tool and publication.active_release is not None:
                release = self.publications.release(publication.id, publication.active_release)
                found.append(self.publications.served(publication, release))
        return found

    def _normalize_published(self) -> None:
        """Releases and export records in the current format, also after a restore (idempotent)."""
        self.publications.normalize_stored()
        exporter.normalize_records(self.store)

    def reload_after_restore(self) -> None:
        """Pick up restored data that is cached in memory: the tool registry and its sandbox."""
        super().reload_after_restore()
        self._normalize_published()
        self.network.disable_after_restore()


class _PublishedRuns:
    """Continuing API runs from the studio: same release, same limits, as "Nodestro"."""

    def __init__(self, nodestro: Nodestro) -> None:
        self.nodestro = nodestro

    def release_exists(self, release_id: str) -> bool:
        return self.nodestro.publications.release_by_id(release_id) is not None

    async def resume(self, source: Run, *, mode: Literal["continue", "restart"], confirm: bool) -> Run:
        ctx = self.nodestro
        publication = ctx.publications.get(source.trigger_ref or "")
        release = ctx.publications.release_by_id(source.release_id or "")
        if release is None:
            raise NodestroError("resume.release_deleted", "The published version of this run no longer exists")
        served = ctx.publications.served(publication, release)
        identity = KeyIdentity(id="studio", name="Nodestro", scopes=["run"], publications=[publication.id])
        try:
            return await ctx.serving.resume(
                identity, served, source, ResumeIn(confirm_side_effects=confirm, restart=mode == "restart")
            )
        except ApiError as exc:
            raise NodestroError(exc.code, exc.message, **exc.params) from None
