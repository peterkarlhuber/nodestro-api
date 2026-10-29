"""Providers through Orkexa: settings, the legacy key, OpenAI, Gemini, endpoints, mixed workflows.

Every provider answers from a fake SDK client shaped like the real one (the same shapes Orkexa's own
contract tests use); nothing is sent anywhere. Real API calls are not part of the test suite.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from copy import deepcopy
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any
import zipfile

from fastapi.testclient import TestClient
from google.genai import types as genai_types
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.responses import Response
import pytest
from test_published_api import publish

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

from conftest import ScriptedClient, parsed, text
from test_runs import agent_node, create, start

USAGE = {
    "input_tokens": 100,
    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "output_tokens": 50,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 150,
}


# ---- fake SDK clients ------------------------------------------------------------------------


class _Script:
    """Answers in order and records every call's keyword arguments."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def next(self, kwargs: dict[str, Any]) -> Any:
        with self._lock:
            self.calls.append(deepcopy({k: v for k, v in kwargs.items() if k not in ("text_format",)}))
            if not self.answers:
                raise AssertionError("no scripted answer left")
            answer = self.answers.pop(0)
        return answer(kwargs) if callable(answer) else answer


def openai_text(value: str, model: str = "gpt-5.5") -> Response:
    return Response.model_validate(
        {
            "id": "resp_1",
            "created_at": 0,
            "model": model,
            "object": "response",
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": value, "annotations": []}],
                }
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "status": "completed",
            "usage": USAGE,
        }
    )


class _ResponsesStream:
    def __init__(self, response: Response) -> None:
        self.response = response

    def __enter__(self) -> _ResponsesStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[Any]:
        for item in self.response.output:
            if item.type == "message":
                for content in item.content:
                    if content.type == "output_text":
                        yield SimpleNamespace(type="response.output_text.delta", delta=content.text)

    def get_final_response(self) -> Response:
        return self.response


class FakeOpenAI:
    """The part of ``openai.OpenAI`` Orkexa's OpenAI adapter uses (Responses API)."""

    def __init__(self, answers: list[Any]) -> None:
        self.script = _Script(answers)

        def parse(**kwargs: Any) -> Any:
            response = self.script.next(kwargs)
            content = response.output[0].content[0]
            object.__setattr__(response, "output_parsed", kwargs["text_format"].model_validate_json(content.text))
            return response

        self.responses = SimpleNamespace(
            create=lambda **kw: self.script.next(kw),
            parse=parse,
            stream=lambda **kw: _ResponsesStream(self.script.next(kw)),
        )


def gemini_text(value: str) -> Any:
    return genai_types.GenerateContentResponse.model_validate(
        {
            "candidates": [{"content": {"role": "model", "parts": [{"text": value}]}, "finish_reason": "STOP"}],
            "usage_metadata": {"prompt_token_count": 80, "candidates_token_count": 20},
            "model_version": "gemini-3-pro",
            "response_id": "gem-1",
        }
    )


class FakeGemini:
    """The part of ``google.genai.Client`` Orkexa's Gemini adapter uses."""

    vertexai = False

    def __init__(self, answers: list[Any]) -> None:
        self.script = _Script(answers)
        self.models = SimpleNamespace(
            generate_content=lambda **kw: self.script.next(kw),
            generate_content_stream=lambda **kw: iter([self.script.next(kw)]),
        )


def chat_text(value: str, model: str = "llama3.1:8b") -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chat-1",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": value}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 60, "completion_tokens": 10, "total_tokens": 70},
        }
    )


def _chunks(completion: ChatCompletion) -> Iterator[ChatCompletionChunk]:
    base = {"id": "chat-1", "object": "chat.completion.chunk", "created": 0, "model": completion.model}
    content = completion.choices[0].message.content or ""
    yield ChatCompletionChunk.model_validate({**base, "choices": [{"index": 0, "delta": {"content": content}}]})
    yield ChatCompletionChunk.model_validate(
        {
            **base,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": completion.usage.model_dump() if completion.usage else None,
        }
    )


class FakeChat:
    """The part of ``openai.OpenAI`` the Chat Completions presets (Ollama, vLLM, compatible) use."""

    def __init__(self, answers: list[Any]) -> None:
        self.script = _Script(answers)

        def create(**kwargs: Any) -> Any:
            completion = self.script.next(kwargs)
            return _chunks(completion) if kwargs.get("stream") else completion

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


@pytest.fixture
def providers_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., TestClient]:
    """``providers_app(anthropic=..., openai=...)``: an app whose providers answer from fake clients.

    A provider without a fake client has no client at all: it needs real settings (and fails without).
    """
    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    clients: list[TestClient] = []

    def factory(**fakes: Any) -> TestClient:
        app = create_app(
            AppConfig(data_dir=tmp_path / f"data{len(clients)}"),
            client_factory=lambda provider: fakes.get(provider),
            scheduler=False,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory  # type: ignore[misc]
    for client in clients:
        client.__exit__(None, None, None)


def _provider(settings: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in settings["providers"] if item["id"] == name)


# ---- settings ----------------------------------------------------------------------------------


def test_several_providers_are_configured_at_once_and_keys_never_leave(
    providers_app: Callable[..., TestClient],
) -> None:
    client = providers_app()
    body = {
        "providers": {
            "anthropic": {"api_key": "sk-ant-first-secret"},
            "openai": {"api_key": "sk-openai-second-secret"},
            "gemini": {"api_key": "AIza-third-secret"},
            "ollama": {"base_url": "http://localhost:11434/v1/"},
            "compatible": {
                "base_url": "https://llm.example.com/v1",
                "api_key": "compat-secret",
                "capabilities": {"tools": "supported", "structured_output": "unsupported", "streaming": "unknown"},
            },
        },
        "default_model": "openai:gpt-5.5",
    }
    settings = client.put("/api/settings", json=body).json()
    text_of = json.dumps(settings) + client.get("/api/settings").text
    for secret in ("first-secret", "second-secret", "third-secret", "compat-secret"):
        assert secret not in text_of
    assert all(_provider(settings, name)["configured"] for name in ("anthropic", "openai", "gemini", "ollama"))
    assert _provider(settings, "openai")["key_source"] == "settings"
    assert _provider(settings, "ollama")["base_url"] == "http://localhost:11434/v1"
    compatible = _provider(settings, "compatible")
    assert compatible["configured"] and compatible["key_stored"]
    # "unknown" is not stored: it is what an undeclared capability is anyway.
    assert compatible["capabilities"] == {"tools": "supported", "structured_output": "unsupported"}
    assert not _provider(settings, "vllm")["configured"]  # never set up: no local server is assumed
    assert settings["default_model"] == "openai:gpt-5.5" and settings["default_provider"] == "openai"
    # An empty text removes a value; None keeps it.
    cleared = client.put("/api/settings", json={"providers": {"openai": {"api_key": ""}}}).json()
    assert not _provider(cleared, "openai")["configured"] and _provider(cleared, "anthropic")["configured"]


def test_environment_keys_win_and_are_named(
    providers_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-from-env")
    settings = providers_app().get("/api/settings").json()
    gemini = _provider(settings, "gemini")
    assert gemini["configured"] and gemini["key_source"] == "environment" and gemini["env_var"] == "GOOGLE_API_KEY"
    assert "from-env" not in json.dumps(settings)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"providers": {"mistral": {"api_key": "x"}}}, "provider.unknown"),
        ({"providers": {"ollama": {"base_url": "localhost:11434"}}}, "provider.base_url_invalid"),
        ({"providers": {"compatible": {"capabilities": {"telepathy": "supported"}}}}, "provider.capability_unknown"),
        ({"default_model": "mistral:large"}, "config.model_provider_missing"),
        ({"default_model": "llama3.1:8b"}, "config.model_provider_missing"),
    ],
)
def test_invalid_provider_settings_are_refused(
    providers_app: Callable[..., TestClient], body: dict[str, Any], code: str
) -> None:
    answer = providers_app().put("/api/settings", json=body)
    assert answer.status_code == 400 and answer.json()["code"] == code


def test_the_anthropic_key_of_earlier_versions_moves_once(providers_app: Callable[..., TestClient]) -> None:
    client = providers_app()
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    nodestro.store.put_raw("settings", "app", {"api_key": "sk-ant-legacy-key", "default_model": "claude-haiku-5-5"})
    settings = client.get("/api/settings").json()
    assert (
        _provider(settings, "anthropic")["configured"] and _provider(settings, "anthropic")["key_source"] == "settings"
    )
    # A bare Claude id keeps its meaning: it is Anthropic's model.
    assert settings["default_model"] == "claude-haiku-5-5" and settings["default_provider"] == "anthropic"
    client.put("/api/settings", json={"default_budget_usd": 1.0})
    stored = nodestro.store.get_raw("settings", "app")
    assert "api_key" not in stored and stored["providers"]["anthropic"]["api_key"] == "sk-ant-legacy-key"
    client.put("/api/settings", json={"default_budget_usd": 1.0})
    assert nodestro.store.get_raw("settings", "app") == stored  # idempotent


def test_the_single_key_field_of_earlier_clients_sets_anthropics_key(providers_app: Callable[..., TestClient]) -> None:
    client = providers_app()
    settings = client.put("/api/settings", json={"api_key": "sk-ant-from-an-old-script"}).json()
    assert _provider(settings, "anthropic")["key_stored"] and not _provider(settings, "openai")["key_stored"]
    assert "sk-ant-from-an-old-script" not in json.dumps(settings)
    settings = client.put("/api/settings", json={"api_key": ""}).json()
    assert not _provider(settings, "anthropic")["key_stored"]


def test_credentials_in_a_base_url_are_refused_and_never_shown(
    providers_app: Callable[..., TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    client = providers_app()
    for url in ("https://user:secret@llm.example.org/v1", "https://llm.example.org/v1?api_key=secret-in-query"):
        answer = client.put("/api/settings", json={"providers": {"compatible": {"base_url": url}}})
        assert answer.status_code == 400 and answer.json()["code"] == "provider.base_url_credentials"
        assert "secret" not in answer.text
    invalid = client.put("/api/settings", json={"providers": {"compatible": {"base_url": "ftp://u:secret@h"}}})
    assert invalid.json()["code"] == "provider.base_url_invalid" and "secret" not in invalid.text
    # A URL stored with credentials keeps working but is only shown redacted.
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    stored_url = "https://user:secret@llm.example.org/v1"
    nodestro.store.put_raw("settings", "app", {"providers": {"compatible": {"base_url": stored_url}}})
    settings = client.get("/api/settings").json()
    shown = _provider(settings, "compatible")["base_url"]
    assert "secret" not in json.dumps(settings) and shown == "https://[REDACTED]@llm.example.org/v1"
    assert nodestro.connection("compatible").base_url == stored_url
    # Sending the shown URL back (e.g. when saving capabilities) keeps the stored one.
    body = {"providers": {"compatible": {"base_url": shown, "capabilities": {"tools": "supported"}}}}
    saved = client.put("/api/settings", json=body)
    assert saved.status_code == 200 and "secret" not in saved.text
    assert nodestro.store.get_raw("settings", "app")["providers"]["compatible"]["base_url"] == stored_url
    # Editing around the placeholder is refused instead of storing it.
    edited = client.put("/api/settings", json={"providers": {"compatible": {"base_url": shown + "/v2"}}})
    assert edited.json()["code"] == "provider.base_url_credentials"
    assert "secret" not in caplog.text


# ---- agents of each provider -----------------------------------------------------------------


def test_openai_agent_runs_with_its_own_key_and_unknown_cost(providers_app: Callable[..., TestClient]) -> None:
    openai = FakeOpenAI([openai_text("Antwort von GPT")])
    client = providers_app(openai=openai)
    workflow_id = create(client, [agent_node("one", "ONE", model="openai:gpt-5.5")], [])
    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    assert run["nodes"]["one"]["output"] == "Antwort von GPT"
    [call] = openai.script.calls
    assert call["model"] == "gpt-5.5"  # the provider's own id, without the prefix
    usage = run["usage"]
    assert usage["requests"] == 1 and usage["input_tokens"] == 100
    assert usage["estimated_cost_usd"] is None  # no known price: unknown, never 0
    report = client.get("/api/usage").json()
    assert report["by_model"][0]["key"] == "openai:gpt-5.5"


def test_gemini_agent_runs(providers_app: Callable[..., TestClient]) -> None:
    gemini = FakeGemini([gemini_text("Antwort von Gemini")])
    client = providers_app(gemini=gemini)
    workflow_id = create(client, [agent_node("one", "ONE", model="gemini:gemini-3-pro")], [])
    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    assert run["nodes"]["one"]["output"] == "Antwort von Gemini"
    assert gemini.script.calls[0]["model"] == "gemini-3-pro"


def test_ollama_agent_with_free_model_id(providers_app: Callable[..., TestClient]) -> None:
    chat = FakeChat([chat_text("Lokal beantwortet")])
    client = providers_app(ollama=chat)
    workflow_id = create(client, [agent_node("one", "ONE", model="ollama:llama3.1:8b", effort=None)], [])
    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    assert run["nodes"]["one"]["output"] == "Lokal beantwortet"
    assert chat.script.calls[0]["model"] == "llama3.1:8b"


def test_a_workflow_mixes_providers(providers_app: Callable[..., TestClient]) -> None:
    anthropic = ScriptedClient({"RESEARCH": [text("Fakten")]})
    openai = FakeOpenAI([openai_text("Artikel")])
    client = providers_app(anthropic=anthropic, openai=openai)
    workflow_id = create(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("research", "RESEARCH", model="claude-sonnet-5-5"),  # bare id: Anthropic
            agent_node("writer", "WRITER", model="openai:gpt-5.5", task_template="Schreibe aus:\n{{Research}}"),
            {"id": "out", "type": "output"},
        ],
        [("start", "research"), ("research", "writer"), ("writer", "out")],
    )
    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    assert run["output"] == "Artikel"
    assert "Fakten" in json.dumps(openai.script.calls[0]["input"])
    assert len(anthropic.messages.calls) == 1


def test_a_missing_provider_never_falls_back(providers_app: Callable[..., TestClient]) -> None:
    anthropic = ScriptedClient({"ONE": [text("darf nicht antworten")]})
    client = providers_app(anthropic=anthropic)  # OpenAI has neither a key nor a client
    workflow_id = create(client, [agent_node("one", "ONE", model="openai:gpt-5.5")], [])
    answer = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"})
    assert answer.status_code == 400
    assert answer.json()["code"] == "api_key_missing" and answer.json()["params"] == {"provider": "openai"}
    assert anthropic.messages.calls == []
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    check = client.post(f"/api/workflows/check?workflow_id={workflow_id}", json=workflow).json()
    found = [item for item in check["problems"] if item["code"] == "check.api_key_missing"]
    assert found and found[0]["params"] == {"provider": "openai"}


def test_anthropic_only_options_are_refused_for_other_providers(providers_app: Callable[..., TestClient]) -> None:
    client = providers_app()
    for option in ({"thinking": "adaptive"}, {"server_tools": ["web_search"]}, {"cache_ttl": "1h"}):
        issues = client.post("/api/agents/validate", json={"model": "openai:gpt-5.5", **option}).json()
        assert [issue["code"] for issue in issues if issue["level"] == "error"] == ["config.anthropic_only"], option
    # For Anthropic they stay available.
    assert not [
        issue
        for issue in client.post(
            "/api/agents/validate", json={"model": "anthropic:claude-opus-5-5", "thinking": "adaptive"}
        ).json()
        if issue["level"] == "error"
    ]
    # Anthropic's fallbacks switch models within one request: another provider's model is refused.
    issues = client.post(
        "/api/agents/validate", json={"model": "claude-sonnet-5-5", "fallbacks": ["openai:gpt-5.5"]}
    ).json()
    assert [issue["code"] for issue in issues if issue["level"] == "error"] == ["config.fallback_provider"]


def test_undeclared_endpoint_capabilities_are_errors_not_dropped(providers_app: Callable[..., TestClient]) -> None:
    client = providers_app()
    client.put("/api/settings", json={"providers": {"vllm": {"base_url": "http://gpu:8000/v1"}}})
    issues = client.post(
        "/api/agents/validate", json={"model": "vllm:my-model", "effort": None, "tools": ["calculator"]}
    ).json()
    errors = [issue for issue in issues if issue["level"] == "error"]
    assert errors and errors[0]["code"] == "config.capability_unknown", issues
    assert errors[0]["params"]["capability"] == "tools"
    client.put("/api/settings", json={"providers": {"vllm": {"capabilities": {"tools": "supported"}}}})
    issues = client.post(
        "/api/agents/validate", json={"model": "vllm:my-model", "effort": None, "tools": ["calculator"]}
    ).json()
    assert not [issue for issue in issues if issue["level"] == "error"], issues


def test_unknown_models_are_accepted(providers_app: Callable[..., TestClient]) -> None:
    client = providers_app()
    issues = client.post("/api/agents/validate", json={"model": "openai:gpt-9-preview", "effort": None}).json()
    assert not [issue for issue in issues if issue["level"] == "error"], issues
    # Whether an unknown model reasons is unknown: an effort is an explicit error, never dropped silently.
    issues = client.post("/api/agents/validate", json={"model": "openai:gpt-9-preview", "effort": "low"}).json()
    errors = [issue for issue in issues if issue["level"] == "error"]
    assert (
        errors and errors[0]["code"] == "config.capability_unknown" and errors[0]["params"]["capability"] == "reasoning"
    )


# ---- internal AI functions -------------------------------------------------------------------


def test_internal_assistants_use_the_assistant_models_provider(providers_app: Callable[..., TestClient]) -> None:
    seed = {"files": [{"path": "/memories/README.md", "content": "# Aufbau"}], "explanation": "Erklärung"}
    openai = FakeOpenAI([openai_text(json.dumps(seed))])
    anthropic = ScriptedClient({})
    client = providers_app(openai=openai, anthropic=anthropic)
    client.put("/api/settings", json={"assistant_model": "openai:gpt-5.5"})
    proposal = client.post("/api/memory/propose", json={"role": "Support", "wishes": "Kunden"}).json()
    assert proposal["seed"]["files"][0]["path"] == "/memories/README.md"
    assert proposal["cost_usd"] is None  # unknown price
    assert len(openai.script.calls) == 1 and anthropic.messages.calls == []


def test_agent_design_offers_models_of_configured_providers(providers_app: Callable[..., TestClient]) -> None:
    spec = parsed(
        lambda model: model(
            name="Pruefer",
            description="Prüft.",
            system_prompt="Du prüfst.",
            model="openai:gpt-5.5",
            effort="low",
            tools=["web_search"],
            missing_tools=[],
            max_tokens=4000,
            max_tool_rounds=2,
        )
    )
    anthropic = ScriptedClient({"multi-agent systems architect": [spec]})
    client = providers_app(anthropic=anthropic, openai=FakeOpenAI([]))
    draft = client.post("/api/agents/design", json={"description": "Ein Prüfer"}).json()
    assert draft["config"]["model"] == "openai:gpt-5.5"
    # Server tools run at Anthropic: left out for an OpenAI agent, with a note.
    assert draft["config"]["server_tools"] == []
    assert any(note["code"] == "draft.server_tool_provider" for note in draft["notes"])
    prompt = json.dumps(anthropic.messages.calls[0]["messages"])
    assert "openai:gpt-5.5" in prompt and "gemini:" not in prompt  # Gemini is not configured


# ---- exports -----------------------------------------------------------------------------------


def test_export_of_an_openai_workflow_names_its_variables_without_keys(
    providers_app: Callable[..., TestClient],
) -> None:
    client = providers_app(openai=FakeOpenAI([]))
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    # A base URL with credentials can no longer be saved; one stored earlier is still never exported.
    nodestro.store.put_raw(
        "settings",
        "app",
        {"providers": {"openai": {"api_key": "sk-openai-never-exported"}, "ollama": {"base_url": "http://u:pw@h/v1"}}},
    )
    workflow_id = create(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("one", "ONE", model="openai:gpt-5.5"),
            agent_node("two", "TWO", model="ollama:llama3.1:8b", effort=None),
            {"id": "out", "type": "output"},
        ],
        [("start", "one"), ("start", "two"), ("one", "out"), ("two", "out")],
    )
    publication = publish(client, workflow_id)["publication"]
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}}).json()
    codes = {item["code"]: item for item in check["problems"]}
    assert codes["export.base_url_withheld"]["params"]["variable"] == "OLLAMA_BASE_URL"
    record = client.post(f"/api/publications/{publication['id']}/exports", json={"options": {}}).json()
    data = client.get(f"/api/exports/{record['id']}/download").content
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        everything = b"".join(archive.read(name) for name in archive.namelist())
        root = archive.namelist()[0].split("/")[0]
        manifest = json.loads(archive.read(f"{root}/workflow/manifest.json"))
        env = archive.read(f"{root}/.env.example").decode()
    assert b"never-exported" not in everything and b"u:pw@" not in everything
    assert manifest["providers"] == ["ollama", "openai"] and manifest["base_urls"] == {}
    assert "OPENAI_API_KEY=" in env and "OLLAMA_BASE_URL=" in env and "ANTHROPIC_API_KEY" not in env


# ---- data in an earlier format -----------------------------------------------------------------


def test_a_release_in_an_earlier_format_still_runs_and_exports(make_app: Callable[..., TestClient]) -> None:
    from nodestro_runtime.release import ReleaseBundle
    from test_published_api import PUBLISHED, auth, finish, form_workflow, new_key

    from nodestro_api import exporter
    from nodestro_api.publishing import RELEASES, Release

    client = make_app({"WRITER": [text("Angebot für ACME")] * 3})
    publication = publish(client, form_workflow(client))["publication"]
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    (key, _release), *_ = nodestro.store.items(RELEASES, Release)
    # An earlier format: bare Claude ids, another runtime component and an export record whose
    # options name the library source under another field.
    raw = nodestro.store.get_raw(RELEASES, key)
    bundle = raw["bundle"]
    bundle["runtime"] = {"nodestro": "0.1.0", "former_library": "0.9.0"}
    bundle["models"] = ["claude-sonnet-5-5"]
    for node in bundle["workflow"]["nodes"]:
        if node.get("agent"):
            node["agent"]["model"] = "claude-sonnet-5-5"
    raw["digest"] = ReleaseBundle.model_validate(bundle).digest()
    nodestro.store.put_raw(RELEASES, key, raw)
    record = client.post(f"/api/publications/{publication['id']}/exports", json={"options": {}}).json()
    stored_record = nodestro.store.get_raw(exporter.EXPORTS, record["id"])
    stored_record["options"] = {"former_library": "bundle", "confirm_distribution": True, "language": "de"}
    stored_record["runtime"] = {"nodestro": "0.1.0", "former_library": "0.9.0"}
    nodestro.store.put_raw(exporter.EXPORTS, record["id"], stored_record)

    nodestro._normalize_published()
    normalized = nodestro.store.get_raw(RELEASES, key)
    assert normalized["bundle"]["runtime"] == {"nodestro": "0.1.0"}
    assert normalized["digest"] == ReleaseBundle.model_validate(normalized["bundle"]).digest() != raw["digest"]
    assert normalized["bundle"]["models"] == ["claude-sonnet-5-5"]  # everything else is kept
    shown = client.get(f"/api/publications/{publication['id']}/exports").json()[0]
    assert shown["options"]["orkexa"] == "bundle" and shown["options"]["language"] == "de"
    assert shown["runtime"] == {"nodestro": "0.1.0"}
    unchanged = (nodestro.store.get_raw(RELEASES, key), nodestro.store.get_raw(exporter.EXPORTS, record["id"]))
    nodestro._normalize_published()
    assert (nodestro.store.get_raw(RELEASES, key), nodestro.store.get_raw(exporter.EXPORTS, record["id"])) == unchanged

    token = new_key(client, publication["id"])
    started = client.post(
        f"{PUBLISHED}/{publication['slug']}/runs", json={"values": {"customer": "ACME"}}, headers=auth(token)
    )
    assert started.status_code == 202, started.text
    done = finish(client, publication["slug"], token, started.json()["run_id"])
    assert done["status"] == "succeeded"
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}}).json()
    differs = [item for item in check["problems"] if item["code"] == "export.runtime_differs"]
    assert differs and differs[0]["params"]["published"] == "0.1.0 / ?"
    exported = client.post(f"/api/publications/{publication['id']}/exports", json={"options": {}})
    assert exported.status_code == 201, exported.text
