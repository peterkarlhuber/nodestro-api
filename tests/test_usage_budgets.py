"""Budgets in the usage report (API-017, UX audit AD-20): the daily limits of published APIs, how much of them the
last 24 hours used, and the runs a budget stopped or holds."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from nodestro_runtime.schemas import RunBudget, RunCause, Usage, now
from test_reliability import _ctx, _run, _store_runs

from nodestro_api.publishing import PUBLICATIONS, Publication
from nodestro_api.serving import ApiLimits


def _publication(ctx: Any, pid: str, name: str, **limits: Any) -> None:
    ctx.store.put(
        PUBLICATIONS,
        pid,
        Publication(id=pid, workflow_id=f"wf_{pid}", slug=name.lower(), name=name, limits=ApiLimits(**limits)),
    )


def _cost(usd: float) -> Usage:
    return Usage(requests=1, total_tokens=10, estimated_cost_usd=usd)


def test_the_report_shows_daily_budget_use_and_budget_stops(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    _publication(ctx, "pub_a", "Quotes", daily_budget_usd=1.0, run_budget_usd=0.5)
    _publication(ctx, "pub_b", "Reports", daily_budget_usd=0.1)
    _publication(ctx, "pub_c", "Open", daily_budget_usd=None)
    _store_runs(
        ctx,
        [
            # Counted against pub_a's daily limit: its top-level API runs of the last 24 hours.
            _run("run_a1", "wf_pub_a", trigger="api", trigger_ref="pub_a", usage=_cost(0.25)),
            _run("run_a2", "wf_pub_a", trigger="api", trigger_ref="pub_a", usage=_cost(0.05)),
            # Not counted: older than 24 hours (still in the period), a sub-workflow run, a studio run.
            _run(
                "run_a3",
                "wf_pub_a",
                trigger="api",
                trigger_ref="pub_a",
                usage=_cost(3.0),
                created_at=now() - timedelta(hours=30),
            ),
            _run("run_a4", "wf_pub_a", trigger="api", trigger_ref="pub_a", usage=_cost(3.0), parent_run_id="run_a1"),
            _run("run_s1", "wf_pub_a", usage=_cost(3.0)),
            # pub_b is over its limit.
            _run("run_b1", "wf_pub_b", trigger="api", trigger_ref="pub_b", usage=_cost(0.2)),
            # Stopped by a budget, and one that waits for a budget decision.
            _run("run_x1", "wf_x", status="failed", error_code="run.budget_exceeded"),
            _run("run_x2", "wf_x", status="cancelled", error_code="run.budget_ended"),
            _run("run_x3", "wf_x", status="failed", error_code="run.nodes_failed"),
            # A step stopped by its budget failed the run (the run's cause names it).
            _run(
                "run_x6",
                "wf_x",
                status="failed",
                error_code="run.nodes_failed",
                cause=RunCause(node_id="w", label="Writer", code="run.budget_exceeded"),
            ),
            _run("run_x4", "wf_x", status="paused", budget=RunBudget(pending_request_id="breq_1")),
            _run("run_x5", "wf_x", status="paused", budget=RunBudget()),
        ],
    )

    budgets = client.get("/api/usage", params={"days": 7}).json()["budgets"]
    assert budgets["stopped_runs"] == 3
    assert budgets["waiting_runs"] == 1
    by_id = {item["publication_id"]: item for item in budgets["publications"]}
    assert by_id["pub_a"] == {
        "publication_id": "pub_a",
        "slug": "quotes",
        "name": "Quotes",
        "daily_budget_usd": 1.0,
        "last_24h_usd": 0.3,
        "run_budget_usd": 0.5,
        "exhausted": False,
    }
    assert by_id["pub_b"]["exhausted"] is True and by_id["pub_b"]["last_24h_usd"] == 0.2
    assert by_id["pub_c"]["daily_budget_usd"] is None and by_id["pub_c"]["exhausted"] is False
    # Closest to its limit first.
    assert [item["publication_id"] for item in budgets["publications"]] == ["pub_b", "pub_a", "pub_c"]


def test_the_daily_use_matches_the_check_before_a_run(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    _publication(ctx, "pub_a", "Quotes", daily_budget_usd=1.0)
    _store_runs(
        ctx,
        [
            _run(f"run_{i}", "wf_pub_a", trigger="api", trigger_ref="pub_a", usage=_cost(0.125), status=status)
            for i, status in enumerate(["succeeded", "failed", "cancelled", "paused"])
        ],
    )
    reported = client.get("/api/usage", params={"days": 1}).json()["budgets"]["publications"][0]["last_24h_usd"]
    checked = ctx.serving.spent_since("pub_a", now() - timedelta(hours=24))
    assert reported == checked == 0.5


def test_a_report_without_publications_or_budget_stops_has_an_empty_section(make_app: Any) -> None:
    budgets = make_app().get("/api/usage").json()["budgets"]
    assert budgets == {"publications": [], "stopped_runs": 0, "waiting_runs": 0}
