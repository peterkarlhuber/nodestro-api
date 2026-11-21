"""The FastAPI application: the studio REST API, run events and the published workflow APIs.

The application serves no interface of its own; ``nodestro-studio`` adds the built frontend
(:func:`nodestro_studio.app.create_app`). Started alone (``nodestro-api``) it is a complete
HTTP backend for scripts, the SDKs and published workflows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
import logging
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from nodestro_runtime.extensions import Integration
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.workspace import DataDirectoryLock

from . import __version__, recovery
from .api import install_error_handlers
from .api_reference import install_api_reference
from .app_contract import ContractHeaderMiddleware
from .body_limit import BodyLimitMiddleware, BodyLimits
from .context import AppConfig, Nodestro
from .launch import LOCAL_COOKIE, LaunchSecretMiddleware, check_secret
from .local_access import LocalAccessMiddleware, allowed_hosts, listens_everywhere, require_loopback_only
from .local_container import CONTAINER_PORT, loopback_hosts, require_safe_configuration
from .local_container import REFUSAL as LOCAL_REFUSAL
from .permissions import check_routes, resolver_for
from .router_groups import GROUPS, REQUIRED_GROUPS, check_groups, include_groups
from .security_headers import SecurityHeadersMiddleware

logger = logging.getLogger(__name__)


def create_app(
    config: AppConfig | None = None,
    *,
    client_factory: Callable[[str], Any] | None = None,
    python_runner: Any | None = None,
    ocr_runner: Any | None = None,
    integrations: Iterable[Integration] | None = None,
    scheduler: bool = True,
) -> FastAPI:
    """Build the app; ``client_factory(provider)`` replaces a provider's SDK client, ``python_runner`` uv,
    ``ocr_runner`` the local OCR (tests). ``scheduler=False`` leaves schedules to explicit ticks (tests).

    Before anything is opened, the configuration is checked as the command line checks it: the operating
    profile must be the desktop or Nodestro Local Docker (``profile.invalid``; a server starts with
    ``nodestro-server``), the login-less studio API is only offered on this computer
    (``access.network_needs_login``), a local container needs its access key (``access.key_required``), and a
    launch secret or access key must be long enough (``access.launch_secret_weak``).
    Every route must declare who may call it (:mod:`nodestro_api.permissions`).

    A desktop database that the runtime refuses but the user may repair (``storage.unsafe_file`` with ``fixable``),
    or a start the desktop shell asked for with ``NODESTRO_RECOVERY``, gives the recovery app instead: the same guards
    and only the confirmed repair of the database's permissions (:mod:`nodestro_api.recovery`, TABRION MIG-NOD-9)."""
    return compose_app(
        config or AppConfig.from_env(),
        GROUPS,
        client_factory=client_factory,
        python_runner=python_runner,
        ocr_runner=ocr_runner,
        integrations=integrations,
        scheduler=scheduler,
    )


def compose_app(
    config: AppConfig,
    groups: Iterable[str],
    *,
    routers: Iterable[APIRouter] = (),
    title: str = "Nodestro",
    version: str = __version__,
    description: str = "",
    client_factory: Callable[[str], Any] | None = None,
    python_runner: Any | None = None,
    ocr_runner: Any | None = None,
    integrations: Iterable[Integration] | None = None,
    scheduler: bool = True,
) -> FastAPI:
    """The app of :func:`create_app` with only the router ``groups`` of :mod:`nodestro_api.router_groups` (the
    required ones always) and then ``routers``: the same checks of the configuration, guards, error answers,
    background services and fail-closed access check (:func:`nodestro_api.host.create_product_app` documents it)."""
    selected = check_groups(groups) | REQUIRED_GROUPS
    profile = config.profile.validate()
    if profile.kind == "desktop":
        require_loopback_only(config.host, config.allowed_hosts)
    elif profile.kind == "local-container":
        require_safe_configuration(config.access_key, config.allowed_hosts, config.browser_host)
    check_secret(config.launch_secret)
    check_secret(config.access_key)
    if recovery.requested(config):
        return _recovery_app(config, None)
    try:
        nodestro = Nodestro(
            config,
            client_factory=client_factory,
            python_runner=python_runner,
            ocr_runner=ocr_runner,
            integrations=integrations,
        )
    except NodestroError as error:
        # A database that only the user's confirmation can repair (TABRION MIG-NOD-9): recovery mode, nothing else.
        if not recovery.recoverable(config, error):
            raise
        return _recovery_app(config, error)

    # /docs belongs to the built-in user documentation; the API reference lives under /api (its page with the
    # vendored Swagger UI: nodestro_api.api_reference).
    app = FastAPI(
        title=title,
        version=version,
        description=description,
        lifespan=service_lifespan(nodestro, scheduler=scheduler),
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.nodestro = nodestro
    # Who sends a request: on the desktop always the local owner (every permission).
    app.state.principal_resolver = resolver_for(profile)
    install_guards(app, config)
    # One registry keeps the order rules between routers whose paths overlap (nodestro_api.router_groups).
    include_groups(app, selected, nodestro)
    for extra in routers:
        app.include_router(extra)
    install_api_reference(app, "/api/openapi.json")
    # Fail closed: an application with a route that declares no access requirement is not built.
    check_routes(app)
    return app


def service_lifespan(
    nodestro: Nodestro, *, scheduler: bool = True
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """The background services of an app on one workspace, as a FastAPI ``lifespan``: schedules (unless
    ``scheduler=False``), the network listener, the managed local models, the reference-price sync, the sweep of
    paused runs and the retention of chat files; at the end every service is stopped and the workspace closed."""

    async def shutdown() -> None:
        # Every service is stopped even when one of them fails, and the database is closed last.
        for name, stop in (
            ("network listener", nodestro.network.stop),
            ("scheduler", nodestro.schedules.stop),
            ("batches", nodestro.batches.shutdown),
            ("runs", nodestro.runs.shutdown),
        ):
            try:
                await stop()
            except Exception:
                logger.exception("Stopping the %s failed", name)
        nodestro.close()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop_prices = asyncio.Event()
        prices: asyncio.Task[None] | None = None
        sweeping: asyncio.Task[None] | None = None
        local: asyncio.Task[None] | None = None
        expiring: asyncio.Task[None] | None = None
        try:
            if scheduler:
                nodestro.schedules.start()
            # The network listener for published APIs runs only when the user switched it on.
            await nodestro.network.start()
            # Local models: the managed Ollama starts in the background when it is installed and not switched off.
            local = asyncio.create_task(asyncio.to_thread(nodestro.local_models.start_on_launch), name="local-models")
            # Reference prices: checked at start when due, then daily; offline the last valid prices stay. The strict
            # offline mode makes no request at all, also when it is switched on while the API runs (the loop asks
            # each round, so switching it off again resumes the checks without a restart).
            if nodestro.price_sync is not None and nodestro.config.price_sync.enabled:
                prices = asyncio.create_task(
                    nodestro.price_sync.run(stop_prices, paused=nodestro.strict_offline), name="price-sync"
                )
            # Runs paused at a tool call (M6b) or waiting for a budget decision (BUDGET-1): the deadline sweep
            # continues decided and overdue ones.
            sweeping = asyncio.create_task(_sweep_tool_waits(nodestro, stop_prices), name="tool-waits")
            # Assistant chat files past the retention an administrator set (API-056; none by default).
            expiring = asyncio.create_task(_expire_chat_files(nodestro, stop_prices), name="chat-file-retention")
        except BaseException:
            await shutdown()
            raise
        try:
            yield
        finally:
            stop_prices.set()
            for task in (prices, sweeping, local, expiring):
                if task is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(task, timeout=5)
            await shutdown()

    return lifespan


def install_guards(app: FastAPI, config: AppConfig) -> None:
    """The guards of every studio API app, the recovery app's and every product app's included, and its error
    answers: the body limits, the launch secret or access key, the host and origin checks, the contract and security
    headers, by the configuration's profile."""
    # Innermost of the guards: a request the host, origin or launch checks refuse is never read (API2-03).
    app.add_middleware(BodyLimitMiddleware, limits=BodyLimits.from_env())
    local = config.profile_kind == "local-container"
    dev_origins = frozenset(DEV_SERVER_ORIGINS) if config.dev_cors and not local else frozenset()
    if dev_origins:  # the frontend's development server (npm run dev), only on request; never in a container
        app.add_middleware(
            CORSMiddleware,
            allow_origins=sorted(dev_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )
    if local:
        # Nodestro Local Docker: the access key, for every browser session and script (nodestro_api.local_container).
        assert config.access_key is not None  # checked above
        app.add_middleware(
            LaunchSecretMiddleware,
            secret=config.access_key,
            reusable=True,
            bearer=True,
            cookie=LOCAL_COOKIE,
            refusal=LOCAL_REFUSAL,
            session_host=config.browser_host,
            idle=config.session_idle_hours * 3600,
            max_age=config.session_max_hours * 3600,
        )
    elif config.launch_secret is not None:
        # The desktop app's window only (nodestro_api.launch); inside the host and origin checks.
        app.add_middleware(LaunchSecretMiddleware, secret=config.launch_secret, hardening=config.hardening)
    # Outermost: web pages in the user's browser must not use the key-less studio API.
    app.add_middleware(
        LocalAccessMiddleware,
        hosts=loopback_hosts(config.allowed_hosts) if local else allowed_hosts(config.host, config.allowed_hosts),
        ip_addresses=listens_everywhere(config.host) and not local,
        # Changes only from the exact origin a request was sent to (API2-10): a page that another program serves on
        # localhost (same site, other port) cannot use the key-less studio API; the development server only with
        # NODESTRO_DEV_CORS=1.
        strict_origin=True,
        extra_origins=dev_origins,
        # The published port, and the container's own port for the launcher's and health check's calls (D3).
        ports=frozenset({config.host_port, CONTAINER_PORT}) if local else None,
    )
    # Every answer of a route of the app contract names its version (NAL-C1), the guards' refusals included.
    app.add_middleware(ContractHeaderMiddleware)
    # Outside everything: refusals get the security headers too (API2-02).
    app.add_middleware(SecurityHeadersMiddleware)
    install_error_handlers(app)


def _recovery_app(config: AppConfig, error: NodestroError | None) -> FastAPI:
    """Recovery mode (:mod:`nodestro_api.recovery`): the regular guards and only the repair of the database's
    permissions; no workspace is opened and nothing else is served (the studio adds the interface)."""
    # Locked before anything is served (the refused workspace released its lock a moment ago), until the app stops.
    lock = DataDirectoryLock(config.data_dir)
    recovery.log_start(error)
    app = FastAPI(
        title="Nodestro (recovery)",
        version=__version__,
        lifespan=recovery.recovery_lifespan(lock),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.recovery = True
    app.state.principal_resolver = resolver_for(config.profile)
    install_guards(app, config)
    app.include_router(
        recovery.build_router(
            recovery.database_path(config),
            "refused" if error else "requested",
            # The repair needs the desktop app's launch session (its secret, from the shell's handoff or the operator).
            protected=config.launch_secret is not None,
        )
    )
    check_routes(app)
    return app


#: The frontend's development server (``npm run dev``): CORS and changes only with ``NODESTRO_DEV_CORS=1``.
DEV_SERVER_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

#: How often the desktop looks for runs paused at a tool call whose question was decided or ran out (seconds).
TOOL_WAIT_SWEEP_SECONDS = 15.0


#: How often the desktop removes assistant chat files past their retention period (API-056).
CHAT_FILE_RETENTION_SECONDS = 3600.0


async def _expire_chat_files(nodestro: Any, stop: asyncio.Event, every: float = CHAT_FILE_RETENTION_SECONDS) -> None:
    """Enforce the retention of assistant chat files at start and then hourly (nothing without a retention period)."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(nodestro.assistant.expire_files)
        except Exception:
            logging.getLogger(__name__).exception("Removing assistant chat files past their retention failed")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


async def _sweep_tool_waits(nodestro: Any, stop: asyncio.Event, every: float = TOOL_WAIT_SWEEP_SECONDS) -> None:
    while not stop.is_set():
        try:
            if nodestro.runs.tool_gate is not None:
                nodestro.runs.sweep_tool_waits()
            nodestro.runs.sweep_budget_waits()
        except Exception:
            logging.getLogger(__name__).exception("Sweeping paused runs failed")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)
