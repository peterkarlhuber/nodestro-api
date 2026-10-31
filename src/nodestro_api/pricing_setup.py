"""Where reference prices come from and who syncs them, on the desktop and on a server (PRICING-1 N3, N10).

- Desktop: the installation owns its reference prices. The API process syncs them in the background
  (:attr:`PriceSyncConfig.enabled`) and on request (``POST /api/pricing/sync``).
- Server: the server owns them; they live in the document store under :data:`PRICE_REFERENCE_TENANT`, which no
  tenant can be created as. Tenants read them (read-only) and keep their own overrides. One worker at a time syncs
  them, under the advisory lock :data:`PRICE_SYNC_LOCK`; operators decide reviews with ``nodestro-server pricing``.
- Offline: a failed sync keeps the last valid prices and records the error; prices age into ``stale``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
import os
from pathlib import Path
from typing import TYPE_CHECKING

from nodestro_runtime.jobs import LeaderLock
from nodestro_runtime.pricing import PricingService
from nodestro_runtime.pricing_sync import PricingSyncService, default_sources
from nodestro_runtime.problems import NodestroError
from pydantic import BaseModel

if TYPE_CHECKING:
    from nodestro_runtime.storage_postgres import PostgresDatabase

#: The document-store tenant that holds a server's reference prices. ``nodestro-server create-tenant`` refuses it,
#: so no tenant can ever read or write these documents as its own.
PRICE_REFERENCE_TENANT = "nodestro-price-reference"
#: The advisory lock key of the server's price sync (any constant; ``prices`` in ASCII).
PRICE_SYNC_LOCK = 0x707269636573


class PriceSyncConfig(BaseModel):
    """Where the price sync gets its candidates from (``NODESTRO_PRICE_*``)."""

    #: Sync in the background (``NODESTRO_PRICE_SYNC``: ``on`` (default) or ``off``). Off: prices change only by
    #: ``POST /api/pricing/sync`` (desktop) or ``nodestro-server pricing sync`` (server).
    enabled: bool = True
    #: LiteLLM's community cost map (``NODESTRO_PRICE_LITELLM``: ``on`` (default) or ``off``).
    litellm: bool = True
    #: A reviewed snapshot file an operator supplies (``NODESTRO_PRICE_SNAPSHOT_FILE``).
    snapshot_file: Path | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PriceSyncConfig:
        environ = os.environ if environ is None else environ

        def switch(name: str) -> bool:
            value = environ.get(name, "").strip().lower()
            if value in ("", "on", "true", "1", "yes"):
                return True
            if value in ("off", "false", "0", "no"):
                return False
            raise NodestroError("profile.invalid", f"{name} must be on or off", field=name, value=value)

        file = environ.get("NODESTRO_PRICE_SNAPSHOT_FILE", "").strip()
        return cls(
            enabled=switch("NODESTRO_PRICE_SYNC"),
            litellm=switch("NODESTRO_PRICE_LITELLM"),
            snapshot_file=Path(file) if file else None,
        )

    def service(
        self, pricing: PricingService, *, lock: Callable[[], AbstractContextManager[bool]] | None = None
    ) -> PricingSyncService:
        return PricingSyncService(
            pricing, default_sources(snapshot_file=self.snapshot_file, litellm=self.litellm), lock=lock
        )


def reference_pricing(database: PostgresDatabase) -> PricingService:
    """The server's own view of its reference prices: their only writer (sync job and operator commands)."""
    from nodestro_runtime.providers import PROVIDERS

    return PricingService(database.store(PRICE_REFERENCE_TENANT), known_providers=PROVIDERS)


def sync_lock(database: PostgresDatabase) -> Callable[[], AbstractContextManager[bool]]:
    """Holds the server's price-sync advisory lock while a sync runs (``False``: another process syncs)."""

    @contextmanager
    def held() -> Iterator[bool]:
        lock = LeaderLock(database, key=PRICE_SYNC_LOCK)
        acquired = lock.acquire()
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()

    return held


def server_sync(database: PostgresDatabase, config: PriceSyncConfig) -> PricingSyncService:
    """The sync of a server's reference prices (worker and ``nodestro-server pricing sync``)."""
    return config.service(reference_pricing(database), lock=sync_lock(database))


__all__ = [
    "PRICE_REFERENCE_TENANT",
    "PRICE_SYNC_LOCK",
    "PriceSyncConfig",
    "reference_pricing",
    "server_sync",
    "sync_lock",
]
