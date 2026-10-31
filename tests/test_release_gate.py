"""The opt-in release gate of a publication (Q-3): critical items block, others need a recorded reason."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from test_automation import answer, form_workflow, poll

GOOD = {"id": "rule_ok", "label": "OK", "field": "ok", "operator": "is_true"}
BAD = {"id": "rule_echo", "label": "Echo", "field": "echo", "operator": "equals", "value": "nobody"}


def suite(client: TestClient, workflow_id: str, *checks: dict[str, Any]) -> str:
    cases = [{"name": name, "values": {"kunde": name}, "checks": list(checks)} for name in ("Alpha", "Beta")]
    response = client.post("/api/benchmarks", json={"name": "Gate", "workflow_id": workflow_id, "cases": cases})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def run_suite(client: TestClient, suite_id: str, **body: Any) -> dict[str, Any]:
    started = client.post(f"/api/benchmarks/{suite_id}/runs", json=body)
    assert started.status_code == 201, started.text
    return poll(client, f"/api/benchmark-runs/{started.json()['id']}", ("finished",))


def gate_of(client: TestClient, workflow_id: str, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/workflows/{workflow_id}/api/gate", json=body)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def publish(client: TestClient, workflow_id: str, **body: Any) -> Any:
    return client.post(f"/api/workflows/{workflow_id}/api/publish", json=body)


def refused(response: Any) -> str:
    assert response.status_code == 400, response.text
    return str(response.json()["code"])


def test_without_a_gate_publishing_is_unchanged(make_app: Any) -> None:
    client = make_app()
    workflow_id = form_workflow(client)
    assert gate_of(client, workflow_id)["status"] == "off"
    first = publish(client, workflow_id)
    assert first.status_code == 201, first.text
    assert first.json()["publication"]["gate"] is None
    assert first.json()["release"]["gate"] is None
    release = client.get(f"/api/publications/{first.json()['publication']['id']}/releases/1").json()
    assert release["gate"] is None


def test_critical_items_block_and_cannot_be_overridden(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 8})
    workflow_id = form_workflow(client)
    suite_id = suite(client, workflow_id, GOOD)
    gate = {"suite_ids": [suite_id]}

    # Only test series of this workflow can be named.
    other = form_workflow(client)
    assert refused(publish(client, other, gate=gate)) == "gate.suite_unknown"

    # Never tested: blocked, and a reason does not help.
    report = gate_of(client, workflow_id, gate=gate)
    assert report["status"] == "blocked"
    assert [(item["kind"], item["code"]) for item in report["items"]] == [("critical", "gate.suite_untested")]
    reason = {"reason": "Tested by hand, release anyway"}
    assert refused(publish(client, workflow_id, gate=gate, override=reason)) == "gate.blocked"
    assert client.get("/api/publications").json() == []

    # A run with other models tested a variant, not this version.
    run_suite(client, suite_id, models={"worker": "anthropic:claude-haiku-4-5"}, max_cost_usd=1)
    assert gate_of(client, workflow_id, gate=gate)["items"][0]["code"] == "gate.suite_variant_only"

    # Tested and passing: released, with the gate's report kept on the release.
    run_suite(client, suite_id)
    report = gate_of(client, workflow_id, gate=gate)
    assert report["status"] == "passed"
    assert {item["code"] for item in report["items"]} == {"gate.tested", "gate.pass_rate"}
    first = publish(client, workflow_id, gate=gate)
    assert first.status_code == 201, first.text
    publication = first.json()["publication"]
    assert publication["gate"]["suite_ids"] == [suite_id]
    assert first.json()["release"]["gate"] == "passed"
    assert [event["kind"] for event in publication["history"]][:2] == ["gate_on", "published"]

    # A new saved version is not covered by the old run.
    current = client.get(f"/api/workflows/{workflow_id}").json()
    current["description"] = "changed"
    assert client.put(f"/api/workflows/{workflow_id}", json=current).status_code == 200
    report = gate_of(client, workflow_id)
    assert report["status"] == "blocked" and report["items"][0]["code"] == "gate.suite_outdated"
    assert refused(publish(client, workflow_id, override=reason)) == "gate.blocked"

    # Run again, then change the suite: the run no longer counts.
    run_suite(client, suite_id)
    assert gate_of(client, workflow_id)["status"] == "passed"
    stored = client.get(f"/api/benchmarks/{suite_id}").json()
    stored["description"] = "more cases soon"
    assert client.put(f"/api/benchmarks/{suite_id}", json=stored).status_code == 200
    assert gate_of(client, workflow_id)["items"][0]["code"] == "gate.suite_changed"

    # Switching the gate off is recorded; publishing then works as before.
    off = client.put(f"/api/publications/{publication['id']}/gate", json={"gate": None})
    assert off.status_code == 200, off.text
    assert off.json()["history"][-1]["kind"] == "gate_off"
    again = publish(client, workflow_id)
    assert again.status_code == 201, again.text
    assert again.json()["release"]["gate"] is None


def test_failed_checks_need_a_recorded_reason(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 4})
    workflow_id = form_workflow(client)
    suite_id = suite(client, workflow_id, BAD)
    run_suite(client, suite_id)
    gate = {"suite_ids": [suite_id], "min_pass_rate": 0.5, "judge_min_score": 0.8}
    report = gate_of(client, workflow_id, gate=gate)
    assert report["status"] == "overridable"
    items = {item["code"]: item for item in report["items"]}
    assert items["gate.pass_rate"]["kind"] == "deterministic"
    assert items["gate.pass_rate"]["value"] == 0 and items["gate.pass_rate"]["passed"] is False
    # No AI verdicts in this run: reported as a missing, probabilistic item, never as a deterministic one.
    assert items["gate.judge_missing"]["kind"] == "judge" and items["gate.judge_missing"]["probabilistic"] is True

    assert refused(publish(client, workflow_id, gate=gate)) == "gate.override_required"
    short = publish(client, workflow_id, gate=gate, override={"reason": "   ok      "})
    assert short.status_code == 422, short.text
    reason = "Known wording difference, approved by the team on 2026-10-31"
    released = publish(client, workflow_id, gate=gate, override={"reason": f"  {reason} "})
    assert released.status_code == 201, released.text
    assert released.json()["release"]["gate"] == "overridden"
    publication = released.json()["publication"]
    override = next(event for event in publication["history"] if event["kind"] == "gate_override")
    assert override["details"]["reason"] == reason and override["details"]["by"] == "local"
    assert set(override["details"]["failed"]) == {"gate.pass_rate", "gate.judge_missing"}
    release = client.get(f"/api/publications/{publication['id']}/releases/1").json()
    assert release["gate"]["override_reason"] == reason
    assert release["gate"]["report"]["status"] == "overridable"


def test_changing_the_gate_is_validated_and_recorded(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 2})
    workflow_id = form_workflow(client)
    publication = publish(client, workflow_id).json()["publication"]
    path = f"/api/publications/{publication['id']}/gate"
    assert refused(client.put(path, json={"gate": {"suite_ids": ["suite_missing"]}})) == "gate.suite_unknown"
    assert client.put(path, json={"gate": {"suite_ids": []}}).status_code == 422
    suite_id = suite(client, workflow_id, GOOD)
    on = client.put(path, json={"gate": {"suite_ids": [suite_id], "min_pass_rate": 0.9}}).json()
    assert on["gate"]["min_pass_rate"] == 0.9 and on["history"][-1]["kind"] == "gate_on"
    changed = client.put(path, json={"gate": {"suite_ids": [suite_id]}}).json()
    assert changed["history"][-1]["kind"] == "gate_changed"
    same = client.put(path, json={"gate": {"suite_ids": [suite_id]}}).json()
    assert len(same["history"]) == len(changed["history"])
    # A deleted test series blocks: the evidence is gone.
    assert client.delete(f"/api/benchmarks/{suite_id}").status_code == 204
    assert gate_of(client, workflow_id)["items"][0]["code"] == "gate.suite_missing"
