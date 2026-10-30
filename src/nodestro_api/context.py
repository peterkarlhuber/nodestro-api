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
from nodestro_runtime.schemas import Run
from nodestro_runtime.workspace import Workspace

from . import exporter
from .api_keys import ApiKeys
from .architect import WorkflowArchitect
from .assistant import Assistant
from .assistants import Assistants
from .batches import Batches
from .benchmarks import Benchmarks
from .docs_index import DocsIndex
from .network import NetworkListener
from .organize import Organizer
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

    @classmethod
    def from_env(cls) -> AppConfig:
        data = Path(os.environ.get("NODESTRO_DATA_DIR", Path.home() / ".nodestro")).expanduser()
        static = os.environ.get("NODESTRO_STATIC_DIR")
        docs = os.environ.get("NODESTRO_DOCS_DIR")
        allowed = os.environ.get("NODESTRO_ALLOWED_HOSTS", "")
        return cls(
            data_dir=data,
            static_dir=Path(static) if static else None,
            docs_dir=Path(docs) if docs else None,
            host=os.environ.get("NODESTRO_HOST", "127.0.0.1"),
            allowed_hosts=[item.strip() for item in allowed.split(",") if item.strip()],
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
    ) -> None:
        """``client_factory(provider)`` replaces the SDK clients, ``python_runner`` uv, ``ocr_runner`` the
        OCR (tests); ``integrations`` replaces the installed integrations."""
        self.config = config
        super().__init__(
            config.data_dir,
            client_factory=client_factory,
            python_runner=python_runner,
            ocr_runner=ocr_runner,
            integrations=integrations,
        )

    def compose(self) -> None:
        self.organize = Organizer(self.store)
        self.assistants = Assistants(self.tools)
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
