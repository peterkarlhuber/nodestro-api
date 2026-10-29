from __future__ import annotations

from collections.abc import Callable
import json
import time
from typing import Any

from fastapi.testclient import TestClient

from conftest import parsed, text, tool_use


def wait_run(client: TestClient, run_id: str, timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not finish: {run['status']}")


def agent_node(node_id: str, prompt: str, **agent: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "label": node_id.title(),
        "agent": {"name": node_id.title(), "system_prompt": prompt, **agent},
    }


def create(client: TestClient, nodes: list[dict[str, Any]], edges: list[tuple[str, str]], **extra: Any) -> str:
    body = {
        "name": "Test",
        "nodes": nodes,
        "edges": [{"id": f"{s}-{t}", "source": s, "target": t} for s, t in edges],
        **extra,
    }
    response = client.post("/api/workflows", json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def start(client: TestClient, workflow_id: str, value: str = "Eingabe") -> dict[str, Any]:
    response = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": value})
    assert response.status_code == 201, response.text
    return wait_run(client, response.json()["id"])


def test_linear_workflow_passes_outputs_along(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"RESEARCH": [text("Fakten A")], "WRITER": [text("Artikel B")]})
    workflow_id = create(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("research", "RESEARCH"),
            agent_node("writer", "WRITER", task_template="Schreibe aus:\n{{Research}}"),
            {"id": "out", "type": "output"},
        ],
        [("start", "research"), ("research", "writer"), ("writer", "out")],
    )
    run = start(client, workflow_id, "Thema X")
    assert run["status"] == "succeeded", run
    assert run["output"] == "Artikel B"
    assert run["nodes"]["research"]["prompt"] == "Thema X"
    assert run["nodes"]["writer"]["prompt"] == "Schreibe aus:\nFakten A"
    assert run["nodes"]["writer"]["text"] == "Artikel B"
    assert run["usage"]["requests"] == 2
    assert run["usage"]["estimated_cost_usd"] > 0
    assert run["nodes"]["research"]["usage"]["requests"] == 1


def test_parallel_branches_merge(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"LEFT": [text("links")], "RIGHT": [text("rechts")], "MERGE": [text("beides")]})
    workflow_id = create(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("left", "LEFT"),
            agent_node("right", "RIGHT"),
            agent_node("merge", "MERGE"),
        ],
        [("start", "left"), ("start", "right"), ("left", "merge"), ("right", "merge")],
    )
    run = start(client, workflow_id)
    assert run["status"] == "succeeded"
    prompt = run["nodes"]["merge"]["prompt"]
    assert "## Left" in prompt and "links" in prompt and "## Right" in prompt and "rechts" in prompt
    assert run["output"] == "beides"


def test_tool_calls_are_recorded(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"CALC": [tool_use("calculator", {"expression": "6*7"}), text("42")]})
    workflow_id = create(client, [agent_node("calc", "CALC", tools=["calculator"])], [])
    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    calls = run["nodes"]["calc"]["tool_calls"]
    assert calls[0]["name"] == "calculator"
    assert calls[0]["output"] == "42.0"
    assert calls[0]["is_error"] is False
    tool_result = client.fake.messages.calls[1]["messages"][-1]["content"][0]  # type: ignore[attr-defined]
    assert tool_result["type"] == "tool_result"


def test_structured_output_is_passed_as_json(make_app: Callable[..., TestClient]) -> None:
    client = make_app(
        {
            "EXTRACT": [parsed(lambda model: model(name="Jane", score=3))],
            "USE": [text("ok")],
        }
    )
    output = {
        "mode": "structured",
        "name": "Person",
        "fields": [{"name": "name", "type": "string"}, {"name": "score", "type": "integer"}],
    }
    workflow_id = create(
        client,
        [agent_node("extract", "EXTRACT", output=output), agent_node("use", "USE")],
        [("extract", "use")],
    )
    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    assert run["nodes"]["extract"]["output"] == {"name": "Jane", "score": 3}
    assert json.loads(run["nodes"]["use"]["prompt"]) == {"name": "Jane", "score": 3}


def test_failed_node_blocks_successors(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"BROKEN": [text("x", stop_reason="max_tokens")], "OTHER": [text("y")]})
    workflow_id = create(
        client,
        [agent_node("broken", "BROKEN"), agent_node("after", "AFTER"), agent_node("other", "OTHER")],
        [("broken", "after")],
    )
    run = start(client, workflow_id)
    assert run["status"] == "failed"
    assert run["nodes"]["broken"]["status"] == "failed"
    assert "IncompleteResponseError" in run["nodes"]["broken"]["error"]
    assert run["nodes"]["broken"]["error_code"] == "run.agent.IncompleteResponseError"
    # Not run because of the failure: "blocked", unlike a branch that was deliberately skipped.
    assert run["nodes"]["after"]["status"] == "blocked"
    assert run["nodes"]["after"]["error_code"] == "run.blocked"
    assert run["nodes"]["other"]["status"] == "succeeded"
    assert run["error_code"] == "run.nodes_failed" and run["error_params"] == {"nodes": "Broken"}


def test_budget_stops_the_run(make_app: Callable[..., TestClient]) -> None:
    huge = {"input_tokens": 1_000_000, "output_tokens": 0}
    client = make_app({"EXPENSIVE": [text("a", usage=huge)], "NEXT": [text("b")]})
    workflow_id = create(
        client,
        [agent_node("expensive", "EXPENSIVE"), agent_node("next", "NEXT")],
        [("expensive", "next")],
        budget_usd=0.5,
    )
    run = start(client, workflow_id)
    assert run["status"] == "failed"
    assert "Budget" in run["nodes"]["expensive"]["error"]
    assert run["nodes"]["expensive"]["error_code"] == "run.budget_exceeded"
    assert run["nodes"]["next"]["status"] == "blocked"


def test_quality_check_lets_the_agent_revise(make_app: Callable[..., TestClient]) -> None:
    def verdict(passed: bool) -> Callable[[dict[str, Any]], Any]:
        return parsed(
            lambda model: model(
                criteria=[{"name": "short", "reasoning": "r", "passed": passed}],
                summary="s",
                suggestions=[] if passed else ["kürzer"],
            )
        )

    client = make_app(
        {
            "WRITE": [text("lang lang lang"), text("kurz")],
            "strict, fair reviewer": [verdict(False), verdict(True)],
        }
    )
    quality = {
        "enabled": True,
        "criteria": [{"name": "short", "description": "Höchstens zwei Wörter"}],
        "max_revisions": 2,
        "judge_model": "claude-sonnet-5-5",
    }
    workflow_id = create(client, [agent_node("write", "WRITE", quality=quality)], [])
    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    assert run["nodes"]["write"]["output"] == "kurz"
    assert run["nodes"]["write"]["evaluation"]["passed"] is True
    assert run["nodes"]["write"]["evaluation"]["attempts"] == 2
    assert run["nodes"]["write"]["usage"]["requests"] == 4  # two answers, two verdicts


def test_run_memory_is_shown_after_the_run(make_app: Callable[..., TestClient]) -> None:
    client = make_app(
        {
            "NOTE": [
                tool_use("memory", {"command": "create", "path": "/memories/n.md", "file_text": "Merken"}),
                text("fertig"),
            ]
        }
    )
    workflow_id = create(client, [agent_node("note", "NOTE", memory={"mode": "run"})], [])
    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    assert run["nodes"]["note"]["memory"] == {"/memories/n.md": "Merken"}
    assert run["nodes"]["note"]["tool_calls"][0]["name"] == "memory"


def test_persistent_memory_survives_runs(make_app: Callable[..., TestClient]) -> None:
    create_call = tool_use("memory", {"command": "create", "path": "/memories/p.md", "file_text": "Peter"})
    client = make_app({"MEM": [create_call, text("ok")]})
    memory = {"mode": "persistent", "space": "Team"}
    workflow_id = create(client, [agent_node("mem", "MEM", memory=memory)], [])
    assert start(client, workflow_id)["status"] == "succeeded"
    assert client.get("/api/memory").json() == [{"name": "Team", "files": 1, "size": 5}]
    content = client.get("/api/memory/Team/file", params={"path": "/memories/p.md"}).json()["content"]
    assert content == "Peter"


def test_events_stream_ends_with_run_finished(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("eins")]})
    workflow_id = create(client, [agent_node("one", "ONE")], [])
    run = start(client, workflow_id)
    with client.stream("GET", f"/api/runs/{run['id']}/events") as response:
        body = "".join(response.iter_text())
    assert body.startswith("event: snapshot")
    assert "event: end" in body


def test_run_needs_an_agent(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    workflow_id = create(client, [{"id": "start", "type": "start"}], [])
    response = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": ""})
    assert response.status_code == 400
    assert response.json()["code"] == "workflow.blocked"
    assert [item["code"] for item in response.json()["params"]["problems"]] == ["workflow.no_agents"]


def test_invalid_agent_blocks_the_run(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    bad = agent_node("bad", "X", model="claude-opus-5-5", tool_choice={"type": "any"}, tools=["calculator"])
    workflow_id = create(client, [bad], [])
    response = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": ""})
    assert response.status_code == 400
    assert response.json()["code"] == "run.agents_invalid"
    assert response.json()["params"] == {"agents": "Bad"}
    assert "tool_choice" in response.json()["detail"]


def test_usage_report_aggregates_runs(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("a"), text("b")]})
    workflow_id = create(client, [agent_node("one", "ONE")], [])
    start(client, workflow_id)
    start(client, workflow_id)
    report = client.get("/api/usage").json()
    assert report["total"]["runs"] == 2
    assert report["total"]["requests"] == 2
    assert report["by_model"][0]["key"] == "anthropic:claude-sonnet-5-5"
    assert len(client.get("/api/runs", params={"workflow_id": workflow_id}).json()) == 2


def test_exported_trace_can_be_imported_and_viewed(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Artikel")]})
    workflow_id = create(
        client, [{"id": "start", "type": "start"}, agent_node("writer", "WRITER")], [("start", "writer")]
    )
    run = start(client, workflow_id)
    trace = json.dumps(run).encode()

    # The original run is still there: importing its own trace changes nothing.
    response = client.post("/api/runs/import", content=trace)
    assert response.status_code == 200, response.text
    assert response.json()["duplicate"] is True
    assert response.json()["run"]["id"] == run["id"]

    # After the original is gone, the trace is stored as a new, read-only run.
    assert client.delete(f"/api/runs/{run['id']}").status_code == 204
    response = client.post("/api/runs/import", content=trace)
    assert response.status_code == 201, response.text
    imported = client.get(f"/api/runs/{response.json()['run']['id']}").json()
    assert imported["id"] != run["id"]
    assert imported["source_run_id"] == run["id"]
    assert imported["imported_at"] is not None
    assert imported["status"] == "succeeded"
    assert imported["output"] == "Artikel"
    assert imported["nodes"]["writer"]["usage"] == run["nodes"]["writer"]["usage"]
    assert imported["usage"]["estimated_cost_usd"] == run["usage"]["estimated_cost_usd"]

    # A second import of the same file opens the first import; re-exported imports too.
    again = client.post("/api/runs/import", content=trace)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["run"]["id"] == imported["id"]
    reexported = client.post("/api/runs/import", content=json.dumps(imported).encode())
    assert reexported.json()["run"]["id"] == imported["id"]

    # Imported runs are shown but do not count as usage of this installation.
    assert client.get("/api/usage").json()["total"]["runs"] == 0
    assert [item["id"] for item in client.get("/api/runs").json()] == [imported["id"]]


def test_unfinished_trace_is_imported_as_stopped(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Artikel")]})
    workflow_id = create(client, [agent_node("writer", "WRITER")], [])
    run = start(client, workflow_id)
    run.update(id="run_other", status="running", finished_at=None)
    run["nodes"]["writer"]["status"] = "running"
    response = client.post("/api/runs/import", content=json.dumps(run).encode())
    assert response.status_code == 201, response.text
    imported = client.get(f"/api/runs/{response.json()['run']['id']}").json()
    assert imported["status"] == "cancelled"
    assert imported["error_code"] == "trace.unfinished"
    assert "had not finished" in imported["error"]
    assert imported["nodes"]["writer"]["status"] == "cancelled"


def test_invalid_traces_are_rejected(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    cases = {
        b"not json": "trace.not_json",
        b"[1, 2]": "trace.not_object",
        json.dumps({"name": "WF", "nodes": [], "edges": []}).encode(): "trace.is_workflow",
        json.dumps({"id": "run_x"}).encode(): "trace.missing_fields",
        json.dumps(
            {"id": "run_x", "workflow_id": "w", "workflow_name": "W", "workflow": {}, "nodes": {}, "status": "nope"}
        ).encode(): "trace.invalid",
    }
    for body, code in cases.items():
        response = client.post("/api/runs/import", content=body)
        assert response.status_code == 400, body
        assert response.json()["code"] == code, response.json()
        assert response.json()["detail"]
    missing = client.post("/api/runs/import", content=json.dumps({"id": "run_x"}).encode()).json()
    assert missing["params"] == {"fields": "workflow_id, workflow, nodes, status"}
    assert client.get("/api/runs").json() == []
