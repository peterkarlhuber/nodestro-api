"""One "computer" of the backup acceptance test (``tests/test_data_lifecycle.py``, N-15): a Python process of its own
with its own data directory and its own key for the data directory's secrets, as on another machine.

``python backup_machine.py fill DATA_DIR VAULT_KEY BACKUP.zip STATE.json``
    Fills a new workspace with conversations, agents, knowledge, memory, access grants and denies, logs (runs, the
    activity log, the usage log, the access audit trail) and secrets (a provider key, a connection's secret), writes a
    backup through ``GET /api/backup`` and the workspace's state (:func:`state`) as JSON.
``python backup_machine.py restore DATA_DIR VAULT_KEY BACKUP.zip STATE.json``
    Opens a new workspace with a provider key of its own, checks and restores the backup through the API, writes the
    answers of the check and the restore and the workspace's state as JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient
from nodestro_runtime import docker_status
from nodestro_runtime.conversations import CONVERSATIONS_ENV
from nodestro_runtime.testing import ScriptedClient, StaticDockerMonitor, text
from nodestro_runtime.tool_approvals import ACCESS_ENV, ENABLE_ENV
from nodestro_runtime.workspace import Workspace

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

#: The collections that stay on each computer (never part of a backup).
LOCAL = ("api_keys", "api_idempotency", "local_vault")
CLOUD = "anthropic:claude-sonnet-5-5"


def _app(data_dir: Path, vault_key: str) -> TestClient:
    # As tests/conftest.py's autouse fixtures: a fixed Docker status, no automatic Docker sandbox.
    docker_status.reset_monitors()
    docker_status.monitor_for = lambda *args, **kwargs: StaticDockerMonitor()  # type: ignore[assignment]
    Workspace._auto_container = lambda self: None  # type: ignore[method-assign,assignment,return-value]
    for name in (CONVERSATIONS_ENV, ENABLE_ENV, ACCESS_ENV):
        os.environ[name] = "1"
    script = ScriptedClient({"AGENT": [text("It is 42.")] * 4, "ONE": [text("Done.")] * 4})
    app = create_app(
        AppConfig(data_dir=data_dir, vault_key=vault_key),
        client_factory=lambda _provider: script,
        integrations=[],
        scheduler=False,
    )
    from nodestro_runtime.schemas import DEFAULT_AGENT_MODEL, DEFAULT_ASSISTANT_MODEL, SettingsIn

    app.state.nodestro.update_settings(
        SettingsIn(default_model=DEFAULT_AGENT_MODEL, assistant_model=DEFAULT_ASSISTANT_MODEL)
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    client.__enter__()
    return client


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()


def _wait(client: TestClient, run_id: str) -> None:
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    client.portal.call(ctx.wait, run_id)  # type: ignore[union-attr]


def fill(client: TestClient) -> None:
    agent = {"name": "Helper", "system_prompt": "AGENT"}
    template = _ok(client.post("/api/agents/templates", json={"agent": agent}), 201)["id"]
    _ok(client.post("/api/agents/templates", json={"agent": {"name": "Reviewer", "system_prompt": "ONE"}}), 201)
    conversation = _ok(
        client.post("/api/conversations", json={"agent": {"template_id": template}, "model": CLOUD}), 201
    )
    sent = _ok(client.post(f"/api/conversations/{conversation['id']}/messages", json={"content": "6 times 7?"}), 201)
    _wait(client, sent["assistant"]["run_id"])
    workflow = {
        "name": "Flow",
        "nodes": [{"id": "one", "type": "agent", "label": "One", "agent": {"name": "One", "system_prompt": "ONE"}}],
        "edges": [],
    }
    workflow_id = _ok(client.post("/api/workflows", json=workflow), 201)["id"]
    run = _ok(client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "go"}), 201)
    _wait(client, run["id"])
    _ok(client.post("/api/knowledge", files={"files": ("note.txt", b"A sentence to search for.", "text/plain")}), 201)
    _ok(client.put("/api/memory/Team/file", json={"path": "/memories/a.md", "content": "Team knowledge"}))
    _ok(client.post("/api/inputs", files=[("files", ("input.txt", b"input data"))]), 201)
    grant = {"resource_type": "cloud_provider", "resource_id": "anthropic", "actions": ["submit"]}
    _ok(client.post("/api/access/grants", json={**grant, "duration": "until_revoked"}), 201)
    deny = {"resource_type": "workflow", "resource_id": workflow_id, "actions": ["execute"]}
    _ok(client.post("/api/access/denies", json={**deny, "duration": "until_revoked"}), 201)
    connection = _ok(
        client.post("/api/connections", json={"type": "search", "provider": "test", "name": "Search"}), 201
    )
    _ok(client.put(f"/api/connections/{connection['id']}/secret", json={"secret": "CANARY-CONNECTION-SECRET"}))
    _ok(client.put("/api/settings", json={"providers": {"anthropic": {"api_key": "sk-ant-CANARY-PROVIDER-KEY-1"}}}))


def state(client: TestClient) -> dict[str, Any]:
    """The workspace's data as the API shows it and as the database holds it (without the collections that stay on
    each computer)."""
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    data_dir = Path(ctx.config.data_dir)
    database = sqlite3.connect(f"file:{ctx.sqlite_store.database}?mode=ro", uri=True)
    try:
        documents: dict[str, dict[str, Any]] = {}
        for collection, key, data in database.execute("SELECT collection, id, data FROM nodestro_documents"):
            if collection not in LOCAL:
                documents.setdefault(collection, {})[key] = json.loads(data)
        tables = {
            "memory_files": database.execute(
                "SELECT namespace, path, content FROM memory_files ORDER BY 1, 2"
            ).fetchall(),
            "stored_files": [
                [namespace, key, entry, hashlib.sha256(data).hexdigest()]
                for namespace, key, entry, data in database.execute(
                    "SELECT namespace, id, entry, data FROM stored_files ORDER BY 1, 2"
                )
            ],
            "index_chunks": database.execute(
                "SELECT namespace, source, title, text FROM index_chunks ORDER BY namespace, source, id"
            ).fetchall(),
        }
    finally:
        database.close()
    files = {
        str(path.relative_to(data_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in ("tools", "inputs")
        for path in sorted((data_dir / folder).rglob("*"))
        if path.is_file()
    }
    api = {
        "conversations": _ok(client.get("/api/conversations")),
        "templates": _ok(client.get("/api/agents/templates")),
        "knowledge": _ok(client.get("/api/knowledge")),
        "grants": _ok(client.get("/api/access/grants")),
        "denies": _ok(client.get("/api/access/denies")),
        "activity": _ok(client.get("/api/activity", params={"limit": 200}))["events"],
        "runs": _ok(client.get("/api/runs")),
        "search": _ok(client.get("/api/knowledge/search", params={"q": "sentence"})),
    }
    secrets = {
        "anthropic": ctx.provider_settings("anthropic").get("api_key"),
        "connections": {item["id"]: item["secret_set"] for item in _ok(client.get("/api/connections"))},
    }
    return {"documents": documents, "tables": tables, "files": files, "api": api, "secrets": secrets}


def main(command: str, data_dir: str, vault_key: str, backup: str, out: str) -> None:
    client = _app(Path(data_dir), vault_key)
    try:
        result: dict[str, Any] = {}
        if command == "fill":
            fill(client)
            Path(backup).write_bytes(client.get("/api/backup").content)
        else:
            _ok(
                client.put("/api/settings", json={"providers": {"anthropic": {"api_key": "sk-ant-OTHER-COMPUTER-KEY"}}})
            )
            upload = {"file": ("backup.zip", Path(backup).read_bytes(), "application/zip")}
            result["inspected"] = _ok(client.post("/api/backup/inspect", files=upload))
            result["restored"] = _ok(
                client.post("/api/backup/restore", json={"id": result["inspected"]["id"], "confirm": True})
            )
        result["state"] = state(client)
        Path(out).write_text(json.dumps(result, default=str), encoding="utf-8")
    finally:
        client.__exit__(None, None, None)


if __name__ == "__main__":
    main(*sys.argv[1:6])
