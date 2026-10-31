"""The FastAPI application: the studio REST API, run events and the published workflow APIs.

The application serves no interface of its own; ``nodestro-studio`` adds the built frontend
(:func:`nodestro_studio.app.create_app`). Started alone (``nodestro-api``) it is a complete
HTTP backend for scripts, the SDKs and published workflows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager, suppress
import logging
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from nodestro_runtime.extensions import Integration

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
from .api_tool_approvals import router as tool_approvals_router
from .context import AppConfig, Nodestro
from .launch import LOCAL_COOKIE, LaunchSecretMiddleware, check_secret
from .local_access import LocalAccessMiddleware, allowed_hosts, listens_everywhere, require_loopback_only
from .local_container import REFUSAL as LOCAL_REFUSAL
from .local_container import loopback_hosts, require_safe_configuration
from .permissions import check_routes, resolver_for
from .serving import build_router

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
    Every route must declare who may call it (:mod:`nodestro_api.permissions`)."""
    config = config or AppConfig.from_env()
    profile = config.profile.validate()
    if profile.kind == "desktop":
        require_loopback_only(config.host, config.allowed_hosts)
    elif profile.kind == "local-container":
        require_safe_configuration(config.access_key, config.allowed_hosts)
    check_secret(config.launch_secret)
    check_secret(config.access_key)
    nodestro = Nodestro(
        config,
        client_factory=client_factory,
        python_runner=python_runner,
        ocr_runner=ocr_runner,
        integrations=integrations,
    )

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
        try:
            if scheduler:
                nodestro.schedules.start()
            # The network listener for published APIs runs only when the user switched it on.
            await nodestro.network.start()
            # Reference prices: checked at start when due, then daily; offline the last valid prices stay.
            if nodestro.price_sync is not None and nodestro.config.price_sync.enabled:
                prices = asyncio.create_task(nodestro.price_sync.run(stop_prices), name="price-sync")
        except BaseException:
            await shutdown()
            raise
        try:
            yield
        finally:
            stop_prices.set()
            if prices is not None:
                with suppress(Exception):
                    await asyncio.wait_for(prices, timeout=5)
            await shutdown()

    # /docs belongs to the built-in user documentation; the API reference lives under /api.
    app = FastAPI(
        title="Nodestro",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.nodestro = nodestro
    # Who sends a request: on the desktop always the local owner (every permission).
    app.state.principal_resolver = resolver_for(profile)
    local = profile.kind == "local-container"
    if not local:  # the frontend's development server (npm run dev); never in a container
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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
        )
    elif config.launch_secret is not None:
        # The desktop app's window only (nodestro_api.launch); inside the host and origin checks.
        app.add_middleware(LaunchSecretMiddleware, secret=config.launch_secret)
    # Outermost: web pages in the user's browser must not use the key-less studio API.
    app.add_middleware(
        LocalAccessMiddleware,
        hosts=loopback_hosts(config.allowed_hosts) if local else allowed_hosts(config.host, config.allowed_hosts),
        ip_addresses=listens_everywhere(config.host) and not local,
        strict_origin=local,
    )
    install_error_handlers(app)
    # Before the main router: /api/tools/drafts must not be taken for a tool named "drafts".
    # Published workflows (``/api/published``): key-protected, answered only on this computer here;
    # the network listener (``nodestro_api.network``) serves them separately when switched on.
    app.include_router(build_router(nodestro.serving, channel="local"))
    app.include_router(publish_router)
    app.include_router(architect_router)
    app.include_router(python_router)
    app.include_router(automation_router)
    app.include_router(assistant_router)
    app.include_router(inputs_router)
    app.include_router(pricing_router)
    app.include_router(mcp_router)
    app.include_router(tool_approvals_router)
    app.include_router(router)
    app.include_router(data_router)
    # Fail closed: an application with a route that declares no access requirement is not built.
    check_routes(app)
    return app
