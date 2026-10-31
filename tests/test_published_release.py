"""What a published version freezes (tools, sub-workflows), continuing API runs, network access,
backups and usage accounting. No test calls the Anthropic API."""

from __future__ import annotations

from collections.abc import Callable
import io
import json
import socket
import zipfile

from fastapi.testclient import TestClient
import httpx
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from test_published_api import PUBLISHED, auth, finish, form_workflow, new_key, publish

from conftest import text, tool_use
from test_flow import edge, save
from test_runs import agent_node


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_tools_are_frozen_with_the_version(make_app: Callable[..., TestClient]) -> None:
    calls = [tool_use("celsius_to_fahrenheit", {"celsius": 100}), text("fertig")]
    client = make_app({"CONVERT": calls * 2})
    body = {"function_name": "celsius_to_fahrenheit", "code": CUSTOM_TOOL_TEMPLATE, "tests": CUSTOM_TESTS_TEMPLATE}
    assert client.post("/api/tools", json=body).json()["tool"]["version"] == 1
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("convert", "CONVERT", tools=["celsius_to_fahrenheit"]),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "convert"), edge("convert", "out")],
        name="Umrechner",
    )
    publication = publish(client, workflow_id)["publication"]
    release = client.get(f"/api/publications/{publication['id']}/releases/1").json()
    assert release["bundle"]["tools"]["celsius_to_fahrenheit"]["version"] == 1
    assert release["bundle"]["tool_trust"] == {"celsius_to_fahrenheit": "user-authored"}
    # The studio's tool changes (a new version that answers differently) ...
    changed = CUSTOM_TOOL_TEMPLATE.replace("celsius * 9 / 5 + 32", "-1.0")
    assert changed != CUSTOM_TOOL_TEMPLATE
    client.post("/api/tools", json={**body, "code": changed, "tests": "def test_nothing():\n    pass\n"})
    token = new_key(client, publication["id"])
    run_id = client.post(f"{PUBLISHED}/umrechner/runs", json={"input": "100 Grad"}, headers=auth(token)).json()[
        "run_id"
    ]
    assert finish(client, "umrechner", token, run_id)["status"] == "succeeded"
    # ... the API still runs the published version 1 of the tool.
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["nodes"]["convert"]["tool_calls"][0]["output"] == "212.0"


def test_subworkflows_are_pinned_in_the_version(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"CHILD": [text("Kind v1")], "CHANGED": [text("Kind v2")], "PARENT": [text("Ende")] * 2})
    child_id = save(
        client,
        [{"id": "start", "type": "start"}, agent_node("child", "CHILD"), {"id": "out", "type": "output"}],
        [edge("start", "child"), edge("child", "out")],
        name="Kind",
    )
    parent_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            {"id": "sub", "type": "subworkflow", "label": "Sub", "subworkflow": {"workflow_id": child_id}},
            agent_node("parent", "PARENT", task_template="{{Sub}}"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "sub"), edge("sub", "parent"), edge("parent", "out")],
        name="Eltern",
    )
    publication = publish(client, parent_id)["publication"]
    bundle = client.get(f"/api/publications/{publication['id']}/releases/1").json()["bundle"]
    assert [item["workflow_id"] for item in bundle["subworkflows"]] == [child_id]
    assert bundle["workflow"]["nodes"][1]["subworkflow"]["version"] == 1
    child = client.get(f"/api/workflows/{child_id}").json()
    child["nodes"][1]["agent"]["system_prompt"] = "CHANGED"
    client.put(f"/api/workflows/{child_id}", json=child)
    token = new_key(client, publication["id"])
    run_id = client.post(f"{PUBLISHED}/eltern/runs", json={"input": "x"}, headers=auth(token)).json()["run_id"]
    assert finish(client, "eltern", token, run_id)["status"] == "succeeded"
    assert "CHILD" in json.dumps(client.fake.messages.calls)  # type: ignore[attr-defined]
    assert "CHANGED" not in json.dumps(client.fake.messages.calls)  # type: ignore[attr-defined]


def test_orchestrator_teams_are_selected_and_pinned_in_the_version(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})

    def team(name: str, prompt: str) -> str:
        return save(
            client,
            [{"id": "start", "type": "start"}, agent_node("work", prompt), {"id": "out", "type": "output"}],
            [edge("start", "work"), edge("work", "out")],
            name=name,
            capabilities=["research"],
        )

    explicit = team("Explicit", "EXPLICIT")
    selected = team("Selected", "SELECTED")
    boss_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            {
                "id": "lead",
                "type": "orchestrator",
                "label": "Lead",
                "orchestrator": {"members": [{"workflow_id": explicit}], "select": {"capabilities": ["Research"]}},
            },
            {"id": "out", "type": "output"},
        ],
        [edge("start", "lead"), edge("lead", "out")],
        name="Boss",
    )
    publication = publish(client, boss_id)["publication"]
    bundle = client.get(f"/api/publications/{publication['id']}/releases/1").json()["bundle"]
    assert sorted(item["workflow_id"] for item in bundle["subworkflows"]) == sorted([explicit, selected])
    lead = bundle["workflow"]["nodes"][1]["orchestrator"]
    assert [(item["workflow_id"], item["version"], item["matched"]) for item in lead["members"]] == [
        (explicit, 1, []),
        (selected, 1, ["Research"]),
    ]
    assert lead["select"]["done"] is True


def test_failed_api_run_continues_with_its_version(make_app: Callable[..., TestClient]) -> None:
    script = {
        "ONE": [text("eins")],
        "TWO": [text("x", stop_reason="max_tokens"), text("zwei"), text("zwei again")],
        "CHANGED": [text("never")],
    }
    client = make_app(script)
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("one", "ONE"),
            agent_node("two", "TWO"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "one"), edge("one", "two"), edge("two", "out")],
        name="Zwei Schritte",
    )
    publication = publish(client, workflow_id)["publication"]
    slug = publication["slug"]
    token = new_key(client, publication["id"])
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"input": "x"}, headers=auth(token)).json()["run_id"]
    failed = finish(client, slug, token, run_id)
    assert failed["status"] == "failed"
    assert failed["error"]["code"]
    # The studio's workflow changes; continuing the API run still uses the published version.
    current = client.get(f"/api/workflows/{workflow_id}").json()
    current["nodes"][1]["agent"]["system_prompt"] = "CHANGED"
    client.put(f"/api/workflows/{workflow_id}", json=current)
    plan = client.get(f"/api/runs/{run_id}/resume").json()
    assert plan["possible"] and [node["action"] for node in plan["nodes"]][:2] == ["reuse", "reuse"]
    resumed = client.post(f"{PUBLISHED}/{slug}/runs/{run_id}/resume", json={}, headers=auth(token))
    assert resumed.status_code == 202, resumed.text
    done = finish(client, slug, token, resumed.json()["run_id"])
    assert done["status"] == "succeeded"
    assert done["resumed_from"] == run_id
    studio = client.get(f"/api/runs/{resumed.json()['run_id']}").json()
    assert studio["nodes"]["one"]["reused_from"] == run_id and studio["release_number"] == 1
    # The studio can continue (here: restart) an API run as well, with the same version.
    again = client.post(f"/api/runs/{run_id}/resume", json={"mode": "restart"})
    assert again.status_code == 201, again.text
    restarted = again.json()
    assert restarted["trigger"] == "api" and restarted["release_id"] == studio["release_id"]
    assert "never" not in json.dumps(client.get(f"/api/runs/{restarted['id']}").json())


def test_network_access_is_off_by_default_and_separate(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Angebot")] * 3})
    workflow_id = form_workflow(client)
    publication = publish(client, workflow_id)["publication"]
    token = new_key(client, publication["id"])
    status = client.get("/api/api-network").json()
    assert status["enabled"] is False and status["running"] is False
    port = _free_port()
    started = client.put("/api/api-network", json={"enabled": True, "host": "127.0.0.1", "port": port})
    assert started.status_code == 200, started.text
    assert started.json()["running"] is True
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, timeout=10) as remote:
            assert remote.get("/health").json() == {"status": "ok"}
            # A publication is reachable in the network only when it is shared there.
            hidden = remote.get(f"{PUBLISHED}/{publication['slug']}", headers=auth(token))
            assert hidden.status_code == 404
            client.patch(f"/api/publications/{publication['id']}", json={"network": True})
            shown = remote.get(f"{PUBLISHED}/{publication['slug']}", headers=auth(token))
            assert shown.status_code == 200, shown.text
            assert remote.get(f"{PUBLISHED}/{publication['slug']}").status_code == 401
            # The studio and its internal endpoints are never part of the network listener.
            for path in ("/api/workflows", "/api/settings", "/api/api-keys", "/", "/api/openapi.json", "/docs"):
                assert remote.get(path).status_code == 404, path
            overview = client.get(f"/api/workflows/{workflow_id}/api").json()
            assert overview["network_urls"]
        stopped = client.put("/api/api-network", json={"enabled": False, "host": "127.0.0.1", "port": port}).json()
        assert stopped["running"] is False
        with httpx.Client(base_url=base, timeout=2) as remote:
            try:
                remote.get("/health")
                raise AssertionError("listener still answers")
            except httpx.TransportError:
                pass
    finally:
        client.put("/api/api-network", json={"enabled": False, "host": "127.0.0.1", "port": port})


def test_network_port_in_use_is_reported(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        status = client.put("/api/api-network", json={"enabled": True, "host": "127.0.0.1", "port": port}).json()
    assert status["running"] is False
    assert status["error_code"] == "lan.port_unavailable"
    client.put("/api/api-network", json={"enabled": False, "host": "127.0.0.1", "port": port})


def test_backup_keeps_publications_but_never_keys(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("a")]})
    publication = publish(client, form_workflow(client))["publication"]
    token = new_key(client, publication["id"])
    data = client.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        database = archive.read("nodestro.db")
    assert token.split("_", 2)[2].encode() not in database
    assert b"api_releases" in database and b'"api_keys"' not in database
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    nodestro.network._save(nodestro.network.config().model_copy(update={"enabled": True, "port": _free_port()}))
    info = client.post("/api/backup/inspect", files={"file": ("b.zip", data, "application/zip")}).json()
    assert client.post("/api/backup/restore", json={"id": info["id"], "confirm": True}).status_code == 200
    # The key of this computer still works, the publication is back, network access is off.
    assert client.get(f"{PUBLISHED}/{publication['slug']}", headers=auth(token)).status_code == 200
    assert client.get("/api/api-network").json()["enabled"] is False


def test_api_runs_are_counted_once(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("a")] * 2})
    publication = publish(client, form_workflow(client))["publication"]
    token = new_key(client, publication["id"])
    run_id = client.post(
        f"{PUBLISHED}/{publication['slug']}/runs", json={"values": {"customer": "A"}}, headers=auth(token)
    ).json()["run_id"]
    finish(client, publication["slug"], token, run_id)
    report = client.get("/api/usage").json()
    assert report["total"]["runs"] == 1 and report["total"]["requests"] == 1
    assert [(item["key"], item["runs"]) for item in report["by_source"]] == [("api_run", 1)]
    assert report["recent"] == []
    runs = client.get("/api/runs").json()
    assert [item["trigger"] for item in runs] == ["api"]


def test_a_release_runs_untrusted_tools_only_with_isolation(make_app: Callable[..., TestClient]) -> None:
    """The studio's provenance decides, also for frozen copies: after a restore-like loss of provenance the
    published tool is untrusted and the run fails before the tool runs (ADR-0006)."""
    calls = [tool_use("celsius_to_fahrenheit", {"celsius": 100}), text("fertig")]
    client = make_app({"CONVERT": calls * 2})
    body = {"function_name": "celsius_to_fahrenheit", "code": CUSTOM_TOOL_TEMPLATE, "tests": CUSTOM_TESTS_TEMPLATE}
    client.post("/api/tools", json=body)
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("convert", "CONVERT", tools=["celsius_to_fahrenheit"]),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "convert"), edge("convert", "out")],
        name="Umrechner",
    )
    publication = publish(client, workflow_id)["publication"]
    client.app.state.nodestro.tools.provenance.clear()  # type: ignore[attr-defined]
    token = new_key(client, publication["id"])
    run_id = client.post(f"{PUBLISHED}/umrechner/runs", json={"input": "100 Grad"}, headers=auth(token)).json()[
        "run_id"
    ]
    assert finish(client, "umrechner", token, run_id)["status"] == "failed"
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["nodes"]["convert"].get("tool_calls", []) == []
    assert "sandbox.isolation_required" in json.dumps(run)
