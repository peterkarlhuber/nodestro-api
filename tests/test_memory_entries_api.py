"""The entries of a memory space on the desktop (N-24): visible with their time and origin, editable and deletable on
``/api/memory``; a person's write is the explicit ``user`` origin, an agent's memory tool the ``tool`` origin."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from orkexa.errors import MemoryToolError
import pytest


def test_entries_show_their_origin_and_time_and_can_be_edited_and_deleted(
    make_app: Callable[..., TestClient],
) -> None:
    client = make_app()
    assert client.put("/api/memory/Team/file", json={"path": "/memories/prefs.md", "content": "German"}).is_success
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    space = nodestro.memory.open("Team")
    space.execute({"command": "create", "path": "/memories/agent/learned.md", "file_text": "likes tea"})

    entries = client.get("/api/memory/Team").json()
    assert [(entry["path"], entry["origin"], entry["scope"]) for entry in entries] == [
        ("/memories/agent/learned.md", "tool", "user"),
        ("/memories/prefs.md", "user", "user"),
    ]
    assert all(entry["updated_at"].endswith("Z") or "+00:00" in entry["updated_at"] for entry in entries)
    assert [(item["name"], item["files"], item["scope"]) for item in client.get("/api/memory").json()] == [
        ("Team", 2, "user")
    ]

    # Edit the agent's entry: the person's write is now its origin; delete the other one.
    edited = client.put("/api/memory/Team/file", json={"path": "/memories/agent/learned.md", "content": "green tea"})
    assert edited.status_code == 200
    assert client.delete("/api/memory/Team/file", params={"path": "/memories/prefs.md"}).status_code == 204
    [entry] = client.get("/api/memory/Team").json()
    assert (entry["path"], entry["origin"], entry["size"]) == ("/memories/agent/learned.md", "user", 9)
    assert client.get("/api/memory/Team/file", params={"path": entry["path"]}).json()["content"] == "green tea"


def test_a_saved_assistant_proposal_is_recorded_as_the_assistants_never_as_the_users(
    make_app: Callable[..., TestClient],
) -> None:
    """N-24 L1: the memory assistant's proposal is model output; ``proposed=true`` records it as ``assistant``.
    The flag can only lower the origin: no request makes the server record anything but ``user`` or ``assistant``."""
    client = make_app()
    put = client.put
    assert put(
        "/api/memory/Team/file", params={"proposed": "true"}, json={"path": "/memories/p.md", "content": "a"}
    ).is_success
    assert put("/api/memory/Team/file", json={"path": "/memories/u.md", "content": "b"}).is_success
    assert put(
        "/api/memory/Team/file", params={"proposed": "false"}, json={"path": "/memories/f.md", "content": "c"}
    ).is_success
    # Claims of another origin are ignored (an unknown query parameter, an extra body field).
    claimed = {"path": "/memories/c.md", "content": "d", "origin": "tool"}
    assert put("/api/memory/Team/file", params={"origin": "import"}, json=claimed).is_success
    origins = {entry["path"]: entry["origin"] for entry in client.get("/api/memory/Team").json()}
    assert origins == {
        "/memories/c.md": "user",
        "/memories/f.md": "user",
        "/memories/p.md": "assistant",
        "/memories/u.md": "user",
    }
    # A plain edit of the proposal makes it the person's own text.
    assert put("/api/memory/Team/file", json={"path": "/memories/p.md", "content": "mine"}).is_success
    assert {e["path"]: e["origin"] for e in client.get("/api/memory/Team").json()}["/memories/p.md"] == "user"
    assert (
        put(
            "/api/memory/Team/file", params={"proposed": "maybe"}, json={"path": "/memories/x.md", "content": "e"}
        ).status_code
        == 422
    )


def test_a_memory_path_is_at_most_512_characters(make_app: Callable[..., TestClient]) -> None:
    """N-24 L2: the API refuses a longer path before it reaches the store; Orkexa refuses it as well."""
    client = make_app()
    longest = "/memories/" + "a" * (512 - len("/memories/") - 3) + ".md"
    assert len(longest) == 512
    assert client.put("/api/memory/Team/file", json={"path": longest, "content": "x"}).is_success
    refused = client.put("/api/memory/Team/file", json={"path": longest + "x", "content": "x"})
    assert refused.status_code == 422
    assert [entry["path"] for entry in client.get("/api/memory/Team").json()] == [longest]
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    space = nodestro.memory.open("Team")
    with pytest.raises(MemoryToolError, match="longer than 512"):
        space.execute({"command": "create", "path": longest + "x", "file_text": "x"})
