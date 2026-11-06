"""Worker processes of a server: queued runs, event streams across processes, reaper, schedules (needs PostgreSQL).

The API (``create_tenant_app`` with a job queue) and the worker (``ServerWorker``) use separate database
connections and contexts, like separate processes. `nodestro-server worker` runs the same class.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
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
from nodestro_runtime.testing import StaticDockerMonitor
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


def test_the_worker_holds_the_configured_runs_and_waiting_slots(make_worker: Callable[..., Background]) -> None:
    default = make_worker().worker.runner
    assert (default.max_runs, default.slots.max_waiting) == (2, 1)
    wider = make_worker(max_runs=4, max_waiting=3).worker.runner
    assert (wider.max_runs, wider.slots.max_waiting) == (4, 3)
    with pytest.raises(ValueError):
        make_worker(max_runs=1, max_waiting=1)


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
    slot = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
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


# ---- audit hardening: streams end with access, housekeeping ---------------------------------------


def _live_lines(browser: TestClient, path: str) -> Iterator[str]:
    """The lines of an event stream as the app sends them, with ``browser``'s cookies.

    Starlette's TestClient before 1.8 (the last release for Python 3.10 is 1.7) reads a whole response before it
    returns, so a stream that stays open cannot be followed through it; this calls the ASGI app directly."""
    base = httpx.URL(str(browser.base_url))
    headers = [(b"host", base.netloc), (b"cookie", "; ".join(f"{k}={v}" for k, v in browser.cookies.items()).encode())]
    if origin := browser.headers.get("origin"):
        headers.append((b"origin", origin.encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": base.scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": (base.host, base.port or (443 if base.scheme == "https" else 80)),
        "state": dict(getattr(browser, "app_state", {})),
    }
    chunks: queue.Queue[bytes | None] = queue.Queue()
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    requested = False

    async def receive() -> dict[str, Any]:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await stop.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunks.put(message.get("body", b""))

    async def serve() -> None:
        try:
            await browser.app(scope, receive, send)  # type: ignore[arg-type]
        finally:
            chunks.put(None)

    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),), daemon=True)
    thread.start()
    buffer = ""
    try:
        while (chunk := chunks.get(timeout=30)) is not None:
            buffer += chunk.decode()
            *lines, buffer = buffer.split("\n")
            yield from lines
        if buffer:
            yield buffer
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(10)


def test_an_open_event_stream_ends_when_the_member_loses_access(server: Server) -> None:
    """A-04: a removed member's open stream ends (``access_ended``) and receives nothing written afterwards."""
    from nodestro_runtime.jobs import PostgresEventLog
    from nodestro_runtime.schemas import RunEvent

    server.app.state.stream_recheck_seconds = 0.2
    owner = server.member("acme", "alice", "owner")
    viewer = server.member("acme", "victor", "viewer")
    run_id = _start(owner)
    received: list[str] = []
    opened = threading.Event()

    def follow() -> None:
        for line in _live_lines(viewer, f"/api/runs/{run_id}/events"):
            if line.startswith(("data: ", "event: ")):
                received.append(line)
                opened.set()

    thread = threading.Thread(target=follow, daemon=True)
    thread.start()
    log = PostgresEventLog(server.identity.database, "acme")
    try:
        assert opened.wait(10)
        server.identity.remove_member("acme", server.identity.login(stub_oidc.ISSUER, "victor").id)
        thread.join(5)
        assert not thread.is_alive(), received
    finally:
        log.append(RunEvent(seq=1, run_id=run_id, type="node_finished", node_id="a", data={"output": "SECRET"}))
        log.append(RunEvent(seq=2, run_id=run_id, type="run_finished", data={"status": "succeeded"}))
        thread.join(10)
    assert received[-2:] == ["event: end", 'data: {"reason": "access_ended"}']
    assert not [line for line in received if "SECRET" in line]


def test_a_member_keeps_streaming_while_access_lasts(server: Server) -> None:
    from nodestro_runtime.jobs import PostgresEventLog
    from nodestro_runtime.schemas import RunEvent

    server.app.state.stream_recheck_seconds = 0.1
    owner = server.member("acme", "alice", "owner")
    run_id = _start(owner)
    received: list[str] = []
    opened = threading.Event()

    def follow() -> None:
        for line in _live_lines(owner, f"/api/runs/{run_id}/events"):
            received.append(line)
            opened.set()
            if line.startswith("event: end"):
                return

    thread = threading.Thread(target=follow, daemon=True)
    thread.start()
    assert opened.wait(10)
    time.sleep(0.6)  # several checks pass
    log = PostgresEventLog(server.identity.database, "acme")
    log.append(RunEvent(seq=1, run_id=run_id, type="node_finished", node_id="a", data={"output": "visible"}))
    log.append(RunEvent(seq=2, run_id=run_id, type="run_finished", data={"status": "succeeded"}))
    thread.join(10)
    assert not thread.is_alive()
    assert [line for line in received if "visible" in line]
    assert not [line for line in received if "access_ended" in line]


def test_the_leader_deletes_expired_sessions_logins_old_jobs_and_events(
    server: Server, make_worker: Callable[..., Background], connect: Callable[[], PostgresDatabase]
) -> None:
    """A-02: abandoned login starts, expired sessions, old finished jobs and old run events do not pile up."""
    database = connect()
    identity = IdentityStore(database)
    alice = server.member("acme", "alice", "owner")
    for _ in range(3):
        assert server.browser().get("/api/auth/login").status_code == 303  # anonymous, abandoned
    database._write("UPDATE nodestro_login_states SET expires_at = clock_timestamp() - interval '1 minute'", ())
    fresh = server.browser().get("/api/auth/login")  # a login that is still starting
    assert fresh.status_code == 303
    user = identity.login(stub_oidc.ISSUER, "alice")
    stale = identity.create_session(user.id, "acme")
    database._write(
        "UPDATE nodestro_sessions SET expires_at = clock_timestamp() - interval '1 minute' WHERE token_hash = %s",
        (hashlib.sha256(stale.encode()).digest(),),
    )
    queue = JobQueue(database)
    for ref, age in (("run_old", "8 days"), ("run_recent", "1 day")):
        job = queue.enqueue("acme", RUN_JOB, ref)
        assert job is not None
        database._write(
            "UPDATE nodestro_jobs SET status = 'done', finished_at = clock_timestamp() - %s::interval WHERE id = %s",
            (age, job.id),
        )
    queued = queue.enqueue("acme", RUN_JOB, "run_waiting")
    for seq, age in ((1, "3 days"), (2, "1 hour")):
        database._write(
            "INSERT INTO nodestro_run_events (tenant_id, run_id, seq, event, created_at)"
            " VALUES ('acme', 'run_x', %s, '{}', clock_timestamp() - %s::interval)",
            (seq, age),
        )

    leader, other = make_worker(), make_worker()
    assert leader.call(leader.worker.fire_schedules()) == []  # takes the scheduler lock
    assert other.call(other.worker.clean_up()) == {}  # only the leader cleans up
    removed = leader.call(leader.worker.clean_up())
    assert removed == {"sessions_and_logins": 4, "jobs": 1, "run_events": 1}, removed

    assert database._query("SELECT count(*) FROM nodestro_login_states") == [(1,)]  # the fresh one
    assert alice.get("/api/auth/me").status_code == 200  # valid sessions stay
    refs = {row[0] for row in database._query("SELECT ref FROM nodestro_jobs")}
    assert refs == {"run_recent", "run_waiting"} and queued is not None
    assert database._query("SELECT seq FROM nodestro_run_events") == [(2,)]


def _old_chat_file(browser: TestClient, background: Background, tenant: str, days: int) -> tuple[str, str]:
    """A conversation with one chat file attached ``days`` ago (the worker's context backdates it)."""
    conversation = browser.post("/api/assistant/conversations", json={"title": "Files"})
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]
    uploaded = browser.post(
        f"/api/assistant/conversations/{conversation_id}/files", files=[("files", ("old.txt", b"old notes"))]
    )
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()[0]["file"]["id"]

    async def backdate() -> None:
        assistant = background.worker.scheduling.get(tenant).assistant
        with assistant._lock:
            found = assistant._conversation(conversation_id)
            found.files[0].added_at = datetime.now(UTC) - timedelta(days=days)
            assistant._save(found, touch=False)

    background.call(backdate())
    return conversation_id, file_id


def test_the_leader_removes_chat_files_past_each_tenants_retention(
    server: Server, make_worker: Callable[..., Background]
) -> None:
    """API-066: on a server the worker enforces the assistant file retention by itself, per tenant, without anyone
    running the input clean-up; a tenant without a retention period, and a suspended tenant, keep their files."""
    alice = server.member("acme", "alice", "owner")
    bob = server.member("globex", "bob", "owner")
    carol = server.member("initech", "carol", "owner")
    leader, other = make_worker(), make_worker()
    acme = _old_chat_file(alice, leader, "acme", 5)
    globex = _old_chat_file(bob, leader, "globex", 5)
    initech = _old_chat_file(carol, leader, "initech", 5)
    for browser in (alice, carol):
        assert browser.put("/api/assistant/files/retention", json={"retention_days": 2}).status_code == 200
    server.identity.set_tenant_status("initech", "suspended")

    assert leader.call(leader.worker.fire_schedules()) == []  # takes the scheduler lock
    assert other.call(other.worker.expire_chat_files()) == {}  # only the leader removes them
    assert leader.call(leader.worker.expire_chat_files()) == {"acme": 1}
    assert leader.call(leader.worker.expire_chat_files()) == {}  # nothing left to remove

    def files(browser: TestClient, conversation_id: str) -> list[str]:
        found = browser.get(f"/api/assistant/conversations/{conversation_id}")
        assert found.status_code == 200, found.text
        return [item["id"] for item in found.json()["files"]]

    assert files(alice, acme[0]) == []
    assert alice.get(f"/api/inputs/{acme[1]}").status_code == 404  # the bytes went too
    assert files(bob, globex[0]) == [globex[1]]  # no retention period: kept
    server.identity.set_tenant_status("initech", "active")
    assert files(carol, initech[0]) == [initech[1]]  # suspended while the pass ran: kept until the next pass
    assert leader.call(leader.worker.expire_chat_files()) == {"initech": 1}


def test_a_failing_tenant_does_not_stop_the_chat_file_retention_of_others(
    server: Server, make_worker: Callable[..., Background], monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = server.member("acme", "alice", "owner")
    bob = server.member("globex", "bob", "owner")
    leader = make_worker()
    _old_chat_file(alice, leader, "acme", 5)
    _old_chat_file(bob, leader, "globex", 5)
    for browser in (alice, bob):
        assert browser.put("/api/assistant/files/retention", json={"retention_days": 1}).status_code == 200
    broken = leader.worker.scheduling.get("acme").assistant

    def fail(**_kwargs: Any) -> int:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(broken, "expire_files", fail)
    assert leader.call(leader.worker.expire_chat_files()) == {"globex": 1}


# ---- suspended tenants ----------------------------------------------------------------------------------


def test_a_worker_does_not_start_a_run_of_a_suspended_tenant(
    server: Server, make_worker: Callable[..., Background], connect: Callable[[], PostgresDatabase]
) -> None:
    alice = server.member("acme", "alice", "owner")
    run_id = _start(alice)
    server.identity.set_tenant_status("acme", "suspended")
    called: list[bool] = []

    def must_not_run(_request: dict[str, Any]) -> Any:
        called.append(True)
        return text("must not run")

    worker = make_worker({"ALPHA": [must_not_run]}, lease=timedelta(milliseconds=200))
    job = worker.call(worker.worker.runner.run_once())
    assert job is not None and job.ref == run_id and not called  # no model call
    stored = JobQueue(connect()).database._query("SELECT status, error FROM nodestro_jobs WHERE id = %s", (job.id,))
    assert stored[0][0] == "failed" and stored[0][1].startswith("identity.tenant_suspended")
    # Resumed, the orphan sweep marks the run interrupted: a person can continue it, nothing ran twice.
    server.identity.set_tenant_status("acme", "active")
    time.sleep(0.5)
    worker.call(asyncio.to_thread(worker.worker.reap))
    run = alice.get(f"/api/runs/{run_id}").json()
    assert run["error_code"] == "run.interrupted" and not called


def test_a_worker_stops_the_runs_of_a_tenant_suspended_while_they_execute(
    server: Server, make_worker: Callable[..., Background], connect: Callable[[], PostgresDatabase]
) -> None:
    release, started = threading.Event(), threading.Event()
    alice = server.member("acme", "alice", "owner")
    run_id = _start(alice)
    worker = make_worker({"ALPHA": [_blocking(release, started)]})
    executing = worker.submit(worker.worker.runner.run_once())
    try:
        assert started.wait(15)
        assert worker.call(worker.worker.stop_suspended()) == []  # active: nothing happens
        server.identity.set_tenant_status("acme", "suspended")
        assert worker.call(worker.worker.stop_suspended()) == [run_id]
    finally:
        release.set()
    job = executing.result(30)
    rows = connect()._query("SELECT status, error FROM nodestro_jobs WHERE id = %s", (job.id,))
    assert rows[0][0] == "failed" and "fence_lost" in rows[0][1]
    server.identity.set_tenant_status("acme", "active")
    assert alice.get(f"/api/runs/{run_id}").json()["status"] != "succeeded"  # its outcome was not stored


def test_a_suspended_tenants_schedules_do_not_fire(server: Server, make_worker: Callable[..., Background]) -> None:
    alice = server.member("acme", "alice", "owner")
    slot = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
    _due_schedule(server, alice, slot)
    worker = make_worker()
    context = worker.call(_scheduling(worker, "acme"))
    context.schedules.clock = lambda: slot + timedelta(minutes=1)
    server.identity.set_tenant_status("acme", "suspended")
    assert worker.call(worker.worker.fire_schedules()) == []
    server.identity.set_tenant_status("acme", "active")
    assert len(worker.call(worker.worker.fire_schedules())) == 1


# ---- the Docker sandbox's proof ---------------------------------------------------------------------------------


class CountingDockerMonitor(StaticDockerMonitor):
    """The process's one Docker monitor in a test: counts status reads (no thread, no docker CLI)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def status(self, *, refresh: bool = False, wait: float = 0.0) -> Any:
        self.reads += 1
        return super().status(refresh=refresh, wait=wait)


def test_a_worker_checks_docker_from_its_start_and_stops_watching_with_stop(
    server: Server,
    make_worker: Callable[..., Background],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from nodestro_runtime import docker_status

    monitor = CountingDockerMonitor()  # "Docker missing": every tool run is refused
    monkeypatch.setattr(docker_status, "monitor_for", lambda *args, **kwargs: monitor)
    caplog.set_level(logging.INFO, logger="nodestro_api.worker")
    server.member("acme", "alice", "owner")
    worker = make_worker(docker_every=0.05)
    stop = asyncio.Event()
    serving = worker.submit(worker.worker.serve(stop))

    async def names() -> set[str]:
        return {task.get_name() for task in asyncio.all_tasks() if not task.done()}

    try:
        # No job was queued: the tenant's context (and the monitor's check) opened at the start, the watch reads it.
        deadline = time.monotonic() + 10
        while monitor.reads < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert monitor.reads >= 3
        assert "worker-docker" in worker.call(names())
        assert worker.worker.executing.opened() == []  # no job ran
    finally:
        worker.loop.call_soon_threadsafe(stop.set)
    serving.result(30)
    assert "worker-docker" not in worker.call(names())
    reads = monitor.reads
    time.sleep(0.2)
    assert monitor.reads == reads  # the watch stopped with ``stop``
    logged = [r.getMessage() for r in caplog.records if r.name == "nodestro_api.worker" and "Docker" in r.getMessage()]
    assert logged == [
        "Docker sandbox unavailable (docker-missing): Docker is not installed; "
        "tool code is refused until a check passes"
    ]


def test_a_batch_on_the_server_finishes_when_the_worker_ran_its_runs(
    server: Server, make_worker: Callable[..., Background]
) -> None:
    """The API process starts a batch, a worker executes its runs: the batch learns from the stored runs that they
    ended (the API's own run listeners never hear of a run another process executed)."""
    alice = server.member("acme", "alice", "owner")
    workflow = alice.post("/api/workflows", json=FLOW).json()["id"]
    rows = [{"input": "one"}, {"input": "two"}]
    created = alice.post("/api/batches", json={"workflow_id": workflow, "rows": rows, "concurrency": 2})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    worker = make_worker({"ALPHA": [text("first"), text("second")]})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if worker.call(worker.worker.runner.run_once()) is None:
            batch = alice.get(f"/api/batches/{batch_id}").json()
            if all(item["status"] != "pending" for item in batch["items"]):
                break
        time.sleep(0.05)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        batch = alice.get(f"/api/batches/{batch_id}").json()
        if batch["status"] != "running":
            break
        time.sleep(0.1)
    assert batch["status"] == "finished", batch
    assert [item["status"] for item in batch["items"]] == ["succeeded", "succeeded"]


def test_a_test_series_on_the_server_finishes_when_the_worker_ran_its_runs(
    server: Server, make_worker: Callable[..., Background]
) -> None:
    alice = server.member("acme", "alice", "owner")
    workflow = alice.post("/api/workflows", json=FLOW).json()["id"]
    suite = alice.post("/api/benchmarks", json={"workflow_id": workflow, "cases": [{"name": "One", "input": "go"}]})
    assert suite.status_code == 201, suite.text
    started = alice.post(f"/api/benchmarks/{suite.json()['id']}/runs", json={"concurrency": 1})
    assert started.status_code == 201, started.text
    worker = make_worker({"ALPHA": [text("done")]})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and worker.call(worker.worker.runner.run_once()) is None:
        time.sleep(0.05)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        series = alice.get(f"/api/benchmark-runs/{started.json()['id']}").json()
        if series["status"] not in ("running", "evaluating"):
            break
        time.sleep(0.1)
    assert series["status"] == "finished", series
    assert series["results"][0]["run_status"] == "succeeded"
