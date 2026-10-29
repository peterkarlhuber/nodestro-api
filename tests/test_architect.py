"""The Workflow Architect: create, revise, questions, tool drafts, cancel, budget, usage, fixes."""

from __future__ import annotations

from collections.abc import Callable
import threading
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.agent_builder import ConfigIssue
from nodestro_runtime.schemas import AgentConfig, Edge, Node, WorkflowIn
import pytest

from nodestro_api.architect import _fix_config, _layout, _structural_fixes

from architect_script import (
    AGENT,
    AGENT_REVISION,
    ANALYST,
    CODER,
    PLANNER,
    REVISER_PLAN,
    agent_spec,
    analysis,
    blocking,
    create_plan,
    create_script,
    revise_plan,
    revision,
    tool_proposal,
)
from test_runs import agent_node, create


def wait_job(client: TestClient, job_id: str, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/architect/jobs/{job_id}").json()
        if job["status"] != "running":
            return job  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not finish")


def start(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/architect/jobs", json={"description": "Blogartikel mit SEO", "language": "de", **body})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def logs(job: dict[str, Any]) -> list[str]:
    return [event["code"] for event in job["events"] if event["type"] == "log"]


def by_label(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["label"]: node for node in workflow["nodes"]}


# ---- creating -------------------------------------------------------------------------


def test_create_designs_a_complete_validated_draft(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready", job["error"]
    draft = job["draft"]
    nodes = by_label(draft)
    assert set(nodes) == {"Start", "Recherche", "SEO-Analyse", "Autor", "Ergebnis"}
    assert draft["name"] == "Blog-Artikel mit SEO"
    assert nodes["Start"]["start"]["default_input"] == "Wärmepumpen im Altbau"
    ids = {label: node["id"] for label, node in nodes.items()}
    pairs = {(edge["source"], edge["target"]) for edge in draft["edges"]}
    assert pairs == {
        (ids["Start"], ids["Recherche"]),
        (ids["Start"], ids["SEO-Analyse"]),
        (ids["Recherche"], ids["Autor"]),
        (ids["SEO-Analyse"], ids["Autor"]),
        (ids["Autor"], ids["Ergebnis"]),
    }
    # Placeholders use the agents' names, the plan decides name, icon, task and output.
    assert nodes["Recherche"]["agent"]["task_template"] == "Recherchiere zum Thema: {{run.input}}"
    assert nodes["Autor"]["agent"]["task_template"] == "Schreibe den Artikel aus {{Recherche}} und {{SEO-Analyse}}."
    assert nodes["Recherche"]["agent"]["server_tools"] == ["web_search"]
    assert nodes["Autor"]["agent"]["icon"] == "pen"
    seo = nodes["SEO-Analyse"]["agent"]["output"]
    assert seo["mode"] == "structured" and [f["name"] for f in seo["fields"]] == ["keywords", "main_keyword"]
    # Parallel steps sit in one column; the system prompt comes from the Agent Architect.
    assert nodes["Recherche"]["position"]["x"] == nodes["SEO-Analyse"]["position"]["x"]
    assert nodes["Autor"]["agent"]["system_prompt"] == "Du bist ein sorgfältiger Spezialist."
    # The new tool is a draft: named in the config, not registered, the run is blocked until approval.
    assert nodes["Autor"]["agent"]["tools"] == ["keyword_density"]
    assert [tool["name"] for tool in job["tool_drafts"]] == ["keyword_density"]
    assert job["tool_drafts"][0]["status"] == "draft" and job["tool_drafts"][0]["check"] is None
    assert "keyword_density" not in {tool["name"] for tool in client.get("/api/tools").json()}
    autor_issues = job["check"]["agents"][ids["Autor"]]
    assert [issue["code"] for issue in autor_issues if issue["level"] == "error"] == ["config.tool_unapproved"]
    assert job["assumptions"] == ["Der Artikel ist etwa 800 Wörter lang.", "Zielgruppe sind Laien."]
    # The progress consists of real steps in order.
    phases = [event["phase"] for event in job["events"] if event["type"] == "phase"]
    assert phases == ["analyze", "plan", "resources", "configure", "tools", "connect", "validate", "present"]
    codes = logs(job)
    for code in (
        "analyze.done",
        "plan.roles",
        "plan.parallel",
        "resources.server_tool",
        "configure.done",
        "tools.done",
    ):
        assert code in codes
    assert "validate.ok" in codes and "validate.tools_pending" in codes
    graphs = [event for event in job["events"] if event["type"] == "graph"]
    assert set(graphs[0]["data"]["marks"].values()) == {"pending"}
    assert set(graphs[-1]["data"]["marks"].values()) == {"configured"}
    # Nothing is stored as a workflow and nothing runs before the user accepts.
    assert client.get("/api/workflows").json() == []
    assert client.get("/api/runs").json() == []


def test_accepting_creates_a_workflow_and_never_runs_it(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    job = wait_job(client, start(client)["id"])
    accepted = client.post(f"/api/architect/jobs/{job['id']}/accept", json={"name": "Mein Blog"}).json()
    assert accepted["status"] == "accepted"
    workflow = client.get(f"/api/workflows/{accepted['result_workflow_id']}").json()
    assert workflow["name"] == "Mein Blog" and len(workflow["nodes"]) == 5
    versions = client.get(f"/api/workflows/{workflow['id']}/versions").json()
    assert len(versions) == 1 and versions[0]["description"].startswith("KI-Entwurf: ")
    assert client.get("/api/runs").json() == []
    # A draft is taken once.
    again = client.post(f"/api/architect/jobs/{job['id']}/accept", json={})
    assert again.status_code == 400 and again.json()["code"] == "architect.not_ready"
    # The workflow check names the tool awaiting approval; approving registers it after the sandbox test.
    check = client.post("/api/workflows/check", json=workflow).json()
    codes = [issue["code"] for issues in check["agents"].values() for issue in issues]
    assert "config.tool_unapproved" in codes
    drafts = client.get("/api/tools/drafts").json()
    assert [draft["name"] for draft in drafts] == ["keyword_density"]
    decision = client.post(f"/api/tools/drafts/{drafts[0]['id']}/approve").json()
    assert decision["tool"]["kind"] == "generated", decision
    assert decision["draft"]["status"] == "approved" and decision["draft"]["check"]["ok"]
    assert client.get("/api/tools/drafts").json() == []
    check = client.post("/api/workflows/check", json=workflow).json()
    assert [issue["code"] for issues in check["agents"].values() for issue in issues if issue["level"] == "error"] == []


def test_tool_drafts_are_tested_only_on_request_and_can_be_rejected(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    job = wait_job(client, start(client)["id"])
    draft_id = job["tool_drafts"][0]["id"]
    tested = client.post(f"/api/tools/drafts/{draft_id}/test").json()
    assert tested["check"]["ok"] and len(tested["check"]["tests"]) == 3
    assert tested["status"] == "draft"
    assert "keyword_density" not in {tool["name"] for tool in client.get("/api/tools").json()}
    assert client.delete(f"/api/tools/drafts/{draft_id}").status_code == 204
    assert client.get("/api/tools/drafts").json() == []


def test_failing_tool_draft_is_not_registered(make_app: Callable[..., TestClient]) -> None:
    broken_tests = (
        "from tool import keyword_density\n\n\ndef test_wrong():\n    assert keyword_density('a', 'a') == 2\n"
    )
    script = create_script()
    script[CODER] = [tool_proposal(tests=broken_tests)]
    client = make_app(script)
    job = wait_job(client, start(client)["id"])
    decision = client.post(f"/api/tools/drafts/{job['tool_drafts'][0]['id']}/approve").json()
    assert decision["tool"] is None and decision["draft"]["status"] == "draft"
    assert not decision["draft"]["check"]["ok"]
    assert "keyword_density" not in {tool["name"] for tool in client.get("/api/tools").json()}


def test_static_check_findings_are_corrected_without_running_code(make_app: Callable[..., TestClient]) -> None:
    unsafe = "import os\n\n" + tool_proposal_code()
    script = create_script()
    script[CODER] = [tool_proposal(code=unsafe), tool_proposal()]
    client = make_app(script)
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready"
    assert "tools.static_failed" in logs(job) and job["repairs"] == 1
    assert job["tool_drafts"][0]["static_errors"] == []
    # The correction request carried the findings of the static check.
    coder_calls = [call for call in client.fake.messages.calls if CODER in str(call["system"])]  # type: ignore[attr-defined]
    assert len(coder_calls) == 2 and "os" in str(coder_calls[1]["messages"])


def tool_proposal_code() -> str:
    from architect_script import WORD_TOOL

    return WORD_TOOL


def test_without_tool_generation_tools_are_only_listed(make_app: Callable[..., TestClient]) -> None:
    script = create_script()
    del script[CODER]
    client = make_app(script)
    job = wait_job(client, start(client, generate_tools=False)["id"])
    assert job["status"] == "ready"
    assert job["tool_drafts"] == [] and [tool["name"] for tool in job["missing_tools"]] == ["keyword_density"]
    assert "tools.listed" in logs(job)
    autor = by_label(job["draft"])["Autor"]["agent"]
    assert autor["tools"] == []


def test_essential_question_stops_before_planning(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ANALYST: [analysis(question="Für welche Branche?")]})
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "needs_input"
    assert job["question"] == "Für welche Branche?" and job["draft"] is None
    assert len(client.fake.messages.calls) == 1  # type: ignore[attr-defined]
    # With the answer, the next job goes on (the question is not asked again).
    client.fake.messages.script.update(create_script())  # type: ignore[attr-defined]
    client.fake.messages.script[ANALYST] = [analysis(question="Noch eine Frage?")]  # type: ignore[attr-defined]
    answered = start(client, clarifications=[{"question": "Für welche Branche?", "answer": "Energie"}])
    job = wait_job(client, answered["id"])
    assert job["status"] == "ready"
    last = client.fake.messages.calls[1]  # type: ignore[attr-defined]
    assert "Answer: Energie" in str(last["messages"])


def test_texts_follow_the_interface_language(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    wait_job(client, start(client, language="fr")["id"])
    for call in client.fake.messages.calls:  # type: ignore[attr-defined]
        if ANALYST in str(call["system"]) or PLANNER in str(call["system"]) or AGENT in str(call["system"]):
            assert "in French, the language of the user's interface" in str(call["system"])
        if CODER in str(call["system"]):
            assert "Write everything in English" in str(call["system"])


# ---- revising -----------------------------------------------------------------------------


def _base(client: TestClient) -> dict[str, Any]:
    nodes = [
        {"id": "start", "type": "start", "label": "Start", "position": {"x": 0, "y": 0}},
        agent_node("a", "Du sammelst.", position={"x": 300, "y": 0}),
        agent_node("b", "Du analysierst.", task_template="Analysiere {{A}}"),
        {"id": "out", "type": "output", "label": "Ergebnis"},
    ]
    workflow_id = create(client, nodes, [("start", "a"), ("a", "b"), ("b", "out")])
    return client.get(f"/api/workflows/{workflow_id}").json()  # type: ignore[no-any-return]


def test_revise_adds_a_critic_and_keeps_everything_else(make_app: Callable[..., TestClient]) -> None:
    client = make_app({REVISER_PLAN: [revise_plan("a", "b")], AGENT: [agent_spec()], AGENT_REVISION: [revision()]})
    saved = _base(client)
    # Unsaved editor changes are part of the base, the stored workflow is never touched.
    editor = {**saved, "description": "ungespeichert"}
    job = wait_job(
        client,
        start(client, mode="revise", workflow=editor, workflow_id=saved["id"], description="Kritiker vor dem Ergebnis")[
            "id"
        ],
    )
    assert job["status"] == "ready", job["error"]
    assert job["base"]["description"] == "ungespeichert"
    draft = job["draft"]
    nodes = {node["id"]: node for node in draft["nodes"]}
    critic = next(node for node in draft["nodes"] if node["label"] == "Kritiker")
    pairs = {(edge["source"], edge["target"]) for edge in draft["edges"]}
    assert pairs == {("start", "a"), ("a", "b"), ("b", critic["id"]), (critic["id"], "out")}
    assert critic["agent"]["task_template"] == "Prüfe kritisch: {{B}}"
    assert nodes["a"]["agent"] == saved["nodes"][1]["agent"]
    assert nodes["b"]["agent"]["system_prompt"] == "Du analysierst und begründest jede Aussage."
    assert nodes["b"]["agent"]["task_template"] == "Analysiere {{A}}"
    # Kept edges keep their ids.
    assert {edge["id"] for edge in draft["edges"]} >= {"start-a", "a-b"}
    kinds = {change["name"]: change["kind"] for change in job["changes"]}
    assert kinds == {"A": "kept", "B": "changed", "Kritiker": "added"}
    assert job["marks"] == {"a": "kept", "b": "changed", critic["id"]: "added"} | {
        key: value for key, value in job["marks"].items() if key not in ("a", "b", critic["id"])
    }
    assert client.get(f"/api/workflows/{saved['id']}").json() == saved
    accepted = client.post(f"/api/architect/jobs/{job['id']}/accept", json={}).json()
    assert accepted["status"] == "accepted" and accepted["result_workflow_id"] == saved["id"]
    # Accepting a revision changes nothing on the server: the editor applies it as an undoable step.
    assert client.get(f"/api/workflows/{saved['id']}").json() == saved
    assert len(client.get("/api/workflows").json()) == 1


def test_revise_removes_agents_and_reconnects(make_app: Callable[..., TestClient]) -> None:
    plan = revise_plan(
        "a",
        "b",
        agents=[{"key": "a", "action": "keep", "name": "A", "purpose": ""}],
        removed=["b"],
        final=["a"],
    )
    client = make_app({REVISER_PLAN: [plan]})
    saved = _base(client)
    job = wait_job(client, start(client, mode="revise", workflow=saved, description="Ohne B")["id"])
    assert job["status"] == "ready", job["error"]
    assert {node["id"] for node in job["draft"]["nodes"]} == {"start", "a", "out"}
    assert {(edge["source"], edge["target"]) for edge in job["draft"]["edges"]} == {("start", "a"), ("a", "out")}
    assert {change["name"]: change["kind"] for change in job["changes"]} == {"A": "kept", "B": "removed"}
    graph = [event for event in job["events"] if event["type"] == "graph"][-1]
    assert [node["id"] for node in graph["data"]["removed"]] == ["b"]


def test_revise_needs_the_workflow(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    response = client.post("/api/architect/jobs", json={"mode": "revise", "description": "x"})
    assert response.status_code == 400 and response.json()["code"] == "architect.workflow_missing"


# ---- cancel, budget, usage -------------------------------------------------------------


def test_cancel_stops_at_once_and_leaves_no_draft(make_app: Callable[..., TestClient]) -> None:
    release, entered = threading.Event(), threading.Event()
    script = create_script()
    script[PLANNER] = [blocking(create_plan(), release, entered)]
    client = make_app(script)
    job = start(client)
    assert entered.wait(10)
    cancelled = client.post(f"/api/architect/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    release.set()
    time.sleep(0.3)
    job = client.get(f"/api/architect/jobs/{job['id']}").json()
    assert job["status"] == "cancelled" and job["draft"] is None
    assert [event["data"]["status"] for event in job["events"] if event["type"] == "finished"] == ["cancelled"]
    # No agent was designed after the cancellation; nothing was stored; the spent requests are counted.
    assert not any(AGENT in str(call["system"]) for call in client.fake.messages.calls)  # type: ignore[attr-defined]
    assert client.get("/api/workflows").json() == [] and client.get("/api/tools/drafts").json() == []
    deadline = time.monotonic() + 5
    entries: list[dict[str, Any]] = []
    while time.monotonic() < deadline and len(entries) < 2:
        entries = client.get("/api/usage").json()["recent"]
        time.sleep(0.05)
    assert sorted(entry["detail"] for entry in entries) == ["analysis", "plan"]


def test_budget_limit_stops_the_job(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    job = wait_job(client, start(client, budget_usd=0.0001)["id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "architect.budget_exceeded"
    assert len(client.fake.messages.calls) == 1  # type: ignore[attr-defined]


def test_budget_and_repairs_come_from_the_settings(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    settings = client.put("/api/settings", json={"architect_budget_usd": 0.0001, "architect_max_repairs": 0}).json()
    assert settings["architect_budget_usd"] == 0.0001 and settings["architect_max_repairs"] == 0
    job = wait_job(client, start(client)["id"])
    assert job["error"]["code"] == "architect.budget_exceeded" and job["max_repairs"] == 0


def test_every_request_is_in_the_usage_ledger_once(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    job = wait_job(client, start(client)["id"])
    report = client.get("/api/usage").json()
    requests = len(client.fake.messages.calls)  # type: ignore[attr-defined]
    assert requests == 6  # analysis, plan, three agents, one tool
    assert report["total"]["requests"] == requests and report["total"]["runs"] == 0
    assert [bucket["key"] for bucket in report["by_source"]] == ["workflow_design"]
    details = sorted(entry["detail"] for entry in report["recent"])
    assert details == ["agent", "analysis", "plan", "tool"]
    assert all(entry["ref"] == job["id"] for entry in report["recent"])
    assert job["usage"]["requests"] == requests
    assert job["usage"]["estimated_cost_usd"] == pytest.approx(report["total"]["estimated_cost_usd"])
    # Every event carries the cost so far, never more than the end result.
    costs = [event["cost_usd"] for event in job["events"] if event["cost_usd"] is not None]
    assert costs == sorted(costs) and costs[-1] == pytest.approx(job["usage"]["estimated_cost_usd"])


def test_discard_removes_the_tool_drafts(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    job = wait_job(client, start(client)["id"])
    assert len(client.get("/api/tools/drafts").json()) == 1
    discarded = client.post(f"/api/architect/jobs/{job['id']}/discard").json()
    assert discarded["status"] == "discarded"
    assert client.get("/api/tools/drafts").json() == []
    assert client.get("/api/workflows").json() == []


def test_events_stream_ends_with_the_snapshot_of_a_finished_job(make_app: Callable[..., TestClient]) -> None:
    client = make_app(create_script())
    job = wait_job(client, start(client)["id"])
    with client.stream("GET", f"/api/architect/jobs/{job['id']}/events") as response:
        body = "".join(response.iter_text())
    assert body.startswith("event: snapshot") and "event: end" in body and '"status": "ready"' in body


# ---- corrections ------------------------------------------------------------------------


def test_invalid_plan_connections_are_corrected(make_app: Callable[..., TestClient]) -> None:
    plan = create_plan(
        agents=[
            {"key": "one", "name": "Eins", "purpose": "x", "inputs": ["start", "ghost", "two"], "task": "{{nobody}}"},
            {"key": "two", "name": "Zwei", "purpose": "y", "inputs": ["one"], "icon": "rocket"},
            {"key": "two", "name": "Doppelt", "purpose": "z"},
        ],
        final=[],
    )
    client = make_app({ANALYST: [analysis()], PLANNER: [plan], AGENT: [agent_spec(), agent_spec(), agent_spec()]})
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready", job["error"]
    codes = logs(job)
    assert "connect.input_dropped" in codes and "connect.cycle_removed" in codes
    assert "validate.placeholder_fixed" in codes and "connect.output_added" in codes
    nodes = by_label(job["draft"])
    # A repeated key is a second agent with its own key, not a duplicate node.
    assert set(nodes) == {"Start", "Eins", "Zwei", "Doppelt", "Ergebnis"}
    assert nodes["Eins"]["agent"]["task_template"] == "{{input}}"
    assert nodes["Zwei"]["agent"]["icon"] == "bot"
    assert "validate.ok" in codes and job["fixes"] >= 4


def test_config_fixes_without_ai() -> None:
    config = AgentConfig(thinking="disabled", thinking_display="omitted", tools=["calculator", "calculator"])
    library = type("Tools", (), {"tools": ["calculator"]})()
    issue = ConfigIssue(code="config.thinking_not_disableable", message="x")
    assert _fix_config(config, issue, library, set())  # type: ignore[arg-type]
    assert config.thinking is None and config.thinking_display is None
    assert _fix_config(config, ConfigIssue(code="config.duplicate_tool", message="x"), library, set())  # type: ignore[arg-type]
    assert config.tools == ["calculator"]
    assert not _fix_config(config, ConfigIssue(code="library", message="?"), library, set())  # type: ignore[arg-type]


def test_structural_fixes_and_layout() -> None:
    workflow = WorkflowIn(
        nodes=[
            Node(id="s", type="start"),
            Node(id="x", agent=AgentConfig(name="X"), label="X"),
            Node(id="o", type="output"),
        ],
        edges=[Edge(id="1", source="o", target="x"), Edge(id="2", source="x", target="ghost")],
    )
    fixes = _structural_fixes(workflow, {"x"})
    assert [fix.code for fix in fixes] == [
        "connect.edge_fixed",
        "connect.edge_fixed",
        "connect.input_added",
        "connect.output_added",
    ]
    _layout(workflow, None)
    assert [node.position.x for node in workflow.nodes] == [0, 340, 680]


def test_remaining_errors_get_a_bounded_number_of_ai_repairs(make_app: Callable[..., TestClient]) -> None:
    from architect_script import REPAIR

    script = create_script()
    script[AGENT] = [agent_spec(system_prompt="BROKEN"), agent_spec(), agent_spec()]
    script[REPAIR] = [revision(system_prompt="BROKEN again"), revision(system_prompt="Repariert.")]
    client = make_app(script)
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    original = nodestro.builder.validate

    def validate(config: AgentConfig) -> list[ConfigIssue]:
        issues = original(config)
        if config.system_prompt.startswith("BROKEN"):
            issues.append(ConfigIssue(code="library", message="The library rejects this prompt"))
        return issues

    nodestro.builder.validate = validate
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready", job["error"]
    assert job["repairs"] == 2 and logs(job).count("validate.repair") == 2
    assert "validate.ok" in logs(job)
    assert "Repariert." in [node["agent"]["system_prompt"] for node in job["draft"]["nodes"] if node["agent"]]
    # With no repairs left, the draft is still offered, with the problem named.
    script = create_script()
    script[AGENT] = [agent_spec(system_prompt="BROKEN"), agent_spec(), agent_spec()]
    client.fake.messages.script.update(script)  # type: ignore[attr-defined]
    client.put("/api/settings", json={"architect_max_repairs": 0})
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready" and job["repairs"] == 0
    assert "validate.remaining" in logs(job) and not job["check"]["ok"]


def test_create_plans_decisions_approvals_and_a_form(make_app: Callable[..., TestClient]) -> None:
    plan = create_plan(
        agents=[
            {
                "key": "triage",
                "name": "Klassifizierer",
                "purpose": "Ordnet die Anfrage ein.",
                "inputs": ["start"],
                "task": "{{start}}",
                "output_fields": [{"name": "category", "type": "string", "description": "complaint oder billing"}],
            },
            {"key": "complaints", "name": "Beschwerden", "purpose": "Beschwerden.", "inputs": ["triage"]},
            {"key": "billing", "name": "Rechnungen", "purpose": "Rechnungsfragen.", "inputs": ["triage"]},
            {
                "key": "reply",
                "name": "Antwort",
                "purpose": "Schreibt die Antwort.",
                "inputs": ["complaints", "billing"],
            },
        ],
        final=["reply"],
        input_fields=[
            {"name": "message", "label": "Nachricht", "type": "textarea", "required": True, "example": "Hallo"},
            {"name": "priority", "label": "Priorität", "type": "select", "options": ["hoch", "normal"]},
        ],
        decisions=[
            {
                "key": "route",
                "name": "Anliegen",
                "source": "triage",
                "branches": [
                    {
                        "label": "Beschwerde",
                        "field": "category",
                        "operator": "equals",
                        "value": "complaint",
                        "targets": ["complaints"],
                    }
                ],
                "default_targets": ["billing"],
            }
        ],
        approvals=[
            {"key": "check", "name": "Antwort prüfen", "instructions": "Ton ok?", "source": ["reply"], "approved": []}
        ],
        result_files=["pdf", "csv", "pdf"],
    )
    client = make_app({ANALYST: [analysis()], PLANNER: [plan], AGENT: [agent_spec() for _ in range(4)]})
    job = wait_job(client, start(client, generate_tools=False)["id"])
    assert job["status"] == "ready", job["error"]
    draft = job["draft"]
    nodes = by_label(draft)
    assert nodes["Anliegen"]["type"] == "condition"
    assert nodes["Anliegen"]["condition"]["rules"][0]["value"] == "complaint"
    assert nodes["Anliegen"]["condition"]["default_label"] == "Sonst"
    ids = {label: node["id"] for label, node in nodes.items()}
    edges = {(edge["source"], edge["target"], edge["source_handle"]) for edge in draft["edges"]}
    assert (ids["Klassifizierer"], ids["Anliegen"], None) in edges
    assert (ids["Anliegen"], ids["Beschwerden"], "rule_1") in edges
    assert (ids["Anliegen"], ids["Rechnungen"], "default") in edges
    # The branch agents get their data through the decision, not around it.
    assert (ids["Klassifizierer"], ids["Beschwerden"], None) not in edges
    assert (ids["Antwort"], ids["Antwort prüfen"], None) in edges
    fields = nodes["Start"]["start"]["fields"]
    assert [field["name"] for field in fields] == ["message", "priority"]
    assert fields[0]["default"] == "Hallo" and fields[1]["options"] == ["hoch", "normal"]
    output = next(node for node in draft["nodes"] if node["type"] == "output")
    assert [spec["format"] for spec in output["output"]["files"]] == ["pdf", "csv"]
    blocking_codes = [item["code"] for item in job["check"]["problems"] if item["level"] == "error"]
    assert blocking_codes in ([], ["check.api_key_missing"])
