"""Backups and the privacy-friendly trace export."""

from __future__ import annotations

from collections.abc import Callable
import io
import json
import zipfile

from fastapi.testclient import TestClient

from conftest import text, tool_use
from test_runs import agent_node, create, start

# ---- 6. backups -----------------------------------------------------------------------


def _fill(client: TestClient) -> str:
    workflow_id = create(client, [agent_node("one", "ONE")], [])
    start(client, workflow_id)
    client.post("/api/agents/templates", json={"agent": {"name": "Vorlage"}})
    client.put("/api/memory/Team/file", json={"path": "/memories/a.md", "content": "Wissen"})
    client.post("/api/knowledge", files={"files": ("notiz.txt", b"Ein Satz zum Suchen.", "text/plain")})
    client.put(f"/api/organize/workflow/{workflow_id}", json={"favorite": True})
    source = {"function_name": "double_it", "code": DOUBLE, "tests": DOUBLE_TESTS}
    assert client.post("/api/tools", json=source).json()["tool"]["name"] == "double_it"
    client.put("/api/settings", json={"api_key": "sk-ant-secret-key-123456", "default_budget_usd": 3.5})
    return workflow_id


DOUBLE = '''\
def double_it(x: int) -> int:
    """Double a number.

    Args:
        x: The number.

    Returns:
        Twice the number.
    """
    return x * 2
'''
DOUBLE_TESTS = """\
from tool import double_it


def test_double():
    assert double_it(2) == 4
"""


def _upload(client: TestClient, data: bytes, name: str = "backup.zip") -> dict:  # type: ignore[type-arg]
    response = client.post("/api/backup/inspect", files={"file": (name, data, "application/zip")})
    return {"status": response.status_code, **response.json()}


def test_backup_round_trip_restores_everything(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("a")]})
    workflow_id = _fill(client)
    response = client.get("/api/backup")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    data = response.content
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert b"sk-ant-secret" not in archive.read("nodestro.db")
        assert "tools/double_it/v1.json" in archive.namelist()
    assert manifest["contents"]["workflows"] == 1 and manifest["contents"]["runs"] == 1
    assert manifest["excluded"] == ["api_key", "provider_keys", "api_keys", "api_idempotency"]

    # Change everything, then restore into the same app.
    client.delete(f"/api/workflows/{workflow_id}")
    client.delete("/api/memory/Team")
    client.delete("/api/tools/double_it")
    for item in client.get("/api/knowledge").json():
        client.delete(f"/api/knowledge/{item['id']}")
    client.put("/api/settings", json={"providers": {"anthropic": {"api_key": "sk-ant-new-key-987654"}}})

    info = _upload(client, data)
    assert info["status"] == 200, info
    assert info["contents"]["workflows"] == 1 and info["current"]["workflows"] == 0
    assert client.post("/api/backup/restore", json={"id": info["id"], "confirm": False}).status_code == 400
    result = client.post("/api/backup/restore", json={"id": info["id"], "confirm": True}).json()
    assert result["safety_backup"].startswith("before-restore-")

    assert client.get(f"/api/workflows/{workflow_id}").status_code == 200
    assert len(client.get("/api/runs").json()) == 1
    assert client.get("/api/memory/Team/file", params={"path": "/memories/a.md"}).json()["content"] == "Wissen"
    assert [item["name"] for item in client.get("/api/knowledge").json()] == ["notiz.txt"]
    assert client.get("/api/knowledge/search", params={"q": "Suchen"}).json()[0]["source"]
    assert client.post("/api/tools/double_it/run", json={"input": {"x": 4}}).json() == {
        "output": "8",
        "is_error": False,
    }
    assert client.get("/api/organize").json()["items"][f"workflow:{workflow_id}"]["favorite"] is True
    settings = client.get("/api/settings").json()
    assert settings["default_budget_usd"] == 3.5
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    assert nodestro.provider_settings("anthropic")["api_key"] == "sk-ant-new-key-987654"  # this computer's key stays
    assert [item["name"] for item in client.get("/api/backup/local").json()] == [result["safety_backup"]]
    # The safety copy is a complete backup too.
    assert client.post(f"/api/backup/local/{result['safety_backup']}/inspect").json()["contents"]["workflows"] == 0


def test_damaged_backups_are_rejected(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("a")]})
    _fill(client)
    data = client.get("/api/backup").content
    assert _upload(client, b"no zip")["code"] == "backup.not_zip"

    def rewrite(change: Callable[[dict[str, bytes]], None]) -> bytes:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        change(files)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    def tamper(files: dict[str, bytes]) -> None:
        files["nodestro.db"] = files["nodestro.db"][:-10] + b"0123456789"

    def evil(files: dict[str, bytes]) -> None:
        files["../evil.txt"] = b"x"

    def no_manifest(files: dict[str, bytes]) -> None:
        del files["manifest.json"]

    assert _upload(client, rewrite(tamper))["code"] == "backup.checksum"
    assert _upload(client, rewrite(evil))["code"] == "backup.unsafe_path"
    assert _upload(client, rewrite(no_manifest))["code"] == "backup.incomplete"
    assert client.post("/api/backup/restore", json={"id": "000000000000", "confirm": True}).json()["code"] == (
        "backup.expired"
    )


# ---- 10. sanitized trace export -------------------------------------------------------


def test_sanitized_export_changes_only_the_copy(make_app: Callable[..., TestClient]) -> None:
    secret = "sk-ant-api03-zyxwvutsrqponmlkjihgfedcba"
    client = make_app(
        {"ONE": [tool_use("calculator", {"expression": "1+1", "api_key": "abc12345"}), text(f"Mail: x@y.de {secret}")]}
    )
    workflow_id = create(client, [agent_node("one", "ONE", tools=["calculator"])], [])
    run = start(client, workflow_id, "Kontakt: peter@example.com")
    full = client.get(f"/api/runs/{run['id']}/export").json()
    assert full["input"] == "Kontakt: peter@example.com" and full.get("sanitized") is None

    response = client.get(f"/api/runs/{run['id']}/export", params={"mode": "sanitized"})
    assert "_sanitized.json" in response.headers["content-disposition"]
    clean = response.json()
    dumped = json.dumps(clean)
    for value in ("peter@example.com", "x@y.de", secret, "abc12345"):
        assert value not in dumped
    assert clean["sanitized"]["replaced"]["email"] >= 2 and clean["sanitized"]["replaced"]["api_key"] >= 1
    assert clean["usage"] == run["usage"] and clean["nodes"]["one"]["usage"] == run["nodes"]["one"]["usage"]
    # The stored run is unchanged.
    assert client.get(f"/api/runs/{run['id']}").json()["input"] == "Kontakt: peter@example.com"

    stripped = client.get(f"/api/runs/{run['id']}/export", params={"mode": "sanitized", "contents": True}).json()
    assert stripped["input"].startswith("[REMOVED:")
    assert stripped["nodes"]["one"]["output"].startswith("[REMOVED:")
    assert stripped["workflow"]["nodes"][0]["agent"]["system_prompt"].startswith("[REMOVED:")
    assert stripped["nodes"]["one"]["tool_calls"][0]["name"] == "calculator"

    # A sanitized trace can be imported and says so.
    imported = client.post("/api/runs/import", content=json.dumps(clean)).json()
    shown = client.get(f"/api/runs/{imported['run']['id']}").json()
    assert shown["sanitized"]["personal_data"] is True
