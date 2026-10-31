"""The worker process of a server (ADR-0004): executes queued runs, reaps dead workers, fires schedules.

A server runs one or more API processes (:func:`nodestro_api.server_app.create_tenant_app` with a job queue) and one
or more worker processes (:class:`ServerWorker`). Every worker:

- claims run jobs and executes them in the tenant's context (:class:`nodestro_runtime.jobs.RunWorker`), with a lease
  it keeps renewing and its events written to the database;
- reaps jobs whose lease expired: their runs are marked ``run.interrupted`` and wait for a person to continue them;
  they are never executed again by themselves;
- fires the tenants' schedules while it holds the scheduler lock (:class:`nodestro_runtime.jobs.LeaderLock`); the
  runs it starts are queued like any other, and each slot fires at most once
  (:class:`nodestro_runtime.jobs.ScheduleFirings`).

**Not startable yet**, like the rest of the server profile (``profile.server_unavailable`` until plan step PR-12):
nothing but tests builds a :class:`ServerWorker`. Only runs go through the queue so far; batches, test series,
assistant drafts and Python environment installations still run in the API process that started them.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.jobs import JobQueue, LeaderLock, QueuedRuns, RunWorker, ScheduleFirings, reap, worker_name

from .context import Nodestro
from .identity import TenantWorkspaces
from .schedules import TICK_SECONDS
from .server_app import TenantFiles, close_tenant

logger = logging.getLogger(__name__)


@dataclass
class WorkerSettings:
    #: How long a claimed job stays this worker's without a heartbeat.
    lease: timedelta = timedelta(seconds=60)
    #: How often the queue is looked at while it is empty (seconds).
    poll: float = 1.0
    #: How often expired leases are reaped (seconds).
    reap_every: float = 15.0
    #: How often the scheduler leader looks for due schedules (seconds).
    schedule_every: float = TICK_SECONDS
    #: This worker's name in the job table (default: host, process and a random part).
    name: str | None = None


class ServerWorker:
    """One worker process of a server (see the module documentation)."""

    def __init__(
        self,
        files: TenantFiles,
        identity: IdentityStore,
        settings: WorkerSettings | None = None,
    ) -> None:
        self.files = files
        self.identity = identity
        self.settings = settings or WorkerSettings()
        self.name = self.settings.name or worker_name()
        self.queue = JobQueue(files.database, lease=self.settings.lease)
        # Contexts that execute claimed runs here, and contexts the scheduler starts (queued) runs with.
        self.executing = TenantWorkspaces(files.open, close_tenant)
        self.scheduling = TenantWorkspaces(self._open_scheduling, close_tenant)
        self.runner = RunWorker(self.queue, self.executing.get, name=self.name, poll=self.settings.poll)
        self.leader = LeaderLock(files.database)

    def _open_scheduling(self, tenant_id: str) -> Nodestro:
        context = self.files.open(tenant_id)
        QueuedRuns(self.queue, tenant_id, context.runs).attach()
        firings = ScheduleFirings(self.files.database, tenant_id)
        context.schedules.claim_slot = firings.claim
        context.schedules.record_slot = firings.record_run
        return context

    # ---- the three duties -------------------------------------------------------------------------

    def reap(self) -> list[Any]:
        """Mark the runs of dead workers as interrupted (any worker may do it)."""
        return reap(self.queue, self.scheduling.get)

    async def fire_schedules(self) -> list[str]:
        """Fire the due schedules of every tenant while this worker leads; returns the queued runs."""
        if not await asyncio.to_thread(self.leader.acquire):
            return []
        started: list[str] = []
        for tenant in await asyncio.to_thread(self.identity.tenants):
            try:
                started += await self.scheduling.get(tenant.id).schedules.tick()
            except Exception:
                logger.exception("Firing the schedules of tenant %s failed", tenant.id)
        return started

    async def serve(self, stop: asyncio.Event) -> None:
        """Execute jobs, reap and fire schedules until ``stop`` is set, then close every context.

        A run that is executing when ``stop`` is set is finished first (drain); a worker that is killed instead
        leaves its job to the reaper of another worker."""
        tasks = [
            asyncio.create_task(self.runner.serve(stop), name="worker-runs"),
            asyncio.create_task(self._every(stop, self.settings.reap_every, self._reap_once), name="worker-reaper"),
            asyncio.create_task(
                self._every(stop, self.settings.schedule_every, self.fire_schedules), name="worker-scheduler"
            ),
        ]
        try:
            await stop.wait()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                task.cancel()
            await self.aclose()

    async def _reap_once(self) -> list[Any]:
        return await asyncio.to_thread(self.reap)

    @staticmethod
    async def _every(stop: asyncio.Event, seconds: float, duty: Any) -> None:
        while not stop.is_set():
            try:
                await duty()
            except Exception:
                logger.exception("A periodic worker duty failed")
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=seconds)

    async def aclose(self) -> None:
        """Close every context (runs still executing here end as cancelled) and give up leadership."""
        await self.executing.aclose()
        await self.scheduling.aclose()
        with suppress(Exception):
            self.leader.release()


__all__ = ["ServerWorker", "WorkerSettings"]
