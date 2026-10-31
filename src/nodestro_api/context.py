"""The application context: the runtime workspace plus the services of the HTTP API.

:class:`Nodestro` is a :class:`~nodestro_runtime.workspace.Workspace` (settings, storage,
providers and the one workflow engine) extended by what only the API offers: the Workflow
Architect and the Nodestro Assistant, automation (batches, test series, schedules),
organisation and search, publishing with its keys and the network listener.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
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
from .network import NetworkListener
from .organize import Organizer
from .pricing_setup import PriceSyncConfig
from .publishing import Publications, StudioHost
from .schedules import Schedules
from .serving import ApiError, KeyIdentity, ResumeIn, ServingCore


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
    #: The operating profile (``NODESTRO_PROFILE``): ``desktop`` (default) or ``server``, see :attr:`profile`.
    profile_kind: ProfileKind = "desktop"
    #: The desktop app's per-launch secret (``NODESTRO_LAUNCH_SECRET``); with it, only the window that
    #: opened ``/api/launch`` can use the studio API, see :mod:`nodestro_api.launch`. ``None``: not required.
    launch_secret: str | None = field(default=None, repr=False)
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
        return cls(
            data_dir=profile.data_dir,
            static_dir=Path(static) if static else None,
            docs_dir=Path(docs) if docs else None,
            host=os.environ.get("NODESTRO_HOST", "127.0.0.1"),
            allowed_hosts=[item.strip() for item in allowed.split(",") if item.strip()],
            profile_kind=profile.kind,
            launch_secret=launch_secret_from_env(),
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
        self.network = NetworkListener(self.store, self.serving)
        self.published = _PublishedRuns(self)
        #: Syncs the reference prices; ``None`` on a server, whose worker syncs them (see :mod:`.pricing_setup`).
        self.price_sync = self.config.price_sync.service(self.pricing) if self.pricing.reference_writable else None

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
