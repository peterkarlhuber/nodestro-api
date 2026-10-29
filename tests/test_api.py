from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
import pytest

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

from conftest import parsed, text


def test_meta_lists_models_and_server_tools(make_app: Callable[..., TestClient]) -> None:
    meta = make_app().get("/api/meta").json()
    ids = [model["id"] for model in meta["models"]]
    assert ids[:2] == ["anthropic:claude-opus-5-5", "anthropic:claude-sonnet-5-5"]
    assert {"openai:gpt-5.5", "gemini:gemini-3-pro"} <= set(ids)
    # Models of other providers have no known price: unknown, never zero.
    gpt = next(model for model in meta["models"] if model["id"] == "openai:gpt-5.5")
    assert gpt["provider"] == "openai" and gpt["input_usd_per_mtok"] is None
    assert [item["id"] for item in meta["providers"]] == [
        "anthropic",
        "openai",
        "gemini",
        "compatible",
        "ollama",
        "vllm",
    ]
    assert "tools" in meta["capabilities"]
    assert {tool["key"] for tool in meta["server_tools"]} == {"web_search", "web_fetch", "code_execution"}


def test_workflow_crud_duplicate_and_import(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    created = client.post("/api/workflows", json={"name": "A", "nodes": [{"id": "n", "type": "start"}]}).json()
    updated = client.put(f"/api/workflows/{created['id']}", json={**created, "name": "B"}).json()
    assert updated["name"] == "B" and updated["created_at"] == created["created_at"]
    copy = client.post(f"/api/workflows/{created['id']}/duplicate").json()
    assert copy["name"] == "B (copy)" and copy["id"] != created["id"]
    german = client.post(f"/api/workflows/{created['id']}/duplicate", headers={"Accept-Language": "de-AT,de;q=0.9"})
    assert german.json()["name"] == "B (Kopie)"
    imported = client.post("/api/workflows/import", json=updated).json()
    assert imported["id"] not in (created["id"], copy["id"])
    assert len(client.get("/api/workflows").json()) == 4
    assert client.delete(f"/api/workflows/{created['id']}").status_code == 204
    assert client.get(f"/api/workflows/{created['id']}").status_code == 404


def test_agent_templates(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    template = client.post("/api/agents/templates", json={"agent": {"name": "Lektor"}}).json()
    changed = client.put(f"/api/agents/templates/{template['id']}", json={"agent": {"name": "Lektorin"}}).json()
    assert changed["agent"]["name"] == "Lektorin"
    assert [t["agent"]["name"] for t in client.get("/api/agents/templates").json()] == ["Lektorin"]
    assert client.delete(f"/api/agents/templates/{template['id']}").status_code == 204


def test_settings_never_return_the_key(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = make_app()

    def anthropic(settings: dict[str, Any]) -> dict[str, Any]:
        return next(item for item in settings["providers"] if item["id"] == "anthropic")

    assert anthropic(client.get("/api/settings").json())["key_source"] == "none"
    body = {"providers": {"anthropic": {"api_key": "sk-ant-secret"}}, "default_budget_usd": 1.5}
    settings = client.put("/api/settings", json=body).json()
    assert anthropic(settings)["configured"] is True and anthropic(settings)["key_source"] == "settings"
    assert settings["default_budget_usd"] == 1.5
    assert "sk-ant" not in client.get("/api/settings").text
    cleared = client.put("/api/settings", json={"providers": {"anthropic": {"api_key": ""}}}).json()
    assert anthropic(cleared)["configured"] is False


def test_settings_store_the_interface_language(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    assert client.get("/api/settings").json()["language"] is None
    assert client.put("/api/settings", json={"language": "pt-BR"}).json()["language"] == "pt-BR"
    assert client.get("/api/settings").json()["language"] == "pt-BR"
    assert client.put("/api/settings", json={"language": "xx"}).status_code == 422
    assert client.put("/api/settings", json={"language": None}).json()["language"] is None


@pytest.mark.parametrize(
    ("agent", "code", "message", "params"),
    [
        (
            {"model": "claude-opus-5-5", "tool_choice": {"type": "any"}, "tools": ["calculator"]},
            "config.forced_tool_choice",
            "tool_choice",
            {"model": "claude-opus-5-5"},
        ),
        ({"model": "claude-opus-5-5", "thinking": "disabled"}, "config.thinking_not_disableable", "disable", {}),
        ({"tools": ["nope"]}, "config.unknown_tools", "Unknown tools", {"names": "nope"}),
        ({"output": {"mode": "structured", "fields": []}}, "config.output_no_fields", "at least one field", {}),
        (
            {"output": {"mode": "structured", "fields": [{"name": "class"}]}},
            "config.output_field_name",
            "Invalid field name",
            {"name": "class"},
        ),
        ({"messages": [{"role": "assistant", "content": "hi"}]}, "config.examples_order", "start with the user", {}),
        ({"memory": {"mode": "persistent"}}, "config.memory_space_missing", "memory space", {}),
        (
            {"model": "claude-haiku-5-5", "tool_choice": {"type": "tool", "name": "calculator"}},
            "config.tool_choice_unknown",
            "does not have",
            {"name": "calculator"},
        ),
    ],
)
def test_validation_uses_library_rules(
    make_app: Callable[..., TestClient], agent: dict[str, Any], code: str, message: str, params: dict[str, str]
) -> None:
    issues = make_app().post("/api/agents/validate", json=agent).json()
    errors = [issue for issue in issues if issue["level"] == "error"]
    assert any(
        issue["code"] == code and message in issue["message"] and params.items() <= issue["params"].items()
        for issue in errors
    ), issues


def test_valid_agent_has_no_errors(make_app: Callable[..., TestClient]) -> None:
    agent = {"tools": ["calculator"], "server_tools": ["web_search"], "memory": {"mode": "persistent", "space": "A"}}
    issues = make_app().post("/api/agents/validate", json=agent).json()
    assert [issue for issue in issues if issue["level"] == "error"] == []


def test_knowledge_upload_search_and_agent_views(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    files = [
        ("files", ("garantie.md", b"# Garantie\n\nDie Garantie betraegt 24 Monate ab Kauf.", "text/markdown")),
        ("files", ("versand.txt", b"Versand dauert drei Werktage.", "text/plain")),
    ]
    uploaded = client.post("/api/knowledge", files=files, data={"description": "Handbuch"}).json()
    assert [item["name"] for item in uploaded] == ["garantie.md", "versand.txt"]
    assert all(item["chunks"] >= 1 for item in uploaded)
    garantie, versand = uploaded[0]["id"], uploaded[1]["id"]

    hits = client.get("/api/knowledge/search", params={"q": "Garantie Monate"}).json()
    assert hits[0]["file_id"] == garantie
    scoped = client.get("/api/knowledge/search", params={"q": "Garantie", "file_ids": versand}).json()
    assert scoped == []

    changed = client.patch(f"/api/knowledge/{garantie}", json={"description": "Garantiebedingungen"}).json()
    assert changed["description"] == "Garantiebedingungen"
    assert client.get(f"/api/knowledge/{versand}/content").content == b"Versand dauert drei Werktage."

    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    from nodestro_runtime.schemas import KnowledgeConfig

    store, index = nodestro.knowledge.views(KnowledgeConfig(file_ids=[garantie], mode="on_demand"))
    assert index is None and [entry.id for entry in store.list()] == [garantie]
    store, index = nodestro.knowledge.views(KnowledgeConfig(file_ids=[versand], mode="search"))
    assert store is None and [hit.source for hit in index.search("Versand")] == [versand]

    assert client.delete(f"/api/knowledge/{garantie}").status_code == 204
    assert [item["id"] for item in client.get("/api/knowledge").json()] == [versand]


def test_context_knowledge_is_sent_before_the_task(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"CTX": [text("ok")]})
    uploaded = client.post("/api/knowledge", files=[("files", ("a.txt", b"Wissen", "text/plain"))]).json()
    agent = {"name": "C", "system_prompt": "CTX", "knowledge": {"file_ids": [uploaded[0]["id"]], "mode": "context"}}
    workflow = client.post("/api/workflows", json={"nodes": [{"id": "c", "type": "agent", "agent": agent}]}).json()
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": "Frage"}).json()
    from test_runs import wait_run

    assert wait_run(client, run["id"])["status"] == "succeeded"
    messages = client.fake.messages.calls[0]["messages"]  # type: ignore[attr-defined]
    assert messages[0]["content"][0]["type"] == "document"
    assert messages[-1] == {"role": "user", "content": "Frage"}


def test_memory_spaces_can_be_edited(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    client.put("/api/memory/Projekt/file", json={"path": "/memories/a.md", "content": "eins"})
    assert client.get("/api/memory/Projekt").json() == [{"path": "/memories/a.md", "size": 4}]
    assert client.get("/api/memory/Projekt/file", params={"path": "/memories/a.md"}).json()["content"] == "eins"
    assert client.put("/api/memory/Projekt/file", json={"path": "/etc/x", "content": "x"}).status_code == 422
    client.delete("/api/memory/Projekt/file", params={"path": "/memories/a.md"})
    assert client.get("/api/memory/Projekt").json() == []
    assert client.get("/api/memory/..%2F/file", params={"path": "/memories/a.md"}).status_code in (400, 404)


def test_custom_tool_is_checked_saved_and_run(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    body = {"function_name": "celsius_to_fahrenheit", "code": CUSTOM_TOOL_TEMPLATE, "tests": CUSTOM_TESTS_TEMPLATE}
    check = client.post("/api/tools/check", json=body).json()
    assert check["ok"], check
    assert [test["name"] for test in check["tests"]] == ["test_boiling_point", "test_freezing_point"]
    saved = client.post("/api/tools", json=body).json()
    assert saved["tool"]["kind"] == "custom"
    result = client.post("/api/tools/celsius_to_fahrenheit/run", json={"input": {"celsius": 100}}).json()
    assert result == {"output": "212.0", "is_error": False}
    again = client.post("/api/tools", json=body).json()
    assert again["tool"]["version"] == 2
    assert client.delete("/api/tools/celsius_to_fahrenheit").status_code == 204
    assert "celsius_to_fahrenheit" not in {tool["name"] for tool in client.get("/api/tools").json()}
    assert client.delete("/api/tools/calculator").status_code == 400


def test_unsafe_custom_tool_is_rejected(make_app: Callable[..., TestClient]) -> None:
    code = (
        'import os\n\ndef leak(name: str) -> str:\n    """Leak.\n\n    Args:\n        name: x.\n\n'
        '    Returns:\n        y.\n    """\n    return os.environ[name]\n'
    )
    tests = "def test_x():\n    pass\n"
    result = make_app().post("/api/tools", json={"function_name": "leak", "code": code, "tests": tests})
    assert result.json()["tool"] is None
    errors = result.json()["check"]["errors"]
    assert any(error["code"] == "tool.code_check" and "os" in error["message"] for error in errors)


def test_generated_tool_waits_for_approval(make_app: Callable[..., TestClient]) -> None:
    proposal = parsed(
        lambda model: model(
            function_name="celsius_to_fahrenheit",
            description="Converts temperatures.",
            code=CUSTOM_TOOL_TEMPLATE,
            tests=CUSTOM_TESTS_TEMPLATE,
        )
    )
    client = make_app({"expert Python developer": [proposal]})
    job = client.post("/api/tools/jobs", json={"requirement": "Celsius in Fahrenheit umrechnen"}).json()
    deadline = time.monotonic() + 20
    while job["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/tools/jobs/{job['id']}").json()
    assert job["status"] == "awaiting_approval", job
    assert job["message_code"] == "job.awaiting_approval"
    assert job["approval"]["code"] == CUSTOM_TOOL_TEMPLATE
    assert len(job["attempts"]) == 1
    client.post(f"/api/tools/jobs/{job['id']}/decision", json={"approved": True})
    while job["status"] == "awaiting_approval" and time.monotonic() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/tools/jobs/{job['id']}").json()
    assert job["status"] == "approved", job
    assert job["message_code"] == "job.approved" and job["message_params"] == {"name": "celsius_to_fahrenheit"}
    tool = client.get("/api/tools/celsius_to_fahrenheit").json()
    assert tool["kind"] == "generated" and tool["requirement"] == "Celsius in Fahrenheit umrechnen"


def test_design_agent_maps_the_spec(make_app: Callable[..., TestClient]) -> None:
    spec = parsed(
        lambda model: model(
            name="Rechnungs_Pruefer",
            description="Prüft Rechnungen.",
            system_prompt="Du prüfst Rechnungen.",
            model="claude-haiku-5-5",
            effort="max",
            tools=["calculator", "web_search", "unknown_tool"],
            missing_tools=[{"name": "iban_check", "requirement": "Prüft IBANs"}],
            max_tokens=999_999,
            max_tool_rounds=3,
        )
    )
    client = make_app({"multi-agent systems architect": [spec]})
    body = {"description": "Ein Agent, der Rechnungen prüft", "language": "de"}
    draft = client.post("/api/agents/design", json=body).json()
    system = str(client.fake.messages.calls[0]["system"])  # type: ignore[attr-defined]
    assert "in German, the language of the user's interface" in system
    config = draft["config"]
    assert config["name"] == "Rechnungs Pruefer"
    assert config["model"] == "anthropic:claude-haiku-5-5"
    assert config["tools"] == ["calculator"] and config["server_tools"] == ["web_search"]
    assert config["effort"] == "xhigh" and config["max_tokens"] == 32_000
    assert draft["missing_tools"] == [{"name": "iban_check", "requirement": "Prüft IBANs"}]
    assert {"code": "draft.unknown_tool", "params": {"name": "unknown_tool"}}.items() <= draft["notes"][0].items()


def test_memory_proposal(make_app: Callable[..., TestClient]) -> None:
    seed = parsed(
        lambda model: model(files=[{"path": "/memories/README.md", "content": "# Aufbau"}], explanation="Erklärung")
    )
    client = make_app({"long-term memory": [seed]})
    body = {"role": "Support", "wishes": "Kundenwünsche", "language": "it"}
    result = client.post("/api/memory/propose", json=body).json()
    assert result["seed"]["files"][0]["path"] == "/memories/README.md"
    system = str(client.fake.messages.calls[0]["system"])  # type: ignore[attr-defined]
    assert "in Italian" in system and "lowercase English" in system


def test_generated_tools_are_written_in_english(make_app: Callable[..., TestClient]) -> None:
    from nodestro_runtime.tool_library import CODING_PROMPT

    assert "Write everything in English, whatever language the requirement uses" in CODING_PROMPT


def test_missing_api_key_is_explained(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with TestClient(create_app(AppConfig(data_dir=tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.post("/api/agents/design", json={"description": "x"})
        assert response.status_code == 400
        assert response.json()["code"] == "api_key_missing"
        assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_the_openapi_contract_is_up_to_date() -> None:
    # The committed document is the API contract the SDKs are generated from.
    from nodestro_api import openapi

    committed = Path(__file__).resolve().parents[1] / "openapi" / "nodestro-api.json"
    assert committed.read_text(encoding="utf-8") == openapi.text(), "run `make openapi`"
