"""The usage report reads only the runs of its period (API-013).

``/api/usage`` pages through the store's creation index with a ``since`` filter (``list_page``) instead of reading
every stored run and dropping the old ones afterwards. The totals stay those of the full read (the C-04 test in
``test_reliability.py`` checks 2,006 runs over several pages); these tests prove that a run before the period never
leaves the store, on SQLite and, with ``NODESTRO_TEST_POSTGRES``, on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

from fastapi.testclient import TestClient
from nodestro_runtime.schemas import Run, Usage, now
from nodestro_runtime.storage import SQLiteStore
import postgres_support
import pytest
from test_reliability import _run

from nodestro_api import api

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


class Spy:
    """Hands every call on to the store and records the ids of the runs it returned."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.read: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def list(self, collection: str, *args: Any, **kwargs: Any) -> Any:
        assert collection != "runs", "the usage report must not read every run"
        return self.store.list(collection, *args, **kwargs)

    def list_page(self, collection: str, *args: Any, **kwargs: Any) -> Any:
        page, more = self.store.list_page(collection, *args, **kwargs)
        self.read += [item.id for item in page]
        return page, more


def _period(days_old: list[int], *, prefix: str) -> list[Run]:
    return [
        _run(f"run_{prefix}{index:04d}", f"wf_{prefix}", created_at=now() - timedelta(days=age, minutes=index))
        for index, age in enumerate(days_old)
    ]


def _store_all(store: Any, runs: list[Run]) -> None:
    with store.transaction():
        for run in runs:
            store.put("runs", run.id, run)


@pytest.fixture(params=["sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    if request.param == "sqlite":
        opened = SQLiteStore(tmp_path / "usage.db")
        yield opened
        opened.close()
        return
    postgres_support.require_postgres()
    pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import PostgresDatabase
    import psycopg
    from psycopg import sql

    schema = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    database = PostgresDatabase(DSN, schema=schema)
    database.migrate()
    yield database.store("tenant-a")
    database.close()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_runs_before_the_period_are_never_read(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_USAGE_PAGE", 7)  # several pages
    recent = _period([0, 1, 2, 5, 9, 12, 20, 25, 28, 29] * 2, prefix="new")
    old = _period([31, 40, 90, 200, 364] * 6, prefix="old")
    child = _run("run_child", "wf_new", parent_run_id=recent[0].id)
    _store_all(store, old + recent + [child])
    spy = Spy(store)

    counted = api._runs_since(SimpleNamespace(store=spy), (now() - timedelta(days=30)).timestamp())  # type: ignore[arg-type]

    assert sorted(run.id for run in counted) == sorted(run.id for run in recent)
    assert [run.created_at for run in counted] == sorted((run.created_at for run in counted), reverse=True)
    # Only the period's top-level runs left the store; no run before it, no sub-workflow run.
    assert sorted(spy.read) == sorted(run.id for run in recent)


def _read_everything(ctx: Any, cutoff: float) -> list[Any]:
    """The report's run read before API-013: every stored run, filtered afterwards (the reference for the totals)."""
    counted = [
        run
        for run in ctx.store.list("runs", api._UsageRun)
        if run.created_at.timestamp() >= cutoff and run.imported_at is None and run.parent_run_id is None
    ]
    return sorted(counted, key=lambda run: run.created_at, reverse=True)


def test_the_route_reads_only_the_period_and_keeps_its_totals(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app()
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    recent = _period([0, 3, 6], prefix="new")
    old = _period([8, 15, 60], prefix="old")
    _store_all(ctx.store, [*recent, *old, _run("run_imported", "wf_new", imported_at=now())])
    with monkeypatch.context() as patch:
        patch.setattr(api, "_runs_since", _read_everything)
        expected = {days: client.get("/api/usage", params={"days": days}).json() for days in (7, 30, 365)}
    spy = Spy(ctx.store)
    monkeypatch.setattr(ctx, "store", spy)

    week = client.get("/api/usage", params={"days": 7}).json()
    assert week["total"]["runs"] == 3
    assert week["total"]["estimated_cost_usd"] == pytest.approx(0.03)
    # The period's runs (an imported trace of the period too, which the report then leaves out), none before it.
    assert sorted(spy.read) == sorted([run.id for run in recent] + ["run_imported"])
    for days, report in expected.items():
        assert client.get("/api/usage", params={"days": days}).json() == report
    assert expected[30]["total"]["runs"] == 5 and expected[365]["total"]["runs"] == 6


def test_usage_is_counted_like_before(make_app: Callable[..., TestClient]) -> None:
    """The fields the lean run reads in summary form still give the same buckets (cost, models, sources)."""
    client = make_app()
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    runs = [
        _run("run_a", "wf_a", usage=Usage(requests=2, total_tokens=30, estimated_cost_usd=0.25)),
        _run("run_b", "wf_a", trigger="api", trigger_ref="pub_1", usage=Usage(requests=1, estimated_cost_usd=None)),
    ]
    _store_all(ctx.store, runs)
    report = client.get("/api/usage").json()
    assert report["total"]["runs"] == 2 and report["total"]["requests"] == 3
    assert report["total"]["unpriced"] == 1 and report["total"]["cost_state"] == "partial"
    sources = {bucket["key"]: bucket["runs"] for bucket in report["by_source"]}
    assert sources == {"workflow_run": 1, "api_run": 1}
