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

``/api/metrics`` (Prometheus text format, :mod:`nodestro_runtime.axentra_metrics`) exists only when the operator set
``NODESTRO_METRICS_TOKEN``; it checks no login but that bearer token, and its output holds no tenant data.
``/api/notifications`` (the member's in-app inbox, :mod:`nodestro_api.api_notifications`) exists only with
``NODESTRO_AXENTRA_NOTIFY=1``.

A suspended tenant's requests are refused at once (every request checks the tenant). Work its context started in the
background before (batches, benchmarks with their judge, assistant answers, architect drafts, tool generation jobs,
live run chat answers, Python environment installations) is stopped within :attr:`TenantComposition.suspension_check`
seconds (:func:`stop_tenant_work`, security review SR-03, SR-03a, NF-1).

``nodestro-server api`` (:mod:`nodestro_api.server`) builds it from validated
:class:`~nodestro_runtime.server_settings.ServerSettings`. Not part of it yet: the published workflow APIs
(tenant-bound keys) and backups (``install``; operators use ``pg_dump``). Schedules fire in the worker process that
holds the scheduler lock, not in the API.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from concurrent.futures import Future
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
import logging
from pathlib import Path
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from nodestro_runtime.bedrock import BedrockOperatorConfig
from nodestro_runtime.extensions import Integration
from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.jobs import JobQueue, QueuedRuns
from nodestro_runtime.postgres_tables import tenant_storage
from nodestro_runtime.server_settings import TenantSecrets
from nodestro_runtime.storage_postgres import PostgresDatabase

from . import __version__
from .api import install_error_handlers, router
from .api_architect import router as architect_router
from .api_assistant import router as assistant_router
from .api_automation import router as automation_router
from .api_budget import router as budget_router
from .api_data import router as data_router
from .api_inputs import router as inputs_router
from .api_local_models import router as local_models_router
from .api_mcp import router as mcp_router
from .api_pricing import router as pricing_router
from .api_publish import router as publish_router
from .api_python import router as python_router
from .api_reference import install_api_reference
from .api_run_chat import router as run_chat_router
from .api_tool_approvals import router as tool_approvals_router
from .body_limit import BodyLimitMiddleware, BodyLimits
from .context import AppConfig, Nodestro
from .error_codes import router as error_codes_router
from .frontend import mount_frontend
from .identity import (
    LoginFlow,
    OidcClient,
    SameOriginMiddleware,
    SessionResolver,
    TenantWorkspaces,
    auth_router,
    redact_auth_logs,
    secure_url,
)
from .observability import RequestIdMiddleware
from .permissions import PUBLIC, READ, check_routes
from .pricing_setup import PRICE_REFERENCE_TENANT
from .security_headers import SecurityHeadersMiddleware
from .serving import PREFIX as PUBLISHED_PREFIX

logger = logging.getLogger(__name__)


@dataclass
class TenantComposition:
    """What a multi-tenant studio API is made of."""

    database: PostgresDatabase
    identity: IdentityStore
    #: The login flow: Nodestro's own client, or Axentra's (``identity.AxentraLoginFlow``, identity backend axentra).
    oidc: OidcClient | LoginFlow
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
    secrets: TenantSecrets | None = None
    #: The built studio interface, served for every path that is not an API route (``None``: API only).
    static_dir: Path | None = None
    #: Model endpoints (origins) tenants may use for the endpoint providers (none by default).
    endpoints: tuple[str, ...] = ()
    #: The operator's Amazon Bedrock roles per tenant (``None``: Bedrock is not available to tenants).
    bedrock: BedrockOperatorConfig | None = None
    #: ``GET /api/metrics``: the metrics (:class:`nodestro_runtime.axentra_metrics.ServerMetrics`) and the check of
    #: the scraper's ``Authorization`` header (``None``: no metrics endpoint).
    metrics: Any | None = None
    metrics_authorized: Callable[[str | None], bool] | None = None
    #: In-app notifications (:class:`nodestro_runtime.axentra_notify.AxentraNotifications`; ``None``: no inbox).
    notifications: Any | None = None
    #: How often the opened tenants are checked for a suspension that stops their background work (seconds).
    suspension_check: float = 5.0
    #: Input file bytes in Axentra's storage (:class:`nodestro_runtime.axentra_storage.AxentraFiles`,
    #: ``NODESTRO_FILES_BACKEND=axentra``); ``None``: on the data volume.
    files: Any | None = None
    #: The size limits of request bodies (``None``: from the environment, :class:`~nodestro_api.body_limit.BodyLimits`).
    body_limits: BodyLimits | None = None


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
    secrets: TenantSecrets | None = None
    endpoints: tuple[str, ...] = ()
    #: The operator's Amazon Bedrock roles; a tenant's workspace receives only its own entry (``for_tenant``).
    bedrock: BedrockOperatorConfig | None = None
    #: Input file bytes in Axentra's storage (``AxentraFiles``); ``None``: on the data volume.
    files: Any | None = None

    def open(self, tenant_id: str) -> Nodestro:
        files = self.files
        blobs = None if files is None else (lambda root: files.blobs(tenant_id, root))
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
                tenant_storage(self.database, tenant_id, self.secrets, self.endpoints, bedrock=self.bedrock),
                price_reference=self.database.store(PRICE_REFERENCE_TENANT),
                input_blobs=blobs,
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


#: The stop code of work stopped because its tenant was suspended.
TENANT_SUSPENDED = "identity.tenant_suspended"


async def stop_tenant_work(context: Nodestro) -> list[str]:
    """Stop the background work a tenant's context runs in this process (its tenant was suspended): benchmarks
    (their judge too), batches, assistant answers, architect drafts, tool generation jobs, live run chat answers and
    Python environment installations. Idempotent; returns the ids stopped."""
    stopped: list[str] = []
    # Benchmarks first: a benchmark follows its batch and would judge the batch's results once it is cancelled.
    for name, stop in (("benchmarks", context.benchmarks.cancel_all), ("batches", context.batches.cancel_all)):
        try:
            stopped += await stop(stop_code=TENANT_SUSPENDED)
        except Exception:
            logger.exception("Stopping the %s of a suspended tenant failed", name)
    for name, sync_stop in (("assistant", context.assistant.cancel_all), ("architect", context.architect.cancel_all)):
        try:
            stopped += sync_stop()
        except Exception:
            logger.exception("Stopping the %s of a suspended tenant failed", name)
    # NF-1: tool generation jobs (failed at once with the code; no further model request, a running sandbox test is
    # killed, a wait for a decision ends, the tool is never registered); before the installations, so a stopped job
    # starts none. SR-03a: live run chat answers (the question fails at once, a late answer is not stored) and Python
    # environment installations (stopped before their next step).
    for name, stop_with_code in (
        ("tool generation jobs", context.tools.cancel_jobs),
        ("run chat answers", context.runs.chat.cancel_answers),
        ("Python environment installations", context.python.cancel_installs),
    ):
        try:
            stopped += await asyncio.to_thread(stop_with_code, TENANT_SUSPENDED)
        except Exception:
            logger.exception("Stopping the %s of a suspended tenant failed", name)
    return stopped


async def stop_suspended_tenants(tenants: TenantWorkspaces, identity: IdentityStore) -> list[str]:
    """:func:`stop_tenant_work` for every opened tenant that is suspended now; returns the ids stopped."""
    stopped: list[str] = []
    for tenant_id, context in tenants.items():
        try:
            active = await asyncio.to_thread(identity.tenant_active, tenant_id)
        except Exception:
            logger.exception("Checking whether tenant %s is active failed", tenant_id)
            continue
        if not active:
            found = await stop_tenant_work(context)
            if found:
                logger.warning("Stopped %s background tasks of suspended tenant %s", len(found), tenant_id)
            stopped += found
    return stopped


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
        bedrock=composition.bedrock,
        files=composition.files,
    )
    queue = composition.queue

    def open_tenant(tenant_id: str) -> Nodestro:
        context = files.open(tenant_id)
        if queue is not None:
            QueuedRuns(queue, tenant_id, context.runs).attach()
        return context

    tenants = TenantWorkspaces(open_tenant, close_tenant)

    async def watch_suspensions() -> None:
        while True:
            await asyncio.sleep(composition.suspension_check)
            try:
                await stop_suspended_tenants(tenants, composition.identity)
            except Exception:
                logger.exception("The suspension check failed")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        watcher = asyncio.get_running_loop().create_task(watch_suspensions(), name="nodestro-suspensions")
        app.state.accepting = True
        try:
            yield
        finally:
            app.state.accepting = False  # /api/ready answers 503 while the process stops (API-011)
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
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
    # Inside the origin check: a cross-site request is refused before its body is read (API2-03).
    app.add_middleware(BodyLimitMiddleware, limits=composition.body_limits or BodyLimits.from_env())
    app.add_middleware(SameOriginMiddleware, origin=composition.origin, exempt=[PUBLISHED_PREFIX])
    # API2-02; the studio's origin is https (http only on localhost for development): HSTS also behind a TLS proxy.
    app.add_middleware(SecurityHeadersMiddleware, hsts=composition.origin.startswith("https://"))
    # Outermost: every answer, a refused one too, carries the request id, and every log line of the request (API-011).
    app.add_middleware(RequestIdMiddleware)
    app.state.accepting = False
    install_error_handlers(app)

    def audit(tenant_id: str, event: Any) -> None:
        from .access_audit import record_quietly

        try:
            record_quietly(tenants.get(tenant_id).store, event)
        except Exception:  # an unknown or unusable tenant: the login itself is not refused for it
            logger.exception("The access audit of tenant %s could not be written", tenant_id)

    app.include_router(auth_router(composition.identity, composition.oidc, audit))
    app.include_router(publish_router)
    app.include_router(architect_router)
    app.include_router(python_router)
    app.include_router(automation_router)
    app.include_router(assistant_router)
    app.include_router(inputs_router)
    app.include_router(pricing_router)
    app.include_router(local_models_router)
    app.include_router(mcp_router)
    app.include_router(budget_router)
    app.include_router(tool_approvals_router)
    app.include_router(run_chat_router)
    app.include_router(error_codes_router)
    app.include_router(router)
    app.include_router(data_router)
    if composition.notifications is not None:
        from .api_notifications import notifications_router

        app.include_router(notifications_router(composition.notifications))
    if composition.metrics is not None and composition.metrics_authorized is not None:
        _metrics(app, composition.metrics, composition.metrics_authorized)
    _readiness(app)
    _api_reference(app)
    check_routes(app)
    if composition.static_dir is not None and (composition.static_dir / "index.html").is_file():
        mount_frontend(app, composition.static_dir)
    return app


def _readiness(app: FastAPI) -> None:
    """``GET /api/ready`` (no login; API-011): whether a load balancer should send this process requests: it has
    started, is not stopping and reaches its database (``503`` otherwise). ``/api/health`` stays the liveness check
    with the database."""

    @app.get("/api/ready", dependencies=PUBLIC, include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        if not getattr(request.app.state, "accepting", False):
            return JSONResponse({"status": "unavailable", "reason": "stopping"}, status_code=503, headers=headers)
        if not await request.app.state.health_check():
            return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503, headers=headers)
        return JSONResponse({"status": "ready", "database": "ok"}, headers=headers)


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


def _metrics(app: FastAPI, metrics: Any, authorized: Callable[[str | None], bool]) -> None:
    """``GET /api/metrics`` for the operator's scraper: no login, the bearer token instead (``401`` without it)."""

    # PUBLIC: no principal is checked; the handler checks the operator's token (constant-time) before anything else.
    @app.get("/api/metrics", dependencies=PUBLIC, include_in_schema=False)
    def metrics_endpoint(request: Request) -> PlainTextResponse:
        if not authorized(request.headers.get("authorization")):
            return PlainTextResponse(
                "metrics.unauthorized: send the operator's metrics token as a bearer token\n",
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="metrics"', "Cache-Control": "no-store"},
            )
        return PlainTextResponse(
            metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )


def _api_reference(app: FastAPI) -> None:
    """The OpenAPI document and its reference page, for logged-in members only (``read``)."""

    @app.get("/api/openapi.json", dependencies=READ, include_in_schema=False)
    def openapi_document() -> JSONResponse:
        return JSONResponse(app.openapi())

    install_api_reference(app, "/api/openapi.json", dependencies=READ)


def _secure_origin(origin: str) -> bool:
    return secure_url(origin)


__all__ = [
    "TENANT_SUSPENDED",
    "DatabaseHealth",
    "TenantComposition",
    "TenantFiles",
    "close_tenant",
    "create_tenant_app",
    "mount_frontend",
    "stop_suspended_tenants",
    "stop_tenant_work",
]
