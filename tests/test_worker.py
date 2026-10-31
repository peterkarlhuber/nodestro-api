"""Worker processes of a server: queued runs, event streams across processes, reaper, schedules (needs PostgreSQL).

The API (``create_tenant_app`` with a job queue) and the worker (``ServerWorker``) use separate database
connections and contexts, like separate processes. `nodestro-server worker` runs the same class.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, TypeVar
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import ScriptedClient, text

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.jobs import RUN_JOB, JobQueue
from nodestro_runtime.storage_postgres import PostgresDatabase
import stub_oidc
from test_identity import DSN, Server, _wait

from nodestro_api.identity import OidcClient, OidcSettings
from nodestro_api.schedules import COLLECTION, Schedule
from nodestro_api.server_app import TenantComposition, TenantFiles, create_tenant_app
from nodestro_api.worker import ServerWorker, WorkerSettings

T = TypeVar("T")

FLOW = {
    "name": "Flow",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "ALPHA"}},
        {"id": "out", "type": "output"},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
}


@pytest.fixture
def schema() -> Iterator[str]:
    if not DSN:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    import psycopg
    from psycopg import sql

    name = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    database = PostgresDatabase(DSN, schema=name)
    database.migrate()
    database.close()
    yield name
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def connect(schema: str) -> Iterator[Callable[[], PostgresDatabase]]:
    opened: list[PostgresDatabase] = []

    def factory() -> PostgresDatabase:
        database = PostgresDatabase(DSN, schema=schema)
        opened.append(database)
        return database

    yield factory
    for database in opened:
        database.close()


class Background:
    """A worker process stand-in: its own event loop in its own thread."""

    def __init__(self, worker: ServerWorker) -> None:
        self.worker = worker
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.closed = False

    def submit(self, work: Coroutine[Any, Any, T]) -> Any:
        return asyncio.run_coroutine_threadsafe(work, self.loop)

    def call(self, work: Coroutine[Any, Any, T], timeout: float = 30) -> T:
        return self.submit(work).result(timeout)  # type: ignore[no-any-return]

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.call(self.worker.aclose())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


@pytest.fixture
def make_worker(connect: Callable[[], PostgresDatabase], tmp_path: Path) -> Iterator[Callable[..., Background]]:
    made: list[Background] = []

    def factory(script: dict[str, list[Any]] | None = None, **settings: Any) -> Background:
        database = connect()
        scripted = ScriptedClient(script or {})
        files = TenantFiles(
            database=database,
            data_root=tmp_path / "server",
            client_factory=lambda _provider: scripted,
            integrations=[],
        )
        background = Background(ServerWorker(files, IdentityStore(database), WorkerSettings(**settings)))
        made.append(background)
        return background

    yield factory
    for background in made:
        background.close()


@pytest.fixture
def server(connect: Callable[[], PostgresDatabase], tmp_path: Path) -> Iterator[Server]:
    database = connect()
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
    identity = IdentityStore(database)
    # The API process never calls a model: every run executes in a worker.
    nothing = ScriptedClient({})
    app = create_tenant_app(
        TenantComposition(
            database=database,
            identity=identity,
            oidc=oidc,
            origin=stub_oidc.ORIGIN,
            data_root=tmp_path / "server",
            client_factory=lambda _provider: nothing,
            integrations=[],
            queue=JobQueue(database),
        )
    )
    built = Server(app, identity, provider, [])
    yield built
    for client in built.clients:
        client.__exit__(None, None, None)


def _start(browser: TestClient) -> str:
    workflow = browser.post("/api/workflows", json=FLOW)
    assert workflow.status_code == 201, workflow.text
    run = browser.post(f"/api/workflows/{workflow.json()['id']}/runs", json={"input": "go"})
    assert run.status_code == 201, run.text
    return str(run.json()["id"])


def _blocking(release: threading.Event, started: threading.Event) -> Callable[[dict[str, Any]], Any]:
    def respond(_request: dict[str, Any]) -> Any:
        started.set()
        release.wait(15)
        return text("from the worker")

    return respond


# ---- runs ---------------------------------------------------------------------------------------


def test_the_api_queues_runs_and_a_worker_executes_them(server: Server, make_worker: Callable[..., Background]) -> None:
    alice = server.member("acme", "alice", "owner")
    run_id = _start(alice)
    time.sleep(0.3)
    assert alice.get(f"/api/runs/{run_id}").json()["status"] == "queued"  # nothing executes in the API
    worker = make_worker({"ALPHA": [text("from the worker")]})
    job = worker.call(worker.worker.runner.run_once())
    assert job is not None and job.tenant_id == "acme" and job.ref == run_id
    finished = _wait(alice, run_id)
    assert finished["status"] == "succeeded", finished
    assert finished["output"] == "from the worker"


def test_the_api_streams_the_events_of_a_run_in_a_worker(
    server: Server, make_worker: Callable[..., Background]
) -> None:
    release, started = threading.Event(), threading.Event()
    alice = server.member("acme", "alice", "owner")
    run_id = _start(alice)
    worker = make_worker({"ALPHA": [_blocking(release, started)]})
    executing = worker.submit(worker.worker.runner.run_once())
    assert started.wait(15)
    kinds: list[str] = []
    seqs: list[int] = []
    try:
        with alice.stream("GET", f"/api/runs/{run_id}/events") as stream:
            release.set()
            for line in stream.iter_lines():
                if line.startswith("event: "):
                    kinds.append(line.removeprefix("event: "))
                elif line.startswith("data: ") and kinds[-1] == "event":
                    seqs.append(json.loads(line.removeprefix("data: "))["seq"])
                if kinds and kinds[-1] == "end":
                    break
    finally:
        release.set()
    executing.result(30)
    assert kinds[0] == "snapshot" and kinds[-1] == "end" and "event" in kinds
    assert seqs == list(range(1, len(seqs) + 1))
    # Another tenant cannot follow it.
    bob = server.member("globex", "bob", "owner")
    assert bob.get(f"/api/runs/{run_id}/events").status_code == 404


def test_cancelling_in_the_api_stops_the_run_in_the_worker(
    server: Server, make_worker: Callable[..., Background]
) -> None:
    release, started = threading.Event(), threading.Event()
    alice = server.member("acme", "alice", "owner")
    run_id = _start(alice)
    worker = make_worker({"ALPHA": [_blocking(release, started)]}, lease=timedelta(seconds=0.6))
    executing = worker.submit(worker.worker.runner.run_once())
    try:
        assert started.wait(15)
        answer = alice.post(f"/api/runs/{run_id}/cancel")
        assert answer.status_code in (200, 202, 204), answer.text
        job = executing.result(30)
    finally:
        release.set()
    assert job is not None
    assert _wait(alice, run_id)["status"] == "cancelled"


def test_the_reaper_interrupts_the_run_of_a_dead_worker(
    server: Server, make_worker: Callable[..., Background], connect: Callable[[], PostgresDatabase]
) -> None:
    alice = server.member("acme", "alice", "owner")
    run_id = _start(alice)
    dead = JobQueue(connect(), lease=timedelta(milliseconds=50))
    assert dead.claim("dead-worker") is not None  # claimed, then the process died
    time.sleep(0.2)
    survivor = make_worker({"ALPHA": [text("must not run")]})
    reaped = survivor.call(asyncio.to_thread(survivor.worker.reap))
    assert [job.ref for job in reaped] == [run_id]
    run = alice.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "failed" and run["error_code"] == "run.interrupted"
    assert survivor.call(survivor.worker.runner.run_once()) is None  # never executed again


# ---- schedules ----------------------------------------------------------------------------------


def _due_schedule(server: Server, browser: TestClient, slot: datetime) -> str:
    workflow = browser.post("/api/workflows", json=FLOW).json()
    context = server.app.state.tenants.get("acme")
    schedule = Schedule(workflow_id=workflow["id"], kind="daily", at="08:00", enabled=True, next_run_at=slot)
    context.store.put(COLLECTION, schedule.id, schedule)
    return schedule.id


def test_only_the_leader_fires_a_schedule_and_each_slot_fires_once(
    server: Server, make_worker: Callable[..., Background], connect: Callable[[], PostgresDatabase]
) -> None:
    alice = server.member("acme", "alice", "owner")
    slot = datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)
    schedule_id = _due_schedule(server, alice, slot)
    first, second = make_worker(), make_worker()
    for background in (first, second):
        context = background.call(_scheduling(background, "acme"))
        context.schedules.clock = lambda: slot + timedelta(minutes=1)
    fired = first.call(first.worker.fire_schedules())
    assert len(fired) == 1
    assert second.call(second.worker.fire_schedules()) == []  # not the leader
    queue = JobQueue(connect())
    assert queue.active("acme", RUN_JOB, fired[0]) is not None  # queued for a worker, not executed here

    # The leader dies; the second worker takes over. Even a schedule that still names the fired slot (e.g. the
    # leader died before storing it) does not fire it again.
    first.close()
    context = server.app.state.tenants.get("acme")
    stored = context.store.get(COLLECTION, schedule_id, Schedule)
    stored.next_run_at = slot
    context.store.put(COLLECTION, schedule_id, stored)
    deadline = time.monotonic() + 5
    while not second.call(asyncio.to_thread(second.worker.leader.acquire)):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert second.call(second.worker.fire_schedules()) == []
    runs = alice.get("/api/runs").json()
    listed = runs["items"] if isinstance(runs, dict) else runs
    assert [item["id"] for item in listed] == fired


async def _scheduling(background: Background, tenant: str) -> Any:
    return background.worker.scheduling.get(tenant)
