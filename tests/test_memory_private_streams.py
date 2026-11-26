"""The event stream of a run that becomes private (RT-089d; needs PostgreSQL).

Before, ``/api/runs/{run_id}/events`` checked whether the reader sees the run and only then read the snapshot it
sends: a step that opened a person's memory space in between made the run private after the check, and the snapshot
went out anyway. Now the snapshot is read first and checked afterwards, on itself (its ``memory_owner``) and on the
stored runs up to the top: a snapshot of a run private to another person is never sent.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from test_identity import Server
from test_worker import _live_lines, _start, connect, schema, server  # noqa: F401  (fixtures)

SECRET = "zanzibar-snapshot"


def _follow(client: Any, run_id: str) -> list[str]:
    received: list[str] = []
    done = threading.Event()

    def follow() -> None:
        for line in _live_lines(client, f"/api/runs/{run_id}/events"):
            if line.startswith(("data: ", "event: ")):
                received.append(line)
            if line.startswith("data: ") and received[-2:-1] in (["event: end"], ["event: snapshot"]):
                break  # the first answer is enough: an end, or the snapshot (a queued run streams on)
        done.set()

    threading.Thread(target=follow, daemon=True).start()
    assert done.wait(15), received
    return received


def _privatize(app_server: Server, run_id: str, owner: str) -> Callable[[], None]:
    """The run as a worker leaves it right after a step marked it (its memory's output already in it), while the
    reader's visibility was checked before: ``run_visible`` answers as it did then."""
    workspace = app_server.app.state.tenants.get("acme")
    stored = workspace.store.get_raw("runs", run_id)
    stored.update({"memory_owner": owner, "status": "running", "output": SECRET})
    workspace.store.put_raw("runs", run_id, stored)
    checked = workspace.conversations.run_visible
    workspace.conversations.run_visible = lambda _run_id, _owner: True

    def restore() -> None:
        workspace.conversations.run_visible = checked

    return restore


def test_the_snapshot_of_a_run_that_became_private_is_never_sent(server: Server) -> None:  # noqa: F811
    alice = server.member("acme", "alice", "owner")
    bob = server.member("acme", "bob", "admin")
    run_id = _start(alice)
    alice_id = alice.get("/api/auth/me").json()["user_id"]
    restore = _privatize(server, run_id, alice_id)
    try:
        received = _follow(bob, run_id)
        mine = _follow(alice, run_id)
    finally:
        restore()
    assert received[:2] == ["event: end", 'data: {"reason": "access_ended"}'], received
    assert not [line for line in received if SECRET in line or "snapshot" in line]
    # The owner still gets the snapshot.
    assert mine[0] == "event: snapshot" and SECRET in mine[1]
