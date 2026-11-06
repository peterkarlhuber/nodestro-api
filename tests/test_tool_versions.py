"""``GET /api/tools/{name}/versions``: a tool's version history with who wrote each version (ST-036)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
from test_tool_adoption_api import NAME, generated

from nodestro_api.identity import ROLE_PERMISSIONS
from nodestro_api.permissions import Principal


def versions(client: TestClient, name: str = NAME) -> list[dict[str, Any]]:
    answer = client.get(f"/api/tools/{name}/versions")
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


def own(client: TestClient, code: str = CUSTOM_TOOL_TEMPLATE) -> dict[str, Any]:
    saved = client.post("/api/tools", json={"function_name": NAME, "code": code, "tests": CUSTOM_TESTS_TEMPLATE})
    assert saved.status_code == 200 and saved.json()["tool"], saved.text
    return saved.json()["tool"]  # type: ignore[no-any-return]


def test_versions_name_their_author_newest_first(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    mine = own(client)  # v1: written in the editor
    generated(client, CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0"))  # v2: the AI changed it
    history = versions(client)
    assert [(item["version"], item["author"], item["own"]) for item in history] == [
        (2, "ai", False),
        (1, "editor", True),
    ]
    assert history[1]["code"] == CUSTOM_TOOL_TEMPLATE and history[1]["tests"] == CUSTOM_TESTS_TEMPLATE
    assert history[1]["content_hash"] == mine["content_hash"]
    current = client.get(f"/api/tools/{NAME}").json()
    assert history[0]["content_hash"] == current["content_hash"] and history[0]["status"] == "approved"


def test_an_adopted_version_counts_as_own_until_its_content_changes(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    shown = generated(client)  # v1: AI
    assert client.post(f"/api/tools/{NAME}/adopt", json={"content_hash": shown["content_hash"]}).status_code == 200
    generated(client, CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0"))  # v2: AI again (ends the adoption)
    history = versions(client)
    assert [(item["version"], item["author"]) for item in history] == [(2, "ai"), (1, "adopted")]


def test_built_in_and_unknown_tools(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    assert versions(client, "calculator") == []
    missing = client.get("/api/tools/missing/versions")
    assert missing.status_code == 404


def test_reading_needs_only_read(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    own(client)
    viewer = Principal(id="u-viewer", permissions=ROLE_PERMISSIONS["viewer"], role="viewer")
    client.app.state.principal_resolver = lambda _connection: viewer  # type: ignore[attr-defined]
    assert [item["author"] for item in versions(client)] == ["editor"]
    nobody = Principal(id="u-none", permissions=frozenset(), role="viewer")
    client.app.state.principal_resolver = lambda _connection: nobody  # type: ignore[attr-defined]
    assert client.get(f"/api/tools/{NAME}/versions").status_code == 403
