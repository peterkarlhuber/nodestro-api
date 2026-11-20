"""Conversations with own agents (N-04) on a server: every route works on the request's tenant only, and a
conversation belongs to the member who started it."""

from __future__ import annotations

from typing import Any

from nodestro_runtime.conversations import CONVERSATIONS_ENV
import pytest
from test_worker import connect, schema, server  # noqa: F401  (PostgreSQL server fixtures)

BASE = "/api/conversations"


def test_conversations_of_another_tenant_or_member_are_not_found(
    server: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    alice = server.member("acme", "alice", "owner")
    bob = server.member("acme", "bob", "editor")
    mallory = server.member("globex", "mallory", "owner")
    template = alice.post("/api/agents/templates", json={"agent": {"name": "Helper", "system_prompt": "AGENT"}})
    assert template.status_code == 201, template.text
    created = alice.post(
        BASE, json={"agent": {"template_id": template.json()["id"]}, "model": "anthropic:claude-sonnet-5-5"}
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    assert (created.json()["tenant_id"], created.json()["owner"]) == (
        "acme",
        alice.get("/api/auth/me").json()["user_id"],
    )
    assert [item["id"] for item in alice.get(BASE).json()] == [conversation_id]
    for other in (bob, mallory):
        assert other.get(BASE).json() == []
        for method, path in [
            ("get", f"{BASE}/{conversation_id}"),
            ("delete", f"{BASE}/{conversation_id}"),
            ("get", f"{BASE}/{conversation_id}/export"),
        ]:
            response = getattr(other, method)(path)
            assert (response.status_code, response.json()["code"]) == (404, "conversation.not_found"), (method, path)
    viewer = server.member("acme", "victor", "viewer")
    assert viewer.post(BASE, json={"agent": {"template_id": "tpl_x"}}).status_code == 403
    assert alice.get(f"{BASE}/{conversation_id}").status_code == 200


def test_n1_search_shows_no_snippet_of_another_members_conversation(
    server: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N-1: a run hit of ``/api/search`` shows a snippet of the run's input. An answer's input is the conversation,
    so the answer runs of another member's conversation are no hits; the owner still finds them."""
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    alice = server.member("acme", "alice", "owner")
    bob = server.member("acme", "bob", "owner")
    template = alice.post("/api/agents/templates", json={"agent": {"name": "Helper", "system_prompt": "AGENT"}})
    assert template.status_code == 201, template.text
    created = alice.post(
        BASE, json={"agent": {"template_id": template.json()["id"]}, "model": "anthropic:claude-sonnet-5-5"}
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    sent = alice.post(f"{BASE}/{conversation_id}/messages", json={"content": "My salary is zanzibar 4711"})
    assert sent.status_code in (200, 201, 202), sent.text
    run_id = sent.json()["assistant"]["run_id"]

    def run_hits(member: Any) -> list[dict[str, Any]]:
        found = member.get("/api/search", params={"q": "zanzibar"})
        assert found.status_code == 200, found.text
        return [hit for hit in found.json() if hit["kind"] == "run"]

    assert [hit["id"] for hit in run_hits(alice)] == [run_id]
    assert "zanzibar" in run_hits(alice)[0]["snippet"]
    assert run_hits(bob) == []
