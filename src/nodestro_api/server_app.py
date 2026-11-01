"""The studio API for many tenants with logins: the composition the server profile will start (ADR-0003).

:func:`create_tenant_app` builds the same routes as :func:`nodestro_api.app.create_app`, but every request needs a
session (:mod:`nodestro_api.identity`) and works on the context of the session's tenant: one
:class:`~nodestro_api.context.Nodestro` per tenant on that tenant's stores in PostgreSQL
(:func:`nodestro_runtime.postgres_tables.tenant_storage`) and its own file area below ``data_root``.

With a job queue (:attr:`TenantComposition.queue`, ADR-0004) the API processes execute no runs themselves: they
store and queue them, and worker processes (:mod:`nodestro_api.worker`) execute them; event streams and waits read
the worker's events from the database. Without a queue, runs execute in the API process (tests).

Unlike the desktop, the API reference (``/api/docs``, ``/api/openapi.json``) needs a login (``read``), and
``/api/health`` also reports whether the process reaches its database (``503`` when not).

``nodestro-server api`` (:mod:`nodestro_api.server`) builds it from validated
:class:`~nodestro_runtime.server_settings.ServerSettings`. Not part of it yet: the published workflow APIs
(tenant-bound keys) and backups (``install``; operators use ``pg_dump``). Schedules fire in the worker process that
holds the scheduler lock, not in the API.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
import logging
from pathlib import Path
import threading
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
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
from .api_mcp import router as mcp_router
from .api_pricing import router as pricing_router
from .api_publish import router as publish_router
from .api_python import router as python_router
from .api_run_chat import router as run_chat_router
from .api_tool_approvals import router as tool_approvals_router
from .context import AppConfig, Nodestro
from .frontend import mount_frontend
from .identity import (
    OidcClient,
    SameOriginMiddleware,
    SessionResolver,
    TenantWorkspaces,
    auth_router,
    redact_auth_logs,
    secure_url,
)
from .permissions import READ, check_routes
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
            # A tenant is served by the server profile (AD-05: /api/meta says so; single-user fallbacks such as
            # provider keys from the environment stay off).
            AppConfig(data_dir=self.data_root / "tenants" / tenant_id, docs_dir=self.docs_dir, profile_kind="server"),
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

    # The API reference needs a login on a server (A-09): FastAPI's own public routes are off, and
    # :func:`_api_reference` serves the same pages with the ``read`` requirement.
    app = FastAPI(
        title="Nodestro",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    redact_auth_logs()
    app.state.tenants = tenants
    app.state.health_check = DatabaseHealth(composition.database)
    # The configured public origin (never a request's Host header): callbacks such as the MCP sign-in use it.
    app.state.public_origin = composition.origin
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
    app.include_router(mcp_router)
    app.include_router(tool_approvals_router)
    app.include_router(run_chat_router)
    app.include_router(router)
    app.include_router(data_router)
    _api_reference(app)
    check_routes(app)
    if composition.static_dir is not None and (composition.static_dir / "index.html").is_file():
        mount_frontend(app, composition.static_dir)
    return app


class DatabaseHealth:
    """``/api/health`` on a server (D-10): whether this process reaches its database (``SELECT 1``).

    At most one check runs at a time, so an unreachable database cannot tie up the worker threads with waiting
    checks; an answer that takes longer than ``timeout`` seconds counts as unreachable."""

    def __init__(self, database: PostgresDatabase, *, timeout: float = 2.0) -> None:
        self.database = database
        self.timeout = timeout
        self._pending: Future[None] | None = None
        self._lock = threading.Lock()

    def _ping(self) -> None:
        # ``ping`` never raises; its timeout bounds the wait for a pooled connection, ``__call__`` the whole check.
        if not self.database.ping(timeout=self.timeout):
            raise RuntimeError("The database does not answer")

    def _start(self) -> Future[None]:
        """Run one check in a daemon thread of its own: it never waits in (or blocks) the pool that serves
        requests, and a check that hangs cannot keep the process from stopping."""
        found: Future[None] = Future()

        def check() -> None:
            if not found.set_running_or_notify_cancel():
                return
            try:
                self._ping()
            except BaseException as exc:
                found.set_exception(exc)
            else:
                found.set_result(None)

        threading.Thread(target=check, name="nodestro-health", daemon=True).start()
        return found

    async def __call__(self) -> bool:
        with self._lock:
            if self._pending is None or self._pending.done():
                self._pending = self._start()
            pending = self._pending
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(pending)), timeout=self.timeout)
        except Exception as exc:
            logger.warning("Health check: the database is not reachable (%s)", type(exc).__name__)
            return False
        return True


def _api_reference(app: FastAPI) -> None:
    """The OpenAPI document and its reference page, for logged-in members only (``read``)."""

    @app.get("/api/openapi.json", dependencies=READ, include_in_schema=False)
    def openapi_document() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/api/docs", dependencies=READ, include_in_schema=False)
    def api_docs() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title=f"{app.title} - API reference")


def _secure_origin(origin: str) -> bool:
    return secure_url(origin)


__all__ = ["DatabaseHealth", "TenantComposition", "TenantFiles", "close_tenant", "create_tenant_app", "mount_frontend"]
