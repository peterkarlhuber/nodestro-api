"""``POST /api/tools/{name}/adopt``: adopting AI-generated code as one's own (ADR-0006, R-4), desktop only."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.sandbox_policy import SandboxPolicy
from nodestro_runtime.tool_library import CUSTOM_TESTS_TEMPLATE, CUSTOM_TOOL_TEMPLATE
import pytest

from nodestro_api.api import adopter_role
from nodestro_api.identity import ROLE_PERMISSIONS
from nodestro_api.permissions import LOCAL_OWNER, Principal

from conftest import ContainerDouble

NAME = "celsius_to_fahrenheit"


def generated(client: TestClient, code: str = CUSTOM_TOOL_TEMPLATE) -> dict[str, Any]:
    """Store an AI-written tool (as an approved job or draft does) and return it as the studio sees it."""
    tools = client.app.state.nodestro.tools  # type: ignore[attr-defined]
    result, info = tools.save_custom(
        code, CUSTOM_TESTS_TEMPLATE, NAME, requirement="Convert temperatures", sandbox=ContainerDouble()
    )
    assert result.ok and info is not None, result
    found = client.get(f"/api/tools/{NAME}").json()
    assert found["trust"] == "untrusted" and found["content_hash"] and found["adoption"] is None
    return found  # type: ignore[no-any-return]


def test_the_local_owner_adopts_a_generated_tool(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    shown = generated(client)
    answer = client.post(f"/api/tools/{NAME}/adopt", json={"content_hash": shown["content_hash"]})
    assert answer.status_code == 200, answer.text
    tool = answer.json()
    assert tool["trust"] == "user-authored" and tool["kind"] == "generated"
    assert tool["adoption"]["actor"] == "local" and tool["adoption"]["role"] == "owner"
    assert tool["adoption"]["version"] == 1 and tool["adoption"]["content_hash"] == shown["content_hash"]
    # It runs in the tool panel on this computer now (without Docker).
    ran = client.post(f"/api/tools/{NAME}/run", json={"input": {"celsius": 100}})
    assert ran.status_code == 200 and ran.json() == {"output": "212.0", "is_error": False}
    audit = client.get("/api/tool-approvals/audit").json()
    assert audit[0]["kind"] == "tool_adopted" and audit[0]["tool"] == NAME and audit[0]["actor"] == "local"
    assert audit[0]["details"]["content_hash"] == shown["content_hash"]


def test_a_stale_confirmation_is_refused_and_audited(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    shown = generated(client)
    generated(client, CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0"))  # an AI changed it after it was shown
    answer = client.post(f"/api/tools/{NAME}/adopt", json={"content_hash": shown["content_hash"]})
    assert answer.status_code == 409 and answer.json()["code"] == "tool.adoption_stale"
    assert answer.json()["params"]["version"] == 2
    assert client.get(f"/api/tools/{NAME}").json()["trust"] == "untrusted"
    audit = client.get("/api/tool-approvals/audit").json()
    assert audit[0]["kind"] == "tool_adoption_refused" and audit[0]["outcome"] == "tool.adoption_stale"


def test_a_later_ai_change_ends_the_adoption(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    shown = generated(client)
    assert client.post(f"/api/tools/{NAME}/adopt", json={"content_hash": shown["content_hash"]}).status_code == 200
    generated(client, CUSTOM_TOOL_TEMPLATE.replace("+ 32", "+ 32.0"))
    assert client.get("/api/tool-approvals/audit").json()[0]["kind"] == "tool_adoption_revoked"


@pytest.mark.parametrize("profile", ["server", "local-container"])
def test_adoption_is_refused_off_the_desktop(make_app: Callable[..., TestClient], profile: Any) -> None:
    client = make_app()
    shown = generated(client)
    # The policy of a server tenant or of Nodestro Local Docker (whose only principal is the local owner, too).
    client.app.state.nodestro.tools.policy = SandboxPolicy(profile)  # type: ignore[attr-defined]
    answer = client.post(f"/api/tools/{NAME}/adopt", json={"content_hash": shown["content_hash"]})
    assert answer.status_code == 403 and answer.json()["code"] == "tool.adoption_desktop_only"
    assert answer.json()["params"]["profile"] == profile


def test_only_an_administrator_or_the_local_owner_may_adopt(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    shown = generated(client)
    editor = Principal(id="u-editor", permissions=ROLE_PERMISSIONS["editor"], role="editor")
    client.app.state.principal_resolver = lambda _connection: editor  # type: ignore[attr-defined]
    answer = client.post(f"/api/tools/{NAME}/adopt", json={"content_hash": shown["content_hash"]})
    assert answer.status_code == 403 and answer.json()["code"] == "access.forbidden"
    admin = Principal(id="u-admin", permissions=ROLE_PERMISSIONS["admin"], role="admin")
    client.app.state.principal_resolver = lambda _connection: admin  # type: ignore[attr-defined]
    answer = client.post(f"/api/tools/{NAME}/adopt", json={"content_hash": shown["content_hash"]})
    assert answer.status_code == 200 and answer.json()["adoption"]["role"] == "admin"


def test_the_recorded_role() -> None:
    assert adopter_role(LOCAL_OWNER) == "owner"
    assert adopter_role(Principal(id="local", permissions=LOCAL_OWNER.permissions, tenant_id="t1")) == ""
    assert adopter_role(Principal(id="x", permissions=frozenset(), role="admin")) == "admin"
    assert adopter_role(Principal(id="x", permissions=frozenset(), role="editor")) == "editor"
    assert adopter_role(None) == ""


def test_tools_that_need_no_adoption(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    answer = client.post("/api/tools/calculator/adopt", json={"content_hash": "x"})
    assert answer.status_code == 400 and answer.json()["code"] == "tool.adoption_not_applicable"
    assert client.post("/api/tools/missing/adopt", json={"content_hash": "x"}).status_code == 404
    saved = client.post(
        "/api/tools", json={"function_name": NAME, "code": CUSTOM_TOOL_TEMPLATE, "tests": CUSTOM_TESTS_TEMPLATE}
    ).json()
    assert saved["tool"]["trust"] == "user-authored", saved
    answer = client.post(f"/api/tools/{NAME}/adopt", json={"content_hash": saved["tool"]["content_hash"]})
    assert answer.status_code == 409 and answer.json()["code"] == "tool.adoption_not_needed"
