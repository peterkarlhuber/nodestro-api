"""Leases of the background work an API process drives on a server (API-006).

On a server several API processes share one database. A batch or a test series is driven by the process that started
it (or continued it): that process is its ``owner`` and renews ``lease_until`` while it drives it. When the process
stops without finishing (a crash, a killed container), nobody renews the lease. Any other process that reads the
item after the lease ran out marks it ``interrupted``, exactly as the desktop does at its next start, and a person
continues it (which makes the continuing process its new owner).

A process that finds a newer owner on an item it still drives stops driving it without writing (it lost the
lease, for example after a long pause of the process). The check and the write are not one transaction; the window
is small against the lease (renewed every few seconds, expires after :data:`LEASE_SECONDS`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import os
import uuid

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now

#: How long a lease holds without renewal (seconds). The drivers renew it at least every few seconds.
LEASE_SECONDS = 60.0

#: This process, as a random id: it is stored with the batch, which a tenant reads, so it names no host or process id
#: (a debug log line maps it to the process id).
PROCESS_OWNER = f"proc_{uuid.uuid4().hex[:16]}"
logging.getLogger(__name__).debug("Leases of this process (pid %s) carry the owner %s", os.getpid(), PROCESS_OWNER)


class LeaseLost(NodestroError):
    """Another process owns the item now; this process stops driving it without writing (``409 lease.lost`` when
    a request of this process tried to change it)."""

    def __init__(self, item_id: str) -> None:
        super().__init__("lease.lost", "Another server process drives this now; reload and try again", id=item_id)


def lease_until(seconds: float = LEASE_SECONDS) -> datetime:
    return now() + timedelta(seconds=seconds)


def expired(until: datetime | None) -> bool:
    """A lease that ran out, or none at all (an item from before leases, or one released by a reaper)."""
    return until is None or until <= now()
