"""Copies of format 2 templates: teams of parts, knowledge files, MCP integrations, model tiers.

The copy route (``POST /api/library/{id}/copy``) resolves everything a copy needs before it stores anything; a
copy that cannot be made leaves no workflow, knowledge file or input file behind.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import importlib.util
from typing import Any

from fastapi.testclient import TestClient
import nodestro_examples as library
from nodestro_runtime.testing import AnswerAllClient
import pytest
from test_library import TeamsClient, wait_run

SDK = importlib.util.find_spec("mcp") is not None
needs_sdk = pytest.mark.skipif(not SDK, reason="the MCP SDK needs Python 3.11+")


def nodestro(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


def stored_nothing(client: TestClient) -> None:
    assert client.get("/api/workflows").json() == []
    assert client.get("/api/knowledge").json() == []
    assert nodestro(client).inputs.all() == []


# ---- teams ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("template_id", ["business-idea", "research-hypotheses", "construction-delay"])
def test_an_orchestrator_copy_pins_its_teams_to_the_saved_parts(
    make_app: Callable[..., TestClient], template_id: str
) -> None:
    client = make_app()
    workflow = client.post(f"/api/library/{template_id}/copy", json={"language": "en"}).json()
    parts = library.part_ids(template_id)
    saved = {item["id"]: item for item in client.get("/api/workflows").json()}
    assert len(saved) == len(parts) + 1
    [orchestrator] = [node for node in workflow["nodes"] if node["type"] == "orchestrator"]
    members = orchestrator["orchestrator"]["members"]
    spec = next(node for node in library.structure(template_id).nodes if node.orchestrator is not None)
    assert len(members) == len(spec.orchestrator.members)  # type: ignore[union-attr]
    for member, part_id in zip(members, spec.orchestrator.members, strict=True):  # type: ignore[union-attr]
        assert member["workflow_id"] in saved and member["version"] == 1
        part = client.get(f"/api/workflows/{member['workflow_id']}").json()
        assert part["name"] == library.part_texts(part_id, "en").name
        assert part["capabilities"] == library.part(part_id).capabilities
        assert len(client.get(f"/api/workflows/{member['workflow_id']}/versions").json()) == 1
    # Nothing in the copy still names the library.
    assert library.PART_PREFIX not in str(workflow)


def test_an_orchestrator_copy_runs_its_teams(make_app: Callable[..., TestClient]) -> None:
    client = make_app(client=TeamsClient())
    workflow = client.post("/api/library/business-idea/copy", json={"language": "de"}).json()
    start = next(node for node in workflow["nodes"] if node["type"] == "start")
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": start["start"]["default_input"]})
    assert run.status_code in (200, 201), run.text
    result = wait_run(client, run.json()["id"])
    assert result["status"] == "succeeded", result.get("error")
    members = {
        member["workflow_id"]
        for node in workflow["nodes"]
        if node["orchestrator"]
        for member in node["orchestrator"]["members"]
    }
    children = result["nodes"]["coach"]["child_run_ids"]
    assert children, "the orchestrator delegated to a team"
    for child_id in children:
        assert client.get(f"/api/runs/{child_id}").json()["workflow_id"] in members

    # Deleting the orchestrated run deletes its team runs too: they hold copies of its inputs and results.
    assert client.delete(f"/api/runs/{result['id']}").status_code == 204
    for child_id in children:
        assert client.get(f"/api/runs/{child_id}").status_code == 404


# ---- model tiers -------------------------------------------------------------------------------------------------


def test_a_copy_maps_the_model_tiers_to_the_workspace_provider(make_app: Callable[..., TestClient]) -> None:
    def models_of(workflow: dict[str, Any]) -> set[str]:
        found = {node["agent"]["model"] for node in workflow["nodes"] if node["agent"]}
        return found | {node["orchestrator"]["model"] for node in workflow["nodes"] if node["orchestrator"]}

    client = make_app()
    client.post("/api/library/construction-delay/copy", json={"language": "en"})
    anthropic = client.get("/api/workflows").json()
    tiers = set(library.DEFAULT_MODELS.values())
    assert set().union(*(models_of(client.get(f"/api/workflows/{item['id']}").json()) for item in anthropic)) <= tiers

    changed = client.put("/api/settings", json={"default_model": "openai:gpt-5.5"})
    assert changed.status_code == 200, changed.text
    client.post("/api/library/construction-delay/copy", json={"language": "en"})
    created = [item for item in client.get("/api/workflows").json() if item["id"] not in {w["id"] for w in anthropic}]
    workflows = [client.get(f"/api/workflows/{item['id']}").json() for item in created]
    expected = library.model_choice("openai:gpt-5.5", client.get("/api/settings").json()["assistant_model"])
    models = set().union(*(models_of(workflow) for workflow in workflows))
    assert models and models <= {expected.fast, expected.standard, expected.strong}
    assert all(not model.startswith("anthropic:") for model in models)
    # Server tools run on Anthropic's side only; with another provider they are left out.
    assert all(
        not node["agent"]["server_tools"] for workflow in workflows for node in workflow["nodes"] if node["agent"]
    )


# ---- knowledge files ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("template_id", "node_id"), [("quality-8d", "root_cause"), ("contract-review", "checkpoints")])
def test_knowledge_files_are_stored_once_and_given_to_the_agents(
    make_app: Callable[..., TestClient], template_id: str, node_id: str
) -> None:
    client = make_app()
    first = client.post(f"/api/library/{template_id}/copy", json={"language": "en"})
    assert first.status_code == 201, first.text
    [file] = client.get("/api/knowledge").json()
    name = library.texts(template_id, "en").name
    assert file["collection"] == f"Examples: {name}" and file["searchable"]
    agent = next(node["agent"] for node in first.json()["nodes"] if node["id"] == node_id)
    assert agent["knowledge"]["file_ids"] == [file["id"]] and agent["knowledge"]["mode"] == "search"
    assert library.KNOWLEDGE_PREFIX not in str(first.json())

    # A second copy reuses the identical file; another language stores its own one.
    second = client.post(f"/api/library/{template_id}/copy", json={"language": "en"}).json()
    again = next(node["agent"] for node in second["nodes"] if node["id"] == node_id)
    assert again["knowledge"]["file_ids"] == [file["id"]]
    assert len(client.get("/api/knowledge").json()) == 1
    german = client.post(f"/api/library/{template_id}/copy", json={"language": "de"}).json()
    files = client.get("/api/knowledge").json()
    assert len(files) == 2
    own = next(node["agent"] for node in german["nodes"] if node["id"] == node_id)["knowledge"]["file_ids"]
    assert own != [file["id"]]
    assert next(item for item in files if item["id"] == own[0])["collection"].startswith("Beispiele: ")


def test_a_failed_copy_leaves_nothing_behind(make_app: Callable[..., TestClient], monkeypatch: Any) -> None:
    from nodestro_api import api

    client = make_app()

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("the test cases cannot be built")

    monkeypatch.setattr(api.library_samples, "suite_cases", broken)
    # The parts are saved before the test cases are built; they are removed again.
    response = client.post("/api/library/business-idea/copy", json={"language": "en", "tests": True})
    assert response.status_code == 400 and "test cases" in response.json()["detail"]
    stored_nothing(client)


# ---- integrations ------------------------------------------------------------------------------------------------


@pytest.fixture
def erp() -> Iterator[Any]:
    """A local MCP server with the tools of the supplier comparison's ERP and the customer service's shop."""
    from mcp.server.mcpserver import MCPServer
    from mcp_test_server import serve
    from mcp_types import ToolAnnotations

    server = MCPServer("nodestro-examples-test")
    read = ToolAnnotations(readOnlyHint=True)

    @server.tool(annotations=read)
    def list_suppliers(category: str = "") -> str:
        """List the suppliers of a category."""
        return f"suppliers {category}"

    @server.tool(annotations=read)
    def supplier_scorecard(supplier: str) -> str:
        """The scorecard of one supplier."""
        return f"scorecard {supplier}"

    @server.tool(annotations=read)
    def get_order(order_id: str) -> str:
        """One order."""
        return f"order {order_id}"

    handle = serve(server)
    yield handle
    handle.stop()


def add_server(client: TestClient, url: str, decisions: dict[str, str | None]) -> str:
    """Register the server, discover its tools and decide them (access, or ``None`` to leave a tool undecided)."""
    added = client.post("/api/mcp/servers", json={"name": "ERP", "url": url, "allow_loopback": True})
    assert added.status_code == 201, added.text
    server_id = str(added.json()["id"])
    found = client.post(f"/api/mcp/servers/{server_id}/discover")
    assert found.status_code == 200, found.text
    hashes = {tool["name"]: tool["hash"] for tool in found.json()["tools"]}
    for name, access in decisions.items():
        if access is None:
            continue
        decided = client.post(
            f"/api/mcp/servers/{server_id}/tools/{name}/decision",
            json={"approve": True, "access": access, "expected_hash": hashes[name]},
        )
        assert decided.status_code == 200, decided.text
    return server_id


@needs_sdk
def test_a_connected_integration_gives_the_agent_the_approved_read_tools(
    make_app: Callable[..., TestClient], erp: Any
) -> None:
    client = make_app()
    server_id = add_server(client, erp.url, {"list_suppliers": "read", "supplier_scorecard": "read"})
    response = client.post(
        "/api/library/supplier-comparison/copy", json={"language": "en", "integrations": {"erp": server_id}}
    )
    assert response.status_code == 201, response.text
    workflow = response.json()
    risk = next(node["agent"] for node in workflow["nodes"] if node["id"] == "risk")
    assert "mcp__erp__list_suppliers" in risk["tools"] and "mcp__erp__supplier_scorecard" in risk["tools"]
    note = library.texts("supplier-comparison", "en").integrations["erp"]
    assert note.online in workflow["description"] and note.offline not in workflow["description"]
    # The check before a run accepts the tools (approved, read-only).
    check = client.post(f"/api/workflows/check?workflow_id={workflow['id']}", json=workflow).json()
    assert not [item for item in check["problems"] if item["code"].startswith("mcp.")], check["problems"]


def test_an_offline_copy_has_no_mcp_tools_and_says_so(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    for template_id, integration in (("supplier-comparison", "erp"), ("customer-service", "shop")):
        workflow = client.post(f"/api/library/{template_id}/copy", json={"language": "en"}).json()
        tools = [tool for node in workflow["nodes"] if node["agent"] for tool in node["agent"]["tools"]]
        assert not [tool for tool in tools if tool.startswith("mcp__")]
        assert library.texts(template_id, "en").integrations[integration].offline in workflow["description"]


@needs_sdk
@pytest.mark.parametrize(
    ("decisions", "missing", "not_read_only"),
    [
        ({"list_suppliers": "read", "supplier_scorecard": None}, "supplier_scorecard", ""),
        ({"list_suppliers": "read", "supplier_scorecard": "write"}, "", "supplier_scorecard"),
    ],
)
def test_an_integration_without_approved_read_tools_stores_nothing(
    make_app: Callable[..., TestClient], erp: Any, decisions: dict[str, str | None], missing: str, not_read_only: str
) -> None:
    client = make_app()
    server_id = add_server(client, erp.url, decisions)
    response = client.post(
        "/api/library/supplier-comparison/copy", json={"language": "en", "integrations": {"erp": server_id}}
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "library.integration_unavailable"
    assert body["params"]["integration"] == "erp" and body["params"]["server"] == server_id
    assert body["params"]["missing"] == missing and body["params"]["not_read_only"] == not_read_only
    assert "supplier_scorecard" in body["detail"]
    stored_nothing(client)


def test_unknown_integrations_and_servers_are_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    unknown = client.post("/api/library/supplier-comparison/copy", json={"integrations": {"crm": "mcp_x"}})
    assert unknown.status_code == 422 and unknown.json()["code"] == "library.integration_unknown"
    no_server = client.post("/api/library/supplier-comparison/copy", json={"integrations": {"erp": "mcp_missing"}})
    assert no_server.status_code == 404 and no_server.json()["code"] == "not_found"
    stored_nothing(client)


@needs_sdk
def test_tests_and_integrations_cannot_be_combined(make_app: Callable[..., TestClient], erp: Any) -> None:
    client = make_app(client=AnswerAllClient())
    server_id = add_server(client, erp.url, {"get_order": "read"})
    refused = client.post(
        "/api/library/customer-service/copy",
        json={"language": "en", "tests": True, "integrations": {"shop": server_id}},
    )
    assert refused.status_code == 422 and refused.json()["code"] == "library.tests_need_offline"
    stored_nothing(client)
    # Offline, the test series is created as before; connected, the copy has no test series.
    offline = client.post("/api/library/customer-service/copy", json={"language": "en", "tests": True}).json()
    assert len(client.get(f"/api/benchmarks?workflow_id={offline['id']}").json()) == 1
    connected = client.post(
        "/api/library/customer-service/copy", json={"language": "en", "integrations": {"shop": server_id}}
    ).json()
    assert client.get(f"/api/benchmarks?workflow_id={connected['id']}").json() == []
    policy = next(node["agent"] for node in connected["nodes"] if node["id"] == "policy")
    assert "mcp__erp__get_order" in policy["tools"] and "days_between" in policy["tools"]
