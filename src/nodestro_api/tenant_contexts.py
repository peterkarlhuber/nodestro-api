"""Resource cleanup of the tenant contexts an API process of a server keeps open (API-001).

:class:`TenantContexts` is the server app's :class:`~nodestro_api.identity.TenantWorkspaces` with references: every
request that works on a context holds it (:meth:`TenantContexts.hold`, from :func:`nodestro_api.api.app_context`)
until its whole answer was sent, a streamed one included (:class:`TenantLeases` releases it), and
:meth:`TenantContexts.close_idle` closes a context that nobody used for a while: only when no request holds it and
``busy(context)`` says it drives no background work in this process (no run, batch, benchmark, export, assistant
answer, installation, reindex, ...). A context closed this way is opened again by the next request; nothing of the
tenant is lost (it lives in the database and its file area).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
import logging
import time
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .identity import TenantWorkspaces

logger = logging.getLogger(__name__)

#: Where a request notes the tenant contexts it holds (``request.state``), released by :class:`TenantLeases`.
LEASES_STATE = "nodestro_tenant_leases"


class TenantContexts(TenantWorkspaces):
    """One context per tenant with request references and idle closing (see the module documentation)."""

    def __init__(
        self,
        factory: Callable[[str], Any],
        close: Callable[[Any], Awaitable[None]] | None = None,
        *,
        busy: Callable[[Any], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(factory, close)
        #: Whether a context drives background work in this process (it is never closed then). ``None``: every
        #: context counts as busy, so nothing is closed for being idle.
        self.busy = busy
        self._clock = clock
        #: Requests holding each context now, and when it was last acquired, released or opened (``clock``).
        self._holders: dict[str, int] = {}
        self._used: dict[str, float] = {}

    def get(self, tenant_id: str) -> Any:
        """The tenant's context (opened when needed), without holding it: for short work of the process itself."""
        found = super().get(tenant_id)
        with self._lock:
            self._used[tenant_id] = self._clock()
        return found

    def acquire(self, tenant_id: str) -> Any:
        """The tenant's context, held until :meth:`release`."""
        while True:
            found = super().get(tenant_id)
            with self._lock:
                if self._open.get(tenant_id) is not found:  # closed between the two steps: open it again
                    continue
                self._holders[tenant_id] = self._holders.get(tenant_id, 0) + 1
                self._used[tenant_id] = self._clock()
                return found

    def release(self, tenant_id: str) -> None:
        with self._lock:
            count = self._holders.get(tenant_id, 0) - 1
            if count > 0:
                self._holders[tenant_id] = count
            else:
                self._holders.pop(tenant_id, None)
            if tenant_id in self._open:
                self._used[tenant_id] = self._clock()

    def hold(self, request: Request, tenant_id: str) -> Any:
        """:meth:`acquire` for a request: released by :class:`TenantLeases` once its whole answer was sent."""
        context = self.acquire(tenant_id)
        held: list[str] = getattr(request.state, LEASES_STATE, None) or []
        held.append(tenant_id)
        setattr(request.state, LEASES_STATE, held)
        return context

    @contextmanager
    def using(self, tenant_id: str) -> Iterator[Any]:
        """:meth:`acquire` for the length of a ``with`` block."""
        context = self.acquire(tenant_id)
        try:
            yield context
        finally:
            self.release(tenant_id)

    def holders(self, tenant_id: str) -> int:
        """How many requests hold the tenant's context now."""
        with self._lock:
            return self._holders.get(tenant_id, 0)

    def _is_busy(self, tenant_id: str, context: Any) -> bool:
        if self.busy is None:
            return True
        try:
            return bool(self.busy(context))
        except Exception:  # not known: keep it open
            logger.exception("Checking whether tenant %s's context is busy failed; it stays open", tenant_id)
            return True

    async def close_idle(self, idle_seconds: float) -> list[str]:
        """Close every context that no request holds, that was not used for ``idle_seconds`` and that drives no
        background work (``busy``); returns the tenants closed. A context that a request acquires while it is checked
        stays open. ``idle_seconds <= 0`` closes nothing."""
        if idle_seconds <= 0:
            return []
        now = self._clock()
        with self._lock:
            candidates = [
                (tenant_id, context, self._used.get(tenant_id, now))
                for tenant_id, context in self._open.items()
                if not self._holders.get(tenant_id) and now - self._used.get(tenant_id, now) >= idle_seconds
            ]
        closed: list[str] = []
        for tenant_id, context, used in candidates:
            if await asyncio.to_thread(self._is_busy, tenant_id, context):
                continue
            with self._lock:
                # Taken again (or replaced) while it was checked: it stays.
                if (
                    self._open.get(tenant_id) is not context
                    or self._holders.get(tenant_id)
                    or self._used.get(tenant_id) != used
                ):
                    continue
                del self._open[tenant_id]
                self._used.pop(tenant_id, None)
            try:
                await self.close(context)
            except Exception:
                logger.exception("Closing the idle context of tenant %s failed", tenant_id)
            closed.append(tenant_id)
        if closed:
            logger.info("Closed the idle contexts of %s tenants", len(closed))
        return closed

    async def aclose(self) -> None:
        await super().aclose()
        with self._lock:
            self._used.clear()


class TenantLeases:
    """Releases the tenant contexts a request held (:meth:`TenantContexts.hold`) once the whole answer was sent, a
    streamed one included. Pure ASGI, so a streamed answer is never buffered."""

    def __init__(self, app: ASGIApp, tenants: TenantContexts) -> None:
        self.app = app
        self.tenants = tenants

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            state = scope.get("state")
            held = state.pop(LEASES_STATE, None) if isinstance(state, dict) else None
            for tenant_id in held or ():
                self.tenants.release(tenant_id)


__all__ = ["LEASES_STATE", "TenantContexts", "TenantLeases"]
