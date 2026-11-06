"""The worker process of a server (ADR-0004): executes queued runs, reaps dead workers, fires schedules.

A server runs one or more API processes (:func:`nodestro_api.server_app.create_tenant_app` with a job queue) and one
or more worker processes (:class:`ServerWorker`). Every worker:

- claims run jobs and executes them in the tenant's context (:class:`nodestro_runtime.jobs.RunWorker`), with a lease
  it keeps renewing and its events written to the database;
- reaps jobs whose lease expired: their runs are marked ``run.interrupted`` and wait for a person to continue them;
  they are never executed again by themselves;
- fires the tenants' schedules while it holds the scheduler lock (:class:`nodestro_runtime.jobs.LeaderLock`); the
  runs it starts are queued like any other, and each slot fires at most once
  (:class:`nodestro_runtime.jobs.ScheduleFirings`);
- syncs the server's reference prices when they are due, under the price-sync lock: one worker at a time
  (:mod:`nodestro_api.pricing_setup`; only with :attr:`WorkerSettings.price_sync` switched on);
- with Axentra switched on (each off by default): settles the usage of finished runs into Axentra's meters
  (``NODESTRO_AXENTRA_RUN_USAGE``) and puts failed runs and pending approvals into the members' in-app inbox
  (``NODESTRO_AXENTRA_NOTIFY``), while it holds the scheduler lock (:meth:`ServerWorker.platform_duties`; both are
  idempotent, so a second worker or a retry never counts or notifies twice);
- deletes what is no longer needed while it holds the scheduler lock (:meth:`ServerWorker.clean_up`): expired
  sessions and abandoned login starts, finished jobs after :attr:`WorkerSettings.job_retention` and run events after
  :attr:`WorkerSettings.event_retention` (the runs themselves stay); and, every
  :attr:`WorkerSettings.chat_file_every` seconds, the assistant chat files past the retention an administrator of the
  tenant set (:meth:`ServerWorker.expire_chat_files`, API-066);
- keeps the Docker sandbox's proof warm (:class:`DockerWatch`): from its start it reads the process's Docker monitor
  (:mod:`nodestro_runtime.docker_status`) every :attr:`WorkerSettings.docker_every` seconds, well within the proof's
  ``PROOF_TTL``, so a job after an idle spell is not refused with ``docker-checking`` only because the proof expired.
  A read never waits and never forces a check: the monitor starts its background check only when its preflight is
  older than ``PREFLIGHT_TTL``, never two at once, and runs the self-test again only for a changed daemon or after a
  failure.
  Nothing runs in Docker before the check passed (fail-closed, as before); the worker logs each change of the state
  (proven, unavailable with its reason) once, with fixed texts and no host details.

A suspended tenant (:meth:`nodestro_runtime.identity_store.IdentityStore.tenant_active`, answered by the identity
backend) is closed for workers too: a claimed job of it fails with ``identity.tenant_suspended`` before its run starts
or continues (the orphan sweep later marks the run interrupted, so it can be continued once the tenant is resumed),
runs it executes here stop before their next model request or tool call (``RunWorker.owns`` asks the identity
backend, reusing an "active" answer for at most ``TENANT_CHECK_SECONDS``; a model call already in flight finishes)
and at the latest within :attr:`WorkerSettings.reap_every` (a run waiting for a person), without storing more (as
after a lost lease), and its schedules and tool-wait sweeps do not run.

``nodestro-server worker`` (:mod:`nodestro_api.server`) runs one. Only runs go through the queue so far; batches,
test series, assistant drafts and Python environment installations still run in the API process that started them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from nodestro_runtime import docker_status
from nodestro_runtime.docker_status import DockerMonitorLike, DockerStatus
from nodestro_runtime.identity_store import IdentityStore, tenant_suspended
from nodestro_runtime.jobs import (
    DEFAULT_MAX_RUNS,
    DEFAULT_MAX_WAITING,
    JobQueue,
    LeaderLock,
    PostgresEventLog,
    QueuedRuns,
    RunWorker,
    ScheduleFirings,
    reap,
    worker_name,
)

from .context import Nodestro
from .identity import TenantWorkspaces
from .pricing_setup import PriceSyncConfig, server_sync
from .sandbox_status import PUBLIC_DETAILS
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
    #: The reference price sync (``nodestro-server worker`` takes it from ``NODESTRO_PRICE_*``; off by default).
    price_sync: PriceSyncConfig = field(default_factory=lambda: PriceSyncConfig(enabled=False))
    #: How often the worker looks whether the reference prices are due (seconds).
    price_check_every: float = 3600.0
    #: Runs this worker holds at once, and how many of them may wait for a person's tool approval (S-2, worker
    #: concept option A): a waiting run keeps its slot, so at least one slot always executes.
    max_runs: int = DEFAULT_MAX_RUNS
    max_waiting: int = DEFAULT_MAX_WAITING
    #: How often the scheduler leader deletes expired sessions, login starts, old jobs and run events (seconds).
    cleanup_every: float = 600.0
    #: Finished jobs are kept this long (the runs themselves are not touched).
    job_retention: timedelta = timedelta(days=7)
    #: Run events are kept this long: live streams and waits read them; a run's own record keeps its results.
    event_retention: timedelta = timedelta(days=2)
    #: How often the scheduler leader removes assistant chat files past the retention an administrator of each
    #: tenant set (``PUT /api/assistant/files/retention``, API-056/API-066; nothing without a retention period).
    chat_file_every: float = 3600.0
    #: How often the scheduler leader settles run usage and sweeps notifications into Axentra (seconds).
    platform_every: float = 30.0
    #: How often the worker reads the Docker monitor's status to keep the sandbox's proof fresh (seconds; at most
    #: half of ``PROOF_TTL``, 600 s). Each read costs at most one ``docker info`` in the monitor's thread.
    docker_every: float = 120.0


#: How often :class:`DockerWatch` reads the status while the check runs (seconds): below the monitor's
#: ``PREFLIGHT_TTL`` (5 s), so the read after the check finished never starts another one.
DOCKER_CHECKING_POLL = 2.0


def uses_docker(context: Any) -> bool:
    """Whether tool code of this workspace can run in Docker (:func:`nodestro_runtime.sandbox_selection.select`):
    always outside the desktop profile (a server tenant), on the desktop in the ``docker`` and ``auto`` modes. In the
    ``linux`` and ``subprocess`` modes Docker runs nothing, so its proof need not be kept warm."""
    if context.sandbox_policy.profile != "desktop":
        return True
    return getattr(context.tools, "sandbox_mode", None) in ("docker", "auto")


class DockerWatch:
    """Keeps the process's Docker proof warm and reports its state (see the module documentation).

    ``monitors`` returns the Docker monitors of the open workspaces that use Docker (one per process and daemon,
    :func:`nodestro_runtime.docker_status.monitor_for`; duplicates are read once). The watch only reads
    :meth:`~nodestro_runtime.docker_status.DockerMonitor.status`, which never blocks: it never waits for the daemon,
    never forces a check or a self-test, and never changes what counts as proven."""

    def __init__(self, monitors: Callable[[], Iterable[DockerMonitorLike]]) -> None:
        self.monitors = monitors
        #: The last reported ``(state, reason)`` per monitor (the monitor is kept, so its id is not reused).
        self._reported: dict[int, tuple[DockerMonitorLike, tuple[str, str]]] = {}

    def poll(self) -> list[DockerStatus]:
        """Read every monitor once (starting a background check where one is due) and log state changes."""
        found: dict[int, DockerMonitorLike] = {}
        for monitor in self.monitors():
            found.setdefault(id(monitor), monitor)
        statuses = []
        for monitor in found.values():
            status = monitor.status()
            self._report(monitor, status)
            statuses.append(status)
        return statuses

    def _report(self, monitor: DockerMonitorLike, status: DockerStatus) -> None:
        state = (status.state, status.reason)
        last = self._reported.get(id(monitor))
        if last is not None and last[1] == state:
            return
        self._reported[id(monitor)] = (monitor, state)
        # Fixed texts only: the monitor's own detail can name socket paths or DOCKER_HOST addresses.
        if status.state == "approved":
            logger.info("Docker sandbox proven (preflight and self-test passed); tool code may run in it")
        elif status.state == "checking":
            logger.info("Docker sandbox check running (preflight and self-test); tool code is refused until it passed")
        else:
            logger.warning(
                "Docker sandbox unavailable (%s): %s; tool code is refused until a check passes",
                status.reason or "docker-unreachable",
                PUBLIC_DETAILS.get(status.reason, "The Docker sandbox is not available"),
            )

    async def run(self, stop: asyncio.Event, every: float, checking_every: float = DOCKER_CHECKING_POLL) -> None:
        """Poll until ``stop`` is set: at once (the warm-up), every ``checking_every`` seconds while a check runs,
        otherwise every ``every`` seconds (at most half of ``PROOF_TTL``)."""
        every = min(every, docker_status.PROOF_TTL / 2)
        while not stop.is_set():
            statuses: list[DockerStatus] = []
            try:
                statuses = self.poll()
            except Exception:
                logger.exception("Reading the Docker sandbox status failed")
            pause = checking_every if any(status.state == "checking" for status in statuses) else every
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=pause)


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
        self.runner = RunWorker(
            self.queue,
            self._executing,
            name=self.name,
            poll=self.settings.poll,
            max_runs=self.settings.max_runs,
            max_waiting=self.settings.max_waiting,
            tenant_active=self.identity.tenant_active,
        )
        self.leader = LeaderLock(files.database)
        self.price_sync = server_sync(files.database, self.settings.price_sync)
        #: Axentra duties, set by ``nodestro-server worker`` when switched on (``None``: off): the run usage
        #: settlement (:meth:`nodestro_runtime.axentra_platform.AxentraEvents.settle_runs`) and the inbox
        #: notifications (:class:`nodestro_runtime.axentra_notify.AxentraNotifications`).
        self.settlement: Any | None = None
        self.notifications: Any | None = None
        self.docker = DockerWatch(self._docker_monitors)

    def _docker_monitors(self) -> list[DockerMonitorLike]:
        """The Docker monitors of the open contexts that use Docker (on a server: one, shared by every tenant)."""
        contexts = [*self.scheduling.opened(), *self.executing.opened()]
        return [context.docker_monitor for context in contexts if uses_docker(context)]

    def _executing(self, tenant_id: str) -> Nodestro:
        """The context a claimed job executes in; a suspended tenant's job fails here (before its run starts or
        continues)."""
        if not self.identity.tenant_active(tenant_id):
            raise tenant_suspended(tenant_id)
        context: Nodestro = self.executing.get(tenant_id)
        return context

    async def _active_tenants(self) -> list[str]:
        return [t.id for t in await asyncio.to_thread(self.identity.tenants) if t.status == "active"]

    async def stop_suspended(self) -> list[str]:
        """Stop the runs this worker executes for tenants that are suspended now (nothing more is stored; the job
        ends as ``run.fence_lost`` and the run is marked interrupted later); returns the stopped runs."""
        stopped: list[str] = []
        for tenant_id, context in self.executing.items():
            runs = context.runs.live_runs()
            if not runs or await asyncio.to_thread(self.identity.tenant_active, tenant_id):
                continue
            for run in runs:
                if context.runs.abandon(run.id, because="identity.tenant_suspended: the tenant was suspended"):
                    stopped.append(run.id)
        if stopped:
            logger.warning("Stopped %s runs of suspended tenants", len(stopped))
        return stopped

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
        for tenant_id in await self._active_tenants():  # a suspended tenant's schedules do not fire
            try:
                started += await self.scheduling.get(tenant_id).schedules.tick()
            except Exception:
                logger.exception("Firing the schedules of tenant %s failed", tenant_id)
        return started

    async def clean_up(self) -> dict[str, int]:
        """Delete expired sessions and login starts, finished jobs and old run events while this worker leads (A-02);
        returns how many rows went (empty: another worker leads)."""
        if not await asyncio.to_thread(self.leader.acquire):
            return {}
        database = self.files.database
        removed = {
            "sessions_and_logins": await asyncio.to_thread(self.identity.cleanup),
            "jobs": await asyncio.to_thread(self.queue.cleanup, older_than=self.settings.job_retention),
            "run_events": await asyncio.to_thread(
                PostgresEventLog.cleanup, database, older_than=self.settings.event_retention
            ),
        }
        if self.notifications is not None:
            purged = await asyncio.to_thread(self.notifications.purge)
            removed["notification_contents"] = int(purged.get("content_erased", 0))
        if any(removed.values()):
            logger.info("Cleanup removed %s", ", ".join(f"{count} {name}" for name, count in removed.items()))
        return removed

    async def expire_chat_files(self) -> dict[str, int]:
        """Remove assistant chat files past their tenant's retention period while this worker leads (API-066): the
        same expiry as the desktop's hourly pass, for every active tenant (a suspended tenant's files stay until it is
        resumed). Returns the files removed per tenant (only tenants where some went; empty: another worker leads).
        A tenant whose pass fails is logged and skipped; the next pass tries again."""
        if not await asyncio.to_thread(self.leader.acquire):
            return {}
        removed: dict[str, int] = {}
        for tenant_id in await self._active_tenants():
            try:
                count = await asyncio.to_thread(self.scheduling.get(tenant_id).assistant.expire_files)
            except Exception:
                logger.exception("Removing the expired assistant chat files of tenant %s failed", tenant_id)
                continue
            if count:
                removed[tenant_id] = count
        return removed

    async def platform_duties(self) -> dict[str, Any]:
        """Settle finished runs' usage and send due inbox notifications while this worker leads (empty: off, or
        another worker leads). Both read what changed since their last pass and are idempotent per record."""
        if self.settlement is None and self.notifications is None:
            return {}
        if not await asyncio.to_thread(self.leader.acquire):
            return {}
        done: dict[str, Any] = {}
        if self.settlement is not None:
            done["run_usage"] = await asyncio.to_thread(self.settlement.settle_runs)
            if done["run_usage"].get("written") or done["run_usage"].get("failed"):
                logger.info("Run usage settled into Axentra: %s", done["run_usage"])
        if self.notifications is not None:
            report = await asyncio.to_thread(self.notifications.sweep)
            done["notifications"] = report.as_dict()
            if report.sent or report.failed:
                logger.info("Inbox notifications: %s", done["notifications"])
        return done

    async def serve(self, stop: asyncio.Event) -> None:
        """Execute jobs, reap and fire schedules until ``stop`` is set, then close every context.

        A run that is executing when ``stop`` is set is finished first (drain); a worker that is killed instead
        leaves its job to the reaper of another worker."""
        # Overdue questions of paused runs expire before this worker continues any run (M6b). The sweep opens the
        # active tenants' contexts, and with them the process's Docker monitor, whose first check starts then.
        await self.sweep_tool_waits()
        tasks = [
            # Reads the Docker status at once and then periodically: the proof stays fresh while the worker runs.
            asyncio.create_task(self.docker.run(stop, self.settings.docker_every), name="worker-docker"),
            asyncio.create_task(
                self._every(stop, self.settings.reap_every, self.sweep_tool_waits), name="worker-tool-waits"
            ),
            asyncio.create_task(self.runner.serve(stop), name="worker-runs"),
            asyncio.create_task(self._every(stop, self.settings.reap_every, self._reap_once), name="worker-reaper"),
            asyncio.create_task(
                self._every(stop, self.settings.reap_every, self.stop_suspended), name="worker-suspended"
            ),
            asyncio.create_task(
                self._every(stop, self.settings.schedule_every, self.fire_schedules), name="worker-scheduler"
            ),
        ]
        tasks.append(
            asyncio.create_task(self._every(stop, self.settings.cleanup_every, self.clean_up), name="worker-cleanup")
        )
        tasks.append(
            asyncio.create_task(
                self._every(stop, self.settings.chat_file_every, self.expire_chat_files), name="worker-chat-files"
            )
        )
        if self.settlement is not None or self.notifications is not None:
            tasks.append(
                asyncio.create_task(
                    self._every(stop, self.settings.platform_every, self.platform_duties), name="worker-platform"
                )
            )
        if self.settings.price_sync.enabled:
            tasks.append(
                asyncio.create_task(
                    self._every(stop, self.settings.price_check_every, self.sync_prices), name="worker-prices"
                )
            )
        try:
            await stop.wait()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                task.cancel()
            await self.aclose()

    async def sync_prices(self) -> None:
        """Sync the reference prices when they are due; another worker holding the lock means it syncs."""
        if self.price_sync.due():
            report = await asyncio.to_thread(self.price_sync.sync)
            if report.ran:
                logger.info("Reference prices checked; active version %s", report.active_version)

    async def sweep_tool_waits(self) -> list[str]:
        """The deadline sweep of runs paused at tool calls (M6b) for every tenant: overdue questions expire, decided
        runs are queued again (a worker continues them under its lease), and with the switch off open questions
        are closed. Runs once before this worker claims anything, then periodically. The budget-decision sweep
        (BUDGET-1) runs with it."""
        continued: list[str] = []
        for tenant_id in await self._active_tenants():  # a suspended tenant's paused runs stay as they are
            try:
                continued += self.scheduling.get(tenant_id).runs.sweep_tool_waits()
            except Exception:
                logger.exception("Sweeping the runs of tenant %s paused at tool calls failed", tenant_id)
            try:
                # Runs waiting for a budget decision (BUDGET-1): overdue requests expire, decided runs continue.
                continued += self.scheduling.get(tenant_id).runs.sweep_budget_waits()
            except Exception:
                logger.exception("Sweeping the runs of tenant %s waiting for a budget decision failed", tenant_id)
        return continued

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


__all__ = ["DOCKER_CHECKING_POLL", "DockerWatch", "ServerWorker", "WorkerSettings", "uses_docker"]
