"""Crashes and several processes on real processes (backend audit C-09, C-15, C-16; API-022).

Each test starts ``tests/crash_child.py`` as its own Python process and ends it with SIGKILL in the middle of its work,
as a crashed or killed API or worker process would end; the test process plays the other processes of the
installation (its own database connections and contexts). Everything except the desktop case needs PostgreSQL
(``NODESTRO_TEST_POSTGRES``).

- C-16: a workflow delete killed inside its store transaction leaves the workflow complete, with its versions and
  schedules (SQLite on the desktop, PostgreSQL on a server), and the data is usable at once by the next process.
- C-15: a test series whose driving API process was killed is marked interrupted (with its batch) by another process
  once the lease ran out, never while the driver is alive; its runs are interrupted by the reaper's orphan sweep, and
  the batch can be continued.
- C-09: two worker processes tick at the same time: only the leader fires, a schedule deleted or switched off by
  another process while the leader starts a run is not brought back, and a leader killed while it starts a run does
  not let the next leader fire that slot again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from nodestro_runtime.schemas import Run, WorkflowIn, now
import postgres_support
import pytest

from nodestro_api.benchmarks import RUNS as BENCHMARK_RUNS
from nodestro_api.benchmarks import BenchmarkRun, SuiteIn, TestCase
from nodestro_api.context import AppConfig, Nodestro
from nodestro_api.schedules import COLLECTION as SCHEDULES
from nodestro_api.schedules import Schedule, ScheduleIn

from conftest import ScriptedClient, text

CHILD = Path(__file__).with_name("crash_child.py")
DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")

FLOW = {
    "name": "Flow",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "ALPHA"}},
        {"id": "out", "type": "output"},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
}

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL")


class Child:
    """``crash_child.py`` in a process of its own, with its output read line by line."""

    def __init__(self, *args: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(CHILD), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.errors: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.errors.extend(self.process.stderr or []), daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout or []:
            self.lines.put(line.strip())

    def expect(self, prefix: str, timeout: float = 60) -> str:
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"no {prefix!r} from the child; stderr: {''.join(self.errors)[-3000:]}") from None
            if line.startswith(prefix):
                return line

    def kill(self) -> None:
        self.process.send_signal(signal.SIGKILL)
        self.wait(-signal.SIGKILL)

    def wait(self, code: int, timeout: float = 60) -> None:
        assert self.process.wait(timeout) == code, "".join(self.errors)[-3000:]

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(10)


@pytest.fixture
def children() -> Iterator[list[Child]]:
    started: list[Child] = []
    yield started
    for child in started:
        child.close()


def _workflow_with_history(ctx: Nodestro) -> tuple[str, str]:
    """A workflow with two versions and a schedule; returns the workflow and schedule ids."""
    workflow = ctx.save_workflow(WorkflowIn(**FLOW))
    ctx.versions.record(workflow, description="first")
    workflow.name = "Second version"
    workflow = ctx.save_workflow(workflow)
    ctx.versions.record(workflow, description="second")
    schedule = ctx.schedules.create(ScheduleIn(workflow_id=workflow.id))
    return workflow.id, schedule.id


def _complete(ctx: Nodestro, workflow_id: str, schedule_id: str) -> None:
    assert ctx.store.get_raw("workflows", workflow_id) is not None
    assert len(ctx.versions.all(workflow_id)) == 2
    assert ctx.store.get_raw(SCHEDULES, schedule_id) is not None


# ---- C-16 ----------------------------------------------------------------------------------------------------


def test_a_desktop_killed_while_it_deletes_a_workflow_keeps_the_whole_workflow(
    tmp_path: Path, children: list[Child]
) -> None:
    from nodestro_api import api

    data = tmp_path / "data"
    ctx = Nodestro(AppConfig(data_dir=data), integrations=[])
    workflow_id, schedule_id = _workflow_with_history(ctx)
    ctx.close()  # one process per data directory: the next one is the child

    child = Child("delete-desktop", str(data), workflow_id)
    children.append(child)
    child.expect("crashing")
    child.wait(-signal.SIGKILL)

    reopened = Nodestro(AppConfig(data_dir=data), integrations=[])  # the killed process's lock is gone with it
    try:
        _complete(reopened, workflow_id, schedule_id)
        api.delete_workflow(workflow_id, reopened)  # and the delete works for the next process
        assert reopened.store.get_raw("workflows", workflow_id) is None
        assert reopened.versions.all(workflow_id) == []
        assert reopened.store.get_raw(SCHEDULES, schedule_id) is None
    finally:
        reopened.close()


@pytest.fixture
def pg_schema() -> Iterator[str]:
    postgres_support.require_postgres()
    psycopg = pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import PostgresDatabase
    from psycopg import sql

    name = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    database = PostgresDatabase(DSN, schema=name)
    database.migrate()
    database.close()
    yield name
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def server_files(pg_schema: str, tmp_path: Path) -> Iterator[Any]:
    """This process as one API process of the server (its own connection)."""
    from nodestro_runtime.storage_postgres import PostgresDatabase

    from nodestro_api.server_app import TenantFiles

    database = PostgresDatabase(DSN, schema=pg_schema)
    files = TenantFiles(
        database=database,
        data_root=tmp_path / "server",
        integrations=[],
        client_factory=lambda _provider: ScriptedClient({"ALPHA": [text("done")] * 20}),
    )
    opened: list[Any] = []
    original = files.open

    def open_tenant(tenant: str) -> Any:
        opened.append(original(tenant))
        return opened[-1]

    files.open = open_tenant  # type: ignore[method-assign]
    yield files
    for ctx in opened:
        ctx.close()
    database.close()


def test_a_server_process_killed_while_it_deletes_a_workflow_keeps_the_whole_workflow(
    pg_schema: str, server_files: Any, tmp_path: Path, children: list[Child]
) -> None:
    from nodestro_api import api

    ctx = server_files.open("acme")
    workflow_id, schedule_id = _workflow_with_history(ctx)
    child = Child("delete-server", pg_schema, str(tmp_path / "server"), "acme", workflow_id)
    children.append(child)
    child.expect("crashing")
    child.wait(-signal.SIGKILL)

    # PostgreSQL rolled the open transaction back when the connection ended: nothing of the delete is visible, and
    # no row lock of the killed process holds this process up.
    _complete(ctx, workflow_id, schedule_id)
    started = time.monotonic()
    api.delete_workflow(workflow_id, ctx)
    assert time.monotonic() - started < 10
    assert ctx.store.get_raw("workflows", workflow_id) is None and ctx.versions.all(workflow_id) == []


# ---- C-15 ----------------------------------------------------------------------------------------------------


def test_a_test_series_whose_api_process_was_killed_is_interrupted_and_can_be_continued(
    pg_schema: str, server_files: Any, tmp_path: Path, children: list[Child]
) -> None:
    from nodestro_runtime.jobs import JobQueue, reap

    ctx = server_files.open("acme")
    workflow = ctx.save_workflow(WorkflowIn(**FLOW))
    suite = ctx.benchmarks.save_suite(
        SuiteIn(name="S", workflow_id=workflow.id, cases=[TestCase(input="one"), TestCase(input="two")])
    )
    child = Child("test-series", pg_schema, str(tmp_path / "server"), "acme", suite.id)
    children.append(child)
    bench_id = child.expect("started ").split()[1]
    child.expect("model called")

    # While its process is alive, other processes leave the test series alone (its lease is renewed).
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        assert ctx.benchmarks.run(bench_id).status == "running"
        time.sleep(0.1)
    batch_id = ctx.benchmarks.run(bench_id).batch_id
    assert batch_id is not None and ctx.batches.get(batch_id).status == "running"

    child.kill()
    deadline = time.monotonic() + 15
    while ctx.benchmarks.run(bench_id).status == "running":
        assert time.monotonic() < deadline, "the test series was not interrupted after its lease"
        time.sleep(0.1)
    bench = ctx.store.get(BENCHMARK_RUNS, bench_id, BenchmarkRun)
    assert (bench.status, bench.stop_code, bench.owner) == ("interrupted", "benchmark.interrupted", None)
    batch = ctx.batches.get(batch_id)
    assert (batch.status, batch.stop_code) == ("interrupted", "batch.interrupted")

    # The run the killed process executed is interrupted by the reaper's orphan sweep (any worker).
    started = [item.run_id for item in batch.items if item.run_id]
    assert started, "the killed process had started a run"
    reap(JobQueue(server_files.database), server_files.open, orphans_after=timedelta(0))
    for run_id in started:
        run = ctx.runs.get(run_id)
        assert run is not None and (run.status, run.error_code) == ("failed", "run.interrupted")

    # A person continues the batch in this process: it finishes with the remaining cases.
    async def resume() -> Any:
        ctx.batches.resume(batch_id)
        deadline = time.monotonic() + 30
        while ctx.batches.get(batch_id).status == "running":
            assert time.monotonic() < deadline, "the continued batch did not finish"
            await asyncio.sleep(0.05)
        return ctx.batches.get(batch_id)

    finished = asyncio.run(resume())
    assert finished.status == "finished", finished
    assert all(item.status == "succeeded" for item in finished.items), [item.status for item in finished.items]


# ---- C-09 ----------------------------------------------------------------------------------------------------


def _due(ctx: Nodestro, workflow_id: str, name: str) -> Schedule:
    schedule = Schedule(
        name=name, workflow_id=workflow_id, kind="daily", at="08:00", enabled=True,
        next_run_at=now() - timedelta(seconds=30),
    )  # fmt: skip
    ctx.store.put(SCHEDULES, schedule.id, schedule)
    return schedule


def _fired(line: str) -> tuple[list[str], bool]:
    _, listed, leader = line.split(" ", 2)
    return json.loads(listed), leader == "leader=True"


def test_two_worker_processes_fire_each_slot_once_and_respect_concurrent_edits(
    pg_schema: str, server_files: Any, tmp_path: Path, children: list[Child]
) -> None:
    from nodestro_runtime.identity_store import IdentityStore
    from nodestro_runtime.jobs import RUN_JOB, JobQueue
    from nodestro_runtime.schemas import SettingsIn

    IdentityStore(server_files.database).create_tenant("acme", "Acme")
    ctx = server_files.open("acme")
    ctx.update_settings(SettingsIn(default_budget_usd=1.0))  # scheduled runs need a budget limit
    workflow = ctx.save_workflow(WorkflowIn(**FLOW))
    _first, second, third = (_due(ctx, workflow.id, name) for name in ("first", "second", "third"))
    gate = tmp_path / "gate"
    data_root = str(tmp_path / "server")

    leader = Child("tick", pg_schema, data_root, str(gate))
    children.append(leader)
    leader.expect("launching")  # the leader starts the first schedule's run and waits at the gate
    # A second worker process ticks meanwhile: it is not the leader and fires nothing.
    follower = Child("tick", pg_schema, data_root, str(gate))
    children.append(follower)
    assert _fired(follower.expect("fired ")) == ([], False)
    follower.wait(0)
    # Another process deletes one schedule and switches one off while the leader starts a run.
    ctx.schedules.delete(second.id)
    ctx.schedules.disable(third.id)
    gate.touch()
    fired, was_leader = _fired(leader.expect("fired "))
    leader.wait(0)
    assert was_leader and len(fired) == 1, [(item.name, item.last_error_code) for item in ctx.schedules.list()]
    stored = {item.name: item for item in ctx.schedules.list()}
    assert set(stored) == {"first", "third"}  # the deleted one is not brought back
    assert stored["third"].enabled is False and stored["third"].runs == 0  # nor the disabled one switched on
    assert stored["first"].runs == 1 and stored["first"].last_run_id == fired[0]
    queue = JobQueue(server_files.database)
    assert queue.active("acme", RUN_JOB, fired[0]) is not None  # queued for a worker

    # A leader killed while it starts a run: the slot is taken, the next leader does not fire it again.
    gate.unlink()
    fourth = _due(ctx, workflow.id, "fourth")
    killed = Child("tick", pg_schema, data_root, str(gate))
    children.append(killed)
    killed.expect("launching")
    killed.kill()
    gate.touch()
    taken = ctx.schedules.get(fourth.id)
    assert taken.next_run_at is not None and taken.next_run_at > now()  # advanced before the run started
    # Even a schedule that still names the slot (as if the advance were lost) is not fired again.
    taken.next_run_at = fourth.next_run_at
    ctx.store.put(SCHEDULES, taken.id, taken)
    deadline = time.monotonic() + 20
    while True:  # the killed leader's session lock goes with its connection
        successor = Child("tick", pg_schema, data_root, str(gate))
        children.append(successor)
        fired, was_leader = _fired(successor.expect("fired "))
        successor.wait(0)
        if was_leader:
            break
        assert time.monotonic() < deadline, "no new leader"
        time.sleep(0.2)
    assert fired == []
    runs = [item for item in ctx.store.list("runs", Run) if item.trigger_ref == fourth.id]
    assert runs == []  # the slot never started a run: a crash during the start never runs it twice
