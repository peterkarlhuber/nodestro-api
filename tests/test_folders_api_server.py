"""Granted folders on a server (N-10): a server has no person's folders, so the routes stay closed there even with
the switch on, and the folder tools are never offered."""

from __future__ import annotations

from typing import Any

from nodestro_runtime.folders import COLLECTION, FOLDERS_ENV, FolderGrant
from nodestro_runtime.tool_approvals import AccessRules
import pytest
from test_worker import connect, schema, server  # noqa: F401  (PostgreSQL server fixtures)

FOLDERS = "/api/folders"


def test_a_server_never_grants_folders(server: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:  # noqa: F811
    monkeypatch.setenv(FOLDERS_ENV, "1")
    alice = server.member("acme", "alice", "owner")
    state = alice.get(f"{FOLDERS}/state")
    assert state.status_code == 200 and state.json()["enabled"] is False
    (tmp_path / "docs").mkdir()
    for response in (
        alice.get(FOLDERS),
        alice.post(FOLDERS, json={"name": "Docs", "path": str(tmp_path / "docs")}),
        alice.delete(f"{FOLDERS}/fgr_x"),
        # The folder browser (N-11) lists nothing of the server's file system either.
        alice.post(f"{FOLDERS}/browse", json={"path": str(tmp_path)}),
    ):
        assert (response.status_code, response.json()["code"]) == (404, "folder.disabled")
    acme = server.app.state.tenants.get("acme")
    assert acme.store.list(COLLECTION, FolderGrant) == [] and AccessRules(acme.store).grants() == []
