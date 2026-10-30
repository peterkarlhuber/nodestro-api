"""Batches, workflow test series and schedules: all start ordinary runs (no API calls)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from nodestro_runtime.schemas import Workflow
import pytest

from nodestro_api.batches import parse_table, rows_from_records, suggest_mapping
from nodestro_api.schedules import ScheduleIn, next_slot, upcoming

from conftest import parsed, text
from test_files import make_xlsx
from test_flow import edge, save
from test_runs import agent_node


def answer(request: dict[str, Any]) -> Any:
    """The agent answers with the run input it received, so items are distinguishable."""
    content = request["messages"][0]["content"]
    prompt = content if isinstance(content, str) else " ".join(str(block.get("text", "")) for block in content)
    if "FAIL" in prompt:
        return text("…", stop_reason="max_tokens")
    return text(f'{{"echo": "{prompt.split()[-1]}", "ok": true}}')


def form_workflow(client: TestClient, **extra: Any) -> str:
    return save(
        client,
        [
            {
                "id": "start",
                "type": "start",
                "start": {
                    "fields": [
                        {"name": "kunde", "label": "Kunde", "type": "text", "required": True},
                        {"name": "betrag", "label": "Betrag", "type": "number"},
                    ]
                },
            },
            agent_node("worker", "WORKER", task_template="Bearbeite {{form.kunde}}"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "worker"), edge("worker", "out")],
        **extra,
    )


def poll(client: TestClient, path: str, done: tuple[str, ...], timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(path).json()
        if body["status"] in done:
            return body  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"{path} did not finish: {body['status']}")


# ---- batches ---------------------------------------------------------------------------


def test_rows_from_csv_xlsx_and_json() -> None:
    rows = parse_table("Kunde;Betrag\nAlpha;10\nBeta;20\n;\n".encode("cp1252"), "liste.csv")
    assert rows == [{"Kunde": "Alpha", "Betrag": "10"}, {"Kunde": "Beta", "Betrag": "20"}]
    assert parse_table(make_xlsx(), "kosten.xlsx")[0] == {"Posten": "Miete", "Betrag": "1200"}
    assert parse_table(b'{"items": [{"a": 1}]}', "x.json") == [{"a": "1"}]
    workflow = Workflow(
        name="w",
        nodes=[{"id": "start", "type": "start", "start": {"fields": [{"name": "kunde", "label": "Kunde"}]}}],
        edges=[],
    )
    assert suggest_mapping(["KUNDE", "Notiz"], workflow) == {"KUNDE": "kunde"}
    assert rows_from_records([{"KUNDE": "A", "Notiz": "x"}], {"KUNDE": "kunde"})[0].values == {"kunde": "A"}


def test_a_batch_runs_every_valid_row_and_reports_the_rest(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 10})
    workflow_id = form_workflow(client)
    rows = [
        {"values": {"kunde": "Alpha", "betrag": "10"}},
        {"values": {"kunde": "FAIL", "betrag": "5"}},
        {"values": {"betrag": "zehn"}},
        {"values": {"kunde": "Gamma"}},
    ]
    plan = client.post("/api/batches/plan", json={"workflow_id": workflow_id, "rows": rows}).json()
    assert (plan["total"], plan["valid"], plan["invalid"]) == (4, 3, 1)
    assert {error["code"] for error in plan["items"][2]["errors"]} == {"form.required", "form.not_number"}
    assert plan["estimate"]["runs"] == 3 and plan["estimate"]["max_total_usd"] == pytest.approx(6.0)

    created = client.post("/api/batches", json={"workflow_id": workflow_id, "rows": rows, "concurrency": 2})
    assert created.status_code == 201, created.text
    batch = poll(client, f"/api/batches/{created.json()['id']}", ("finished",))
    statuses = [item["status"] for item in batch["items"]]
    assert statuses == ["succeeded", "failed", "invalid", "succeeded"]
    assert batch["counts"]["succeeded"] == 2 and batch["counts"]["failed"] == 1
    assert batch["cost_usd"] > 0

    runs = client.get("/api/runs").json()
    batch_runs = [run for run in runs if run["trigger"] == "batch" and run["trigger_ref"] == batch["id"]]
    assert len(batch_runs) == 3
    # The batch only collects: the usage report counts each run once.
    usage = client.get("/api/usage").json()
    assert usage["total"]["estimated_cost_usd"] == pytest.approx(batch["cost_usd"])

    exported = client.get(f"/api/batches/{batch['id']}/export").content.decode("utf-8")
    header, first = exported.lstrip("﻿").splitlines()[:2]
    assert header.startswith("row,kunde,betrag,result.echo,result.ok,status")
    assert first.startswith("1,Alpha,10,Alpha,True,succeeded")
    as_json = client.get(f"/api/batches/{batch['id']}/export", params={"format": "json"}).json()
    assert as_json[2]["status"] == "invalid"


def test_batch_limits_pause_and_cancel(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 10})
    workflow_id = form_workflow(client)
    rows = [{"values": {"kunde": f"K{number}"}} for number in range(5)]
    created = client.post(
        "/api/batches", json={"workflow_id": workflow_id, "rows": rows, "concurrency": 1, "max_cost_usd": 0.001}
    ).json()
    batch = poll(client, f"/api/batches/{created['id']}", ("stopped",))
    assert batch["stop_code"] == "batch.cost_limit"
    assert batch["counts"]["succeeded"] == 1 and batch["counts"]["skipped"] == 4

    # Slow answers keep the first item running while the batch is paused, so no other item starts.
    def slow(request: dict[str, Any]) -> Any:
        time.sleep(0.5)
        return answer(request)

    client = make_app({"WORKER": [slow] * 10})
    workflow_id = form_workflow(client)
    created = client.post("/api/batches", json={"workflow_id": workflow_id, "rows": rows, "concurrency": 1}).json()
    client.post(f"/api/batches/{created['id']}/pause")
    paused = client.get(f"/api/batches/{created['id']}").json()
    assert paused["status"] == "paused"
    cancelled = client.post(f"/api/batches/{created['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    final = poll(client, f"/api/batches/{created['id']}", ("cancelled",))
    assert final["counts"]["pending"] == 0 and final["counts"]["skipped"] >= 3


def test_own_tools_need_agreement_before_a_batch(make_app: Any) -> None:
    client = make_app({"WORKER": [answer]})
    source = (
        "def send_mail(to: str) -> str:\n"
        '    """Send a mail.\n\n    Args:\n        to: Recipient.\n\n    Returns:\n        Status.\n    """\n'
        "    return 'sent'\n"
    )
    tests = "from tool import send_mail\n\n\ndef test_send_mail():\n    assert send_mail('a') == 'sent'\n"
    saved = client.post("/api/tools", json={"function_name": "send_mail", "code": source, "tests": tests})
    assert saved.json()["tool"] is not None, saved.text
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("worker", "WORKER", tools=["send_mail"]),
        ],
        [edge("start", "worker")],
    )
    body = {"workflow_id": workflow_id, "rows": [{"input": "a"}]}
    assert client.post("/api/batches/plan", json=body).json()["external_tools"] == ["send_mail"]
    refused = client.post("/api/batches", json=body)
    assert refused.status_code == 400 and refused.json()["code"] == "batch.confirm_external"
    assert client.post("/api/batches", json={**body, "confirm_external": True}).status_code == 201


def test_batch_items_wait_for_approvals_without_blocking(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 4})
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            {"id": "check", "type": "approval", "label": "Freigabe"},
            agent_node("worker", "WORKER"),
        ],
        [edge("start", "check"), edge("check", "worker", "approved")],
    )
    created = client.post(
        "/api/batches", json={"workflow_id": workflow_id, "rows": [{"input": "eins"}, {"input": "zwei"}]}
    ).json()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        batch = client.get(f"/api/batches/{created['id']}").json()
        if batch["counts"]["paused"] == 2:
            break
        time.sleep(0.05)
    assert batch["counts"]["paused"] == 2 and batch["status"] == "running"
    approvals = client.get("/api/approvals").json()
    assert len(approvals) == 2
    for pending in approvals:
        response = client.post(
            f"/api/runs/{pending['run_id']}/approvals/{pending['node_id']}", json={"decision": "approved"}
        )
        assert response.status_code == 200, response.text
    batch = poll(client, f"/api/batches/{created['id']}", ("finished",))
    assert batch["counts"]["succeeded"] == 2


# ---- test series -----------------------------------------------------------------------


def verdict(*passed: bool) -> Any:
    return parsed(
        lambda model: model.model_validate(
            {
                "criteria": [
                    {"name": f"c{index}", "reasoning": "geprüft", "passed": value}
                    for index, value in enumerate(passed, start=1)
                ],
                "summary": "ok",
                "suggestions": [],
            }
        )
    )


def test_a_test_series_checks_judges_and_compares_versions(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 6, "strict, fair reviewer": [verdict(True, True), verdict(True, False)]})
    workflow_id = form_workflow(client)
    suite = client.post(
        "/api/benchmarks",
        json={
            "name": "Kunden",
            "workflow_id": workflow_id,
            "cases": [
                {
                    "name": "Alpha",
                    "values": {"kunde": "Alpha"},
                    "checks": [
                        {"id": "rule_echo", "label": "Echo", "field": "echo", "operator": "equals", "value": "Alpha"},
                        {"id": "rule_ok", "label": "OK", "field": "ok", "operator": "is_true"},
                    ],
                    "judge_criteria": "Nennt den Kunden\nIst höflich",
                },
                {
                    "name": "Beta",
                    "values": {"kunde": "Beta"},
                    "checks": [{"id": "rule_x", "field": "echo", "operator": "equals", "value": "Gamma"}],
                },
                {"name": "Fehler", "values": {"kunde": "FAIL"}, "judge_criteria": "Irgendwas"},
            ],
        },
    )
    assert suite.status_code == 201, suite.text
    suite_id = suite.json()["id"]
    plan = client.post(f"/api/benchmarks/{suite_id}/plan", json={}).json()
    assert plan["cases"] == 3 and plan["judged_cases"] == 2 and plan["problems"] == []

    started = client.post(f"/api/benchmarks/{suite_id}/runs", json={"concurrency": 3})
    assert started.status_code == 201, started.text
    first = poll(client, f"/api/benchmark-runs/{started.json()['id']}", ("finished",))
    results = {result["name"]: result for result in first["results"]}
    assert results["Alpha"]["passed"] is True
    assert results["Alpha"]["judge"]["ai"] is True and results["Alpha"]["judge"]["score"] == 1.0
    assert [check["passed"] for check in results["Alpha"]["checks"]] == [True, True]
    assert results["Beta"]["passed"] is False and results["Beta"]["judge"] is None
    assert results["Fehler"]["run_status"] == "failed" and results["Fehler"]["judge"] is None
    summary = first["summary"]
    assert (summary["passed"], summary["failed"], summary["errors"]) == (1, 2, 1)
    assert summary["checks_passed"] == 2 and summary["checks_total"] == 3
    assert summary["judged"] == 1 and summary["judge_cost_usd"] > 0

    # The judge's cost is in the usage ledger, the runs' cost in the runs: nothing counted twice.
    ledger = client.get("/api/usage").json()
    judge_bucket = next(bucket for bucket in ledger["by_source"] if bucket["key"] == "benchmark_judge")
    assert judge_bucket["estimated_cost_usd"] == pytest.approx(summary["judge_cost_usd"])

    second = client.post(f"/api/benchmarks/{suite_id}/runs", json={"concurrency": 1}).json()
    second = poll(client, f"/api/benchmark-runs/{second['id']}", ("finished",))
    comparison = client.get("/api/benchmark-runs/compare", params={"a": first["id"], "b": second["id"]}).json()
    changes = {case["name"]: case["change"] for case in comparison["cases"]}
    assert changes["Alpha"] == "worse" and changes["Beta"] == "same"


def test_test_series_reject_invalid_checks(make_app: Any) -> None:
    client = make_app()
    workflow_id = form_workflow(client)
    response = client.post(
        "/api/benchmarks",
        json={
            "workflow_id": workflow_id,
            "cases": [{"name": "x", "checks": [{"field": "a", "operator": "gt", "value": "viel"}]}],
        },
    )
    assert response.status_code == 400 and response.json()["code"] == "condition.rule_not_number"


# ---- schedules -------------------------------------------------------------------------


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)  # type: ignore[misc]


def test_daily_schedules_keep_their_wall_clock_time_across_dst() -> None:
    schedule = ScheduleIn(workflow_id="w", kind="daily", at="08:00", time_zone="Europe/Berlin")
    before, after = upcoming(schedule, utc(2026, 3, 27, 12, 0), 2)
    berlin = ZoneInfo("Europe/Berlin")
    assert [slot.astimezone(berlin).hour for slot in (before, after)] == [8, 8]
    assert after - before == timedelta(hours=23)


def test_skipped_and_repeated_times_on_dst_days() -> None:
    gap = ScheduleIn(workflow_id="w", kind="once", at="02:30", start_date="2026-03-29", time_zone="Europe/Berlin")
    slot = next_slot(gap, utc(2026, 3, 1))
    assert slot is not None and slot.astimezone(ZoneInfo("Europe/Berlin")).strftime("%H:%M") == "03:30"
    repeated = ScheduleIn(workflow_id="w", kind="daily", at="02:30", time_zone="Europe/Berlin")
    slots = upcoming(repeated, utc(2026, 10, 24, 12), 3)
    assert [item.strftime("%m-%d %H:%M") for item in slots] == ["10-25 00:30", "10-26 01:30", "10-27 01:30"]


def test_weekly_monthly_and_hourly_slots() -> None:
    weekly = ScheduleIn(workflow_id="w", kind="weekly", at="09:15", weekdays=[0, 4], time_zone="UTC")
    assert [slot.strftime("%a %H:%M") for slot in upcoming(weekly, utc(2026, 10, 28), 3)] == [
        "Fri 09:15",
        "Mon 09:15",
        "Fri 09:15",
    ]
    monthly = ScheduleIn(workflow_id="w", kind="monthly", at="07:00", day_of_month=31, time_zone="UTC")
    assert [slot.strftime("%m-%d") for slot in upcoming(monthly, utc(2026, 10, 28), 3)] == ["10-31", "11-30", "12-31"]
    hourly = ScheduleIn(workflow_id="w", kind="hourly", at="00:20", every_hours=6, time_zone="UTC")
    assert [slot.strftime("%H:%M") for slot in upcoming(hourly, utc(2026, 10, 28, 7), 2)] == ["12:20", "18:20"]


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_schedules_are_checked_before_switching_on_and_run_through_the_engine(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 5})
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    clock = Clock(utc(2026, 10, 28, 6, 0))
    ctx.schedules.clock = clock
    workflow_id = form_workflow(client)
    body = {"workflow_id": workflow_id, "kind": "daily", "at": "08:00", "time_zone": "Europe/Berlin"}
    created = client.post("/api/schedules", json=body).json()
    assert created["enabled"] is False

    refused = client.post(f"/api/schedules/{created['id']}/enable").json()
    assert refused["schedule"]["enabled"] is False
    assert [item["code"] for item in refused["activation"]["problems"]] == ["form.required"]

    client.put(f"/api/schedules/{created['id']}", json={**body, "values": {"kunde": "Plan"}})
    enabled = client.post(f"/api/schedules/{created['id']}/enable").json()
    assert enabled["activation"]["ok"] is True and enabled["schedule"]["enabled"] is True
    assert enabled["schedule"]["next_run_at"].startswith("2026-10-28T07:00")

    assert client.portal.call(ctx.schedules.tick) == []  # type: ignore[attr-defined]
    clock.value = utc(2026, 10, 28, 7, 1)
    started = client.portal.call(ctx.schedules.tick)  # type: ignore[attr-defined]
    assert len(started) == 1
    run = poll(client, f"/api/runs/{started[0]}", ("succeeded",))
    assert run["trigger"] == "schedule" and run["trigger_ref"] == created["id"]
    # A missing optional value is null, so later steps can tell it is missing.
    assert run["values"] == {"kunde": "Plan", "betrag": None}
    state = client.get(f"/api/schedules/{created['id']}").json()
    assert state["runs"] == 1 and state["next_run_at"].startswith("2026-10-29T07:00")

    # Nodestro was not running for three days: slots are missed, by default skipped.
    clock.value = utc(2026, 11, 1, 12, 0)
    assert client.portal.call(ctx.schedules.tick) == []  # type: ignore[attr-defined]
    state = client.get(f"/api/schedules/{created['id']}").json()
    assert state["missed_total"] == 4 and len(state["missed_slots"]) == 4
    assert state["next_run_at"].startswith("2026-11-02T07:00")

    # With "run_once" the missed slots are caught up by one run.
    client.put(f"/api/schedules/{created['id']}", json={**body, "values": {"kunde": "Plan"}, "missed": "run_once"})
    clock.value = utc(2026, 11, 4, 12, 0)
    assert len(client.portal.call(ctx.schedules.tick)) == 1  # type: ignore[attr-defined]


def test_schedules_need_a_budget_and_survive_a_restart(make_app: Any, tmp_path: Any) -> None:
    client = make_app({"WORKER": [answer]})
    workflow_id = form_workflow(client)
    client.put("/api/settings", json={"default_budget_usd": None})
    body = {
        "workflow_id": workflow_id,
        "kind": "once",
        "at": "08:00",
        "start_date": "2099-01-01",
        "values": {"kunde": "A"},
    }
    created = client.post("/api/schedules", json=body).json()
    result = client.post(f"/api/schedules/{created['id']}/enable").json()
    assert [item["code"] for item in result["activation"]["problems"]] == ["schedule.no_budget"]
    client.put("/api/settings", json={"default_budget_usd": 1.0})
    assert client.post(f"/api/schedules/{created['id']}/enable").json()["schedule"]["enabled"] is True

    from nodestro_api.context import AppConfig, Nodestro

    data_dir = client.app.state.nodestro.config.data_dir  # type: ignore[attr-defined]
    # A restart: one process per data directory, so the first one stops before the next one opens it.
    client.app.state.nodestro.close()  # type: ignore[attr-defined]
    reopened = Nodestro(AppConfig(data_dir=data_dir))
    try:
        stored = reopened.schedules.get(created["id"])
        assert stored.enabled is True and stored.next_run_at is not None
    finally:
        reopened.close()
