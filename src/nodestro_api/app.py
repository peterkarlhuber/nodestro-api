"""The FastAPI application: the studio REST API, run events and the published workflow APIs.

The application serves no interface of its own; ``nodestro-studio`` adds the built frontend
(:func:`nodestro_studio.app.create_app`). Started alone (``nodestro-api``) it is a complete
HTTP backend for scripts, the SDKs and published workflows.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
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
from .api_publish import router as publish_router
from .api_python import router as python_router
from .context import AppConfig, Nodestro
from .local_access import LocalAccessMiddleware, allowed_hosts, listens_everywhere
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
    ``ocr_runner`` the local OCR (tests). ``scheduler=False`` leaves schedules to explicit ticks (tests)."""
    config = config or AppConfig.from_env()
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
        try:
            if scheduler:
                nodestro.schedules.start()
            # The network listener for published APIs runs only when the user switched it on.
            await nodestro.network.start()
        except BaseException:
            await shutdown()
            raise
        try:
            yield
        finally:
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Outermost: web pages in the user's browser must not use the key-less studio API.
    app.add_middleware(
        LocalAccessMiddleware,
        hosts=allowed_hosts(config.host, config.allowed_hosts),
        ip_addresses=listens_everywhere(config.host),
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
    app.include_router(router)
    app.include_router(data_router)
    return app
