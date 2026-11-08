"""Portable export builds as queued jobs of a server (API-048; needs PostgreSQL).

The API process (``create_tenant_app`` with a job queue) only stores and queues an export job; a worker
(``ServerWorker.exporter``) builds it with a lease, one at a time on the whole server, and a build whose worker died
fails with ``export.interrupted`` instead of being failed by another API process that opens the tenant.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import hashlib
from pathlib import Path
import threading
import time
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from fault_proxy import FaultProxy, fault_proxy  # noqa: F401  (fixture)
from nodestro_runtime.jobs import JobQueue
from nodestro_runtime.storage_postgres import PostgresDatabase
from test_identity import DSN, Server
from test_server_exports import OPTIONS, _published
from test_worker import Background, connect, make_worker, schema, server  # noqa: F401  (fixtures)

from nodestro_api import exporter, portable_build
from nodestro_api.export_jobs import EXPORT_JOB, UNQUEUED_AFTER, QueuedExports, interrupt_reaped

PACKAGE = b"PK portable package stand-in"


class Builds:
    """Stands in for :func:`nodestro_api.portable_build.build`: records which process built what, can hold a build
    until it is released (checking for a cancel meanwhile), and stores a real export record with its ZIP."""

    def __init__(self, hold: bool = False) -> None:
        self.calls: list[str] = []
        self.started = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()
        self.error: Exception | None = None

    def __call__(self, ctx: Any, publication: Any, release: Any, options: Any, progress: Any) -> Any:
        self.calls.append(progress.job.id)
        progress.step("python", "a step")
        # The work folder exists while it builds (the reaper removes it).
        portable_build.work_folder(ctx, progress.job.id).mkdir(parents=True, exist_ok=True)
        self.started.set()
        while not self.release.wait(0.02):
            progress.check()
        progress.check()
        if self.error is not None:
            raise self.error
        record = exporter.ExportRecord(
            publication_id=publication.id,
            release_id=release.id,
            release_number=release.number,
            options=options,
            file_name="package.zip",
            size=len(PACKAGE),
            sha256=hashlib.sha256(PACKAGE).hexdigest(),
            files=1,
        )
        target = ctx.config.data_dir / "exports" / f"{record.id}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PACKAGE)
        exporter.record_export(ctx, publication, release, record)
        return record


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> Builds:
    monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, "1")
    monkeypatch.delenv(portable_build.SERVER_MAX_BYTES_ENV, raising=False)
    monkeypatch.delenv(portable_build.SERVER_TENANT_QUOTA_ENV, raising=False)
    found = Builds()
    monkeypatch.setattr(portable_build, "build", found)
    return found


def _queue(client: Any, publication: str) -> dict[str, Any]:
    answer = client.post(f"/api/publications/{publication}/export-jobs", json={"options": OPTIONS})
    assert answer.status_code == 202, answer.text
    return answer.json()  # type: ignore[no-any-return]


def _api_process(database: PostgresDatabase, tmp_path: Path) -> Any:
    """Another API process of the same server (its own connection and contexts, the shared data folder)."""
    import httpx
    from nodestro_runtime.identity_store import IdentityStore
    import stub_oidc

    from nodestro_api.identity import OidcClient, OidcSettings
    from nodestro_api.server_app import TenantComposition, create_tenant_app

    from conftest import ScriptedClient

    provider = stub_oidc.StubProvider()
    oidc = OidcClient(
        OidcSettings(
            issuer=stub_oidc.ISSUER,
            client_id=stub_oidc.CLIENT_ID,
            client_secret=stub_oidc.CLIENT_SECRET,
            redirect_uri=stub_oidc.REDIRECT_URI,
        ),
        http=httpx.Client(transport=provider.transport()),
    )
    return create_tenant_app(
        TenantComposition(
            database=database,
            identity=IdentityStore(database),
            oidc=oidc,
            origin=stub_oidc.ORIGIN,
            data_root=tmp_path / "server",
            client_factory=lambda _provider: ScriptedClient({}),
            integrations=[],
            queue=JobQueue(database),
        )
    )


def _job(client: Any, job_id: str) -> dict[str, Any]:
    answer = client.get(f"/api/export-jobs/{job_id}")
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


def _queue_row(database: PostgresDatabase, ref: str) -> tuple[str, str | None, bool]:
    rows = database._query(
        "SELECT status, error, cancel_requested FROM nodestro_jobs WHERE kind = %s AND ref = %s", (EXPORT_JOB, ref)
    )
    assert len(rows) == 1
    return rows[0][0], rows[0][1], rows[0][2]


def _until(check: Callable[[], bool], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_the_api_queues_an_export_that_a_worker_builds(
    server: Server,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    builds: Builds,
    connect: Callable[[], PostgresDatabase],  # noqa: F811
) -> None:
    alice = server.member("acme", "alice", "owner")
    viewer = server.member("acme", "vic", "viewer")
    publication = _published(alice)
    job = _queue(alice, publication)
    assert job["state"] == "running" and job["queued"] is True
    time.sleep(0.3)
    assert builds.calls == []  # nothing is built in the API process
    assert _job(viewer, job["id"])["queued"] is True
    # One active export job per tenant.
    busy = alice.post(f"/api/publications/{publication}/export-jobs", json={"options": OPTIONS})
    assert busy.status_code == 400 and busy.json()["code"] == "export.busy", busy.text
    assert _queue_row(connect(), job["id"])[0] == "queued"

    worker = make_worker()
    claimed = worker.call(worker.worker.exporter.run_once())
    assert claimed is not None and claimed.tenant_id == "acme" and claimed.ref == job["id"]
    assert builds.calls == [job["id"]]
    done = _job(viewer, job["id"])
    assert done["state"] == "done" and done["queued"] is False and done["progress"] == 1.0, done
    assert _queue_row(connect(), job["id"])[:2] == ("done", None)
    # The package is in the tenant's shared data folder: the API process serves it.
    download = alice.get(f"/api/exports/{done['export_id']}/download")
    assert download.status_code == 200 and download.content == PACKAGE
    listed = alice.get(f"/api/publications/{publication}/export-jobs").json()
    assert [item["state"] for item in listed] == ["done"]
    # The tenant can queue the next one now; no worker holds the export lock between builds.
    assert _queue(alice, publication)["queued"] is True
    assert worker.call(worker.worker.exporter.run_once()) is not None


def test_one_export_is_built_at_a_time_on_the_whole_server(
    server: Server,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    builds: Builds,
    connect: Callable[[], PostgresDatabase],  # noqa: F811
) -> None:
    builds.release.clear()
    alice = server.member("acme", "alice", "owner")
    bob = server.member("globex", "bob", "owner")
    first = _queue(alice, _published(alice))
    second = _queue(bob, _published(bob))  # another tenant waits in the queue instead of export.busy
    one, two = make_worker(), make_worker()
    building = one.submit(one.worker.exporter.run_once())
    try:
        assert builds.started.wait(15)
        # The other worker holds no export lock while one is building: it takes nothing.
        assert two.call(two.worker.exporter.run_once()) is None
        assert _job(bob, second["id"])["queued"] is True
        assert _queue_row(connect(), second["id"])[0] == "queued"
    finally:
        builds.release.set()
    assert building.result(30).ref == first["id"]
    assert _job(alice, first["id"])["state"] == "done"
    assert two.call(two.worker.exporter.run_once()).ref == second["id"]
    assert _job(bob, second["id"])["state"] == "done"
    assert builds.calls == [first["id"], second["id"]]


def test_cancelling_withdraws_a_queued_export_and_stops_a_building_one(
    server: Server,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    builds: Builds,
    connect: Callable[[], PostgresDatabase],  # noqa: F811
) -> None:
    alice = server.member("acme", "alice", "owner")
    viewer = server.member("acme", "vic", "viewer")
    publication = _published(alice)
    queued = _queue(alice, publication)
    assert viewer.post(f"/api/export-jobs/{queued['id']}/cancel").status_code == 403
    cancelled = alice.post(f"/api/export-jobs/{queued['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled", cancelled.text
    assert cancelled.json()["error_code"] == "export.cancelled"
    assert _queue_row(connect(), queued["id"])[0] == "cancelled"
    worker = make_worker(lease=timedelta(seconds=0.6))
    assert worker.call(worker.worker.exporter.run_once()) is None  # withdrawn: no worker builds it
    # Cancelling an ended job changes nothing.
    assert alice.post(f"/api/export-jobs/{queued['id']}/cancel").json()["state"] == "cancelled"

    builds.release.clear()
    running = _queue(alice, publication)
    building = worker.submit(worker.worker.exporter.run_once())
    try:
        assert builds.started.wait(15)
        _until(lambda: _job(alice, running["id"])["queued"] is False)
        asked = alice.post(f"/api/export-jobs/{running['id']}/cancel")
        assert asked.status_code == 200 and asked.json()["state"] == "running"  # the worker stops it
        assert _queue_row(connect(), running["id"])[2] is True
        assert building.result(30) is not None
    finally:
        builds.release.set()
    ended = _job(alice, running["id"])
    assert ended["state"] == "cancelled" and ended["error_code"] == "export.cancelled", ended
    assert _queue_row(connect(), running["id"])[0] == "cancelled"
    assert alice.get(f"/api/publications/{publication}/exports").json() == []


def test_a_build_of_a_dead_worker_fails_as_interrupted_and_no_api_process_fails_a_live_one(
    server: Server,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    builds: Builds,
    connect: Callable[[], PostgresDatabase],  # noqa: F811
    tmp_path: Path,
) -> None:
    """Opening a tenant in another API process no longer marks a build failed (the old in-process recovery did); a
    build whose worker died is failed by the reaper of another worker, which also removes its work folder."""
    alice = server.member("acme", "alice", "owner")
    job = _queue(alice, _published(alice))
    dead = JobQueue(connect(), lease=timedelta(milliseconds=600))
    assert dead.claim("dead-worker", (EXPORT_JOB,)) is not None  # claimed, then the worker process died
    context = server.app.state.tenants.get("acme")
    work = portable_build.work_folder(context, job["id"])
    (work / "package").mkdir(parents=True)
    (work / "package.partial").write_bytes(b"half an archive")

    # A second API process on its own connection opens the tenant: the job stays running, its files stay.
    other = _api_process(connect(), tmp_path)
    opened = other.state.tenants.get("acme")
    assert portable_build.runner(opened).get(job["id"]).state == "running"
    assert _job(alice, job["id"])["state"] == "running"
    assert work.is_dir()

    time.sleep(0.8)  # the lease ran out
    survivor = make_worker()
    reaped = survivor.call(asyncio.to_thread(survivor.worker.reap))
    assert [item.ref for item in reaped] == [job["id"]]
    ended = _job(alice, job["id"])
    assert ended["state"] == "failed" and ended["error_code"] == "export.interrupted", ended
    assert not work.exists()
    assert survivor.call(survivor.worker.exporter.run_once()) is None  # never built again by itself
    assert builds.calls == []
    # The tenant can start it again.
    assert _queue(alice, _published(alice))["queued"] is True


def test_reading_a_job_settles_a_queue_job_that_ended_without_a_worker(
    server: Server,  # noqa: F811
    builds: Builds,
    connect: Callable[[], PostgresDatabase],  # noqa: F811
) -> None:
    """Without a reaper pass of the API's own (another worker reaped the queue job but failed to store the end), the
    next read stores it; a job that never reached the queue is settled once it is old enough."""
    alice = server.member("acme", "alice", "owner")
    publication = _published(alice)
    job = _queue(alice, publication)
    database = connect()
    database._write(
        "UPDATE nodestro_jobs SET status = 'interrupted' WHERE kind = %s AND ref = %s", (EXPORT_JOB, job["id"])
    )
    assert _job(alice, job["id"])["error_code"] == "export.interrupted"

    failed = _queue(alice, publication)
    database._write(
        "UPDATE nodestro_jobs SET status = 'failed', error = 'export.disk_full: no space' WHERE kind = %s AND ref = %s",
        (EXPORT_JOB, failed["id"]),
    )
    listed = {item["id"]: item for item in alice.get(f"/api/publications/{publication}/export-jobs").json()}
    assert (listed[failed["id"]]["state"], listed[failed["id"]]["error_code"]) == ("failed", "export.disk_full")
    assert listed[failed["id"]]["error"] == "no space"

    # A record without a queue job: left alone while it may be queued right now, settled when it is old.
    context = server.app.state.tenants.get("acme")
    stray = portable_build.ExportJob(publication_id=publication, release_number=1, queued=True)
    context.store.put(portable_build.JOBS, stray.id, stray)
    assert _job(alice, stray.id)["state"] == "running"
    stray.created_at = stray.created_at - UNQUEUED_AFTER - timedelta(seconds=1)
    context.store.put(portable_build.JOBS, stray.id, stray)
    assert _job(alice, stray.id)["error_code"] == "export.interrupted"
    assert isinstance(context.export_queue, QueuedExports)


def test_a_worker_that_loses_its_lease_or_stops_ends_the_build_as_interrupted(
    server: Server,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    builds: Builds,
    connect: Callable[[], PostgresDatabase],  # noqa: F811
) -> None:
    builds.release.clear()
    alice = server.member("acme", "alice", "owner")
    publication = _published(alice)
    lost = _queue(alice, publication)
    worker = make_worker(lease=timedelta(seconds=0.6))
    building = worker.submit(worker.worker.exporter.run_once())
    assert builds.started.wait(15)
    # The worker's lease is taken away (as when the reaper of another worker judged it dead).
    connect()._write("UPDATE nodestro_jobs SET status = 'interrupted' WHERE ref = %s", (lost["id"],))
    assert building.result(30) is not None
    ended = _job(alice, lost["id"])
    assert ended["state"] == "failed" and ended["error_code"] == "export.interrupted", ended
    assert _queue_row(connect(), lost["id"])[0] == "interrupted"
    assert alice.get(f"/api/publications/{publication}/exports").json() == []

    builds.started.clear()
    stopping = _queue(alice, publication)
    stop = asyncio.Event()
    building = worker.submit(worker.worker.exporter.run_once(stop))
    assert builds.started.wait(15)
    worker.loop.call_soon_threadsafe(stop.set)
    assert building.result(30) is not None
    ended = _job(alice, stopping["id"])
    assert ended["state"] == "failed" and ended["error_code"] == "export.interrupted", ended
    status, error, _ = _queue_row(connect(), stopping["id"])
    assert status == "failed" and error is not None and error.startswith("export.interrupted: ")


def test_a_worker_refuses_or_stops_builds_it_must_not_finish(
    server: Server,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    builds: Builds,
    connect: Callable[[], PostgresDatabase],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suspended tenant's build does not start, or stops; a worker whose operator did not enable export jobs
    refuses them; a failed build keeps its code and parameters; a deleted publication fails the job."""
    alice = server.member("acme", "alice", "owner")
    publication = _published(alice)
    worker = make_worker(lease=timedelta(seconds=0.6))
    context = server.app.state.tenants.get("acme")

    def stored(job_id: str) -> portable_build.ExportJob:
        return portable_build.runner(context).get(job_id)

    # Suspended before a worker took it.
    first = _queue(alice, publication)
    server.identity.set_tenant_status("acme", "suspended")
    assert worker.call(worker.worker.exporter.run_once()) is not None
    assert (stored(first["id"]).state, stored(first["id"]).error_code) == ("failed", "identity.tenant_suspended")
    assert builds.calls == []
    server.identity.set_tenant_status("acme", "active")

    # Suspended while it builds.
    builds.release.clear()
    second = _queue(alice, publication)
    building = worker.submit(worker.worker.exporter.run_once())
    assert builds.started.wait(15)
    server.identity.set_tenant_status("acme", "suspended")
    assert building.result(30) is not None
    assert (stored(second["id"]).state, stored(second["id"]).error_code) == ("failed", "identity.tenant_suspended")
    assert _queue_row(connect(), second["id"])[0] == "failed"
    server.identity.set_tenant_status("acme", "active")
    builds.release.set()

    # The worker's operator setting is off.
    third = _queue(alice, publication)
    monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, "0")
    assert worker.call(worker.worker.exporter.run_once()) is not None
    assert stored(third["id"]).error_code == "export.server_unsupported"
    monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, "1")

    # The build fails with a code and parameters.
    builds.error = portable_build.NodestroError("export.server_too_large", "Too large", size=5, limit=4)
    fourth = _queue(alice, publication)
    assert worker.call(worker.worker.exporter.run_once()) is not None
    job = _job(alice, fourth["id"])
    assert job["state"] == "failed" and job["error_code"] == "export.server_too_large"
    assert job["error_params"] == {"size": 5, "limit": 4}
    assert _queue_row(connect(), fourth["id"])[1] == "export.server_too_large: Too large"
    builds.error = None

    # The publication is gone before a worker takes the job.
    fifth = _queue(alice, publication)
    workflow = context.publications.get(publication).workflow_id
    assert alice.delete(f"/api/workflows/{workflow}").status_code == 204
    calls = len(builds.calls)
    assert worker.call(worker.worker.exporter.run_once()) is not None
    gone = stored(fifth["id"])
    assert gone.state == "failed" and gone.error_code is not None and len(builds.calls) == calls


def test_reaping_skips_a_tenant_it_cannot_open(tmp_path: Path) -> None:
    from nodestro_runtime.jobs import Job
    from nodestro_runtime.schemas import now

    def broken(_tenant: str) -> Any:
        raise RuntimeError("cannot open")

    job = Job(id=1, tenant_id="acme", kind=EXPORT_JOB, ref="xjob_1", status="interrupted", created_at=now())
    run = Job(id=2, tenant_id="acme", kind="run", ref="run_1", status="interrupted", created_at=now())
    assert interrupt_reaped(broken, [job, run]) == []


def test_an_export_whose_enqueue_reply_is_lost_keeps_its_document_and_is_built(
    server: Server,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    builds: Builds,
    connect: Callable[[], PostgresDatabase],  # noqa: F811
    schema: str,  # noqa: F811
    fault_proxy: FaultProxy,  # noqa: F811
    tmp_path: Path,
) -> None:
    """JQ-4 (runtime JQ-3, RT2-03): the queue row of an export is committed but the reply to the enqueue is lost.

    Before the runtime fix the runtime repeated the INSERT, got "already queued" back, the API answered
    ``export.busy`` and ``JobRunner._enqueue`` (``portable_build.py``) deleted the export document while its queue row
    stayed; the worker then ended that row with ``export.failed: the export job is gone``. Now the enqueue returns the
    queued job: the request succeeds, the document stays, and a worker finds and builds it."""
    from fastapi.testclient import TestClient
    import stub_oidc

    alice = server.member("acme", "alice", "owner")
    publication = _published(alice)
    # Another API process of the server whose database connection goes through the fault proxy, with alice's session.
    proxied = PostgresDatabase(fault_proxy.dsn(DSN), schema=schema, pool_size=2)
    try:
        with TestClient(_api_process(proxied, tmp_path), base_url=stub_oidc.ORIGIN, follow_redirects=False) as other:
            other.headers["Origin"] = stub_oidc.ORIGIN
            other.cookies = alice.cookies
            fault_proxy.arm(b"INSERT INTO nodestro_jobs")
            answer = other.post(f"/api/publications/{publication}/export-jobs", json={"options": OPTIONS})
            assert (fault_proxy.fired, fault_proxy.replies_lost) == (1, 1)  # committed, then the reply was lost
            assert answer.status_code == 202, answer.text
            job = answer.json()
    finally:
        proxied.close()
    stored = _job(alice, job["id"])
    assert stored["state"] == "running" and stored["queued"] is True, stored
    assert _queue_row(connect(), job["id"])[0] == "queued"

    worker = make_worker()
    claimed = worker.call(worker.worker.exporter.run_once())
    assert claimed is not None and claimed.ref == job["id"]
    assert builds.calls == [job["id"]]
    done = _job(alice, job["id"])
    assert done["state"] == "done", done
    assert _queue_row(connect(), job["id"])[:2] == ("done", None)
