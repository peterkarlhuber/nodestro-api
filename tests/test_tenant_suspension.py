"""Background work of a suspended tenant stops in the API process (security review SR-03).

Requests of a suspended tenant are refused at once (``test_identity.py``), and its runs stop in the worker at their
next step (runtime ``test_job_delivery.py``). Work a tenant's context had started in the API process before
(batches, benchmarks with their judge, assistant answers, architect drafts) is stopped by
:func:`nodestro_api.server_app.stop_tenant_work`, which the tenant app's suspension check calls for every opened
tenant that is suspended now (:func:`nodestro_api.server_app.stop_suspended_tenants`).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from fastapi.testclient import TestClient

from nodestro_api.identity import TenantWorkspaces
from nodestro_api.server_app import TENANT_SUSPENDED, stop_suspended_tenants, stop_tenant_work

from test_automation import answer, form_workflow, poll, verdict


def _stop(client: TestClient) -> list[str]:
    """``stop_tenant_work`` on the app's own event loop (where its background tasks run)."""
    stopped: list[str] = client.portal.call(stop_tenant_work, client.app.state.nodestro)  # type: ignore[union-attr,attr-defined]
    return stopped


def test_a_suspension_stops_a_running_batch(make_app: Any) -> None:
    def slow(request: dict[str, Any]) -> Any:
        time.sleep(0.3)
        return answer(request)

    client = make_app({"WORKER": [slow] * 20})
    workflow_id = form_workflow(client)
    rows = [{"values": {"kunde": f"K{number}"}} for number in range(6)]
    created = client.post("/api/batches", json={"workflow_id": workflow_id, "rows": rows, "concurrency": 1}).json()
    assert created["status"] == "running"
    assert created["id"] in _stop(client)
    final = poll(client, f"/api/batches/{created['id']}", ("cancelled",))
    assert final["stop_code"] == TENANT_SUSPENDED
    assert final["counts"]["pending"] == 0 and final["counts"]["skipped"] >= 4
    assert _stop(client) == []  # idempotent


def test_a_suspension_stops_a_benchmark_before_its_judge_calls_again(make_app: Any) -> None:
    entered, release = threading.Event(), threading.Event()
    judged: list[int] = []

    def blocking_verdict(request: dict[str, Any]) -> Any:
        judged.append(1)
        entered.set()
        release.wait(20)
        return verdict(True)(request)

    client = make_app({"WORKER": [answer] * 4, "strict, fair reviewer": [blocking_verdict] * 4})
    workflow_id = form_workflow(client)
    cases = [{"name": name, "values": {"kunde": name}, "judge_criteria": "Nennt den Kunden"} for name in ("A", "B")]
    suite = client.post("/api/benchmarks", json={"name": "S", "workflow_id": workflow_id, "cases": cases}).json()
    bench = client.post(f"/api/benchmarks/{suite['id']}/runs", json={"concurrency": 2}).json()
    assert entered.wait(20), "the judge was never asked"
    try:
        assert bench["id"] in _stop(client)
    finally:
        release.set()
    stopped = client.get(f"/api/benchmark-runs/{bench['id']}").json()
    assert (stopped["status"], stopped["stop_code"]) == ("cancelled", TENANT_SUSPENDED)
    time.sleep(0.3)
    assert judged == [1]  # the second case is never judged
    assert client.get(f"/api/benchmark-runs/{bench['id']}").json()["status"] == "cancelled"


class _Work:
    """A tenant context's background services, recording what was stopped."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name, self.log = name, log

    async def _async(self, *, stop_code: str) -> list[str]:
        self.log.append(f"{self.name}:{stop_code}")
        return [self.name]

    def _sync(self) -> list[str]:
        self.log.append(self.name)
        return [self.name]


class _Context:
    def __init__(self, tenant: str, log: list[str], *, broken: bool = False) -> None:
        self.batches = _Work(f"{tenant}-batches", log)
        self.batches.cancel_all = self.batches._async  # type: ignore[attr-defined]
        self.benchmarks = _Work(f"{tenant}-benchmarks", log)
        self.benchmarks.cancel_all = self.benchmarks._async  # type: ignore[attr-defined]
        self.assistant = _Work(f"{tenant}-assistant", log)
        self.assistant.cancel_all = self.assistant._sync  # type: ignore[attr-defined]
        self.architect = _Work(f"{tenant}-architect", log)
        if broken:

            def fail() -> list[str]:
                raise RuntimeError("broken")

            self.architect.cancel_all = fail  # type: ignore[attr-defined]
        else:
            self.architect.cancel_all = self.architect._sync  # type: ignore[attr-defined]


class _Identity:
    def __init__(self, suspended: set[str]) -> None:
        self.suspended = suspended

    def tenant_active(self, tenant_id: str) -> bool:
        if tenant_id == "unknown":
            raise ConnectionError("the database is gone")
        return tenant_id not in self.suspended


def test_only_suspended_tenants_have_their_work_stopped() -> None:
    asyncio.run(_only_suspended())


async def _only_suspended() -> None:
    log: list[str] = []
    contexts = {
        "acme": _Context("acme", log),
        "beta": _Context("beta", log, broken=True),
        "unknown": _Context("u", log),
    }
    tenants = TenantWorkspaces(lambda tenant: contexts[tenant])
    for tenant in contexts:
        tenants.get(tenant)
    stopped = await stop_suspended_tenants(tenants, _Identity({"beta"}))  # type: ignore[arg-type]
    # beta's architect fails to stop: the rest of beta is stopped anyway; acme and the unknown status are untouched.
    assert stopped == ["beta-benchmarks", "beta-batches", "beta-assistant"]
    assert log == [f"beta-benchmarks:{TENANT_SUSPENDED}", f"beta-batches:{TENANT_SUSPENDED}", "beta-assistant"]
    log.clear()
    assert await stop_suspended_tenants(tenants, _Identity(set())) == []  # type: ignore[arg-type]
    assert log == []
