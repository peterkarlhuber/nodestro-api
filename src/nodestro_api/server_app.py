"""The studio API for many tenants with logins: the composition the server profile will start (ADR-0003).

:func:`create_tenant_app` builds the same routes as :func:`nodestro_api.app.create_app`, but every request needs a
session (:mod:`nodestro_api.identity`) and works on the context of the session's tenant: one
:class:`~nodestro_api.context.Nodestro` per tenant on that tenant's stores in PostgreSQL
(:func:`nodestro_runtime.postgres_tables.tenant_storage`) and its own file area below ``data_root``.

With a job queue (:attr:`TenantComposition.queue`, ADR-0004) the API processes execute no runs themselves: they
store and queue them, and worker processes (:mod:`nodestro_api.worker`) execute them; event streams and waits read
the worker's events from the database. Without a queue, runs execute in the API process (tests).

``nodestro-server api`` (:mod:`nodestro_api.server`) builds it from validated
:class:`~nodestro_runtime.server_settings.ServerSettings`. Not part of it yet: the published workflow APIs
(tenant-bound keys) and backups (``install``; operators use ``pg_dump``). Schedules fire in the worker process that
holds the scheduler lock, not in the API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nodestro_runtime.extensions import Integration
from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.jobs import JobQueue, QueuedRuns
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.server_settings import SecretBox
from nodestro_runtime.storage_postgres import PostgresDatabase

from . import __version__
from .api import install_error_handlers, router
from .api_architect import router as architect_router
from .api_assistant import router as assistant_router
from .api_automation import router as automation_router
from .api_data import router as data_router
from .api_inputs import router as inputs_router
from .api_pricing import router as pricing_router
from .api_publish import router as publish_router
from .api_python import router as python_router
from .api_tool_approvals import router as tool_approvals_router
from .context import AppConfig, Nodestro
from .identity import (
    OidcClient,
    SameOriginMiddleware,
    SessionResolver,
    TenantWorkspaces,
    auth_router,
    secure_url,
)
from .permissions import check_routes
from .pricing_setup import PRICE_REFERENCE_TENANT
from .serving import PREFIX as PUBLISHED_PREFIX

logger = logging.getLogger(__name__)


@dataclass
class TenantComposition:
    """What a multi-tenant studio API is made of."""

    database: PostgresDatabase
    identity: IdentityStore
    oidc: OidcClient
    #: The studio's own origin as browsers see it (``https://nodestro.example.com``), for the CSRF check.
    origin: str
    #: Files of each tenant live in ``<data_root>/tenants/<tenant id>``.
    data_root: Path
    docs_dir: Path | None = None
    client_factory: Callable[[str], Any] | None = None
    python_runner: Any | None = None
    ocr_runner: Any | None = None
    integrations: Iterable[Integration] | None = field(default=None)
    #: The job queue: runs are queued for worker processes instead of executing here (``None``: here).
    queue: JobQueue | None = None
    #: Encrypts the tenants' provider keys at rest (``None`` only in tests).
    secrets: SecretBox | None = None
    #: The built studio interface, served for every path that is not an API route (``None``: API only).
    static_dir: Path | None = None
    #: Model endpoints (origins) tenants may use for the endpoint providers (none by default).
    endpoints: tuple[str, ...] = ()


@dataclass
class TenantFiles:
    """How the context of one tenant is opened (shared by API and worker processes)."""

    database: PostgresDatabase
    #: Files of each tenant live in ``<data_root>/tenants/<tenant id>``.
    data_root: Path
    docs_dir: Path | None = None
    client_factory: Callable[[str], Any] | None = None
    python_runner: Any | None = None
    ocr_runner: Any | None = None
    integrations: Iterable[Integration] | None = field(default=None)
    secrets: SecretBox | None = None
    endpoints: tuple[str, ...] = ()

    def open(self, tenant_id: str) -> Nodestro:
        return Nodestro(
            AppConfig(data_dir=self.data_root / "tenants" / tenant_id, docs_dir=self.docs_dir),
            client_factory=self.client_factory,
            python_runner=self.python_runner,
            ocr_runner=self.ocr_runner,
            integrations=self.integrations,
            # Reference prices are the server's (read-only for tenants); overrides stay in the tenant's store.
            storage=replace(
                tenant_storage(self.database, tenant_id, self.secrets, self.endpoints),
                price_reference=self.database.store(PRICE_REFERENCE_TENANT),
            ),
        )


async def close_tenant(context: Nodestro) -> None:
    """Stop what a tenant's context runs in this process, then close it."""
    for name, stop in (("batches", context.batches.shutdown), ("runs", context.runs.shutdown)):
        try:
            await stop()
        except Exception:
            logger.exception("Stopping the %s of a tenant failed", name)
    context.close()


def create_tenant_app(composition: TenantComposition) -> FastAPI:
    """The studio API with logins and one context per tenant (see the module documentation)."""
    if not _secure_origin(composition.origin):
        raise ValueError("The studio of a server is reached over https (http only on localhost for development)")
    files = TenantFiles(
        database=composition.database,
        data_root=composition.data_root,
        docs_dir=composition.docs_dir,
        client_factory=composition.client_factory,
        python_runner=composition.python_runner,
        ocr_runner=composition.ocr_runner,
        integrations=composition.integrations,
        secrets=composition.secrets,
        endpoints=composition.endpoints,
    )
    queue = composition.queue

    def open_tenant(tenant_id: str) -> Nodestro:
        context = files.open(tenant_id)
        if queue is not None:
            QueuedRuns(queue, tenant_id, context.runs).attach()
        return context

    tenants = TenantWorkspaces(open_tenant, close_tenant)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await tenants.aclose()

    app = FastAPI(
        title="Nodestro",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.tenants = tenants
    app.state.principal_resolver = SessionResolver(composition.identity)
    app.add_middleware(SameOriginMiddleware, origin=composition.origin, exempt=[PUBLISHED_PREFIX])
    install_error_handlers(app)
    app.include_router(auth_router(composition.identity, composition.oidc))
    app.include_router(publish_router)
    app.include_router(architect_router)
    app.include_router(python_router)
    app.include_router(automation_router)
    app.include_router(assistant_router)
    app.include_router(inputs_router)
    app.include_router(pricing_router)
    app.include_router(tool_approvals_router)
    app.include_router(router)
    app.include_router(data_router)
    check_routes(app)
    if composition.static_dir is not None and (composition.static_dir / "index.html").is_file():
        mount_frontend(app, composition.static_dir)
    return app


def _secure_origin(origin: str) -> bool:
    return secure_url(origin)


def mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the built single-page studio: real files as they are, every other non-API path as ``index.html``."""
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index = static_dir / "index.html"
    root = static_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> Any:
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)


__all__ = ["TenantComposition", "TenantFiles", "close_tenant", "create_tenant_app", "mount_frontend"]
