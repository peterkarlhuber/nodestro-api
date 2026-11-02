"""The API of run budget decisions (BUDGET-1): the run's budget view, raising and ending, idempotency, refusals,
caps, the rule who may decide (starter or admin, never read-only), tenant isolation, unattended runs, audit."""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime import run_budget
from nodestro_runtime.run_budget import BudgetRequests
import pytest
from test_worker import connect, schema, server  # noqa: F401  (PostgreSQL server fixtures)

from nodestro_api.permissions import Permission, Principal

from conftest import text


def flow(budget: float | None = 0.0001, name: str = "Budget") -> dict[str, Any]:
    return {
        "name": name,
        "budget_usd": budget,
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "agent", "agent": {"name": "Agent", "system_prompt": "AGENT", "max_tokens": 1000}},
            {"id": "out", "type": "output"},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
    }


def settled(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("the run did not settle")


def waiting(
    client: TestClient, budget: float | None = 0.0001, *, starter: Principal | None = None
) -> tuple[str, dict[str, Any]]:
    workflow = client.post("/api/workflows", json=flow(budget))
    assert workflow.status_code == 201, workflow.text
    if starter is not None:  # the run is started by someone who may run, not edit
        client.app.state.principal_resolver = lambda _connection: starter  # type: ignore[attr-defined]
    run = client.post(f"/api/workflows/{workflow.json()['id']}/runs", json={"input": "x"})
    assert run.status_code == 201, run.text
    found = settled(client, run.json()["id"])
    assert found["status"] == "paused", found
    view = client.get(f"/api/runs/{found['id']}/budget")
    assert view.status_code == 200, view.text
    return found["id"], view.json()


def decide(client: TestClient, run_id: str, request_id: str, **body: Any) -> Any:
    return client.post(f"/api/runs/{run_id}/budget/decisions", json={"request_id": request_id, **body})


@pytest.fixture
def app(make_app: Callable[..., TestClient]) -> TestClient:
    return make_app({"AGENT": [text("done"), text("again"), text("third")]})


def test_a_paused_run_shows_its_decision_and_continues_after_a_raise(app: TestClient) -> None:
    run_id, view = waiting(app)
    pending = view["pending"]
    assert pending is not None and pending["status"] == "pending" and pending["reason"] == "request_too_large"
    assert view["limit_usd"] == "0.0001" and view["source"] == "workflow" and view["policy"]["can_decide"]
    assert view["policy"]["max_limit_usd"] is None and view["policy"]["decision_hours"] == 24
    run = app.get(f"/api/runs/{run_id}").json()
    assert run["nodes"]["a"]["status"] == "awaiting_budget"
    assert run["budget"]["pending_request_id"] == pending["id"] and run["started_by"] == "local"
    listed = app.get("/api/budget-requests?status=pending").json()
    assert [item["id"] for item in listed] == [pending["id"]]
    summaries = [item for item in app.get("/api/runs").json() if item["id"] == run_id]
    assert summaries and summaries[0]["budget"]["pending_request_id"] == pending["id"]

    answer = decide(app, run_id, pending["id"], action="raise", limit_usd="2.50", idempotency_key="k-1")
    assert answer.status_code == 200, answer.text
    done = settled(app, run_id)
    assert done["status"] == "succeeded", done
    assert done["budget"]["limit_usd"] == "2.50" and done["budget"]["pending_request_id"] is None
    after = app.get(f"/api/runs/{run_id}/budget").json()
    assert after["pending"] is None and [item["status"] for item in after["history"]] == ["raised"]
    assert after["raises"][0]["new_limit_usd"] == "2.50" and after["raises"][0]["decided_by"] == "local"
    assert app.get("/api/budget-requests").json() == []
    # The workflow's stored budget is unchanged.
    workflow = app.get(f"/api/workflows/{done['workflow_id']}").json()
    assert workflow["budget_usd"] == 0.0001
    # The same decision again with the same key answers the stored result; another decision is refused.
    again = decide(app, run_id, pending["id"], action="raise", limit_usd="2.50", idempotency_key="k-1")
    assert again.status_code == 200, again.text
    late = decide(app, run_id, pending["id"], action="end")
    assert late.status_code == 409 and late.json()["code"] == "budget.not_pending"


def test_ending_the_run(app: TestClient) -> None:
    run_id, view = waiting(app)
    answer = decide(app, run_id, view["pending"]["id"], action="end")
    assert answer.status_code == 200, answer.text
    done = settled(app, run_id)
    assert done["status"] == "cancelled" and done["error_code"] == "run.budget_ended"


@pytest.mark.parametrize(
    ("limit", "status", "code"),
    [
        ("0.00001", 422, "budget.limit_invalid"),
        ("abc", 422, "budget.limit_invalid"),
        ("-1", 422, "budget.limit_invalid"),
        ("0.00", 422, "budget.limit_not_higher"),
        (None, 422, "budget.limit_invalid"),
    ],
)
def test_invalid_limits_are_refused_and_audited(app: TestClient, limit: str | None, status: int, code: str) -> None:
    run_id, view = waiting(app)
    answer = decide(app, run_id, view["pending"]["id"], action="raise", limit_usd=limit)
    assert answer.status_code == status and answer.json()["code"] == code, answer.text
    assert app.get(f"/api/runs/{run_id}").json()["status"] == "paused"
    ctx = app.app.state.nodestro  # type: ignore[attr-defined]
    kinds = [event.kind for event in run_budget.audit_events(ctx.store, run_id=run_id)]
    assert kinds[0] == "refused" and "requested" in kinds


def test_the_tenant_cap_limits_a_raise(app: TestClient) -> None:
    assert app.put("/api/settings", json={"max_run_budget_usd": "1.00"}).status_code == 200
    run_id, view = waiting(app)
    assert view["policy"]["max_limit_usd"] == "1.00"
    answer = decide(app, run_id, view["pending"]["id"], action="raise", limit_usd="5")
    assert answer.status_code == 403 and answer.json()["code"] == "budget.over_policy", answer.text
    ok = decide(app, run_id, view["pending"]["id"], action="raise", limit_usd="1")
    assert ok.status_code == 200, ok.text
    assert app.put("/api/settings", json={"clear_max_run_budget": True}).json()["max_run_budget_usd"] is None


def test_an_unknown_request_and_run_are_not_found(app: TestClient) -> None:
    run_id, _view = waiting(app)
    unknown = decide(app, run_id, "bud_unknown", action="end")
    assert unknown.status_code == 404 and unknown.json()["code"] == "budget.request_not_found", unknown.text
    assert decide(app, "run_unknown", "bud_unknown", action="end").status_code == 404
    assert app.get("/api/runs/run_unknown/budget").status_code == 404


def test_read_only_and_other_people_cannot_decide(app: TestClient) -> None:
    run_id, view = waiting(app)  # started by the desktop's owner ("local")
    request_id = view["pending"]["id"]
    reader = Principal(id="reader", permissions=frozenset({Permission.READ}))
    runner = Principal(id="runner", permissions=frozenset({Permission.READ, Permission.RUN}))
    admin = Principal(id="admin", permissions=frozenset(Permission))
    app.app.state.principal_resolver = lambda _connection: reader  # type: ignore[attr-defined]
    seen = app.get(f"/api/runs/{run_id}/budget").json()
    assert seen["pending"]["id"] == request_id and seen["policy"]["can_decide"] is False
    assert decide(app, run_id, request_id, action="raise", limit_usd="5").status_code == 403
    app.app.state.principal_resolver = lambda _connection: runner  # type: ignore[attr-defined]
    seen = app.get(f"/api/runs/{run_id}/budget").json()
    assert seen["policy"] == {**seen["policy"], "can_decide": False, "reason": "budget.not_starter"}
    refused = decide(app, run_id, request_id, action="raise", limit_usd="5")
    assert refused.status_code == 403 and refused.json()["code"] == "access.forbidden"
    assert app.get(f"/api/runs/{run_id}").json()["status"] == "paused"
    app.app.state.principal_resolver = lambda _connection: admin  # type: ignore[attr-defined]
    answer = decide(app, run_id, request_id, action="raise", limit_usd="5")
    assert answer.status_code == 200, answer.text
    assert settled(app, run_id)["budget"]["raises"][0]["decided_by"] == "admin"


def test_the_starter_decides_their_own_run(app: TestClient) -> None:
    runner = Principal(id="runner", permissions=frozenset({Permission.READ, Permission.RUN}))
    run_id, view = waiting(app, starter=runner)
    assert view["policy"]["can_decide"] is True
    assert app.get(f"/api/runs/{run_id}").json()["started_by"] == "runner"
    assert decide(app, run_id, view["pending"]["id"], action="end").status_code == 200


def test_runs_of_a_published_api_never_wait(app: TestClient) -> None:
    """A published API's run is unattended: it fails as before and never asks anybody for a budget decision."""
    workflow = app.post("/api/workflows", json=flow(0.0001, name="Budget API")).json()
    published = app.post(f"/api/workflows/{workflow['id']}/api/publish", json={})
    assert published.status_code == 201, published.text
    publication = published.json()["publication"]
    key = app.post(
        "/api/api-keys", json={"name": "CRM", "scopes": ["run", "read"], "publications": [publication["id"]]}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {key}"}
    started = app.post(f"/api/published/{publication['slug']}/runs", json={"input": "x"}, headers=headers)
    assert started.status_code in (200, 201, 202), started.text
    run_id = started.json()["run_id"] if "run_id" in started.json() else started.json()["id"]
    done = settled(app, run_id)
    assert done["status"] == "failed", done
    assert done["nodes"]["a"]["error_code"] == "run.budget_exceeded", done["nodes"]["a"]
    ctx = app.app.state.nodestro  # type: ignore[attr-defined]
    assert BudgetRequests(ctx.store).of_run(run_id) == []
    assert app.get("/api/budget-requests").json() == []


def test_requests_of_another_tenant_are_not_found(server: Any) -> None:  # noqa: F811
    """On a server every route works on the request's tenant only; a viewer reads, never decides; an operator who did
    not start the run is refused."""
    from test_worker import _start

    alice = server.member("acme", "alice", "editor")
    mallory = server.member("globex", "mallory", "owner")
    run_id = _start(alice)
    acme = server.app.state.tenants.get("acme")
    request = BudgetRequests(acme.store).put(
        run_budget.BudgetRequest(
            run_id=run_id,
            workflow_id="wf",
            workflow_name="Budget",
            block=run_budget.BudgetBlock(scope="run", run_id=run_id),
            limit_usd=run_budget.to_decimal(0.01),
            spent_usd=run_budget.to_decimal(0),
        )
    )
    assert mallory.get("/api/budget-requests").json() == []
    assert mallory.get(f"/api/runs/{run_id}/budget").status_code == 404
    assert decide(mallory, run_id, request.id, action="end").status_code == 404
    assert BudgetRequests(acme.store).get(run_id, request.id).status == "pending"  # type: ignore[union-attr]
    viewer = server.member("acme", "victor", "viewer")
    assert [item["id"] for item in viewer.get("/api/budget-requests").json()] == [request.id]
    assert decide(viewer, run_id, request.id, action="end").status_code == 403
    other = server.member("acme", "otto", "operator")
    refused = decide(other, run_id, request.id, action="end")
    assert refused.status_code == 403 and refused.json()["params"]["reason"] == "budget.not_starter"
    alice_id = alice.get("/api/auth/me").json()["user_id"]
    assert acme.runs.get(run_id).started_by == alice_id
