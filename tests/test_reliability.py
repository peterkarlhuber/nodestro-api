"""Data integrity under volume and concurrency (backend audit, stream C).

- Lists and reports see every stored object, not a window of the newest ones (C-04, C-15).
- Opening a server tenant never marks another process's batches or test series as interrupted (C-07).
- The scheduler never brings back a deleted schedule or switches a disabled one on (C-09).
- A damaged workflow document does not hide the other workflows (C-10).
- A backup with any damaged document is refused at inspection (C-13).
- A workflow is deleted as one step, not while it runs, and a racing save cannot bring it back (C-16).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
import io
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from typing import Any
import uuid
import zipfile

from fastapi.testclient import TestClient
from nodestro_runtime.schemas import Run, Usage, Workflow, now
import pytest

from nodestro_api.batches import COLLECTION as BATCHES
from nodestro_api.batches import Batch, estimate_cost
from nodestro_api.benchmarks import RUNS as BENCHMARK_RUNS
from nodestro_api.benchmarks import BenchmarkRun, Benchmarks
from nodestro_api.schedules import COLLECTION as SCHEDULES
from nodestro_api.schedules import Schedule

FLOW = {
    "name": "Flow",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "ALPHA"}},
        {"id": "out", "type": "output"},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
}


def _ctx(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


def _run(run_id: str, workflow_id: str, **fields: Any) -> Run:
    fields.setdefault("status", "succeeded")
    fields.setdefault("usage", Usage(requests=1, total_tokens=10, estimated_cost_usd=0.01))
    return Run(
        id=run_id,
        workflow_id=workflow_id,
        workflow_name=f"Flow {workflow_id}",
        workflow=Workflow(id=workflow_id, name=f"Flow {workflow_id}"),
        **fields,
    )


def _store_runs(ctx: Any, runs: list[Run]) -> None:
    with ctx.store.transaction():
        for run in runs:
            ctx.store.put("runs", run.id, run)


def _old_then_new(ctx: Any, old: list[Run], new: list[Run]) -> None:
    """``old`` is stored before ``new``; on equal update times the store lists ``run_0…`` ids before ``run_z…``."""
    _store_runs(ctx, old)
    time.sleep(0.01)
    _store_runs(ctx, new)


# ---- C-04: run list and usage report ------------------------------------------------------------------


def test_run_list_finds_runs_of_a_workflow_beyond_the_newest_2000(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    old = [_run(f"run_z{i:03d}", "wf_old", trigger="schedule", trigger_ref="schedule_old") for i in range(5)]
    new = [_run(f"run_0{i:05d}", "wf_new") for i in range(2001)]
    _old_then_new(ctx, old, new)

    listed = client.get("/api/runs", params={"workflow_id": "wf_old"}).json()
    assert sorted(run["id"] for run in listed) == [run.id for run in old]
    by_ref = client.get("/api/runs", params={"trigger_ref": "schedule_old"}).json()
    assert len(by_ref) == 5
    # The default list is unchanged: the newest top-level runs, newest first, at most ``limit``.
    default = client.get("/api/runs", params={"limit": 3}).json()
    assert len(default) == 3 and all(run["workflow_id"] == "wf_new" for run in default)


def test_run_list_shows_top_level_runs_behind_many_sub_workflow_runs(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    parents = [_run(f"run_z{i:03d}", "wf_parent") for i in range(3)]
    children = [_run(f"run_0{i:05d}", "wf_child", parent_run_id="run_z000") for i in range(2001)]
    _old_then_new(ctx, parents, children)

    listed = client.get("/api/runs").json()
    assert sorted(run["id"] for run in listed) == [run.id for run in parents]


def test_usage_report_counts_every_run_of_the_period(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    old = [_run(f"run_z{i:03d}", "wf_old") for i in range(5)]
    new = [_run(f"run_0{i:05d}", "wf_new") for i in range(2001)]
    # Neither counted: imported traces, sub-workflow runs and runs before the period.
    ignored = [
        _run("run_0imported", "wf_new", imported_at=now()),
        _run("run_0child", "wf_new", parent_run_id="run_000000"),
        _run("run_0ancient", "wf_new", created_at=now() - timedelta(days=40)),
    ]
    _old_then_new(ctx, old, new + ignored)

    report = client.get("/api/usage", params={"days": 30}).json()
    assert report["total"]["runs"] == 2006
    workflows = {bucket["key"]: bucket["runs"] for bucket in report["by_workflow"]}
    assert workflows == {"wf_new": 2001, "wf_old": 5}
    assert report["total"]["estimated_cost_usd"] == pytest.approx(20.06)


# ---- C-15: batches, test series and the cost estimate ---------------------------------------------------


def test_batches_and_test_series_beyond_the_newest_500(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    oldest = Batch(id="batch_z_old", workflow_id="wf_old", workflow_name="Old", status="finished")
    old_bench = BenchmarkRun(id="bench_z_old", suite_id="suite_old", suite_name="Old", workflow_id="wf_old")
    old_bench.status = "finished"
    ctx.store.put(BATCHES, oldest.id, oldest)
    ctx.store.put(BENCHMARK_RUNS, old_bench.id, old_bench)
    time.sleep(0.01)
    with ctx.store.transaction():
        for i in range(501):
            batch = Batch(id=f"batch_0{i:04d}", workflow_id="wf_new", workflow_name="New", status="finished")
            ctx.store.put(BATCHES, batch.id, batch)
            bench = BenchmarkRun(id=f"bench_0{i:04d}", suite_id="suite_new", suite_name="New", workflow_id="wf_new")
            bench.status = "finished"
            ctx.store.put(BENCHMARK_RUNS, bench.id, bench)

    listed = client.get("/api/batches").json()
    assert len(listed) == 502 and "batch_z_old" in {item["id"] for item in listed}
    assert [run.id for run in ctx.benchmarks.runs("suite_old")] == ["bench_z_old"]


def test_cost_estimate_uses_runs_older_than_the_newest_500(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    old = [_run(f"run_z{i:03d}", "wf_old", usage=Usage(requests=1, estimated_cost_usd=0.5)) for i in range(3)]
    new = [_run(f"run_0{i:05d}", "wf_new") for i in range(501)]
    _old_then_new(ctx, old, new)

    estimate = estimate_cost(ctx, "wf_old", 10)
    assert estimate.based_on == 3
    assert estimate.per_run_usd == pytest.approx(0.5) and estimate.total_usd == pytest.approx(5.0)


def test_desktop_start_recovers_running_test_series_beyond_the_newest_500(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    stuck = BenchmarkRun(id="bench_z_stuck", suite_id="s", suite_name="S", workflow_id="wf")
    stuck.status = "running"
    stuck_batch = Batch(id="batch_z_stuck", workflow_id="wf", workflow_name="W", status="running")
    ctx.store.put(BENCHMARK_RUNS, stuck.id, stuck)
    ctx.store.put(BATCHES, stuck_batch.id, stuck_batch)
    time.sleep(0.01)
    with ctx.store.transaction():
        for i in range(501):
            done = BenchmarkRun(id=f"bench_0{i:04d}", suite_id="s", suite_name="S", workflow_id="wf")
            done.status = "finished"
            ctx.store.put(BENCHMARK_RUNS, done.id, done)
            batch = Batch(id=f"batch_0{i:04d}", workflow_id="wf", workflow_name="W", status="finished")
            ctx.store.put(BATCHES, batch.id, batch)

    Benchmarks(ctx)  # what the desktop does when it starts
    ctx.batches._recover()
    assert ctx.store.get(BENCHMARK_RUNS, stuck.id, BenchmarkRun).status == "interrupted"
    assert ctx.store.get(BATCHES, stuck_batch.id, Batch).status == "interrupted"


# ---- C-07: opening a server tenant ----------------------------------------------------------------------

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


@pytest.fixture
def pg_schema() -> Iterator[str]:
    if not DSN:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
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


def test_opening_a_server_tenant_leaves_running_batches_of_other_processes_alone(
    pg_schema: str, tmp_path: Path
) -> None:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    from nodestro_api.server_app import TenantFiles

    databases = [PostgresDatabase(DSN, schema=pg_schema) for _ in range(2)]
    try:
        api_process = TenantFiles(database=databases[0], data_root=tmp_path, integrations=[])
        ctx = api_process.open("acme")
        batch = Batch(workflow_id="wf_x", workflow_name="X", status="running")
        bench = BenchmarkRun(suite_id="s", suite_name="S", workflow_id="wf_x")
        bench.status = "running"
        ctx.store.put(BATCHES, batch.id, batch)
        ctx.store.put(BENCHMARK_RUNS, bench.id, bench)

        # A worker or a second API process opens the same tenant (its first job, its schedule tick).
        other = TenantFiles(database=databases[1], data_root=tmp_path, integrations=[]).open("acme")
        assert ctx.store.get(BATCHES, batch.id, Batch).status == "running"
        assert ctx.store.get(BENCHMARK_RUNS, bench.id, BenchmarkRun).status == "running"
        assert [item.status for item in ctx.batches.list()] == ["running"]
        other.close()
        ctx.close()
    finally:
        for database in databases:
            database.close()


# ---- C-09: the scheduler and concurrent edits ------------------------------------------------------------


def test_schedule_tick_never_brings_back_deleted_or_disabled_schedules(
    make_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app()
    ctx = _ctx(client)
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    due = now() - timedelta(seconds=30)
    names = ("first", "second", "third", "fourth")
    schedules = {
        name: Schedule(
            name=name,
            workflow_id=workflow_id,
            input="go",
            enabled=True,
            next_run_at=due,
            created_at=now() - timedelta(hours=10 - index),
        )
        for index, name in enumerate(names)
    }
    for schedule in schedules.values():
        ctx.store.put(SCHEDULES, schedule.id, schedule)
    started: list[str] = []

    async def launch(_ctx: Any, workflow: Any, body: Any, **params: Any) -> Any:
        ref = params["trigger_ref"]
        started.append(ref)
        if ref == schedules["first"].id:
            # Starting a run takes a moment; meanwhile the user deletes one schedule and switches another off.
            ctx.schedules.delete(schedules["second"].id)
            ctx.schedules.disable(schedules["third"].id)
        if ref == schedules["fourth"].id:
            ctx.schedules.delete(ref)  # deleted while its own run starts
        return SimpleNamespace(id=f"run_{len(started)}")

    monkeypatch.setattr("nodestro_api.schedules.launch", launch)
    fired = client.portal.call(ctx.schedules.tick)  # type: ignore[attr-defined]

    assert started == [schedules["first"].id, schedules["fourth"].id]
    assert fired == ["run_1", "run_2"]
    stored = {item.name: item for item in ctx.schedules.list()}
    assert set(stored) == {"first", "third"}
    assert stored["third"].enabled is False and stored["third"].next_run_at is None and stored["third"].runs == 0
    assert stored["first"].runs == 1 and stored["first"].last_run_id == "run_1"
    assert stored["first"].next_run_at is not None and stored["first"].next_run_at > now()


def test_schedule_tick_skips_a_schedule_changed_between_list_and_save(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    schedule = Schedule(name="s", workflow_id=workflow_id, enabled=True, next_run_at=now() - timedelta(seconds=30))
    ctx.store.put(SCHEDULES, schedule.id, schedule)
    listed = ctx.schedules.list()[0]
    ctx.schedules.disable(schedule.id)
    # The tick listed it while it was on; the user switched it off before the tick reached it.
    assert client.portal.call(ctx.schedules._due, listed, now()) is None  # type: ignore[attr-defined]
    stored = ctx.schedules.get(schedule.id)
    assert stored.enabled is False and stored.next_run_at is None and stored.runs == 0


# ---- C-10: a damaged workflow document ---------------------------------------------------------------------


def test_a_damaged_workflow_is_skipped_by_the_list_and_the_search(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    good = client.post("/api/workflows", json={**FLOW, "name": "Invoice check"}).json()["id"]
    damaged = client.post("/api/workflows", json={**FLOW, "name": "Invoice broken"}).json()["id"]
    ctx.store.put_raw("workflows", damaged, {"id": damaged, "name": "Invoice broken", "nodes": "oops"})

    listed = client.get("/api/workflows")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [good]
    found = client.get("/api/search", params={"q": "invoice"})
    assert found.status_code == 200
    assert [item["id"] for item in found.json() if item["kind"] == "workflow"] == [good]


def test_a_workflow_with_cut_off_json_is_skipped(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    good = client.post("/api/workflows", json=FLOW).json()["id"]
    damaged = client.post("/api/workflows", json=FLOW).json()["id"]
    with ctx.store.transaction():
        ctx.store._connection.execute(
            "UPDATE nodestro_documents SET data = substr(data, 1, 50) WHERE collection = 'workflows' AND id = ?",
            (damaged,),
        )
    listed = client.get("/api/workflows")
    assert listed.status_code == 200 and [item["id"] for item in listed.json()] == [good]


# ---- C-13: backup inspection ---------------------------------------------------------------------------------


def _backup_with(client: TestClient, change: Callable[[sqlite3.Connection], None]) -> bytes:
    """A backup of ``client``'s data whose database was changed by ``change`` (manifest checksum fixed)."""
    import hashlib
    import json

    original = client.get("/api/backup").content
    source = zipfile.ZipFile(io.BytesIO(original))
    work = Path(client.app.state.nodestro.config.data_dir).parent / f"backup-{uuid.uuid4().hex}.db"  # type: ignore[attr-defined]
    work.write_bytes(source.read("nodestro.db"))
    connection = sqlite3.connect(work)
    change(connection)
    connection.commit()
    connection.close()
    data = work.read_bytes()
    manifest = json.loads(source.read("manifest.json"))
    manifest["files"]["nodestro.db"] = hashlib.sha256(data).hexdigest()
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for name in source.namelist():
            if name == "nodestro.db":
                archive.writestr(name, data)
            elif name == "manifest.json":
                archive.writestr(name, json.dumps(manifest))
            else:
                archive.writestr(name, source.read(name))
    return target.getvalue()


def _inspect(client: TestClient, backup: bytes) -> Any:
    return client.post("/api/backup/inspect", files={"file": ("backup.zip", backup, "application/zip")})


@pytest.mark.parametrize("collection", ["schedules", "workflow_versions", "batches"])
def test_backup_inspection_refuses_a_damaged_document_of_any_restored_collection(
    make_app: Any, collection: str
) -> None:
    client = make_app()
    ctx = _ctx(client)
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    created = client.post("/api/schedules", json={"workflow_id": workflow_id}).json()
    ctx.store.put(BATCHES, "batch_1", Batch(id="batch_1", workflow_id=workflow_id, workflow_name="F"))
    key = {"schedules": created["id"], "workflow_versions": f"{workflow_id}:000001", "batches": "batch_1"}[collection]
    assert ctx.store.get_raw(collection, key) is not None, "the document to damage exists"
    assert _inspect(client, _backup_with(client, lambda _c: None)).status_code == 200

    def damage(connection: sqlite3.Connection) -> None:
        connection.execute(
            "UPDATE nodestro_documents SET data = json_set(data, '$.created_at', 'not a time')"
            " WHERE collection = ? AND id = ?",
            (collection, key),
        )

    refused = _inspect(client, _backup_with(client, damage))
    assert refused.status_code == 400
    assert refused.json()["code"] == "backup.document_invalid"
    assert refused.json()["params"]["collection"] == collection


def test_backup_inspection_refuses_invalid_json_in_any_collection(make_app: Any) -> None:
    client = make_app()

    def damage(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO nodestro_documents (collection, id, data) VALUES ('some_cache', 'x', '{\"cut')")

    refused = _inspect(client, _backup_with(client, damage))
    assert refused.status_code == 400 and refused.json()["code"] == "backup.document_invalid"


# ---- C-16: deleting a workflow ---------------------------------------------------------------------------------


def test_a_workflow_is_not_deleted_while_its_runs_execute(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    ctx.store.put("runs", "run_live", _run("run_live", workflow_id, status="running"))

    refused = client.delete(f"/api/workflows/{workflow_id}")
    assert refused.status_code == 409
    assert refused.json()["code"] == "workflow.runs_active" and refused.json()["params"] == {"runs": 1}
    assert client.get(f"/api/workflows/{workflow_id}").status_code == 200

    ctx.store.put("runs", "run_live", _run("run_live", workflow_id, status="cancelled"))
    assert client.delete(f"/api/workflows/{workflow_id}").status_code == 204
    assert client.get(f"/api/workflows/{workflow_id}").status_code == 404


def test_a_save_racing_a_delete_does_not_bring_the_workflow_back(make_app: Any) -> None:
    from nodestro_runtime.schemas import WorkflowIn

    from nodestro_api import api

    client = make_app()
    ctx = _ctx(client)
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    # The save read the workflow; the delete lands before the save writes.
    current = api._workflow(ctx, workflow_id)
    assert client.delete(f"/api/workflows/{workflow_id}").status_code == 204
    monkey = pytest.MonkeyPatch()
    monkey.setattr(api, "_workflow", lambda _ctx, _id: current)
    try:
        with pytest.raises(KeyError):
            api.update_workflow(workflow_id, WorkflowIn(**{**FLOW, "name": "Edited"}), ctx, note="")
    finally:
        monkey.undo()
    assert ctx.store.get_raw("workflows", workflow_id) is None
    assert ctx.versions.all(workflow_id) == []
    assert client.put(f"/api/workflows/{workflow_id}", json=FLOW).status_code == 404


def test_workflow_delete_is_one_transaction(make_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_app()
    ctx = _ctx(client)
    workflow_id = client.post("/api/workflows", json=FLOW).json()["id"]
    client.put(f"/api/workflows/{workflow_id}", json={**FLOW, "name": "Second version"})
    schedule = client.post("/api/schedules", json={"workflow_id": workflow_id}).json()

    def crash(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("crash in the middle of the delete")

    monkeypatch.setattr(ctx.benchmarks, "suites", crash)  # after the workflow, its versions and its schedules
    with pytest.raises(RuntimeError):
        client.delete(f"/api/workflows/{workflow_id}")
    # Nothing was deleted: the workflow is complete, with its versions and schedule.
    assert ctx.store.get_raw("workflows", workflow_id) is not None
    assert len(ctx.versions.all(workflow_id)) == 2
    assert ctx.store.get_raw(SCHEDULES, schedule["id"]) is not None
