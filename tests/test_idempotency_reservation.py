"""An Idempotency-Key is reserved atomically in the store before its run starts (API-043).

Two concurrent retries with the same key, also on different API processes, never both start a run: the reservation
is the runtime's ``put_raw_if_absent`` (one ``INSERT ... ON CONFLICT DO NOTHING``), not a lock of one process. The
store tests run on SQLite and, with ``NODESTRO_TEST_POSTGRES``, on PostgreSQL; each concurrent caller uses its own
store object (its own connection), as separate processes would.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any
import uuid

from fastapi.testclient import TestClient
from nodestro_runtime.storage import SQLiteStore
import postgres_support
import pytest
from test_published_api import (
    PUBLISHED,
    auth,
    finish,
    published,  # noqa: F401  (fixture)
)

from nodestro_api.serving import ApiError, ServingCore

from conftest import text
from test_runs import agent_node, create

COLLECTION = "api_idempotency"
DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


# ---- the reservation on both stores -------------------------------------------------------------


@pytest.fixture(params=["sqlite", "postgres"])
def stores(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Callable[[], Any]]:
    """A factory of store objects on one database: every call is a new connection (another "process")."""
    opened: list[Any] = []
    if request.param == "sqlite":

        def sqlite() -> Any:
            store = SQLiteStore(tmp_path / "shared.db")
            opened.append(store)
            return store

        yield sqlite
    else:
        postgres_support.require_postgres()
        pytest.importorskip("psycopg")
        from nodestro_runtime.storage_postgres import PostgresDatabase
        import psycopg
        from psycopg import sql

        schema = f"nodestro_test_{uuid.uuid4().hex[:16]}"
        PostgresDatabase(DSN, schema=schema).migrate()

        def postgres() -> Any:
            database = PostgresDatabase(DSN, schema=schema)
            opened.append(database)
            return database.store("tenant-a")

        yield postgres
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
    for item in opened:
        item.close()


def core(store: Any) -> ServingCore:
    return ServingCore(SimpleNamespace(store=store))  # type: ignore[arg-type]


def nothing(_run_id: str) -> None:
    return None


def test_the_first_request_reserves_and_a_retry_replays(stores: Callable[[], Any]) -> None:
    serving = core(stores())
    claim = serving.reserve_idempotent_start("k1", "request-a", nothing)
    assert claim.replay is None and claim.stamp
    # While the first request starts its run, a retry neither starts another nor waits forever.
    with pytest.raises(ApiError) as busy:
        serving.reserve_idempotent_start("k1", "request-a", nothing)
    assert busy.value.status == 409 and busy.value.code == "idempotency.in_progress"
    assert busy.value.headers == {"Retry-After": "1"}
    serving.remember_idempotent_run("k1", "run_1", "request-a")
    runs = {"run_1": "the run"}
    replay = serving.reserve_idempotent_start("k1", "request-a", runs.get)
    assert replay.replay == "the run"
    with pytest.raises(ApiError) as conflict:
        serving.reserve_idempotent_start("k1", "request-b", runs.get)
    assert conflict.value.code == "idempotency.conflict"


def test_a_failed_start_releases_only_its_own_reservation(stores: Callable[[], Any]) -> None:
    store = stores()
    serving = core(store)
    claim = serving.reserve_idempotent_start("k1", "request-a", nothing)
    serving.release_idempotent_start(claim)
    assert store.get_raw(COLLECTION, "k1") is None
    again = serving.reserve_idempotent_start("k1", "request-a", nothing)
    assert again.replay is None
    # A stale claim (the key was taken over in between, or its run was recorded) deletes nothing.
    serving.release_idempotent_start(claim)
    assert store.get_raw(COLLECTION, "k1") is not None
    serving.remember_idempotent_run("k1", "run_1", "request-a")
    serving.release_idempotent_start(again)
    assert store.get_raw(COLLECTION, "k1")["run_id"] == "run_1"


def test_expired_abandoned_and_orphaned_keys_are_taken_over(stores: Callable[[], Any]) -> None:
    store = stores()
    serving = core(store)
    expired = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.put_raw(COLLECTION, "old", {"run_id": "run_old", "request": "other", "at": expired})
    assert serving.reserve_idempotent_start("old", "request-a", lambda _id: "exists").replay is None
    assert store.get_raw(COLLECTION, "old")["request"] == "request-a"
    # A reservation whose process died before it recorded a run.
    abandoned = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    store.put_raw(COLLECTION, "dead", {"run_id": "", "request": "request-a", "at": abandoned})
    assert serving.reserve_idempotent_start("dead", "request-a", nothing).replay is None
    # A key whose run no longer exists starts a new one.
    serving.remember_idempotent_run("gone", "run_deleted", "request-a")
    claim = serving.reserve_idempotent_start("gone", "request-a", nothing)
    assert claim.replay is None and store.get_raw(COLLECTION, "gone")["run_id"] == ""
    # An unreadable record is replaced, not trusted.
    store.put_raw(COLLECTION, "broken", {"run_id": "run_x", "request": "request-a", "at": "yesterday"})
    assert serving.reserve_idempotent_start("broken", "request-a", lambda _id: "exists").replay is None


def test_concurrent_reservations_on_separate_connections_start_one_run(stores: Callable[[], Any]) -> None:
    connections = [stores() for _ in range(8)]
    barrier = threading.Barrier(len(connections))

    def attempt(store: Any) -> str:
        barrier.wait()
        try:
            claim = core(store).reserve_idempotent_start("same-key", "request-a", nothing)
        except ApiError as error:
            return error.code
        return "reserved" if claim.replay is None else "replayed"

    with ThreadPoolExecutor(len(connections)) as pool:
        outcomes = list(pool.map(attempt, connections))
    assert outcomes.count("reserved") == 1, outcomes
    assert outcomes.count("idempotency.in_progress") == len(connections) - 1, outcomes


def test_concurrent_takeovers_of_an_expired_key_start_one_run(stores: Callable[[], Any]) -> None:
    connections = [stores() for _ in range(6)]
    expired = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    connections[0].put_raw(COLLECTION, "old", {"run_id": "run_old", "request": "request-a", "at": expired})
    barrier = threading.Barrier(len(connections))

    def attempt(store: Any) -> str:
        barrier.wait()
        try:
            claim = core(store).reserve_idempotent_start("old", "request-a", nothing)
        except ApiError as error:
            return error.code
        return "reserved" if claim.replay is None else "replayed"

    with ThreadPoolExecutor(len(connections)) as pool:
        outcomes = list(pool.map(attempt, connections))
    assert outcomes.count("reserved") == 1, outcomes
    assert set(outcomes) <= {"reserved", "idempotency.in_progress"}, outcomes


def test_a_store_error_while_reserving_starts_no_run(stores: Callable[[], Any]) -> None:
    store = stores()

    class Broken:
        def __getattr__(self, name: str) -> Any:
            return getattr(store, name)

        def put_raw_if_absent(self, *_args: Any) -> bool:
            raise OSError("disk full")

    with pytest.raises(OSError):
        core(Broken()).reserve_idempotent_start("k1", "request-a", nothing)
    assert store.get_raw(COLLECTION, "k1") is None


# ---- the routes ---------------------------------------------------------------------------------


def _only_key(store: Any) -> str:
    from test_published_api import _keys

    (key,) = _keys(store, COLLECTION)
    return key


def test_studio_start_while_another_process_starts_the_same_key(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("done")] * 5})
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    workflow_id = create(client, [agent_node("one", "ONE")], [])
    path = f"/api/workflows/{workflow_id}/runs"
    first = client.post(path, json={"input": "a"}, headers={"Idempotency-Key": "order-1"})
    assert first.status_code == 201
    # Another API process has reserved the same key and is starting its run: no second run starts here.
    key = _only_key(store)
    record = store.get_raw(COLLECTION, key)
    store.put_raw(COLLECTION, key, {**record, "run_id": "", "at": datetime.now(UTC).isoformat()})
    busy = client.post(path, json={"input": "a"}, headers={"Idempotency-Key": "order-1"})
    assert busy.status_code == 409 and busy.json()["code"] == "idempotency.in_progress"
    assert busy.headers["retry-after"] == "1"
    store.put_raw(COLLECTION, key, record)
    again = client.post(path, json={"input": "a"}, headers={"Idempotency-Key": "order-1"})
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert len(client.get("/api/runs", params={"workflow_id": workflow_id}).json()) == 1


def test_studio_start_that_fails_frees_the_key(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_api import api

    client = make_app({"ONE": [text("done")] * 5})
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    workflow_id = create(client, [agent_node("one", "ONE")], [])
    path = f"/api/workflows/{workflow_id}/runs"
    real = api.launch

    async def failing(*_args: Any, **_kwargs: Any) -> Any:
        raise api.ApiError(503, "model.unavailable", "No provider")

    monkeypatch.setattr(api, "launch", failing)
    failed = client.post(path, json={"input": "a"}, headers={"Idempotency-Key": "order-1"})
    assert failed.status_code == 503
    assert _keys_of(store) == []
    monkeypatch.setattr(api, "launch", real)
    retry = client.post(path, json={"input": "a"}, headers={"Idempotency-Key": "order-1"})
    assert retry.status_code == 201
    assert store.get_raw(COLLECTION, _only_key(store))["run_id"] == retry.json()["id"]


def _keys_of(store: Any) -> list[str]:
    from test_published_api import _keys

    return _keys(store, COLLECTION)


def test_published_start_that_hits_a_limit_frees_the_key(request: pytest.FixtureRequest) -> None:
    client, publication, token = request.getfixturevalue("published")()
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    slug = publication["slug"]
    serving = client.app.state.nodestro.serving  # type: ignore[attr-defined]
    original = serving.check_limits

    def refuse(_release: Any) -> None:
        raise ApiError(429, "limit.parallel", "Too many runs at once")

    serving.check_limits = refuse
    headers = {**auth(token), "Idempotency-Key": "order-9"}
    body = {"values": {"customer": "ACME"}}
    refused = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers)
    assert refused.status_code == 429
    assert _keys_of(store) == []
    serving.check_limits = original
    started = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers)
    assert started.status_code == 202, started.text
    # Another process holds a fresh reservation of the same key: 409, nothing starts.
    key = _only_key(store)
    record = store.get_raw(COLLECTION, key)
    store.put_raw(COLLECTION, key, {**record, "run_id": "", "at": datetime.now(UTC).isoformat()})
    busy = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers)
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "idempotency.in_progress"
    store.put_raw(COLLECTION, key, record)
    finish(client, slug, token, started.json()["run_id"])
    assert len(client.get(f"/api/publications/{publication['id']}/runs").json()) == 1
