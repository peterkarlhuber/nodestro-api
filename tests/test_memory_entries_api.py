"""The entries of a memory space on the desktop (N-24): visible with their time and origin, editable and deletable on
``/api/memory``; a person's write is the explicit ``user`` origin, an agent's memory tool the ``tool`` origin."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient


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
