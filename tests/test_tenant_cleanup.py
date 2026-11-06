"""Resource cleanup of idle tenant contexts on a server (API-001).

An API process opens one context per tenant. Each request holds its tenant's context until its whole answer was sent
(``TenantLeases``); a context that no request held for a while and that drives no background work in the process is
closed, and the next request opens it again. The PostgreSQL cases need ``NODESTRO_TEST_POSTGRES``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
import pytest
from test_identity import database, identity  # noqa: F401  (fixtures)

from nodestro_api import server as command
from nodestro_api.api import Ctx
from nodestro_api.server_app import DEFAULT_TENANT_IDLE_SECONDS, TenantComposition, tenant_busy
from nodestro_api.tenant_contexts import TenantContexts, TenantLeases

from conftest import text
from test_automation import answer, form_workflow, poll


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Context:
    """A stand-in tenant context: ``work`` is what it drives in the background."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant
        self.work: list[str] = []
        self.closed = False

    def background_work(self) -> list[str]:
        return list(self.work)


def _tenants(clock: Clock, **options: Any) -> tuple[TenantContexts, list[Context]]:
    made: list[Context] = []

    def factory(tenant: str) -> Context:
        made.append(Context(tenant))
        return made[-1]

    async def close(context: Context) -> None:
        context.closed = True

    options.setdefault("busy", tenant_busy)
    return TenantContexts(factory, close, clock=clock, **options), made


def _close_idle(tenants: TenantContexts, seconds: float) -> list[str]:
    return asyncio.run(tenants.close_idle(seconds))


def test_an_idle_context_is_closed_and_opened_again_on_the_next_use() -> None:
    clock = Clock()
    tenants, made = _tenants(clock)
    first = tenants.get("acme")
    tenants.get("globex")
    clock.now += 100
    tenants.get("globex")  # used recently
    clock.now += 1700
    assert _close_idle(tenants, 1800) == ["acme"]
    assert first.closed and [item for item, _ in tenants.items()] == ["globex"]
    again = tenants.get("acme")
    assert again is not first and not again.closed and len(made) == 3
    assert _close_idle(tenants, 0) == [] and _close_idle(tenants, -5) == []  # 0: never


def test_a_context_held_by_a_request_is_never_closed() -> None:
    clock = Clock()
    tenants, _ = _tenants(clock)
    held = tenants.acquire("acme")
    clock.now += 10_000
    assert _close_idle(tenants, 1) == [] and not held.closed
    assert tenants.holders("acme") == 1
    with tenants.using("acme"):  # a second request at the same time
        assert tenants.holders("acme") == 2
    tenants.release("acme")
    assert tenants.holders("acme") == 0
    assert _close_idle(tenants, 1) == []  # just released: it counts as used now
    clock.now += 2
    assert _close_idle(tenants, 1) == ["acme"] and held.closed


@pytest.mark.parametrize("work", ["run:run_1", "batch:bat_1", "export:portable", "stream", "reindex:knowledge"])
def test_a_context_with_background_work_is_never_closed(work: str) -> None:
    clock = Clock()
    tenants, _ = _tenants(clock)
    context = tenants.get("acme")
    context.work = [work]
    clock.now += 10_000
    assert _close_idle(tenants, 1) == [] and not context.closed
    context.work = []
    assert _close_idle(tenants, 1) == ["acme"] and context.closed


def test_a_context_that_cannot_say_whether_it_is_busy_stays_open() -> None:
    clock = Clock()

    def broken(_context: Any) -> bool:
        raise RuntimeError("cannot tell")

    tenants, _ = _tenants(clock, busy=broken)
    context = tenants.get("acme")
    clock.now += 10_000
    assert _close_idle(tenants, 1) == [] and not context.closed
    without, _ = _tenants(clock, busy=None)  # no busy check at all: nothing is closed for being idle
    kept = without.get("acme")
    clock.now += 10_000
    assert _close_idle(without, 1) == [] and not kept.closed
    assert tenant_busy(SimpleNamespace()) is True  # a context without background_work counts as busy


def test_a_context_taken_while_it_is_checked_stays_open() -> None:
    clock = Clock()
    holder: dict[str, Any] = {}

    def busy(context: Any) -> bool:
        # A request takes the context between the sweep's first look and its decision.
        holder["context"] = tenants.acquire("acme")
        tenants.release("acme")
        return False

    tenants, _ = _tenants(clock, busy=busy)
    context = tenants.get("acme")
    clock.now += 10_000
    assert _close_idle(tenants, 1) == [] and not context.closed
    assert holder["context"] is context


def test_a_failing_close_still_forgets_the_context() -> None:
    clock = Clock()

    async def close(_context: Any) -> None:
        raise RuntimeError("closing failed")

    tenants = TenantContexts(lambda tenant: Context(tenant), close, busy=tenant_busy, clock=clock)
    tenants.get("acme")
    clock.now += 10
    assert _close_idle(tenants, 1) == ["acme"] and tenants.items() == []


def test_a_request_holds_its_context_until_a_streamed_answer_ended() -> None:
    """The lease covers the whole answer: a streaming route still holds the context while it sends its body."""
    clock = Clock()
    tenants, _ = _tenants(clock)
    seen: list[int] = []
    app = FastAPI()
    app.state.tenants = tenants

    @app.middleware("http")
    async def principal(request: Request, call_next: Any) -> Any:
        request.state.principal = SimpleNamespace(tenant_id=request.headers.get("x-tenant", "acme"))
        return await call_next(request)

    @app.get("/plain")
    def plain(ctx: Ctx) -> dict[str, int]:
        return {"holders": tenants.holders("acme")}

    @app.get("/stream")
    def stream(ctx: Ctx) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            for _ in range(3):
                seen.append(tenants.holders("acme"))
                await asyncio.sleep(0)
                yield b"x"

        return StreamingResponse(body())

    @app.get("/fails")
    def fails(ctx: Ctx) -> None:
        raise RuntimeError("the route failed")

    app.add_middleware(TenantLeases, tenants=tenants)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/plain").json() == {"holders": 1}
        assert tenants.holders("acme") == 0
        assert client.get("/stream").content == b"xxx"
        assert seen == [1, 1, 1] and tenants.holders("acme") == 0
        assert client.get("/fails").status_code == 500
        assert tenants.holders("acme") == 0  # released after a failure too
        client.get("/plain", headers={"x-tenant": "globex"})
        assert tenants.holders("globex") == 0


def test_the_desktop_context_reports_its_batches_as_background_work(make_app: Any) -> None:
    """The real context's ``background_work``: a running batch keeps it busy until it finished."""
    release = threading.Event()

    def slow(request: dict[str, Any]) -> Any:
        release.wait(20)
        return answer(request)

    client = make_app({"WORKER": [slow] * 4})
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    assert nodestro.background_work() == [] and tenant_busy(nodestro) is False
    workflow_id = form_workflow(client)
    created = client.post(
        "/api/batches", json={"workflow_id": workflow_id, "rows": [{"values": {"kunde": "K"}}], "concurrency": 1}
    ).json()
    try:
        deadline = time.monotonic() + 20
        while f"batch:{created['id']}" not in nodestro.background_work():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert tenant_busy(nodestro) is True
    finally:
        release.set()
    poll(client, f"/api/batches/{created['id']}", ("finished",))
    deadline = time.monotonic() + 20
    while nodestro.background_work():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_the_idle_time_comes_from_the_environment(tmp_path: Path) -> None:
    from test_server_command import _environment

    assert DEFAULT_TENANT_IDLE_SECONDS == 1800.0
    assert TenantComposition.__dataclass_fields__["idle_close"].default == DEFAULT_TENANT_IDLE_SECONDS
    assert command.load(_environment(tmp_path)).tenant_idle == 1800.0
    assert command.load(_environment(tmp_path, NODESTRO_TENANT_IDLE_MINUTES="5")).tenant_idle == 300.0
    assert command.load(_environment(tmp_path, NODESTRO_TENANT_IDLE_MINUTES="0")).tenant_idle == 0.0
    for bad in ("-1", "soon", "nan", "inf"):
        with pytest.raises(NodestroError) as error:
            command.load(_environment(tmp_path, NODESTRO_TENANT_IDLE_MINUTES=bad))
        assert [item["field"] for item in error.value.params["problems"]] == ["NODESTRO_TENANT_IDLE_MINUTES"], bad


# ---- on a server (PostgreSQL) ------------------------------------------------------------------


@pytest.fixture
def backend() -> str:
    return "nodestro"


class Model:
    """The model of the server's tenants: blocks while ``release`` is not set, once ``block`` is set."""

    def __init__(self) -> None:
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, _request: dict[str, Any]) -> Any:
        if self.block:
            self.entered.set()
            self.release.wait(20)
        return text("done")


@pytest.fixture
def model() -> Model:
    return Model()


@pytest.fixture
def studio(database: Any, identity: Any, tmp_path: Path, model: Model) -> Iterator[Any]:  # noqa: F811
    """A server without a job queue (runs execute in the API process) with :class:`Model` as every tenant's model."""
    import httpx
    import stub_oidc
    from test_identity import Server, oidc_settings

    from nodestro_api.identity import OidcClient
    from nodestro_api.server_app import create_tenant_app

    from conftest import ScriptedClient

    provider = stub_oidc.StubProvider()
    scripted = ScriptedClient({"ALPHA": [model] * 10})
    app = create_tenant_app(
        TenantComposition(
            database=database,
            identity=identity,
            oidc=OidcClient(oidc_settings(provider.issuer), http=httpx.Client(transport=provider.transport())),
            origin=stub_oidc.ORIGIN,
            data_root=tmp_path / "server",
            client_factory=lambda _provider: scripted,
            integrations=[],
        )
    )
    built = Server(app, identity, provider, [])
    yield built
    for client in built.clients:
        client.__exit__(None, None, None)


def test_a_server_closes_an_idle_tenant_and_reopens_it_with_its_data(studio: Any) -> None:
    alice = studio.member("acme", "alice", "owner")
    bob = studio.member("globex", "bob", "owner")
    created = alice.post("/api/workflows", json={"name": "Kept", "nodes": [], "edges": []})
    assert created.status_code == 201, created.text
    assert bob.get("/api/workflows").status_code == 200
    tenants: TenantContexts = studio.app.state.tenants
    first = dict(tenants.items())["acme"]
    assert tenants.holders("acme") == 0  # the answers were sent: nothing is held
    time.sleep(0.05)
    found = alice.portal.call(tenants.close_idle, 0.01)  # type: ignore[attr-defined]  # on the app's event loop
    assert sorted(found) == ["acme", "globex"]
    assert tenants.items() == []
    names = [item["name"] for item in alice.get("/api/workflows").json()]
    assert "Kept" in names  # reopened with its data
    assert dict(tenants.items())["acme"] is not first
    assert bob.get("/api/workflows").status_code == 200


def test_a_server_keeps_a_tenant_whose_run_executes(studio: Any, model: Model) -> None:
    """A run executing in the API process (a composition without a queue) keeps its tenant's context open, while an
    idle tenant next to it is closed."""
    alice = studio.member("acme", "alice", "owner")
    bob = studio.member("globex", "bob", "owner")
    flow = {
        "name": "Slow",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "ALPHA"}},
            {"id": "out", "type": "output"},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
    }
    workflow = alice.post("/api/workflows", json=flow)
    assert workflow.status_code == 201, workflow.text
    assert bob.get("/api/workflows").status_code == 200
    tenants: TenantContexts = studio.app.state.tenants
    context = tenants.get("acme")
    model.block = True
    started = alice.post(f"/api/workflows/{workflow.json()['id']}/runs", json={"input": "x"})
    assert started.status_code in (200, 201, 202), started.text
    try:
        assert model.entered.wait(20), "the run never called the model"
        time.sleep(0.05)
        assert alice.portal.call(tenants.close_idle, 0.01) == ["globex"]  # type: ignore[attr-defined]
        assert dict(tenants.items())["acme"] is context and not context.store.closed
    finally:
        model.release.set()
    run_id = started.json()["id"]
    deadline = time.monotonic() + 20
    while alice.get(f"/api/runs/{run_id}").json()["status"] not in ("succeeded", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    deadline = time.monotonic() + 20
    while context.background_work():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    time.sleep(0.05)
    assert alice.portal.call(tenants.close_idle, 0.01) == ["acme"]  # type: ignore[attr-defined]
