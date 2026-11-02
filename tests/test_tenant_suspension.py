"""Background work of a suspended tenant stops in the API process (security review SR-03).

Requests of a suspended tenant are refused at once (``test_identity.py``), and its runs stop in the worker at their
next step (runtime ``test_job_delivery.py``). Work a tenant's context had started in the API process before
(batches, benchmarks with their judge, assistant answers, architect drafts, live run chat answers and Python
environment installations, SR-03a; tool generation jobs, NF-1) is stopped by
:func:`nodestro_api.server_app.stop_tenant_work`, which the tenant app's suspension check calls for every opened
tenant that is suspended now (:func:`nodestro_api.server_app.stop_suspended_tenants`).
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from test_run_chat_api import CHAT, message, setup

from nodestro_api.identity import TenantWorkspaces
from nodestro_api.server_app import TENANT_SUSPENDED, stop_suspended_tenants, stop_tenant_work

from conftest import isolate, parsed, text
from test_automation import answer, form_workflow, poll, verdict

CODER = "expert Python developer"  # in the Coding Agent's system prompt
TOOL = '''def add_numbers(a: int, b: int) -> int:
    """Add two numbers.

    Args:
        a: The first number.
        b: The second number.

    Returns:
        The sum.
    """
    return a + b
'''
TESTS = "from tool import add_numbers\n\n\ndef test_add():\n    assert add_numbers(2, 3) == 5\n"
PROPOSAL = parsed(lambda model: model(function_name="add_numbers", description="Adds numbers.", code=TOOL, tests=TESTS))


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


def test_a_suspension_stops_a_run_chat_answer(make_app: Any) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocking_answer(_request: dict[str, Any]) -> Any:
        entered.set()
        release.wait(20)
        return text("an answer after the suspension")

    client, run_id = setup(make_app, {CHAT: [blocking_answer]})
    asked = client.post(
        f"/api/runs/{run_id}/chat/messages",
        json={"kind": "question", "target_run_id": run_id, "node_id": "lead", "content": "Where are we?"},
    )
    assert asked.status_code == 201, asked.text
    question_id = asked.json()["id"]
    assert entered.wait(20), "the answer was never asked for"
    try:
        assert question_id in _stop(client)
        found = message(client, run_id, question_id)
        assert (found["status"], found["code"]) == ("failed", TENANT_SUSPENDED)
    finally:
        release.set()
    time.sleep(0.3)  # the model's late answer arrives meanwhile: it is not stored
    found = message(client, run_id, question_id)
    assert (found["status"], found["code"]) == ("failed", TENANT_SUSPENDED)
    view = client.get(f"/api/runs/{run_id}/chat").json()
    assert [item["kind"] for item in view["messages"]] == ["question"]
    assert question_id not in _stop(client)  # idempotent


def _job(client: TestClient, job_id: str, *states: str) -> dict[str, Any]:
    """The tool job now, or once it reaches one of ``states`` (at most 20 s)."""
    deadline = time.monotonic() + 20
    while True:
        job: dict[str, Any] = client.get(f"/api/tools/jobs/{job_id}").json()
        if not states or job["status"] in states or time.monotonic() > deadline:
            return job
        time.sleep(0.05)


def _stopped(job: dict[str, Any]) -> bool:
    return (job["status"], job["message_code"], job["tool_name"], job["approval"]) == (
        "failed",
        TENANT_SUSPENDED,
        None,
        None,
    ) and job["finished_at"] is not None


def test_a_suspension_stops_the_tenants_tool_jobs_and_only_them(make_app: Any) -> None:
    # NF-1: two contexts stand for two tenants; each has a job waiting for its approval.
    suspended, other = make_app({CODER: [PROPOSAL]}), make_app({CODER: [PROPOSAL]})
    for client in (suspended, other):
        isolate(client)
    jobs = [
        client.post("/api/tools/jobs", json={"requirement": "Add two numbers"}).json() for client in (suspended, other)
    ]
    for client, job in zip((suspended, other), jobs, strict=True):
        assert _job(client, job["id"], "awaiting_approval")["status"] == "awaiting_approval"
    assert jobs[0]["id"] in _stop(suspended)
    assert _stopped(client_job := _job(suspended, jobs[0]["id"])), client_job  # final at once
    late = suspended.post(f"/api/tools/jobs/{jobs[0]['id']}/decision", json={"approved": True})
    assert late.status_code == 400 and late.json()["code"] == "job.not_awaiting", late.text
    time.sleep(0.3)  # the job's thread ends meanwhile: nothing changes
    assert _stopped(_job(suspended, jobs[0]["id"]))
    assert suspended.get("/api/tools/add_numbers").status_code == 404  # never registered
    assert _stop(suspended) == []  # idempotent
    # The other tenant's job still waits for its decision and is carried out.
    assert _job(other, jobs[1]["id"])["status"] == "awaiting_approval"
    other.post(f"/api/tools/jobs/{jobs[1]['id']}/decision", json={"approved": True})
    assert _job(other, jobs[1]["id"], "approved")["status"] == "approved"
    assert other.get("/api/tools/add_numbers").status_code == 200


def test_a_suspension_stops_a_tool_job_in_its_model_loop(make_app: Any) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocking_proposal(request: dict[str, Any]) -> Any:
        entered.set()
        release.wait(20)
        return PROPOSAL(request)

    client = make_app({CODER: [blocking_proposal, PROPOSAL]})
    container = isolate(client)
    job = client.post("/api/tools/jobs", json={"requirement": "Add two numbers"}).json()
    assert entered.wait(20), "the Coding Agent was never asked"
    try:
        assert job["id"] in _stop(client)
        assert _stopped(_job(client, job["id"]))
    finally:
        release.set()
    time.sleep(0.5)  # the model's late answer arrives meanwhile: it is not tested, the job stays stopped
    final = _job(client, job["id"])
    assert _stopped(final) and final["attempts"] == []
    assert container.calls == [] and len(client.fake.messages.calls) == 1  # no further model request


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

        def stopped_with(name: str) -> Any:
            def stop(code: str) -> list[str]:
                log.append(f"{tenant}-{name}:{code}")
                return [f"{tenant}-{name}"]

            return stop

        self.tools = SimpleNamespace(cancel_jobs=stopped_with("tools"))
        self.runs = SimpleNamespace(chat=SimpleNamespace(cancel_answers=stopped_with("chat")))
        self.python = SimpleNamespace(cancel_installs=stopped_with("python"))


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
    # Tool jobs before installations: a stopped job starts no installation, cancel_installs stops a running one.
    assert stopped == ["beta-benchmarks", "beta-batches", "beta-assistant", "beta-tools", "beta-chat", "beta-python"]
    assert log == [
        f"beta-benchmarks:{TENANT_SUSPENDED}",
        f"beta-batches:{TENANT_SUSPENDED}",
        "beta-assistant",
        f"beta-tools:{TENANT_SUSPENDED}",
        f"beta-chat:{TENANT_SUSPENDED}",
        f"beta-python:{TENANT_SUSPENDED}",
    ]
    log.clear()
    assert await stop_suspended_tenants(tenants, _Identity(set())) == []  # type: ignore[arg-type]
    assert log == []
