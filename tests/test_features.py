"""Agent test, pre-check, context preview, versions, organisation, search, usage ledger, diagnosis."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from conftest import parsed, text, tool_use
from test_runs import agent_node, create, start

# ---- 1. agent test --------------------------------------------------------------------


def test_agent_test_runs_the_agent_without_creating_a_run(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"CALC": [tool_use("calculator", {"expression": "6*7"}), text("Es sind 42.")]})
    agent = {"name": "Rechner", "system_prompt": "CALC", "tools": ["calculator"], "task_template": "Rechne: {{input}}"}
    result = client.post("/api/agents/test", json={"agent": agent, "input": "6 mal 7"}).json()
    assert result["kind"] == "agent_test"
    assert result["status"] == "succeeded", result
    assert result["output"] == "Es sind 42."
    assert result["prompt"] == "Rechne: 6 mal 7"
    assert [call["name"] for call in result["tool_calls"]] == ["calculator"]
    assert result["tool_calls"][0]["output"] == "42.0"
    assert result["usage"]["requests"] == 2 and result["usage"]["estimated_cost_usd"] > 0
    # Not a workflow run: nothing appears among the runs, the ledger records it once.
    assert client.get("/api/runs").json() == []
    report = client.get("/api/usage").json()
    assert report["total"]["runs"] == 0 and report["total"]["operations"] == 1
    assert [bucket["key"] for bucket in report["by_source"]] == ["agent_test"]
    assert report["recent"][0]["source"] == "agent_test" and report["recent"][0]["label"] == "Rechner"


def test_agent_test_keeps_persistent_memory_unchanged(make_app: Callable[..., TestClient]) -> None:
    write = tool_use("memory", {"command": "create", "path": "/memories/neu.md", "file_text": "Test"})
    client = make_app({"MEM": [write, text("ok")]})
    client.put("/api/memory/Team/file", json={"path": "/memories/alt.md", "content": "bleibt"})
    agent = {"name": "Merker", "system_prompt": "MEM", "memory": {"mode": "persistent", "space": "Team"}}
    result = client.post("/api/agents/test", json={"agent": agent, "input": "x"}).json()
    assert result["status"] == "succeeded", result
    assert result["memory_isolated"] is True
    assert result["memory"] == {"/memories/alt.md": "bleibt", "/memories/neu.md": "Test"}
    assert client.get("/api/memory/Team").json() == [{"path": "/memories/alt.md", "size": 6}]


def test_agent_test_reports_errors_and_refuses_invalid_configurations(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"FAIL": [RuntimeError("boom")]})

    def explode(_request: dict[str, Any]) -> Any:
        raise RuntimeError("boom")

    client.fake.messages.script["FAIL"] = [explode]  # type: ignore[attr-defined]
    result = client.post("/api/agents/test", json={"agent": {"system_prompt": "FAIL"}, "input": "x"}).json()
    assert result["status"] == "failed"
    assert result["error_code"] == "run.failed" and "boom" in result["error"]
    invalid = {
        "system_prompt": "X",
        "model": "claude-opus-5-5",
        "tool_choice": {"type": "any"},
        "tools": ["calculator"],
    }
    response = client.post("/api/agents/test", json={"agent": invalid})
    assert response.status_code == 400 and response.json()["code"] == "run.agents_invalid"


# ---- 2. pre-check ---------------------------------------------------------------------


def _check(client: TestClient, nodes: list[dict[str, Any]], edges: list[tuple[str, str]]) -> dict[str, Any]:
    body = {"name": "T", "nodes": nodes, "edges": [{"id": f"{s}-{t}", "source": s, "target": t} for s, t in edges]}
    return client.post("/api/workflows/check", json=body).json()  # type: ignore[no-any-return]


def test_check_attributes_problems_to_nodes_and_connections(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    check = _check(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("a", "A"),
            agent_node("b", "B", task_template="{{Ghost}} und {{C}}"),
            agent_node("c", "C"),
            {"id": "out", "type": "output"},
            {"id": "out2", "type": "output"},
        ],
        [("start", "a"), ("a", "b"), ("b", "out"), ("out", "start")],
    )
    assert check["ok"] is False
    found = {(item["code"], item["node_id"], item["edge_id"], item["level"]) for item in check["problems"]}
    assert ("workflow.start_has_inputs", "start", "out-start", "error") in found
    assert ("workflow.output_has_outputs", "out", "out-start", "error") in found
    assert ("check.output_unconnected", "out2", None, "error") in found
    assert ("check.placeholder_unknown", "b", None, "warning") in found
    assert ("check.placeholder_not_connected", "b", None, "warning") in found
    assert ("check.agent_no_input", "c", None, "warning") in found
    assert ("check.api_key_missing", None, None, "error") in found
    assert check["problems"][0]["level"] == "error"  # errors first


def test_warnings_do_not_block_but_errors_do(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"A": [text("a")]})
    nodes = [{"id": "start", "type": "start"}, agent_node("a", "A", task_template="{{Unbekannt}}")]
    workflow_id = create(client, nodes, [("start", "a")])
    assert start(client, workflow_id)["status"] == "succeeded"
    blocked = create(client, [agent_node("a", "A"), {"id": "out", "type": "output"}], [])
    response = client.post(f"/api/workflows/{blocked}/runs", json={"input": ""})
    assert response.status_code == 400 and response.json()["code"] == "workflow.blocked"
    assert [item["code"] for item in response.json()["params"]["problems"]] == ["check.output_unconnected"]


def test_check_makes_no_api_request(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    _check(client, [agent_node("a", "A", tools=["calculator"], quality={"enabled": True})], [])
    assert client.fake.messages.calls == []  # type: ignore[attr-defined]


# ---- 3. context preview ---------------------------------------------------------------


def test_preview_marks_inputs_without_results(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    nodes = [
        {"id": "start", "type": "start", "start": {"default_input": "Thema KI"}},
        agent_node("research", "R"),
        agent_node(
            "writer",
            "W",
            task_template="Schreibe zu {{run.input}} aus:\n{{Research}}",
            request_options={"extra_headers": {"x-api-key": "sk-ant-abcdefghijklmnop"}},
        ),
    ]
    workflow_id = create(client, nodes, [("start", "research"), ("research", "writer")])
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    body = {"workflow": workflow, "node_id": "writer", "workflow_id": workflow_id}
    preview = client.post("/api/workflows/preview", json=body).json()
    assert preview["complete"] is False
    assert preview["run_input_source"] == "start_default"
    assert [segment["kind"] for segment in preview["segments"]] == ["text", "run_input", "text", "pending"]
    assert preview["segments"][1]["text"] == "Thema KI"
    assert preview["inputs"] == [
        {"node_id": "research", "label": "Research", "status": "pending", "run_status": None, "chars": 0}
    ]
    assert {"name": "Research", "status": "input"} in preview["placeholders"]
    assert "sk-ant" not in str(preview["request_options"])
    assert client.fake.messages.calls == []  # type: ignore[attr-defined]


def test_preview_uses_the_latest_run(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"R": [text("Fakten")], "W": [text("Text")]})
    nodes = [agent_node("research", "R"), agent_node("writer", "W", task_template="Aus: {{Research}}")]
    workflow_id = create(client, nodes, [("research", "writer")])
    run = start(client, workflow_id, "Thema")
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    body = {"workflow": workflow, "node_id": "writer", "workflow_id": workflow_id}
    preview = client.post("/api/workflows/preview", json=body).json()
    assert preview["complete"] is True and preview["run_id"] == run["id"]
    assert "".join(segment["text"] for segment in preview["segments"]) == run["nodes"]["writer"]["prompt"]


# ---- 5. versions ----------------------------------------------------------------------


def test_every_save_becomes_a_version_and_restoring_keeps_all(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    workflow_id = create(client, [agent_node("a", "A")], [])
    body = client.get(f"/api/workflows/{workflow_id}").json()
    body["name"] = "Zweite"
    client.put(f"/api/workflows/{workflow_id}", json=body, params={"note": "umbenannt"})
    client.put(f"/api/workflows/{workflow_id}", json=body)  # unchanged: no new version
    versions = client.get(f"/api/workflows/{workflow_id}/versions").json()
    assert [(item["label"], item["name"], item["current"]) for item in versions] == [
        ("v2", "Zweite", True),
        ("v1", "Test", False),
    ]
    assert versions[0]["description"] == "umbenannt"
    restored = client.post(f"/api/workflows/{workflow_id}/versions/1/restore").json()
    assert restored["name"] == "Test" and restored["id"] == workflow_id
    versions = client.get(f"/api/workflows/{workflow_id}/versions").json()
    assert [(item["number"], item["restored_from"], item["current"]) for item in versions] == [
        (3, 1, True),
        (2, None, False),
        (1, None, False),
    ]
    client.patch(f"/api/workflows/{workflow_id}/versions/2", json={"description": "Notiz"})
    assert client.get(f"/api/workflows/{workflow_id}/versions/2").json()["description"] == "Notiz"
    client.delete(f"/api/workflows/{workflow_id}")
    assert client.get(f"/api/workflows/{workflow_id}/versions").status_code == 404


def test_workflow_from_before_the_history_keeps_its_state(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    workflow_id = create(client, [agent_node("a", "A")], [])
    app_context = client.app.state.nodestro  # type: ignore[attr-defined]
    app_context.versions.delete_all(workflow_id)  # as if saved by an older Nodestro
    body = client.get(f"/api/workflows/{workflow_id}").json()
    body["description"] = "neu"
    client.put(f"/api/workflows/{workflow_id}", json=body)
    versions = client.get(f"/api/workflows/{workflow_id}/versions").json()
    assert [item["number"] for item in versions] == [2, 1]


# ---- 7./8. search, favorites and tags --------------------------------------------------


def test_favorites_and_tags(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    workflow_id = create(client, [agent_node("a", "A")], [])
    item = client.put(
        f"/api/organize/workflow/{workflow_id}", json={"favorite": True, "tags": [" Kunde ", "kunde", "Q3"]}
    )
    assert item.json() == {"favorite": True, "tags": ["Kunde", "Q3"]}
    client.put("/api/organize/tool/calculator", json={"favorite": False, "tags": ["Mathe"]})
    state = client.get("/api/organize").json()
    assert state["tags"] == ["Kunde", "Mathe", "Q3"]
    assert state["items"][f"workflow:{workflow_id}"]["favorite"] is True
    client.delete(f"/api/workflows/{workflow_id}")
    assert f"workflow:{workflow_id}" not in client.get("/api/organize").json()["items"]
    assert client.put("/api/organize/unknown/x", json={}).status_code == 422


def test_global_search_finds_every_kind(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    nodes = [agent_node("research", "Du recherchierst Quartalszahlen.", description="Sammelt Fakten")]
    workflow_id = create(client, nodes, [], description="Analyse der Quartalszahlen")
    client.post("/api/agents/templates", json={"agent": {"name": "Quartals-Prüfer", "system_prompt": "x"}})
    client.put(f"/api/organize/workflow/{workflow_id}", json={"tags": ["Finanzen"]})
    files = {"files": ("bericht.txt", b"Der Umsatz im Quartal stieg deutlich.", "text/plain")}
    client.post("/api/knowledge", files=files)
    results = client.get("/api/search", params={"q": "quartal"}).json()
    kinds = {item["kind"] for item in results}
    assert {"workflow", "agent", "template", "knowledge"} <= kinds
    agent = next(item for item in results if item["kind"] == "agent")
    assert agent["workflow_id"] == workflow_id and agent["node_id"] == "research"
    assert client.get("/api/search", params={"q": "finanzen"}).json()[0]["id"] == workflow_id
    assert client.get("/api/search", params={"q": "calculator"}).json()[0]["kind"] == "tool"
    assert client.get("/api/search", params={"q": "x"}).json() == []


# ---- 13. usage of the AI assistants -----------------------------------------------------


def test_assistants_and_runs_are_counted_once_each(make_app: Callable[..., TestClient]) -> None:
    seed = parsed(lambda model: model(files=[{"path": "/memories/README.md", "content": "x"}], explanation="y"))
    client = make_app({"long-term memory": [seed], "ONE": [text("a")]})
    client.post("/api/memory/propose", json={"role": "Support", "wishes": "Kunden"})
    workflow_id = create(client, [agent_node("one", "ONE")], [])
    run = start(client, workflow_id)
    report = client.get("/api/usage").json()
    assert report["total"]["runs"] == 1 and report["total"]["operations"] == 1
    assert report["total"]["requests"] == 2
    sources = {bucket["key"]: bucket for bucket in report["by_source"]}
    assert sources["workflow_run"]["requests"] == 1 and sources["memory_proposal"]["requests"] == 1
    total = run["usage"]["estimated_cost_usd"] + report["recent"][0]["usage"]["estimated_cost_usd"]
    assert abs(report["total"]["estimated_cost_usd"] - total) < 1e-9
    assert report["recent"][0]["model"] == "anthropic:claude-sonnet-5-5"  # the provider and model the API reported


def test_failed_design_still_records_usage(make_app: Callable[..., TestClient]) -> None:
    broken = parsed(lambda model: (_ for _ in ()).throw(ValueError("kaputt")))
    client = make_app({"multi-agent systems architect": [broken]})
    response = client.post("/api/agents/design", json={"description": "x"})
    assert response.status_code >= 400
    assert client.get("/api/usage").json()["by_source"] == []  # no request was answered, nothing to count


# ---- 9. diagnosis ---------------------------------------------------------------------


def test_diagnostics_never_show_the_key(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    client.put("/api/settings", json={"providers": {"anthropic": {"api_key": "sk-ant-geheim-1234567890"}}})
    report = client.get("/api/diagnostics").json()
    assert "geheim" not in str(report)
    checks = {item["id"]: item for item in report["checks"]}
    assert checks["database"]["status"] == "ok"
    assert checks["data_dir"]["status"] == "ok"
    assert checks["sandbox"]["status"] == "ok", checks["sandbox"]
    assert checks["providers"]["code"] == "ok" and checks["providers"]["params"]["providers"] == "Anthropic"
    assert report["versions"]["orkexa"]


def test_connection_check_lists_models_only(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    assert client.post("/api/diagnostics/connection").json()["code"] == "no_key"
    client.put("/api/settings", json={"providers": {"anthropic": {"api_key": "sk-ant-test"}}})
    client.fake.models = SimpleNamespace(list=lambda limit: SimpleNamespace(data=[{"id": "m"}]))  # type: ignore[attr-defined]
    result = client.post("/api/diagnostics/connection").json()
    assert result["status"] == "ok", result
    assert client.fake.messages.calls == []  # type: ignore[attr-defined]
