"""The local model of every agent when an example is copied (EX-011): task profiles, installed models, hardware,
language, the European preference, and the person's choice first."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.local_models import Hardware
import pytest

from nodestro_api import library_models

MakeApp = Callable[..., TestClient]
GB = 1024**3
CPU16 = Hardware(system="linux", machine="x86_64", cpus=4, ram_bytes=16 * GB)
GPU48 = Hardware(system="linux", machine="x86_64", cpus=16, ram_bytes=64 * GB, gpu="nvidia", gpu_memory_bytes=48 * GB)
CHAT = ["completion", "tools", "vision", "thinking"]


def _local(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, installed: list[str], hardware: Hardware) -> TestClient:
    client = make_app(local_defaults=True)
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    nodestro.local_models._hardware = hardware
    monkeypatch.setattr(
        library_models, "_installed", lambda _ctx: (list(installed), {name: list(CHAT) for name in installed})
    )
    return client


def _by_key(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {agent["key"]: agent for agent in plan["agents"]}


def test_a_cloud_workspace_keeps_its_tiers(make_app: MakeApp) -> None:
    client = make_app()
    plan = client.get("/api/library/customer-service/models", params={"language": "en"}).json()
    assert plan["local"] is False and plan["judge"] is None
    assert all(agent["model"] is None for agent in plan["agents"])
    assert _by_key(plan)["triage"]["task"] == "classify"


def test_each_agent_gets_an_installed_model_for_its_task(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _local(make_app, monkeypatch, ["qwen3.5:4b", "qwen3.5:9b"], CPU16)
    plan = client.get("/api/library/customer-service/models", params={"language": "de"}).json()
    agents = _by_key(plan)
    assert plan["local"] is True
    # The router needs no large model; the writer gets the larger installed one.
    assert agents["triage"]["model"] == "ollama:qwen3.5:4b" and agents["triage"]["installed"]
    assert agents["reply"]["model"] == "ollama:qwen3.5:9b"
    assert all(agent["installed"] for agent in plan["agents"])
    copy = client.post("/api/library/customer-service/copy", json={"language": "de"})
    assert copy.status_code == 201, copy.text
    models = {node["id"]: node["agent"]["model"] for node in copy.json()["nodes"] if node.get("agent")}
    assert models["triage"] == "ollama:qwen3.5:4b" and models["reply"] == "ollama:qwen3.5:9b"


def test_nothing_is_downloaded_for_a_copy(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _local(make_app, monkeypatch, ["qwen3.5:4b"], GPU48)
    agents = _by_key(client.get("/api/library/code-review/models", params={"language": "en"}).json())
    bugs = agents["bugs"]
    # On a large GPU a coding model would suit better; it is suggested, the installed model is used.
    assert bugs["task"] == "coding" and bugs["model"] == "ollama:qwen3.5:4b" and bugs["installed"]
    assert bugs["suggested"] is not None and bugs["suggested"] != "ollama:qwen3.5:4b"
    # The installed 4B model is below the minimum of coding work: the agent keeps the default model, and says so.
    assert bugs["reasons"] == ["default_model"]
    # Work a small model can do takes the installed one.
    writer = _by_key(client.get("/api/library/customer-service/models", params={"language": "en"}).json())["reply"]
    assert writer["model"] == "ollama:qwen3.5:4b" and "installed" in writer["reasons"]


def test_a_coding_agent_gets_devstral_when_it_is_installed_and_fits(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _local(make_app, monkeypatch, ["qwen3.5:4b", "devstral-small-2:24b"], GPU48)
    agents = _by_key(client.get("/api/library/code-review/models", params={"language": "en"}).json())
    assert agents["bugs"]["model"] == "ollama:devstral-small-2:24b"
    # Devstral is for code only: the other work keeps a general model.
    other = _by_key(client.get("/api/library/customer-service/models", params={"language": "en"}).json())
    assert all(agent["model"] != "ollama:devstral-small-2:24b" for agent in other.values())


def test_without_a_suitable_installed_model_the_default_stays(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _local(make_app, monkeypatch, [], CPU16)
    plan = client.get("/api/library/customer-service/models", params={"language": "en"}).json()
    default = client.get("/api/settings").json()["default_model"]
    assert {agent["model"] for agent in plan["agents"]} == {default}
    assert all(agent["reasons"] == ["default_model"] for agent in plan["agents"])


def test_the_persons_choice_always_wins(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _local(make_app, monkeypatch, ["qwen3.5:4b", "qwen3.5:9b"], CPU16)
    copy = client.post(
        "/api/library/customer-service/copy",
        json={"language": "en", "models": {"reply": "ollama:qwen3.5:4b", "triage": "anthropic:claude-haiku-5-5"}},
    )
    assert copy.status_code == 201, copy.text
    models = {node["id"]: node["agent"]["model"] for node in copy.json()["nodes"] if node.get("agent")}
    assert models["reply"] == "ollama:qwen3.5:4b" and models["triage"] == "anthropic:claude-haiku-5-5"
    unknown = client.post("/api/library/customer-service/copy", json={"models": {"nobody": "ollama:qwen3.5:4b"}})
    assert unknown.status_code == 422 and unknown.json()["code"] == "library.agent_unknown"
    invalid = client.post("/api/library/customer-service/copy", json={"models": {"reply": "unknownprov:x"}})
    assert invalid.status_code == 400


def test_the_european_preference_picks_an_equal_european_model(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _local(make_app, monkeypatch, ["qwen3.5:4b", "ministral-3:3b"], CPU16)
    plain = _by_key(client.get("/api/library/customer-service/models", params={"language": "fr"}).json())
    assert plain["triage"]["model"] == "ollama:qwen3.5:4b"  # Qwen stays the standard
    assert client.put("/api/settings", json={"prefer_european_models": True}).status_code == 200
    preferred = _by_key(client.get("/api/library/customer-service/models", params={"language": "fr"}).json())
    assert preferred["triage"]["model"] == "ollama:ministral-3:3b"
    assert "preferred_region" in preferred["triage"]["reasons"]


def test_every_template_has_a_plan(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _local(make_app, monkeypatch, ["qwen3.5:4b"], CPU16)
    for template in client.get("/api/library").json():
        plan = client.get(f"/api/library/{template['id']}/models", params={"language": "en"}).json()
        assert plan["local"] and plan["agents"], template["id"]
        assert all(agent["model"] == "ollama:qwen3.5:4b" for agent in plan["agents"]), template["id"]
    assert client.get("/api/library/nope/models").status_code == 404
