"""Network access for published APIs: a separate listener that serves nothing else.

Nodestro itself listens on ``127.0.0.1`` only. When the user explicitly enables network
access, a second HTTP server starts in the same process on its own port. Its application
contains the published API endpoints and ``/health`` and nothing more: no studio, no
settings, no internal endpoints, no documentation page. Each publication additionally
needs its own "reachable in the network" switch.

The listener speaks HTTP unless a TLS certificate and key are configured. Without TLS the
API keys travel unencrypted through the local network; the studio says so before enabling.
After restoring a backup, network access is always off again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager, suppress
import logging
from pathlib import Path
import socket
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from nodestro_runtime.problems import NodestroError
from pydantic import BaseModel, Field

from .serving import ServingCore, build_router

if TYPE_CHECKING:
    from nodestro_runtime.storage import Store

logger = logging.getLogger(__name__)

_COLLECTION = "api_settings"
_KEY = "network"


class NetworkConfig(BaseModel):
    enabled: bool = False
    #: The interface to listen on; ``0.0.0.0`` means every network of this computer.
    host: str = "0.0.0.0"
    port: int = Field(default=8100, ge=1024, le=65535)
    tls_certfile: str = ""
    tls_keyfile: str = ""


class NetworkStatus(BaseModel):
    enabled: bool
    running: bool
    host: str
    port: int
    tls: bool
    #: Paths of the certificate and its key (files on this computer, never their content).
    tls_certfile: str = ""
    tls_keyfile: str = ""
    #: Addresses other devices can use (best guess of this computer's addresses).
    urls: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    #: The port the studio itself uses (never reachable from the network through this listener).
    studio_local_only: bool = True


def network_app(core: ServingCore) -> FastAPI:
    """The whole application of the network listener: published endpoints and a health check."""
    app = FastAPI(title="Nodestro published workflows", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(build_router(core, channel="network"))

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def local_addresses() -> list[str]:
    """IPv4 addresses of this computer that other devices may reach (without loopback)."""
    found: list[str] = []
    with suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        # No packet is sent: connecting a UDP socket only chooses the outgoing interface.
        probe.connect(("192.0.2.1", 9))
        found.append(probe.getsockname()[0])
    with suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if address not in found and not address.startswith("127."):
                found.append(address)
    return found


class _Server:
    """A uvicorn server that leaves signal handling to the studio's own server."""

    def __init__(self, app: FastAPI, config: NetworkConfig) -> None:
        import uvicorn

        options: dict[str, Any] = {"host": config.host, "port": config.port, "log_level": "warning", "lifespan": "off"}
        if config.tls_certfile and config.tls_keyfile:
            options["ssl_certfile"] = config.tls_certfile
            options["ssl_keyfile"] = config.tls_keyfile
        server = uvicorn.Server(uvicorn.Config(app, **options))

        @contextmanager
        def no_signals() -> Iterator[None]:
            yield

        server.capture_signals = no_signals  # type: ignore[method-assign]
        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]
        self.server = server
        self.config = config
        self.task: asyncio.Task[None] | None = None

    def _bind(self) -> socket.socket:
        """Bind the port here: uvicorn would end the whole process when the port is taken."""
        family = socket.AF_INET6 if ":" in self.config.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.config.host, self.config.port))
        except OSError as error:
            sock.close()
            raise NodestroError(
                "lan.port_unavailable",
                f"Port {self.config.port} cannot be used: {error.strerror or error}",
                port=self.config.port,
            ) from None
        sock.set_inheritable(True)
        return sock

    async def start(self) -> None:
        sock = self._bind()
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]), name="nodestro-network")
        for _ in range(100):
            if self.server.started or self.task.done():
                break
            await asyncio.sleep(0.05)
        if self.task.done():
            # E.g. a certificate or key uvicorn cannot load; the port must not stay taken.
            sock.close()
            error = self.task.exception() if not self.task.cancelled() else None
            raise NodestroError("lan.start_failed", f"The network listener did not start: {error}", error=str(error))

    async def stop(self) -> None:
        self.server.should_exit = True
        if self.task is not None:
            with suppress(Exception):
                await asyncio.wait_for(self.task, timeout=5)


class NetworkListener:
    def __init__(self, store: Store, core: ServingCore) -> None:
        self.store = store
        self.core = core
        self._server: _Server | None = None
        self._error: tuple[str, str] | None = None
        #: One change at a time: starting and stopping wait for the server, other changes wait for them.
        self._changing = asyncio.Lock()

    def config(self) -> NetworkConfig:
        raw = self.store.get_raw(_COLLECTION, _KEY)
        return NetworkConfig.model_validate(raw) if raw else NetworkConfig()

    def _save(self, config: NetworkConfig) -> None:
        self.store.put_raw(_COLLECTION, _KEY, config.model_dump())

    def status(self) -> NetworkStatus:
        config = self.config()
        running = self._server is not None and self._server.server.started
        tls = bool(config.tls_certfile and config.tls_keyfile)
        scheme = "https" if tls else "http"
        hosts = local_addresses() if config.host in ("0.0.0.0", "") else [config.host]
        return NetworkStatus(
            enabled=config.enabled,
            running=running,
            host=config.host,
            port=config.port,
            tls=tls,
            tls_certfile=config.tls_certfile,
            tls_keyfile=config.tls_keyfile,
            urls=[f"{scheme}://{host}:{config.port}" for host in hosts] if running else [],
            error=self._error[1] if self._error else None,
            error_code=self._error[0] if self._error else None,
        )

    async def apply(self, config: NetworkConfig) -> NetworkStatus:
        """Store the configuration and start, restart or stop the listener accordingly."""
        for path in (config.tls_certfile, config.tls_keyfile):
            if path and not Path(path).expanduser().is_file():
                raise NodestroError("lan.tls_file_missing", f"File not found: {path}", path=path)
        if bool(config.tls_certfile) != bool(config.tls_keyfile):
            raise NodestroError("lan.tls_incomplete", "TLS needs both a certificate and its key")
        config.tls_certfile = str(Path(config.tls_certfile).expanduser()) if config.tls_certfile else ""
        config.tls_keyfile = str(Path(config.tls_keyfile).expanduser()) if config.tls_keyfile else ""
        async with self._changing:
            previous = self.config()
            was_running = self._server is not None
            await self._stop()
            self._save(config)
            if config.enabled:
                await self._start()
                if self._server is None and was_running:
                    # The new settings do not work (port taken, certificate unusable): keep serving
                    # with the ones that did, and report why the change failed.
                    failure = self._error
                    self._save(previous)
                    await self._start()
                    self._error = failure
        return self.status()

    async def start(self) -> None:
        async with self._changing:
            await self._start()

    async def _start(self) -> None:
        config = self.config()
        if not config.enabled or self._server is not None:
            return
        self._error = None
        server = _Server(network_app(self.core), config)
        try:
            await server.start()
        except NodestroError as error:
            self._error = (error.code, str(error))
            logger.warning("Network access for published APIs could not start: %s", error)
            return
        self._server = server
        logger.info("Published APIs are reachable in the network on port %s", config.port)

    async def stop(self) -> None:
        async with self._changing:
            await self._stop()

    async def _stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.stop()

    def disable_after_restore(self) -> None:
        """A restored backup never switches network access on (the listener stops on the next apply)."""
        config = self.config()
        if config.enabled:
            config.enabled = False
            self._save(config)
