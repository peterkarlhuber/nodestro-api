"""Output limits and model profiles through the API: the Workflow Architect's limits per step, its handling of answers
cut off at the limit (retry within the setting and the budget, never around them), the settings round trip, the
assistants' limits and ``GET /api/models/profile``. The scripted client records every request's ``max_tokens``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.testing import text
from test_architect import logs, start, wait_job

from architect_script import AGENT, ANALYST, CODER, PLANNER, create_script


def _app(make_app: Callable[..., TestClient], script: dict[str, list[Any]]) -> tuple[TestClient, Any]:
    from nodestro_runtime.testing import ScriptedClient

    scripted = ScriptedClient(script)
    return make_app(client=scripted), scripted


def _sent(scripted: Any, phrase: str) -> list[int]:
    return [call["max_tokens"] for call in scripted.messages.calls if phrase in str(call.get("system"))]


def _cut_off() -> Any:
    return text('{"summary": "Recherche und', stop_reason="max_tokens")


def test_architect_steps_send_their_output_limits(make_app: Callable[..., TestClient]) -> None:
    client, scripted = _app(make_app, create_script())
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready", job["error"]
    assert _sent(scripted, ANALYST) == [8000]
    assert _sent(scripted, PLANNER) == [32_000]  # formerly a fixed 16,000
    assert set(_sent(scripted, AGENT)) == {16_000}  # agent design (the assistant's default)
    assert _sent(scripted, CODER) == [16_000]  # tool generation
    limits = [event for event in job["events"] if event["type"] == "log" and event["code"] == "architect.limit"]
    assert {event["params"]["operation"] for event in limits} >= {"architect_analysis", "architect_plan"}


def test_the_settings_reach_the_architect(make_app: Callable[..., TestClient]) -> None:
    client, scripted = _app(make_app, create_script())
    answer = client.put("/api/settings", json={"output_limits": {"architect_plan": 20_000, "architect_analysis": 3000}})
    assert answer.status_code == 200, answer.text
    assert answer.json()["output_limits"] == {"architect_plan": 20_000, "architect_analysis": 3000}
    assert answer.json()["output_limit_defaults"]["architect_plan"] == 32_000
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready", job["error"]
    assert _sent(scripted, ANALYST) == [3000] and _sent(scripted, PLANNER) == [20_000]


def test_a_setting_above_the_model_maximum_is_lowered_with_a_warning(make_app: Callable[..., TestClient]) -> None:
    client, scripted = _app(make_app, create_script())
    client.put("/api/settings", json={"output_limits": {"architect_plan": 500_000}})
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "ready", job["error"]
    assert _sent(scripted, PLANNER) == [128_000]  # Claude's documented maximum output
    reduced = next(event for event in job["events"] if event.get("code") == "architect.limit_reduced")
    assert reduced["level"] == "warning" and reduced["params"]["configured"] == 500_000
    assert reduced["params"]["reduced"] == "model_maximum"


def test_invalid_settings_are_refused(make_app: Callable[..., TestClient]) -> None:
    client, _scripted = _app(make_app, {})
    answer = client.put("/api/settings", json={"output_limits": {"architect_plan": 12}})
    assert answer.status_code == 400 and answer.json()["code"] == "settings.output_limit_invalid"
    answer = client.put("/api/settings", json={"architect_retry_max_tokens": 10})
    assert answer.status_code == 422


def test_a_cut_off_plan_is_asked_again_with_a_higher_limit(make_app: Callable[..., TestClient]) -> None:
    script = create_script()
    script[PLANNER] = [_cut_off(), *script[PLANNER]]
    client, scripted = _app(make_app, script)
    # A retry is sent only when its whole cost bound fits into the budget (64,000 tokens of Claude Opus 5.5 do not
    # fit into the default $2 per job).
    job = wait_job(client, start(client, budget_usd=10.0)["id"])
    assert job["status"] == "ready", job["error"]
    assert _sent(scripted, PLANNER) == [32_000, 64_000]
    assert "architect.truncated_retry" in logs(job)
    assert job["usage"]["requests"] >= 2  # the cut-off answer is paid and counted like any other


def test_a_cut_off_plan_without_retry_fails_clearly(make_app: Callable[..., TestClient]) -> None:
    script = create_script()
    script[PLANNER] = [_cut_off()]
    client, scripted = _app(make_app, script)
    client.put("/api/settings", json={"architect_retry_truncated": False})
    job = wait_job(client, start(client)["id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "architect.truncated"
    assert job["error"]["params"]["operation"] == "architect_plan" and job["error"]["params"]["max_tokens"] == 32_000
    assert job["error"]["params"]["reason"] == "limit"
    assert _sent(scripted, PLANNER) == [32_000]


def test_the_retry_stops_at_its_cap(make_app: Callable[..., TestClient]) -> None:
    script = create_script()
    script[PLANNER] = [_cut_off(), _cut_off()]
    client, scripted = _app(make_app, script)
    client.put("/api/settings", json={"architect_retry_max_tokens": 48_000})
    job = wait_job(client, start(client, budget_usd=10.0)["id"])
    assert job["status"] == "failed" and job["error"]["code"] == "architect.truncated"
    assert _sent(scripted, PLANNER) == [32_000, 48_000]
    assert job["error"]["params"]["max_tokens"] == 48_000


def test_the_retry_never_bypasses_the_budget(make_app: Callable[..., TestClient]) -> None:
    """A retry is sent only when the whole cost bound of its higher limit fits into what is left of the job's budget
    (never as an "oversized" request that may overshoot); otherwise the job fails as cut off, naming the budget."""
    script = create_script()
    script[PLANNER] = [_cut_off(), *script[PLANNER]]
    client, scripted = _app(make_app, script)
    # Enough for the analysis and a 32,000-token plan at Claude Opus 5.5's output price, not for 64,000 tokens.
    job = wait_job(client, start(client, budget_usd=2.0)["id"])
    assert job["status"] == "failed", job
    assert job["error"]["code"] == "architect.truncated_over_budget"
    assert job["error"]["params"]["reason"] == "budget" and job["error"]["params"]["max_tokens"] == 64_000
    assert _sent(scripted, PLANNER) == [32_000]  # the retry was never sent


# ---- model profiles --------------------------------------------------------------------------------------------


def test_model_profile_route(make_app: Callable[..., TestClient]) -> None:
    client, _scripted = _app(make_app, {})
    claude = client.get("/api/models/profile", params={"model": "anthropic:claude-opus-5-5"}).json()
    assert claude["limits"]["max_output_tokens"] == 128_000 and claude["max_tokens_limit"] == 128_000
    assert claude["options"]["thinking"] == {"available": True, "reason": None, "values": ["adaptive"]}
    assert claude["capabilities"]["image_input"] == "supported"
    gpt = client.get("/api/models/profile", params={"model": "openai:gpt-6.1-sol"}).json()
    assert gpt["options"]["thinking"]["reason"] == "provider"
    assert gpt["limits"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    web = next(tool for tool in gpt["native_tools"] if tool["key"] == "web_search")
    assert web == {"key": "web_search", "available": False, "reason": "not_integrated"}
    invalid = client.get("/api/models/profile", params={"model": "llama3.1:8b"}).json()
    assert invalid["error"] == "config.model_provider_missing"


def test_declared_endpoint_limits_round_trip(make_app: Callable[..., TestClient]) -> None:
    client, _scripted = _app(make_app, {})
    answer = client.put(
        "/api/settings",
        json={"providers": {"vllm": {"base_url": "http://localhost:8000/v1", "limits": {"max_output_tokens": 8192}}}},
    )
    assert answer.status_code == 200, answer.text
    vllm = next(item for item in answer.json()["providers"] if item["id"] == "vllm")
    assert vllm["limits"] == {"max_output_tokens": 8192}
    profile = client.get("/api/models/profile", params={"model": "vllm:my-model"}).json()
    assert profile["limits"]["max_output_tokens"] == 8192 and profile["limits"]["source"] == "declared"
    issues = client.post(
        "/api/agents/validate", json={"model": "vllm:my-model", "effort": None, "max_tokens": 16_000}
    ).json()
    assert "config.max_tokens_above_model" in {issue["code"] for issue in issues}


# ---- assistants ------------------------------------------------------------------------------------------------


def test_agent_design_uses_its_limit_and_lowers_the_agent_to_the_model(make_app: Callable[..., TestClient]) -> None:
    from architect_script import agent_spec

    client, scripted = _app(make_app, {AGENT: [agent_spec(max_tokens=200_000, model="claude-haiku-4-5")]})
    client.put("/api/settings", json={"output_limits": {"agent_design": 12_000}})
    answer = client.post("/api/agents/design", json={"description": "Ein Agent, der Texte kürzt"})
    assert answer.status_code == 200, answer.text
    assert _sent(scripted, AGENT) == [12_000]
    draft = answer.json()
    # The draft's own bound (32,000) is below Claude Haiku 4.5's maximum of 64,000: nothing to lower.
    assert draft["config"]["max_tokens"] == 32_000
    assert "draft.max_tokens_reduced" not in {note["code"] for note in draft["notes"]}
