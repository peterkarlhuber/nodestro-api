"""Portable export builds as jobs of a server's job queue (API-048).

A server's API process does not build portable packages itself when it has a job queue: ``POST
/api/publications/{id}/export-jobs`` stores the :class:`~nodestro_api.portable_build.ExportJob` (``queued``) and
queues an :data:`EXPORT_JOB` for it (:class:`QueuedExports`). A worker process (:class:`ExportWorker`, part of
:class:`nodestro_api.worker.ServerWorker`) claims it and builds it:

- **One build at a time on the whole server:** a worker claims an export job only while it holds the export lock
  (a session-level advisory lock, :data:`EXPORT_LOCK`), and it keeps the lock while it builds. Jobs of other tenants
  wait in the queue in their order; a tenant has at most one active export job (``export.busy``).
- **Lease:** the worker renews the job's lease while it builds. A worker that dies loses its lease and its database
  session (and with it the export lock); another worker's reaper marks the job ``interrupted``
  (:func:`interrupt_reaped`): the export job fails with ``export.interrupted``, its work folder is removed, and
  another worker can build the next job. A build never starts again by itself.
- **Every API process reads the same job:** progress is stored in the tenant's store as the build goes; an API
  process never marks a job failed because it is not building it. A job whose queue job ended without a worker
  storing the end (reaped, withdrawn) is settled when it is read (:meth:`QueuedExports.settle`).
- **Cancelling** a queued job withdraws it at once; a running one gets ``cancel_requested``, which the worker reads
  with its next heartbeat and passes to the build (``export.cancelled``).
- The worker stops a build (failed, ``export.interrupted``) when it stops itself, and (failed,
  ``identity.tenant_suspended``) when the tenant is suspended; it refuses to build (``export.server_unsupported``)
  while its own ``NODESTRO_SERVER_EXPORT_JOBS`` is not ``1``.

The worker writes into the tenant's data folder (``<data>/exports``, ``<data>/export-work``), which the API processes
and the workers share on a server (nodestro-server RUNBOOK). The export quota and size limit apply as before
(:func:`nodestro_api.portable_build.keep_within_server_limits`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
import logging
import shutil
import threading
from typing import Any

from nodestro_runtime.jobs import Job, JobQueue, JobStatus, LeaderLock, worker_name
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now

from . import portable_build
from .portable_build import JOBS, ExportJob

logger = logging.getLogger(__name__)

#: The job kind of a portable export build; its ref is the export job's id.
EXPORT_JOB = "export"
#: The advisory lock a worker holds while it claims and builds an export ("nodexprt"): one build on the whole server.
EXPORT_LOCK = 0x6E6F646578707274
#: A job stored as running that has no queue job after this long was never queued (or its queue job was removed):
#: it is settled as interrupted.
UNQUEUED_AFTER = timedelta(minutes=5)

#: The texts of the ends a worker or the settlement stores (the codes are in the error catalog).
INTERRUPTED = "The server stopped while the export was built; start it again"
SUSPENDED = "The workspace was suspended while the export was built"


def _end(job: ExportJob, state: str, code: str, message: str, **params: Any) -> ExportJob:
    job.state, job.error_code, job.error = state, code, message  # type: ignore[assignment]
    job.error_params = dict(params)
    job.queued = False
    job.finished_at = now()
    return job


def _split(error: str | None, default: str) -> tuple[str, str]:
    """A queue job's ``error`` (``code: message``) as code and message."""
    if not error:
        return default, "The export could not be built"
    code, _, message = error.partition(": ")
    if not message or " " in code or "." not in code:
        return default, error
    return code, message


class QueuedExports:
    """Connects one tenant's export jobs to the job queue (a server's API process): after :meth:`attach`,
    :class:`~nodestro_api.portable_build.JobRunner` queues jobs here instead of building them."""

    def __init__(self, queue: JobQueue, tenant_id: str) -> None:
        self.queue = queue
        self.tenant_id = tenant_id

    def attach(self, ctx: Any) -> None:
        ctx.export_queue = self

    def enqueue(self, job: ExportJob) -> None:
        payload = {"publication_id": job.publication_id, "release_number": job.release_number}
        if self.queue.enqueue(self.tenant_id, EXPORT_JOB, job.id, payload) is None:
            raise NodestroError("export.busy", "This export is already queued")

    def cancel(self, ctx: Any, job: ExportJob) -> ExportJob:
        """Withdraw a queued job (cancelled at once) or ask the worker building it to stop."""
        found = self.queue.request_cancel(self.tenant_id, EXPORT_JOB, job.id)
        if found is not None and found.status == "cancelled":
            stored = ctx.store.get(JOBS, job.id, ExportJob) or job
            if stored.state == "running":
                _end(stored, "cancelled", "export.cancelled", "The export was cancelled")
                ctx.store.put(JOBS, stored.id, stored)
            return stored
        if found is None:
            return self.settle(ctx, job) or job
        return job

    def settle(self, ctx: Any, job: ExportJob) -> ExportJob | None:
        """The end of a job stored as running whose queue job ended without the worker storing it (stored now);
        ``None`` while it is queued or built."""
        queued = self.queue.latest(self.tenant_id, EXPORT_JOB, job.id)
        if queued is not None and queued.status in ("queued", "running"):
            return None
        stored: ExportJob | None = ctx.store.get(JOBS, job.id, ExportJob)
        if stored is None:
            return None
        if stored.state != "running":
            return stored  # the worker stored the end meanwhile
        if queued is None:
            if now() - stored.created_at < UNQUEUED_AFTER:
                return None  # being queued right now
            _end(stored, "failed", "export.interrupted", INTERRUPTED)
        elif queued.status == "cancelled":
            _end(stored, "cancelled", "export.cancelled", "The export was cancelled")
        elif queued.status == "failed":
            code, message = _split(queued.error, "export.failed")
            _end(stored, "failed", code, message)
        else:  # interrupted (reaped), or done without a stored end
            _end(stored, "failed", "export.interrupted", INTERRUPTED)
        ctx.store.put(JOBS, stored.id, stored)
        return stored


def interrupt_reaped(contexts: Callable[[str], Any], reaped: list[Job]) -> list[str]:
    """For the export jobs among ``reaped`` (their worker died): fail the export job with ``export.interrupted`` and
    remove its work folder. Returns the export job ids. A tenant that fails is logged and skipped."""
    done: list[str] = []
    for found in reaped:
        if found.kind != EXPORT_JOB:
            continue
        try:
            ctx = contexts(found.tenant_id)
            job = ctx.store.get(JOBS, found.ref, ExportJob)
            if job is not None and job.state == "running":
                ctx.store.put(JOBS, job.id, _end(job, "failed", "export.interrupted", INTERRUPTED))
            shutil.rmtree(portable_build.work_folder(ctx, found.ref), ignore_errors=True)
            done.append(found.ref)
        except Exception:
            logger.exception("Marking export job %s of tenant %s as interrupted failed", found.ref, found.tenant_id)
    return done


class ExportWorker:
    """Claims export jobs and builds them in this process, one at a time on the whole server (see the module
    documentation). ``contexts(tenant_id)`` returns the tenant's context (a worker's executing context)."""

    def __init__(
        self,
        queue: JobQueue,
        contexts: Callable[[str], Any],
        *,
        name: str | None = None,
        heartbeat: float | None = None,
        poll: float = 1.0,
        tenant_active: Callable[[str], bool] | None = None,
        lock: LeaderLock | None = None,
    ) -> None:
        self.queue = queue
        self.contexts = contexts
        self.name = name or worker_name()
        self.heartbeat_seconds = heartbeat if heartbeat is not None else queue.lease.total_seconds() / 3
        self.poll = poll
        self.tenant_active = tenant_active
        self.lock = lock if lock is not None else LeaderLock(queue.database, key=EXPORT_LOCK)

    async def run_once(self, stop: asyncio.Event | None = None) -> Job | None:
        """Claim one export job and build it to its end while holding the export lock; ``None`` when none was queued
        or another worker is building one."""
        if not await asyncio.to_thread(self.lock.acquire):
            return None
        try:
            job: Job | None = await asyncio.to_thread(self.queue.claim, self.name, (EXPORT_JOB,))
            if job is None:
                return None
            await self._execute(job, stop or asyncio.Event())
            return job
        finally:
            with suppress(Exception):
                await asyncio.to_thread(self.lock.release)

    async def _finish(self, job: Job, status: JobStatus, error: str | None = None) -> None:
        if not await asyncio.to_thread(self.queue.finish, job.id, self.name, status, error):
            logger.warning("Export job %s was no longer this worker's when it ended", job.id)

    async def _active(self, tenant_id: str) -> bool:
        if self.tenant_active is None:
            return True
        try:
            return bool(await asyncio.to_thread(self.tenant_active, tenant_id))
        except Exception:  # unknown: stop, as for a run (SR-03)
            return False

    async def _execute(self, job: Job, stop: asyncio.Event) -> None:
        try:
            ctx = await asyncio.to_thread(self.contexts, job.tenant_id)
            record = await asyncio.to_thread(ctx.store.get, JOBS, job.ref, ExportJob)
        except Exception as exc:
            logger.exception("Opening tenant %s for export job %s failed", job.tenant_id, job.id)
            code = exc.code if isinstance(exc, NodestroError) else "export.failed"
            await self._finish(job, "failed", f"{code}: the workspace could not be opened")
            return
        if record is None:
            await self._finish(job, "failed", "export.failed: the export job is gone")
            return
        if record.state != "running":  # ended meanwhile (settled as cancelled)
            await self._finish(job, "cancelled", "export.cancelled: the export was cancelled")
            return

        async def refuse(code: str, message: str, **params: Any) -> None:
            await asyncio.to_thread(ctx.store.put, JOBS, record.id, _end(record, "failed", code, message, **params))
            await self._finish(job, "failed", f"{code}: {message}")

        if not await self._active(job.tenant_id):
            await refuse("identity.tenant_suspended", SUSPENDED)
            return
        if not portable_build.server_jobs_enabled():
            await refuse(
                "export.server_unsupported",
                "Portable packages are not built on this server; its operator can enable them",
            )
            return
        try:
            from .exporter import ExportOptions

            publication = ctx.publications.get(record.publication_id)
            release = ctx.publications.release(publication.id, record.release_number)
            options = ExportOptions.model_validate(record.options)
        except NodestroError as exc:
            await refuse(exc.code, str(exc), **exc.params)
            return
        except Exception as exc:  # the publication or release is gone, or the stored options no longer validate
            logger.warning("Export job %s cannot be built: %s", record.id, type(exc).__name__)
            await refuse("export.failed", "The publication or its release is no longer available")
            return
        record.queued = False
        await asyncio.to_thread(ctx.store.put, JOBS, record.id, record)
        cancel = threading.Event()
        building = asyncio.ensure_future(
            asyncio.to_thread(portable_build.execute, ctx, record, publication, release, options, cancel)
        )
        lost = False
        stopped_by: str | None = None
        while not building.done():
            await asyncio.wait({building}, timeout=self.heartbeat_seconds)
            if building.done() or lost:
                continue
            beat = await asyncio.to_thread(self.queue.heartbeat, job.id, self.name)
            if beat is None:
                lost = True
                cancel.set()
                logger.error("Export job %s lost its lease; stopping it", job.id)
            elif cancel.is_set():
                continue
            elif beat.cancel_requested:
                cancel.set()
            elif stop.is_set():
                stopped_by = "stopped"
                cancel.set()
            elif not await self._active(job.tenant_id):
                stopped_by = "suspended"
                cancel.set()
        try:
            building.result()
        except BaseException:
            logger.exception("Export job %s ended without an outcome", job.id)
            _end(record, "failed", "export.failed", "The export could not be built")
        if lost or (stopped_by == "stopped" and record.state == "cancelled"):
            _end(record, "failed", "export.interrupted", INTERRUPTED)
        elif stopped_by == "suspended" and record.state == "cancelled":
            _end(record, "failed", "identity.tenant_suspended", SUSPENDED)
        await asyncio.to_thread(ctx.store.put, JOBS, record.id, record)
        if lost:
            return  # the reaper ended the queue job
        status: JobStatus = record.state if record.state in ("done", "cancelled") else "failed"
        error = f"{record.error_code}: {record.error}" if record.error_code and status != "done" else None
        await self._finish(job, status, error)

    async def serve(self, stop: asyncio.Event) -> None:
        """Build export jobs until ``stop`` is set (a running build is stopped then, ``export.interrupted``)."""
        while not stop.is_set():
            try:
                job = await self.run_once(stop)
            except Exception:
                logger.exception("The export worker loop failed; retrying")
                job = None
            if job is None:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll)


__all__ = ["EXPORT_JOB", "EXPORT_LOCK", "ExportWorker", "QueuedExports", "interrupt_reaped"]
