"""A test case proposed from a failed run (Q-4): redacted, with its origin, stored only when saved."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from test_automation import answer, form_workflow, poll

TOKEN = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"


def failed_run(client: TestClient, workflow_id: str, **body: Any) -> dict[str, Any]:
    started = client.post(f"/api/workflows/{workflow_id}/runs", json=body)
    assert started.status_code in (200, 201), started.text
    run = poll(client, f"/api/runs/{started.json()['id']}", ("succeeded", "failed"))
    assert run["status"] == "failed", run["status"]
    return run


def propose(client: TestClient, run_id: str, **body: Any) -> Any:
    return client.post(f"/api/runs/{run_id}/test-case", json=body)


def test_a_failed_run_becomes_a_redacted_proposal(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 4})
    workflow_id = form_workflow(client)
    values = {"kunde": "FAIL anna@example.com", "betrag": 12}
    run = failed_run(client, workflow_id, input=f"Key {TOKEN}", values=values)

    response = propose(client, run["id"])
    assert response.status_code == 200, response.text
    proposal = response.json()
    case = proposal["case"]
    assert proposal["workflow_id"] == workflow_id
    assert case["values"] == {"kunde": "FAIL [EMAIL_1]", "betrag": 12}
    assert TOKEN not in case["input"] and "[API_KEY_1]" in case["input"]
    assert case["checks"] == [] and case["judge_criteria"] == ""
    origin = case["origin"]
    assert origin["run_id"] == run["id"] and origin["run_status"] == "failed"
    assert origin["personal_data"] is True and origin["replaced"] == {"api_key": 1, "email": 1}
    assert origin["error_code"] == run["error_code"]
    # Nothing was stored.
    assert client.get("/api/benchmarks").json() == []

    # Personal data can be kept; credentials are always replaced.
    kept = propose(client, run["id"], personal_data=False).json()["case"]
    assert kept["values"]["kunde"] == "FAIL anna@example.com"
    assert TOKEN not in kept["input"] and kept["origin"]["personal_data"] is False

    # Saved into a series, the case keeps its origin and runs like any other.
    created = client.post(
        "/api/benchmarks", json={"name": "Regressions", "workflow_id": workflow_id, "cases": [{**case, "name": "Anna"}]}
    )
    assert created.status_code == 201, created.text
    stored = created.json()["cases"][0]
    assert stored["origin"]["run_id"] == run["id"]
    again = client.put(f"/api/benchmarks/{created.json()['id']}", json=created.json())
    assert again.json()["cases"][0]["origin"]["run_id"] == run["id"]


def test_only_failed_runs_of_this_workspace(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 2})
    workflow_id = form_workflow(client)
    started = client.post(f"/api/workflows/{workflow_id}/runs", json={"values": {"kunde": "Alpha"}})
    run = poll(client, f"/api/runs/{started.json()['id']}", ("succeeded", "failed"))
    assert run["status"] == "succeeded"
    refused = propose(client, run["id"])
    assert refused.status_code == 400 and refused.json()["code"] == "testcase.run_not_failed"
    assert propose(client, "run_missing").status_code == 404


def test_form_problems_and_missing_files_are_reported(make_app: Any) -> None:
    client = make_app({"WORKER": [answer] * 2})
    workflow_id = form_workflow(client)
    run = failed_run(client, workflow_id, values={"kunde": "FAIL"})
    # The form changed since the run: the proposal says what no longer fits.
    current = client.get(f"/api/workflows/{workflow_id}").json()
    start = next(node for node in current["nodes"] if node["type"] == "start")
    start["start"]["fields"].append({"name": "datum", "label": "Datum", "type": "text", "required": True})
    assert client.put(f"/api/workflows/{workflow_id}", json=current).status_code == 200
    proposal = propose(client, run["id"]).json()
    assert [problem["field"] for problem in proposal["problems"]] == ["datum"]
    assert proposal["missing_files"] == []
