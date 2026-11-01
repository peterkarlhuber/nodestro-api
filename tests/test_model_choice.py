"""Choosing an agent's model from measured test series runs (AO M6): only recorded measurements decide."""

from __future__ import annotations

from typing import Any

from conftest import text
from test_automation import form_workflow, poll

HAIKU = "anthropic:claude-haiku-4-5"


def series(client: Any, workflow_id: str, cases: int = 2) -> str:
    body = {
        "workflow_id": workflow_id,
        "cases": [{"name": f"C{n}", "values": {"kunde": f"K{n}"}} for n in range(cases)],
    }
    return str(client.post("/api/benchmarks", json=body).json()["id"])


def measure(client: Any, suite_id: str, **body: Any) -> dict[str, Any]:
    started = client.post(f"/api/benchmarks/{suite_id}/runs", json=body)
    assert started.status_code == 201, started.text
    return poll(client, f"/api/benchmark-runs/{started.json()['id']}", ("finished",))


def choice(client: Any, suite_id: str, **params: Any) -> dict[str, Any]:
    response = client.get(f"/api/benchmarks/{suite_id}/model-choice", params={"node_id": "worker", **params})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def by_model(failing: str | None = None) -> Any:
    def answer(request: dict[str, Any]) -> Any:
        if request["model"] == failing:
            return text("…", stop_reason="max_tokens", model=request["model"])
        return text('{"echo": "x", "ok": true}', model=request["model"])

    return answer


def test_without_measurements_the_configured_model_stays(make_app: Any) -> None:
    client = make_app()
    workflow_id = form_workflow(client)
    suite_id = series(client, workflow_id)
    found = choice(client, suite_id)
    assert found["outcome"] == "no_measurement" and found["recommended"] is None
    assert found["current_model"].startswith("anthropic:")
    unknown = client.get(f"/api/benchmarks/{suite_id}/model-choice", params={"node_id": "start"})
    assert unknown.status_code == 400 and unknown.json()["code"] == "model_choice.node_unknown"


def test_a_cheaper_model_that_passes_every_case_is_recommended(make_app: Any) -> None:
    client = make_app({"WORKER": [by_model()] * 4})
    workflow_id = form_workflow(client)
    suite_id = series(client, workflow_id)
    measure(client, suite_id)
    # Only the variant was measured so far? No: the baseline first; now the variant.
    variant = measure(client, suite_id, models={"worker": HAIKU}, max_cost_usd=1)
    found = choice(client, suite_id)
    models = {item["model"]: item for item in found["measurements"]}
    assert set(models) == {found["current_model"], HAIKU}
    assert models[HAIKU]["benchmark_run_id"] == variant["id"] and models[HAIKU]["pass_rate"] == 1.0
    assert found["required_pass_rate"] == 1.0
    assert models[HAIKU]["cost_per_case_usd"] is not None
    assert models[HAIKU]["cost_per_case_usd"] < models[found["current_model"]]["cost_per_case_usd"]
    assert found["outcome"] == "switch" and found["recommended"] == HAIKU
    assert "model_choice.few_cases" in found["warnings"]
    # Nothing changed on its own.
    saved = client.get(f"/api/workflows/{workflow_id}").json()["nodes"][1]["agent"]["model"]
    assert saved == found["current_model"]


def test_a_model_that_fails_cases_is_excluded_and_the_measurement_follows_the_workflow(make_app: Any) -> None:
    client = make_app({"WORKER": [by_model(failing="claude-haiku-4-5")] * 4})
    workflow_id = form_workflow(client)
    suite_id = series(client, workflow_id)
    measure(client, suite_id)
    measure(client, suite_id, models={"worker": HAIKU}, max_cost_usd=1)
    found = choice(client, suite_id)
    haiku = next(item for item in found["measurements"] if item["model"] == HAIKU)
    assert haiku["pass_rate"] == 0.0 and set(haiku["excluded"]) >= {"model.pass_rate_low", "model.new_failures"}
    assert found["outcome"] == "keep" and found["recommended"] is None
    # An explicit least pass rate of 0 still refuses new failures unless that is switched off too.
    assert choice(client, suite_id, min_pass_rate=0)["outcome"] == "keep"
    loose = choice(client, suite_id, min_pass_rate=0, no_new_failures=False)
    assert loose["outcome"] == "switch" and loose["recommended"] == HAIKU

    # After a change of the prompt, the old measurements say nothing about the workflow any more.
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    workflow["nodes"][1]["agent"]["task_template"] = "Etwas anderes mit {{form.kunde}}"
    assert client.put(f"/api/workflows/{workflow_id}", json=workflow).status_code == 200
    changed = choice(client, suite_id)
    assert changed["outcome"] == "no_measurement" and changed["skipped"] == {"workflow_changed": 2}

    # Moving a node or renaming the workflow does not.
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    workflow["nodes"][1]["agent"]["task_template"] = "Bearbeite {{form.kunde}}"
    workflow["nodes"][1]["position"] = {"x": 999, "y": 1}
    workflow["name"] = "Umbenannt"
    assert client.put(f"/api/workflows/{workflow_id}", json=workflow).status_code == 200
    assert choice(client, suite_id)["skipped"] == {}


def test_edited_cases_and_unknown_baselines(make_app: Any) -> None:
    client = make_app({"WORKER": [by_model()] * 4})
    workflow_id = form_workflow(client)
    suite_id = series(client, workflow_id)
    measure(client, suite_id, models={"worker": HAIKU}, max_cost_usd=1)
    # Only the variant was measured: without a baseline or a least pass rate, nothing is recommended.
    found = choice(client, suite_id)
    assert found["outcome"] == "no_baseline" and found["required_pass_rate"] is None
    assert choice(client, suite_id, min_pass_rate=1)["recommended"] == HAIKU
    suite = client.get(f"/api/benchmarks/{suite_id}").json()
    suite["cases"][0]["values"] = {"kunde": "anders"}
    assert client.put(f"/api/benchmarks/{suite_id}", json=suite).status_code == 200
    assert choice(client, suite_id)["skipped"] == {"cases_changed": 1}


def test_an_unknown_cost_never_wins_on_cost_and_old_records_are_not_used(make_app: Any) -> None:
    from nodestro_api.benchmarks import RUNS, BenchmarkRun

    client = make_app({"WORKER": [by_model()] * 4})
    workflow_id = form_workflow(client)
    suite_id = series(client, workflow_id)
    measure(client, suite_id)
    variant = measure(client, suite_id, models={"worker": HAIKU}, max_cost_usd=1)
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    stored = store.get(RUNS, variant["id"], BenchmarkRun)
    stored.results[0].cost_usd = None
    store.put(RUNS, stored.id, stored)
    found = choice(client, suite_id)
    haiku = next(item for item in found["measurements"] if item["model"] == HAIKU)
    assert haiku["cost_per_case_usd"] is None and haiku["excluded"] == ["model.cost_unknown"]
    assert found["outcome"] == "keep"
    # By duration or quality the cost does not decide; the measured duration and the pass rate do.
    assert choice(client, suite_id, objective="duration")["outcome"] in ("keep", "switch")
    quality = choice(client, suite_id, objective="quality")
    assert quality["outcome"] == "keep"  # same pass rate: the known cost of the configured model ranks first

    # A test run recorded before AO M6 has no basis: it is counted, never used.
    stored.basis = None
    store.put(RUNS, stored.id, stored)
    assert choice(client, suite_id)["skipped"] == {"older_record": 1}
