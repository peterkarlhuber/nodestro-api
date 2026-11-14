"""Skill groups, usage and tool drafts from skill scripts in the API (NAL-B4)."""

from __future__ import annotations

from collections.abc import Callable
import time

from fastapi.testclient import TestClient
from nodestro_runtime.testing import text, tool_use
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE

from conftest import isolate, parsed
from test_flow import edge, save
from test_runs import agent_node

SKILL = "---\nname: {name}\ndescription: {description}\n---\nFollow the method.\n"


def _create(client: TestClient, name: str, description: str = "A method.", **files: str) -> dict[str, object]:
    payload = [
        {"path": "SKILL.md", "text": SKILL.format(name=name, description=description)},
        *({"path": path, "text": value} for path, value in files.items()),
    ]
    answer = client.post("/api/skills", json={"files": payload})
    assert answer.status_code == 201, answer.text
    return dict(answer.json())


def test_groups_are_sets_of_skills(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    whys = _create(client, "five-whys")
    fmea = _create(client, "fmea")
    made = client.post("/api/skill-groups", json={"name": "Quality methods", "skill_ids": [whys["id"], fmea["id"]]})
    assert made.status_code == 201, made.text
    group = made.json()
    assert group["skill_ids"] == [whys["id"], fmea["id"]] and group["id"].startswith("skg_")
    again = client.post("/api/skill-groups", json={"name": "Quality methods"})
    assert again.status_code == 409 or again.json()["code"] == "skill.group_name_taken"
    missing = client.post("/api/skill-groups", json={"name": "Other", "skill_ids": ["skl_nope"]})
    assert missing.json()["code"] == "skill.not_found"
    changed = client.put(f"/api/skill-groups/{group['id']}", json={"name": "QM", "skill_ids": [fmea["id"]]})
    assert changed.json()["name"] == "QM" and changed.json()["skill_ids"] == [fmea["id"]]
    assert [item["name"] for item in client.get("/api/skill-groups").json()] == ["QM"]
    assert client.delete(f"/api/skill-groups/{group['id']}").status_code == 204
    assert client.delete(f"/api/skill-groups/{group['id']}").status_code == 404
    assert len(client.get("/api/skills").json()) == 2


def test_usage_counts_what_runs_loaded(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [tool_use("load_skill", {"name": "five-whys"}), text("done")]})
    whys = _create(client, "five-whys")
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER", skills=[{"skill_id": whys["id"], "version": 1, "group_id": "skg_x"}]),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "writer"), edge("writer", "out")],
        name="Skilled",
    )
    run = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "Why does it fail?"}).json()
    deadline = time.monotonic() + 20
    while run["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.05)
        run = client.get(f"/api/runs/{run['id']}").json()
    assert run["status"] == "succeeded", run
    assert run["nodes"]["writer"]["skill_selection"]["offered"] == ["five-whys"]
    [stats] = client.get("/api/skill-stats", params={"days": 7}).json()
    assert (stats["name"], stats["offered"], stats["loaded"], stats["runs"], stats["succeeded"]) == (
        "five-whys",
        1,
        1,
        1,
        1,
    )
    assert client.get("/api/skill-stats", params={"days": 0}).status_code == 422


def test_a_script_becomes_a_tool_only_through_the_tool_chain(make_app: Callable[..., TestClient]) -> None:
    """Acceptance (plan B4): without isolation nothing is drafted; with it, the draft waits for a person."""
    proposal = parsed(
        lambda model: model(
            function_name="celsius_to_fahrenheit",
            description="Converts temperatures.",
            code=CUSTOM_TOOL_TEMPLATE,
            tests=CUSTOM_TESTS_TEMPLATE,
        )
    )
    client = make_app({"expert Python developer": [proposal]})
    skill = _create(client, "units", "Unit conversions.", **{"scripts/convert.py": "print(1.8 * 0 + 32)\n"})
    url = f"/api/skills/{skill['id']}/versions/1/script-draft"
    refused = client.post(url, json={"path": "scripts/convert.py"})
    assert refused.status_code == 400 and refused.json()["code"] == "sandbox.isolation_required"
    assert client.get("/api/tools/jobs").json() == []
    assert client.post(url, json={"path": "SKILL.md"}).json()["code"] == "skill.script_not_found"
    isolate(client)
    job = client.post(url, json={"path": "scripts/convert.py", "note": "for the lab"}).json()
    assert job["origin"] == "skill:units@1/scripts/convert.py"
    assert job["requirement"].startswith("Origin: skill:units@1/scripts/convert.py")
    deadline = time.monotonic() + 20
    while job["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/tools/jobs/{job['id']}").json()
    # The draft waits for a person; until then there is no tool.
    assert job["status"] == "awaiting_approval", job
    assert client.get("/api/tools/celsius_to_fahrenheit").status_code == 404
    client.post(f"/api/tools/jobs/{job['id']}/decision", json={"approved": True})
    while job["status"] == "awaiting_approval" and time.monotonic() < deadline:
        time.sleep(0.05)
        job = client.get(f"/api/tools/jobs/{job['id']}").json()
    tool = client.get("/api/tools/celsius_to_fahrenheit").json()
    assert tool["trust"] == "untrusted" and "skill:units@1/scripts/convert.py" in tool["requirement"]
    # A disabled skill gives no drafts.
    client.post(f"/api/skills/{skill['id']}/status", json={"status": "disabled"})
    assert client.post(url, json={"path": "scripts/convert.py"}).json()["code"] == "skill.inactive"


def test_groups_are_in_the_backup() -> None:
    from nodestro_api import backup

    assert "skill_groups" in backup._document_models()
