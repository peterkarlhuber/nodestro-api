"""Decisions, approvals, input forms, sub-workflows and continued runs (no API calls)."""

from __future__ import annotations

from collections.abc import Callable
import itertools
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.conditions import decide, evaluate
from nodestro_runtime.forms import FormError, check_values, prepare
from nodestro_runtime.schemas import ConditionConfig, ConditionRule, InputField
import pytest

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

from conftest import ScriptedClient, parsed, text
from test_runs import agent_node


def wait(client: TestClient, run_id: str, timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not settle: {run['status']}")


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    return {"id": f"{source}-{target}-{handle or ''}", "source": source, "target": target, "source_handle": handle}


def save(client: TestClient, nodes: list[dict[str, Any]], edges: list[dict[str, Any]], **extra: Any) -> str:
    response = client.post(
        "/api/workflows", json={"name": extra.pop("name", "Flow"), "nodes": nodes, "edges": edges, **extra}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def run(client: TestClient, workflow_id: str, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "Anfrage", **body})
    assert response.status_code == 201, response.text
    return wait(client, response.json()["id"])


def systems(client: TestClient) -> list[str]:
    return [str(call.get("system", "")) for call in client.fake.messages.calls]  # type: ignore[attr-defined]


CLASSIFIER = agent_node(
    "classify",
    "CLASSIFY",
    output={"mode": "structured", "name": "Kind", "fields": [{"name": "category", "type": "string"}]},
)


def router(mode: str = "first", on_invalid: str = "default", field: str = "category") -> dict[str, Any]:
    return {
        "id": "route",
        "type": "condition",
        "label": "Routing",
        "condition": {
            "mode": mode,
            "on_invalid": on_invalid,
            "default_label": "Sonst",
            "rules": [
                {"id": "complaint", "label": "Beschwerde", "field": field, "operator": "equals", "value": "complaint"},
                {
                    "id": "billing",
                    "label": "Rechnung",
                    "field": field,
                    "operator": "in",
                    "value": ["billing", "invoice"],
                },
            ],
        },
    }


def routed_workflow(client: TestClient, **router_options: Any) -> str:
    return save(
        client,
        [
            {"id": "start", "type": "start"},
            CLASSIFIER,
            router(**router_options),
            agent_node("complaints", "COMPLAINTS", task_template="Bearbeite: {{Classify}}"),
            agent_node("billing", "BILLING"),
            agent_node("general", "GENERAL"),
            agent_node("final", "FINAL", task_template="Fasse zusammen: {{input}} / {{Billing}}"),
            {"id": "out", "type": "output"},
        ],
        [
            edge("start", "classify"),
            edge("classify", "route"),
            edge("route", "complaints", "complaint"),
            edge("route", "billing", "billing"),
            edge("route", "general", "default"),
            edge("complaints", "final"),
            edge("billing", "final"),
            edge("general", "final"),
            edge("final", "out"),
        ],
    )


def category(value: str) -> Callable[[dict[str, Any]], Any]:
    return parsed(lambda model: model(category=value))


# ---- rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operator", "value", "result", "expected"),
    [
        ("equals", "Complaint", {"category": "complaint"}, True),
        ("equals", 3, {"category": "3.0"}, True),
        ("not_equals", "x", {"category": "y"}, True),
        ("contains", "rechnung", "Frage zur Rechnung 42", True),
        ("contains", "b", {"category": ["a", "b"]}, True),
        ("gt", 100, {"category": 120}, True),
        ("lte", "2026-05-01", {"category": "2026-04-30"}, True),
        ("gt", 10, {"category": "viel"}, None),
        ("is_true", None, {"category": "ja"}, True),
        ("is_false", None, {"category": False}, True),
        ("in", "billing, invoice", {"category": "Invoice"}, True),
        ("not_in", ["a"], {"category": "b"}, True),
        ("is_empty", None, {}, True),
        ("is_not_empty", None, {"category": "x"}, True),
        ("equals", "x", {"other": 1}, None),
        ("starts_with", "Hal", {"category": "Hallo"}, True),
    ],
)
def test_rules_are_deterministic(operator: str, value: Any, result: Any, expected: bool | None) -> None:
    rule = ConditionRule(
        field="category" if operator not in ("contains",) or isinstance(result, dict) else "",
        operator=operator,
        value=value,
    )  # type: ignore[arg-type]
    assert evaluate(rule, result) is expected


def test_decision_falls_back_with_a_reason() -> None:
    config = ConditionConfig(rules=[ConditionRule(id="r", field="score", operator="gt", value=5)])
    assert decide(config, {"score": 7}).branches == ["r"]
    record = decide(config, {"score": 2})
    assert record.branches == ["default"] and record.fallback_code == "condition.no_match"
    record = decide(config, {"other": 1})
    assert record.branches == ["default"] and record.fallback_code == "condition.field_missing"
    record = decide(config, '```json\n{"score": "abc"}\n```')
    assert record.fallback_code == "condition.not_evaluable"
    assert decide(config, None, available=False).fallback_code == "condition.no_input"


def test_all_mode_opens_every_matching_branch() -> None:
    config = ConditionConfig(
        mode="all",
        rules=[
            ConditionRule(id="a", field="x", operator="gt", value=1),
            ConditionRule(id="b", field="x", operator="lt", value=10),
        ],
    )
    assert decide(config, {"x": 5}).branches == ["a", "b"]


# ---- forms -------------------------------------------------------------------------


FIELDS = [
    InputField(name="destination", label="Reiseziel", type="text", required=True),
    InputField(name="start", label="Beginn", type="date", required=True),
    InputField(name="people", label="Personen", type="number", default=2),
    InputField(name="pets", label="Haustiere", type="boolean"),
    InputField(name="style", label="Stil", type="select", options=["Aktiv", "Entspannt"]),
]


def test_form_values_are_checked_and_typed() -> None:
    values, errors = check_values(FIELDS, {"destination": "Rom", "start": "2026-05-01", "pets": "ja", "style": "aktiv"})
    assert not errors
    assert values == {"destination": "Rom", "start": "2026-05-01", "people": 2, "pets": True, "style": "Aktiv"}
    _, errors = check_values(FIELDS, {"start": "morgen", "people": "viele", "style": "Wild"})
    assert {error.code for error in errors} == {"form.required", "form.not_date", "form.not_number", "form.not_option"}


def test_form_builds_a_readable_input() -> None:
    prepared = prepare(FIELDS, {"destination": "Rom", "start": "2026-05-01"}, "Bitte mit Museen", [])
    assert prepared.input.startswith("Reiseziel: Rom\nBeginn: 2026-05-01\nPersonen: 2")
    assert prepared.input.endswith("Bitte mit Museen")
    with pytest.raises(FormError) as caught:
        prepare(FIELDS, {}, "", [])
    assert caught.value.code == "form.invalid" and caught.value.params["count"] == 2


def test_run_with_form_passes_typed_values(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"PLAN": [text("Plan")]})
    start = {"id": "start", "type": "start", "start": {"fields": [field.model_dump() for field in FIELDS]}}
    workflow_id = save(
        client,
        [
            start,
            agent_node("plan", "PLAN", task_template="Ziel {{form.destination}} für {{form.people}}:\n{{input}}"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "plan"), edge("plan", "out")],
    )
    bad = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "", "values": {"start": "x"}})
    assert bad.status_code == 400 and bad.json()["code"] == "form.invalid"
    result = run(client, workflow_id, input="", values={"destination": "Rom", "start": "2026-05-01", "people": "3"})
    assert result["status"] == "succeeded", result
    assert result["values"]["people"] == 3
    prompt = result["nodes"]["plan"]["prompt"]
    assert prompt.startswith("Ziel Rom für 3:")
    assert '"destination": "Rom"' in prompt
    assert result["input"].startswith("Reiseziel: Rom")


def test_plain_workflows_keep_free_text(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"A": [text("ok")]})
    workflow_id = save(client, [{"id": "start", "type": "start"}, agent_node("a", "A")], [edge("start", "a")])
    result = run(client, workflow_id, input="Nur Text")
    assert result["nodes"]["a"]["prompt"] == "Nur Text" and result["values"] == {}


# ---- decisions in runs -------------------------------------------------------------


def test_router_runs_only_the_chosen_branch(make_app: Callable[..., TestClient]) -> None:
    client = make_app(
        {"CLASSIFY": [category("complaint")], "COMPLAINTS": [text("Entschuldigung")], "FINAL": [text("Fertig")]}
    )
    workflow_id = routed_workflow(client)
    result = run(client, workflow_id)
    assert result["status"] == "succeeded", result
    nodes = result["nodes"]
    assert nodes["route"]["decision"]["branches"] == ["complaint"]
    assert nodes["route"]["decision"]["labels"] == ["Beschwerde"]
    assert nodes["complaints"]["status"] == "succeeded"
    # Branches not chosen are skipped on purpose: no request, no error.
    for skipped in ("billing", "general"):
        assert nodes[skipped]["status"] == "skipped" and nodes[skipped]["error"] is None
    assert not any("BILLING" in system or "GENERAL" in system for system in systems(client))
    # The merge runs with what arrived; the classifier's result passes through the decision.
    assert nodes["complaints"]["prompt"] == 'Bearbeite: {\n  "category": "complaint"\n}'
    assert nodes["final"]["prompt"] == "Fasse zusammen: Entschuldigung / -"
    assert result["output"] == "Fertig"


def test_router_takes_the_default_branch(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"CLASSIFY": [category("question")], "GENERAL": [text("Info")], "FINAL": [text("Fertig")]})
    result = run(client, routed_workflow(client))
    assert result["status"] == "succeeded"
    assert result["nodes"]["route"]["decision"]["branches"] == ["default"]
    assert result["nodes"]["route"]["decision"]["fallback_code"] == "condition.no_match"
    assert result["nodes"]["general"]["status"] == "succeeded"


def test_invalid_data_can_fail_the_decision(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"CLASSIFY": [category("x")]})
    result = run(client, routed_workflow(client, on_invalid="fail", field="missing"))
    assert result["status"] == "failed"
    assert result["nodes"]["route"]["status"] == "failed"
    assert result["nodes"]["route"]["error_code"] == "condition.field_missing"
    assert result["nodes"]["final"]["status"] == "blocked"


def test_workflow_check_reports_branch_problems(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    body = {
        "nodes": [
            {"id": "start", "type": "start"},
            CLASSIFIER,
            {
                "id": "route",
                "type": "condition",
                "condition": {"rules": [{"id": "r1", "operator": "gt", "value": "viel"}]},
            },
            agent_node("a", "A"),
            agent_node("b", "B"),
            agent_node("other", "O"),
            agent_node("merge", "M"),
        ],
        "edges": [
            edge("start", "classify"),
            edge("classify", "route"),
            edge("route", "a", "nonsense"),
            edge("route", "b", "r1"),
            edge("start", "other"),
            edge("b", "merge"),
            edge("other", "merge"),
        ],
    }
    check = client.post("/api/workflows/check", json=body).json()
    codes = {item["code"] for item in check["problems"]}
    assert {"workflow.branch_unknown", "condition.rule_not_number", "check.merge_partial"} <= codes
    assert "check.default_unconnected" in codes


# ---- approvals ---------------------------------------------------------------------


def approval_workflow(client: TestClient, on_reject: str = "branch") -> str:
    return save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("draft", "DRAFT"),
            {
                "id": "check",
                "type": "approval",
                "label": "Angebot prüfen",
                "approval": {
                    "instructions": "Passt der Preis?",
                    "on_reject": on_reject,
                    "require_comment_on_reject": True,
                },
            },
            agent_node("send", "SEND", task_template="Finalisiere: {{input}}"),
            agent_node("revise", "REVISE"),
            {"id": "out", "type": "output"},
        ],
        [
            edge("start", "draft"),
            edge("draft", "check"),
            edge("check", "send", "approved"),
            edge("check", "revise", "rejected"),
            edge("send", "out"),
            edge("revise", "out"),
        ],
    )


def test_approval_pauses_and_continues_after_approval(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("Angebot 1000 EUR")], "SEND": [text("Final")]})
    workflow_id = approval_workflow(client)
    paused = run(client, workflow_id)
    assert paused["status"] == "paused"
    assert paused["nodes"]["check"]["status"] == "awaiting_approval"
    assert paused["nodes"]["send"]["status"] == "waiting"
    assert paused["finished_at"] is None
    pending = client.get("/api/approvals").json()
    assert [(item["label"], item["items"][0]["value"]) for item in pending] == [("Angebot prüfen", "Angebot 1000 EUR")]
    # Waiting does not approve anything.
    time.sleep(0.2)
    assert client.get(f"/api/runs/{paused['id']}").json()["status"] == "paused"
    assert not any("SEND" in system for system in systems(client))
    response = client.post(
        f"/api/runs/{paused['id']}/approvals/check", json={"decision": "approved", "comment": "Rabatt ok"}
    )
    assert response.status_code == 200, response.text
    done = wait(client, paused["id"])
    assert done["status"] == "succeeded", done
    assert done["nodes"]["check"]["approval"]["decision"] == "approved"
    assert done["nodes"]["revise"]["status"] == "skipped"
    prompt = done["nodes"]["send"]["prompt"]
    assert "Angebot 1000 EUR" in prompt and "Rabatt ok" in prompt
    assert done["output"] == "Final"
    assert client.get("/api/approvals").json() == []
    # The same approval cannot be decided twice.
    again = client.post(f"/api/runs/{paused['id']}/approvals/check", json={"decision": "rejected"})
    assert again.status_code == 400 and again.json()["code"] == "approval.not_pending"


def test_rejection_follows_its_branch_or_ends_the_run(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("A"), text("B")], "REVISE": [text("Neu")]})
    paused = run(client, approval_workflow(client))
    missing = client.post(f"/api/runs/{paused['id']}/approvals/check", json={"decision": "rejected"})
    assert missing.status_code == 400 and missing.json()["code"] == "approval.comment_required"
    client.post(f"/api/runs/{paused['id']}/approvals/check", json={"decision": "rejected", "comment": "zu teuer"})
    done = wait(client, paused["id"])
    assert done["status"] == "succeeded"
    assert done["nodes"]["send"]["status"] == "skipped" and done["nodes"]["revise"]["status"] == "succeeded"

    stopped = run(client, approval_workflow(client, on_reject="stop"))
    client.post(f"/api/runs/{stopped['id']}/approvals/check", json={"decision": "rejected", "comment": "nein"})
    ended = wait(client, stopped["id"])
    assert ended["status"] == "cancelled" and ended["error_code"] == "run.approval_rejected"


def test_pending_approvals_survive_a_restart(tmp_path: Path) -> None:
    fake = ScriptedClient({"DRAFT": [text("Entwurf")], "SEND": [text("Gesendet")]})
    config = AppConfig(data_dir=tmp_path / "data")
    with TestClient(create_app(config, client_factory=lambda _key: fake), base_url="http://127.0.0.1") as client:
        client.fake = fake  # type: ignore[attr-defined]
        run_id = run(client, approval_workflow(client))["id"]
    with TestClient(create_app(config, client_factory=lambda _key: fake), base_url="http://127.0.0.1") as client:
        pending = client.get("/api/approvals").json()
        assert [item["run_id"] for item in pending] == [run_id]
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "paused"
        client.post(f"/api/runs/{run_id}/approvals/check", json={"decision": "approved"})
        done = wait(client, run_id)
        assert done["status"] == "succeeded" and done["output"] == "Gesendet"


def test_paused_run_can_be_cancelled(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("Entwurf")]})
    paused = run(client, approval_workflow(client))
    assert client.post(f"/api/runs/{paused['id']}/cancel").json() == {"cancelled": True}
    cancelled = client.get(f"/api/runs/{paused['id']}").json()
    assert cancelled["status"] == "cancelled" and cancelled["nodes"]["check"]["status"] == "cancelled"
    assert client.get("/api/approvals").json() == []


def test_interrupted_runs_are_marked_on_start(tmp_path: Path) -> None:
    from nodestro_runtime.schemas import NodeRun, Run, Workflow
    from nodestro_runtime.storage import Store

    config = AppConfig(data_dir=tmp_path / "data")
    config.data_dir.mkdir(parents=True)
    store = Store(config.data_dir / "nodestro.db")
    workflow = Workflow(name="W")
    store.put(
        "runs",
        "run_x",
        Run(
            id="run_x",
            workflow_id=workflow.id,
            workflow_name="W",
            workflow=workflow,
            status="running",
            nodes={"a": NodeRun(status="running")},
        ),
    )
    store.close()
    with TestClient(
        create_app(config, client_factory=lambda _key: ScriptedClient({})), base_url="http://127.0.0.1"
    ) as client:
        stored = client.get("/api/runs/run_x").json()
        assert stored["status"] == "failed" and stored["error_code"] == "run.interrupted"
        assert stored["nodes"]["a"]["status"] == "failed"


# ---- sub-workflows -----------------------------------------------------------------


def child_workflow(client: TestClient, *, approval: bool = False) -> str:
    nodes: list[dict[str, Any]] = [
        {"id": "start", "type": "start", "start": {"fields": [{"name": "topic", "label": "Thema", "required": True}]}},
        agent_node("review", "QUALITY", task_template="Prüfe {{form.topic}}: {{run.input}}"),
        {"id": "out", "type": "output"},
    ]
    edges = [edge("start", "review"), edge("review", "out")]
    if approval:
        nodes.append({"id": "ok", "type": "approval", "label": "Prüfung freigeben"})
        edges = [edge("start", "review"), edge("review", "ok"), edge("ok", "out", "approved")]
    return save(client, nodes, edges, name="Qualitätsprüfung")


def parent_workflow(client: TestClient, child_id: str, **extra: Any) -> str:
    return save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("analyse", "ANALYSE"),
            {
                "id": "qc",
                "type": "subworkflow",
                "label": "QS",
                "subworkflow": {
                    "workflow_id": child_id,
                    "input_template": "Bericht: {{Analyse}}",
                    "values": {"topic": "Stillstand"},
                    **extra,
                },
            },
            agent_node("report", "REPORT", task_template="Bericht mit {{QS}}"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "analyse"), edge("analyse", "qc"), edge("qc", "report"), edge("report", "out")],
        name="Produktion",
    )


def test_subworkflow_runs_as_child_and_returns_its_result(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ANALYSE": [text("Lager defekt")], "QUALITY": [text("geprüft: ok")], "REPORT": [text("Fertig")]})
    child_id = child_workflow(client)
    parent_id = parent_workflow(client, child_id)
    result = run(client, parent_id)
    assert result["status"] == "succeeded", result
    qc = result["nodes"]["qc"]
    assert qc["child_version"] == 1 and qc["output"] == "geprüft: ok"
    assert result["workflow"]["nodes"][2]["subworkflow"]["version"] == 1  # pinned when the run started
    child = client.get(f"/api/runs/{qc['child_run_id']}").json()
    assert child["parent_run_id"] == result["id"] and child["trigger"] == "subworkflow"
    assert child["values"] == {"topic": "Stillstand"}
    assert child["nodes"]["review"]["prompt"] == "Prüfe Stillstand: Thema: Stillstand\n\nBericht: Lager defekt"
    assert result["nodes"]["report"]["prompt"] == "Bericht mit geprüft: ok"
    # The child's cost is part of the parent's node and run; the usage report counts it once.
    assert qc["usage"]["requests"] == 1 and result["usage"]["requests"] == 3
    report = client.get("/api/usage").json()
    assert report["total"]["requests"] == 3 and report["total"]["runs"] == 1
    assert client.get("/api/runs").json()[0]["id"] == result["id"]
    assert len(client.get("/api/runs?include_children=true").json()) == 2


def test_subworkflow_version_stays_pinned(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ANALYSE": [text("x")], "QUALITY": [text("alt")], "REPORT": [text("r")]})
    child_id = child_workflow(client)
    parent_id = parent_workflow(client, child_id, version=1)
    changed = client.get(f"/api/workflows/{child_id}").json()
    changed["nodes"][1]["agent"]["system_prompt"] = "NEW QUALITY"
    client.put(f"/api/workflows/{child_id}", json=changed)
    check = client.post(
        f"/api/workflows/check?workflow_id={parent_id}", json=client.get(f"/api/workflows/{parent_id}").json()
    ).json()
    assert "check.subworkflow_outdated" in {item["code"] for item in check["problems"]}
    result = run(client, parent_id)
    assert result["status"] == "succeeded" and result["nodes"]["qc"]["output"] == "alt"


def test_recursive_subworkflows_are_rejected(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    first = save(client, [{"id": "start", "type": "start"}, agent_node("a", "A")], [edge("start", "a")], name="A")
    second = parent_workflow(client, first)
    looped = client.get(f"/api/workflows/{first}").json()
    looped["nodes"].append({"id": "loop", "type": "subworkflow", "subworkflow": {"workflow_id": second}})
    looped["edges"].append(edge("a", "loop"))
    client.put(f"/api/workflows/{first}", json=looped)
    check = client.post(f"/api/workflows/check?workflow_id={first}", json=looped).json()
    assert "check.subworkflow_cycle" in {item["code"] for item in check["problems"]}
    blocked = client.post(f"/api/workflows/{first}/runs", json={"input": "x"})
    assert blocked.status_code == 400 and blocked.json()["code"] == "workflow.blocked"


def test_subworkflow_missing_field_is_reported(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    child_id = child_workflow(client)
    parent = parent_workflow(client, child_id)
    body = client.get(f"/api/workflows/{parent}").json()
    body["nodes"][2]["subworkflow"]["values"] = {}
    check = client.post(f"/api/workflows/check?workflow_id={parent}", json=body).json()
    assert "check.subworkflow_field_unmapped" in {item["code"] for item in check["problems"]}


def test_approval_inside_a_subworkflow_pauses_the_parent(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ANALYSE": [text("x")], "QUALITY": [text("Prüfbericht")], "REPORT": [text("Ende")]})
    parent_id = parent_workflow(client, child_workflow(client, approval=True))
    paused = run(client, parent_id)
    assert paused["status"] == "paused" and paused["nodes"]["qc"]["status"] == "awaiting_approval"
    [pending] = client.get("/api/approvals").json()
    assert pending["root_run_id"] == paused["id"] and pending["path"] == ["Produktion", "Qualitätsprüfung"]
    client.post(f"/api/runs/{pending['run_id']}/approvals/ok", json={"decision": "approved"})
    done = wait(client, paused["id"])
    assert done["status"] == "succeeded", done
    assert done["nodes"]["qc"]["output"] == "Prüfbericht" and done["output"] == "Ende"
    # Nothing ran twice.
    assert sum("ANALYSE" in system for system in systems(client)) == 1
    assert sum("QUALITY" in system for system in systems(client)) == 1


# ---- continuing failed runs --------------------------------------------------------


def six_steps(client: TestClient) -> str:
    names = ["one", "two", "three", "four", "five", "six"]
    nodes = [{"id": "start", "type": "start"}] + [agent_node(name, name.upper()) for name in names]
    chain = ["start", *names]
    return save(client, nodes, [edge(a, b) for a, b in itertools.pairwise(chain)])


def test_continue_reuses_successful_steps(make_app: Callable[..., TestClient]) -> None:
    script = {name: [text(f"{name} ok")] for name in ("ONE", "TWO", "THREE", "FOUR", "SIX")}
    script["FIVE"] = [text("x", stop_reason="max_tokens"), text("five ok")]
    client = make_app(script)
    workflow_id = six_steps(client)
    failed = run(client, workflow_id)
    assert failed["status"] == "failed" and failed["nodes"]["five"]["status"] == "failed"
    assert failed["nodes"]["six"]["status"] == "blocked"
    plan = client.get(f"/api/runs/{failed['id']}/resume").json()
    assert plan["possible"] and plan["reused"] == 4 and plan["rerun"] == 2
    assert [node["action"] for node in plan["nodes"]] == ["reuse"] * 5 + ["rerun", "run"]
    assert plan["saved_cost_usd"] > 0
    response = client.post(f"/api/runs/{failed['id']}/resume", json={"mode": "continue"})
    assert response.status_code == 201, response.text
    continued = wait(client, response.json()["id"])
    assert continued["status"] == "succeeded", continued
    assert continued["resumed_from"] == failed["id"] and continued["trigger"] == "resume"
    assert continued["nodes"]["four"]["reused_from"] == failed["id"]
    assert continued["nodes"]["four"]["output"] == "FOUR ok"
    # Only the two remaining steps cost anything; the old run stays as it was.
    assert continued["usage"]["requests"] == 2
    assert client.get(f"/api/runs/{failed['id']}").json()["status"] == "failed"
    assert sum("ONE" in system for system in systems(client)) == 1
    report = client.get("/api/usage").json()
    assert report["total"]["requests"] == 7  # five in the failed run (one of them failed), two now


def test_changed_step_runs_again(make_app: Callable[..., TestClient]) -> None:
    script = {
        name: [text(f"{name} ok"), text(f"{name} neu")] for name in ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX")
    }
    script["FIVE"] = [text("x", stop_reason="max_tokens"), text("five ok")]
    client = make_app(script)
    workflow_id = six_steps(client)
    failed = run(client, workflow_id)
    body = client.get(f"/api/workflows/{workflow_id}").json()
    body["nodes"][3]["agent"]["system_prompt"] = "THREE changed"
    client.put(f"/api/workflows/{workflow_id}", json=body)
    plan = client.get(f"/api/runs/{failed['id']}/resume").json()
    assert plan["workflow_changed"]
    actions = {node["node_id"]: (node["action"], node["reason"]) for node in plan["nodes"]}
    assert actions["two"] == ("reuse", None)
    assert actions["three"] == ("rerun", "resume.changed")
    assert actions["four"] == ("rerun", "resume.upstream_changed")


def test_reran_tool_calls_need_confirmation(make_app: Callable[..., TestClient]) -> None:
    from conftest import tool_use

    client = make_app(
        {"ACT": [tool_use("record_note", {"note": "x"}), text("x", stop_reason="max_tokens"), text("ok")]}
    )
    source = (
        "def record_note(note: str) -> str:\n"
        '    """Store a note.\n\n    Args:\n        note: The note.\n\n    Returns:\n        A confirmation.\n    """\n'
        "    return 'stored'\n"
    )
    tests = "from tool import record_note\n\n\ndef test_record_note():\n    assert record_note('a') == 'stored'\n"
    saved = client.post("/api/tools", json={"function_name": "record_note", "code": source, "tests": tests})
    assert saved.json()["tool"] is not None, saved.text
    workflow_id = save(
        client,
        [{"id": "start", "type": "start"}, agent_node("act", "ACT", tools=["record_note"])],
        [edge("start", "act")],
    )
    failed = run(client, workflow_id)
    assert failed["status"] == "failed"
    plan = client.get(f"/api/runs/{failed['id']}/resume").json()
    assert plan["needs_confirmation"] and plan["nodes"][1]["side_effects"] == ["record_note"]
    refused = client.post(f"/api/runs/{failed['id']}/resume", json={"mode": "continue"})
    assert refused.status_code == 400 and refused.json()["code"] == "resume.confirm_side_effects"
    accepted = client.post(f"/api/runs/{failed['id']}/resume", json={"mode": "continue", "confirm_side_effects": True})
    assert accepted.status_code == 201


def test_continue_keeps_decided_approvals(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("Entwurf")], "SEND": [text("x", stop_reason="max_tokens"), text("Gesendet")]})
    paused = run(client, approval_workflow(client))
    client.post(f"/api/runs/{paused['id']}/approvals/check", json={"decision": "approved", "comment": "passt"})
    failed = wait(client, paused["id"])
    assert failed["status"] == "failed"
    response = client.post(f"/api/runs/{failed['id']}/resume", json={"mode": "continue"})
    continued = wait(client, response.json()["id"])
    assert continued["status"] == "succeeded", continued
    approval = continued["nodes"]["check"]["approval"]
    assert approval["decision"] == "approved" and approval["reused_from"] == failed["id"]
    assert sum("DRAFT" in system for system in systems(client)) == 1


def test_running_runs_cannot_continue(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"A": [text("ok")]})
    workflow_id = save(client, [{"id": "start", "type": "start"}, agent_node("a", "A")], [edge("start", "a")])
    done = run(client, workflow_id)
    plan = client.get(f"/api/runs/{done['id']}/resume").json()
    assert not plan["possible"] and plan["issues"][0]["code"] == "resume.not_failed"
