"""Agent templates, their versions and their origin through the product host (N-25, ``docs/embedding.md``).

The group ``agents`` serves the studio's agent template routes unchanged: a product that selects it gets the same
versions, the same origin rules and the same ``nodestro-agent/1`` export and import, behind the same guards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nodestro_examples import agents as agent_library

from nodestro_api.context import AppConfig
from nodestro_api.host import Branding, HostSwitches, create_product_app

SECRET = "s" * 43
TEMPLATES = "/api/agents/templates"


def _product(tmp_path: Path, groups: tuple[str, ...] = ("agents",), **config: object) -> FastAPI:
    return create_product_app(
        "desktop",
        tmp_path / "data",
        list(groups),
        Branding(title="Test Product", version="1.0.0"),
        HostSwitches(scheduler=False),
        config=AppConfig(data_dir=tmp_path / "ignored", **config),  # type: ignore[arg-type]
        integrations=[],
    )


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _versioned_template(client: TestClient) -> dict[str, Any]:
    """A template saved three times (versions 1 to 3) with a description, examples and tests."""
    meta = {
        "category": "text",
        "summary": "Checks texts",
        "requirements": {"services": []},
        "examples": ["Check this paragraph"],
        "tests": [],
        "locale": "en",
    }
    created = client.post(TEMPLATES, json={"agent": {"name": "Reviewer", "system_prompt": "v1"}, "meta": meta})
    assert created.status_code == 201, created.text
    url = f"{TEMPLATES}/{created.json()['id']}"
    for prompt in ("v2", "v3"):
        saved = client.put(url, json={"agent": {"name": "Reviewer", "system_prompt": prompt}, "meta": meta})
        assert saved.status_code == 200, saved.text
    result: dict[str, Any] = client.get(TEMPLATES).json()[0]
    assert result["version"] == 3 and result["meta"]["origin"] == "user"
    return result


def _without_origin(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in meta.items() if key != "origin"}


def test_the_agents_group_serves_templates_versions_and_the_library(tmp_path: Path) -> None:
    with _client(_product(tmp_path)) as client:
        template = _versioned_template(client)
        url = f"{TEMPLATES}/{template['id']}"
        versions = client.get(f"{url}/versions").json()
        assert [item["version"] for item in versions] == [3, 2, 1]
        assert client.get(f"{url}/versions/1").json()["agent"]["system_prompt"] == "v1"
        assert client.get(f"{url}/check").status_code == 200
        listed = client.get("/api/library/agents").json()
        assert [item["id"] for item in listed] == list(agent_library.agent_ids())
        copy = client.post("/api/library/agents/summary/copy", json={"language": "en"}).json()
        assert copy["meta"]["origin"] == agent_library.agent_origin("summary")
        # Groups the product did not select stay closed.
        assert client.get("/api/workflows").json()["code"] == "route.not_found"
        assert client.get("/api/library").json()["code"] == "route.not_found"


def test_export_and_reimport_give_the_same_agent(tmp_path: Path) -> None:
    """U-3: an exported template imported into another product workspace is the same agent with the same
    description; its origin names the imported file and its history starts anew (an export carries the latest
    version only)."""
    with _client(_product(tmp_path / "source")) as source:
        template = _versioned_template(source)
        exported = source.get(f"{TEMPLATES}/{template['id']}/export")
        assert exported.status_code == 200
        assert "attachment" in exported.headers["content-disposition"]
        document = exported.json()
    assert document["format"] == "nodestro-agent/1"
    assert document["agent"]["system_prompt"] == "v3"

    with _client(_product(tmp_path / "target")) as target:
        imported = target.post(f"{TEMPLATES}/import", json=document)
        assert imported.status_code == 201, imported.text
        result = imported.json()
        assert result["removed"] == []
        copy = result["template"]
        assert copy["agent"] == template["agent"]
        assert _without_origin(copy["meta"]) == _without_origin(template["meta"])
        assert copy["meta"]["origin"].startswith("import:") and copy["version"] == 1
        versions = target.get(f"{TEMPLATES}/{copy['id']}/versions").json()
        assert [(item["version"], item["note"]) for item in versions] == [(1, "import")]
        # Exporting the copy gives the same file again, apart from the origin it got here.
        again = target.get(f"{TEMPLATES}/{copy['id']}/export").json()
        assert again["agent"] == document["agent"]
        assert _without_origin(again["meta"]) == _without_origin(document["meta"])
        # A change of the imported template is its next version and keeps the import origin.
        changed = target.put(
            f"{TEMPLATES}/{copy['id']}", json={"agent": {**copy["agent"], "system_prompt": "own"}}
        ).json()
        assert changed["version"] == 2 and changed["meta"]["origin"] == copy["meta"]["origin"]


def test_a_library_copy_keeps_its_origin_and_round_trips(tmp_path: Path) -> None:
    with _client(_product(tmp_path)) as client:
        copy = client.post("/api/library/agents/summary/copy", json={"language": "en"}).json()
        document = client.get(f"{TEMPLATES}/{copy['id']}/export").json()
        # The file names where the template came from; an import never takes that origin over.
        assert document["meta"]["origin"] == agent_library.agent_origin("summary")
        imported = client.post(f"{TEMPLATES}/import", json=document).json()["template"]
        assert imported["agent"] == copy["agent"]
        assert imported["meta"]["origin"].startswith("import:")


def test_agent_routes_keep_the_hosts_guards(tmp_path: Path) -> None:
    with _client(_product(tmp_path / "plain")) as client:
        template = _versioned_template(client)
        # A write from a foreign origin (cross-site request) is refused and stores nothing.
        foreign = {"Origin": "https://evil.example"}
        assert client.post(TEMPLATES, json={"agent": {"name": "X"}}, headers=foreign).status_code == 403
        document = client.get(f"{TEMPLATES}/{template['id']}/export").json()
        assert client.post(f"{TEMPLATES}/import", json=document, headers=foreign).status_code == 403
        assert client.delete(f"{TEMPLATES}/{template['id']}", headers=foreign).status_code == 403
        assert len(client.get(TEMPLATES).json()) == 1
        assert client.get(TEMPLATES, headers={"Host": "evil.example:8000"}).status_code in {400, 403}

    with _client(_product(tmp_path / "secret", launch_secret=SECRET)) as client:
        for method, path in (
            ("GET", TEMPLATES),
            ("GET", "/api/library/agents"),
            ("POST", f"{TEMPLATES}/import"),
            ("GET", f"{TEMPLATES}/tpl_x/export"),
        ):
            answer = client.request(method, path, json={} if method == "POST" else None)
            assert answer.status_code == 401, path
            assert answer.json()["code"] == "access.launch_required", path
        client.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
        assert client.get(TEMPLATES).json() == []


def test_an_import_through_the_host_stays_untrusted(tmp_path: Path) -> None:
    document = {
        "format": "nodestro-agent/1",
        "agent": {
            "name": "Imported",
            "tools": ["web_search"],
            "tool_settings": {"web_search": {"connection_id": "conn_x"}},
            "tool_policies": {"web_search": {"ask": "never", "legacy": True}},
            "knowledge": {"file_ids": ["file_9"]},
        },
        "meta": {"origin": "builtin:nodestro-examples@0.1.0+r1/summary"},
    }
    with _client(_product(tmp_path)) as client:
        result = client.post(f"{TEMPLATES}/import", json=document).json()
        agent = result["template"]["agent"]
        assert agent["tool_policies"] == {} and agent["knowledge"]["file_ids"] == []
        assert agent["tool_settings"]["web_search"]["connection_id"] is None
        assert sorted(result["removed"]) == ["connections", "knowledge", "tool_policies"]
        # A file cannot claim to be a shipped template.
        assert result["template"]["meta"]["origin"].startswith("import:")
        wrong = client.post(f"{TEMPLATES}/import", json={"format": "nodestro-workflow/1", "agent": {}})
        assert wrong.status_code == 422 and wrong.json()["code"] == "template.import_invalid"
