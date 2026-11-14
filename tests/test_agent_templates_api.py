"""Agent templates with versions, export and import, and the shipped agent library (NAL-B1)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_examples import agents as agent_library


def _template(client: TestClient, **agent: Any) -> dict[str, Any]:
    answer = client.post("/api/agents/templates", json={"agent": {"name": "Reviewer", **agent}})
    assert answer.status_code == 201, answer.text
    result: dict[str, Any] = answer.json()
    return result


def test_every_changed_save_is_a_version(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    template = _template(client, system_prompt="v1")
    assert template["version"] == 1 and template["meta"]["origin"] == "user"
    url = f"/api/agents/templates/{template['id']}"
    meta = {"category": "text", "summary": "Checks texts"}
    second = client.put(url, json={"agent": {"name": "Reviewer", "system_prompt": "v2"}, "meta": meta, "note": "x"})
    assert second.json()["version"] == 2
    # An unchanged save stores nothing; a save without meta keeps the stored meta.
    same = client.put(url, json={"agent": {"name": "Reviewer", "system_prompt": "v2"}}).json()
    assert same["version"] == 2 and same["meta"]["summary"] == "Checks texts"
    # The origin cannot be set by a client.
    forged = client.put(
        url, json={"agent": {"name": "Reviewer", "system_prompt": "v3"}, "meta": {"origin": "import:" + "0" * 64}}
    )
    assert forged.json()["meta"]["origin"] == "user" and forged.json()["version"] == 3
    versions = client.get(f"{url}/versions").json()
    assert [item["version"] for item in versions] == [3, 2, 1]
    assert versions[1]["note"] == "x"
    assert client.get(f"{url}/versions/1").json()["agent"]["system_prompt"] == "v1"
    assert client.get(f"{url}/versions/9").status_code == 404
    # The list shows the latest version of each template.
    assert [item["version"] for item in client.get("/api/agents/templates").json()] == [3]
    assert client.delete(url).status_code == 204
    assert client.get(f"{url}/versions").status_code == 404


def test_a_created_template_cannot_claim_an_origin(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    answer = client.post(
        "/api/agents/templates",
        json={"agent": {"name": "A"}, "meta": {"origin": "builtin:nodestro-examples@0.1.0+r1/summary"}},
    )
    assert answer.json()["meta"]["origin"] == "user"


def test_export_leaves_out_what_belongs_to_the_workspace(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    template = _template(
        client,
        tools=["web_search"],
        tool_settings={"web_search": {"connection_id": "conn_secret"}},
        tool_policies={"web_search": {"ask": "never", "legacy": True}},
        knowledge={"file_ids": ["file_1"]},
    )
    answer = client.get(f"/api/agents/templates/{template['id']}/export")
    assert answer.status_code == 200
    assert "attachment" in answer.headers["content-disposition"]
    document = answer.json()
    assert document["format"] == "nodestro-agent/1"
    assert "conn_secret" not in answer.text and "file_1" not in answer.text
    assert document["agent"]["tool_policies"] == {}


def test_import_is_untrusted(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    document = {
        "format": "nodestro-agent/1",
        "agent": {
            "name": "Imported",
            "tools": ["web_search"],
            "tool_settings": {"web_search": {"connection_id": "conn_x"}},
            "tool_policies": {"web_search": {"ask": "never", "legacy": True}},
            "knowledge": {"file_ids": ["file_9"]},
        },
        "meta": {"origin": "user", "requirements": {"services": ["search"]}},
    }
    answer = client.post("/api/agents/templates/import", json=document)
    assert answer.status_code == 201, answer.text
    result = answer.json()
    agent = result["template"]["agent"]
    assert agent["tool_policies"] == {} and agent["knowledge"]["file_ids"] == []
    assert agent["tool_settings"]["web_search"]["connection_id"] is None
    assert sorted(result["removed"]) == ["connections", "knowledge", "tool_policies"]
    assert result["template"]["meta"]["origin"].startswith("import:")
    assert result["template"]["version"] == 1
    # The search connection is missing in this workspace: the check says so.
    codes = [issue["code"] for issue in result["check"]["issues"]]
    assert "template.service_missing" in codes
    check = client.get(f"/api/agents/templates/{result['template']['id']}/check").json()
    assert "template.service_missing" in [issue["code"] for issue in check["issues"]]


def test_import_refuses_other_files(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    wrong = client.post("/api/agents/templates/import", json={"format": "nodestro-workflow/1", "agent": {}})
    assert wrong.status_code == 422 and wrong.json()["code"] == "template.import_invalid"
    invalid = client.post("/api/agents/templates/import", json={"format": "nodestro-agent/1", "agent": {"name": 3}})
    assert invalid.status_code == 422 and invalid.json()["code"] == "template.import_invalid"
    huge = client.post(
        "/api/agents/templates/import",
        json={"format": "nodestro-agent/1", "agent": {"name": "Big", "system_prompt": "x" * 1_000_001}},
    )
    assert huge.status_code in (413, 422)
    assert huge.json()["code"] in ("template.import_too_large", "request.too_large", "validation")
    assert client.get("/api/agents/templates").json() == []


def test_the_library_lists_the_shipped_templates(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    listed = client.get("/api/library/agents", params={"language": "de"}).json()
    assert [item["id"] for item in listed] == list(agent_library.agent_ids())
    summary = next(item for item in listed if item["id"] == "summary")
    assert summary["meta"]["locale"] == "de" and summary["version"] == agent_library.agent_version("summary")
    detail = client.get("/api/library/agents/web-research", params={"language": "en"}).json()
    assert detail["help"] and detail["agent"]["tools"]
    assert detail["meta"]["requirements"]["services"] == ["search"]
    assert client.get("/api/library/agents/nope").status_code == 404
    # Nothing was stored by reading.
    assert client.get("/api/agents/templates").json() == []


def test_a_copy_is_an_own_template(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    answer = client.post("/api/library/agents/summary/copy", json={"language": "fr"})
    assert answer.status_code == 201, answer.text
    copy = answer.json()
    assert copy["version"] == 1 and copy["meta"]["origin"] == agent_library.agent_origin("summary")
    assert copy["meta"]["locale"] == "fr"
    # It is an ordinary template: it can be changed, which stores version 2 and keeps the origin.
    changed = client.put(
        f"/api/agents/templates/{copy['id']}", json={"agent": {**copy["agent"], "system_prompt": "own"}}
    ).json()
    assert changed["version"] == 2 and changed["meta"]["origin"] == copy["meta"]["origin"]
    model = client.post("/api/library/agents/summary/copy", json={"model": "anthropic:claude-haiku-5-5"}).json()
    assert model["agent"]["model"] == "anthropic:claude-haiku-5-5"


def test_a_copy_takes_only_fitting_service_connections(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    unknown = client.post("/api/library/agents/summary/copy", json={"services": {"search": "conn_1"}})
    assert unknown.status_code == 422 and unknown.json()["code"] == "library.service_unknown"
    missing = client.post("/api/library/agents/web-research/copy", json={"services": {"search": "conn_none"}})
    assert missing.status_code in (404, 422)
    connection = client.post("/api/connections", json={"type": "search", "provider": "test", "name": "Test search"})
    assert connection.status_code == 201, connection.text
    connection_id = connection.json()["id"]
    copy = client.post("/api/library/agents/web-research/copy", json={"services": {"search": connection_id}}).json()
    settings = copy["agent"]["tool_settings"]
    assert connection_id in {item.get("connection_id") for item in settings.values()}
    check = client.get(f"/api/agents/templates/{copy['id']}/check").json()
    assert "template.service_missing" not in [issue["code"] for issue in check["issues"]]
    # Without a chosen connection, the copy is stored and the check names what is missing.
    open_copy = client.post("/api/library/agents/web-research/copy", json={}).json()
    assert open_copy["meta"]["origin"].startswith("builtin:")


def test_versions_are_in_the_backup(make_app: Callable[..., TestClient]) -> None:
    from nodestro_api import backup

    assert "agent_template_versions" in backup._document_models()
