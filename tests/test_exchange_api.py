"""Exchange files over the API (N-14): MCP services, service connections, knowledge collections, workflow versions.

Round trips between two desktop workspaces, a canary secret that never reaches an export, newer versions and
tampered or oversized files refused with their codes, and imports that switch nothing on.
"""

from __future__ import annotations

from collections.abc import Callable
import io
import json
from typing import Any
import zipfile

from fastapi.testclient import TestClient
import pytest

MakeApp = Callable[..., TestClient]
CANARY = "CANARY-SECRET-api-n14-8d2f61b7c4e9"


def _without_time(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "created_at"}


def _ok(answer: Any, status: int = 200) -> Any:
    assert answer.status_code == status, answer.text
    return answer.json()


def test_mcp_servers_round_trip_without_tokens_and_stay_switched_off(make_app: MakeApp) -> None:
    source, target = make_app(), make_app()
    _ok(
        source.post(
            "/api/mcp/servers",
            json={
                "name": "Docs",
                "url": "http://127.0.0.1:9/mcp",
                "allow_loopback": True,
                "auth": "bearer",
                "token": CANARY,
            },
        ),
        201,
    )
    answer = source.get("/api/mcp/export")
    assert answer.status_code == 200 and "attachment" in answer.headers["content-disposition"]
    assert CANARY not in answer.text
    exported = answer.json()
    assert exported["format"] == "nodestro-mcp/1" and [item["name"] for item in exported["servers"]] == ["Docs"]

    preview = _ok(target.post("/api/mcp/import", params={"dry_run": True}, json=exported))
    assert preview["dry_run"] is True and preview["items"][0]["status"] == "ready"
    assert target.get("/api/mcp/servers").json() == []
    report = _ok(target.post("/api/mcp/import", json=exported))
    server_id = report["items"][0]["id"]
    assert report["secrets_to_reenter"] == [f"mcp:{server_id}"]
    server = target.get(f"/api/mcp/servers/{server_id}").json()
    assert server["enabled"] is False and server["token_set"] is False and server["tools_approved"] == 0
    assert _without_time(target.get("/api/mcp/export").json()) == _without_time(exported)


def test_mcp_imports_refuse_newer_tampered_oversized_and_unisolated_files(make_app: MakeApp) -> None:
    client = make_app()
    newer = client.post("/api/mcp/import", json={"format": "nodestro-mcp/2", "servers": []})
    assert newer.status_code == 422 and newer.json()["code"] == "exchange.version_newer"
    assert newer.json()["params"] == {"version": 2, "supported": 1}
    other = client.post("/api/mcp/import", json={"format": "nodestro-connection/1", "connections": []})
    assert other.status_code == 422 and other.json()["code"] == "exchange.format_mismatch"
    tampered = {"format": "nodestro-mcp/1", "servers": [{"name": "x", "url": "https://a.example.com", "token": "t"}]}
    answer = client.post("/api/mcp/import", json=tampered)
    assert answer.status_code == 422 and answer.json()["code"] == "exchange.content_invalid"
    big = {"format": "nodestro-mcp/1", "servers": [], "pad": "x" * 1_100_000}
    answer = client.post("/api/mcp/import", json=big)
    assert answer.status_code == 413 and answer.json()["code"] == "exchange.too_large"
    # The raw body goes to the shared check: its own size (not a re-serialization), nesting before parsing.
    spaced = b'{"format": "nodestro-mcp/1", "servers": []' + b" " * 1_000_000 + b"}"
    answer = client.post("/api/mcp/import", content=spaced, headers={"Content-Type": "application/json"})
    assert answer.status_code == 413 and answer.json()["code"] == "exchange.too_large"
    deep = b'{"format": "nodestro-mcp/1", "servers": [], "x": ' + b"[" * 50_000 + b"]" * 50_000 + b"}"
    for path in ("/api/mcp/import", "/api/connections/import"):
        answer = client.post(path, content=deep, headers={"Content-Type": "application/json"})
        assert answer.status_code == 422 and answer.json()["code"] == "exchange.content_invalid"
        assert answer.json()["params"]["problems"] == [{"field": "file", "reason": "too_deep"}]
        answer = client.post(path, content=b"[1, 2", headers={"Content-Type": "application/json"})
        assert answer.status_code == 422 and answer.json()["code"] == "exchange.invalid_json"
    stdio = {"requirements": ["mcp-demo==1.0"], "module": "mcp_demo"}
    local = {"format": "nodestro-mcp/1", "servers": [{"name": "Local", "transport": "stdio", "stdio": stdio}]}
    preview = _ok(client.post("/api/mcp/import", params={"dry_run": True}, json=local))
    assert preview["items"][0]["code"] == "sandbox.isolation_required"
    refused = client.post("/api/mcp/import", json=local)
    assert refused.status_code == 422 and refused.json()["code"] == "exchange.items_refused"
    assert client.get("/api/mcp/servers").json() == []


def test_connections_round_trip_without_secrets(make_app: MakeApp) -> None:
    source, target = make_app(), make_app()
    body = {
        "type": "smtp",
        "name": "Mail",
        "settings": {"host": "smtp.example.com", "port": 587, "from_address": "bot@example.com"},
    }
    made = _ok(source.post("/api/connections", json=body), 201)
    _ok(source.put(f"/api/connections/{made['id']}/secret", json={"secret": CANARY}))
    answer = source.get("/api/connections/export")
    assert answer.status_code == 200 and CANARY not in answer.text
    exported = answer.json()
    assert exported["format"] == "nodestro-connection/1"

    preview = _ok(target.post("/api/connections/import", params={"dry_run": True}, json=exported))
    assert preview["items"][0] == {**preview["items"][0], "status": "ready", "needs_secret": True}
    report = _ok(target.post("/api/connections/import", json=exported))
    imported = target.get(f"/api/connections/{report['items'][0]['id']}").json()
    assert imported["secret_set"] is False and imported["egress"] == ["smtp.example.com:587"]
    assert report["secrets_to_reenter"] == [f"connection:{imported['id']}"]
    assert _without_time(target.get("/api/connections/export").json()) == _without_time(exported)
    again = target.post("/api/connections/import", json=exported)
    assert again.status_code == 422 and again.json()["code"] == "exchange.items_refused"
    newer = target.post("/api/connections/import", json={**exported, "format": "nodestro-connection/3"})
    assert newer.status_code == 422 and newer.json()["code"] == "exchange.version_newer"
    missing = source.get("/api/connections/export", params={"ids": ["conn_000000000000"]})
    assert missing.status_code == 400 and missing.json()["code"] == "connection.not_found"


def _collection(client: TestClient) -> bytes:
    files = [
        ("files", ("hr.md", b"Employees have 30 days of vacation.", "text/markdown")),
        ("files", ("travel.txt", b"Book trains before flights.", "text/plain")),
    ]
    _ok(client.post("/api/knowledge", files=files, data={"collection": "Policies"}), 201)
    answer = client.get("/api/knowledge/collections/export", params={"name": "Policies"})
    assert answer.status_code == 200 and answer.headers["content-type"] == "application/zip"
    return answer.content


def _upload(client: TestClient, data: bytes, **form: Any) -> Any:
    return client.post(
        "/api/knowledge/collections/import",
        files={"file": ("policies.nodestro-collection.zip", data, "application/zip")},
        data=form,
    )


def test_a_collection_round_trips_into_a_new_collection(make_app: MakeApp) -> None:
    source, target = make_app(), make_app()
    data = _collection(source)
    preview = _ok(_upload(target, data, dry_run="true"))
    assert preview["collection"] == "Policies" and [item["status"] for item in preview["items"]] == ["ready"] * 2
    assert preview["embedding"]["matches"] is False and target.get("/api/knowledge").json() == []
    report = _ok(_upload(target, data))
    assert [item["status"] for item in report["items"]] == ["created", "created"]
    assert {item["name"]: item["files"] for item in target.get("/api/knowledge/collections").json()} == {"Policies": 2}
    hits = target.get("/api/knowledge/search", params={"q": "vacation"}).json()
    assert hits and hits[0]["file_id"] in {item["id"] for item in report["items"]}
    again = target.get("/api/knowledge/collections/export", params={"name": "Policies"}).content
    with zipfile.ZipFile(io.BytesIO(data)) as first, zipfile.ZipFile(io.BytesIO(again)) as second:
        assert (
            json.loads(first.read("manifest.json"))["documents"]
            == json.loads(second.read("manifest.json"))["documents"]
        )
    exists = _upload(target, data)
    assert exists.status_code == 409 and exists.json()["code"] == "exchange.collection_exists"
    missing = target.get("/api/knowledge/collections/export", params={"name": "Nothing"})
    assert missing.status_code == 404 and missing.json()["code"] == "exchange.collection_not_found"


def test_tampered_or_newer_collections_are_refused(make_app: MakeApp) -> None:
    source, target = make_app(), make_app()
    data = _collection(source)

    def rebuilt(**extra: bytes) -> bytes:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files.update(extra)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return out.getvalue()

    manifest = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("manifest.json"))
    cases = {
        "exchange.version_newer": rebuilt(
            **{"manifest.json": json.dumps({**manifest, "format": "nodestro-collection/9"}).encode()}
        ),
        "exchange.unsafe_path": rebuilt(**{"../../outside.txt": b"x"}),
        "exchange.incomplete": rebuilt(**{"documents/0009": b"x"}),
        "exchange.checksum": rebuilt(**{"documents/0001": b"changed and longer than before"}),
        "exchange.zip_invalid": b"PK not really",
    }
    for expected, package in cases.items():
        answer = _upload(target, package, dry_run="true")
        assert answer.status_code == 422 and answer.json()["code"] == expected, (expected, answer.text)
    assert target.get("/api/knowledge").json() == []


@pytest.mark.parametrize(
    ("format", "status", "code"),
    [
        ("nodestro-workflow/1", 201, None),
        ("nodestro-workflow/2", 422, "exchange.version_newer"),
        ("nodestro-mcp/1", 422, "exchange.format_mismatch"),
        ("workflow", 422, "exchange.format_invalid"),
    ],
)
def test_the_workflow_import_checks_the_format_version(
    make_app: MakeApp, format: str, status: int, code: str | None
) -> None:
    client = make_app()
    body = {"format": format, "name": "Imported", "nodes": [{"id": "a", "type": "start"}], "edges": []}
    answer = client.post("/api/workflows/import", json=body)
    assert answer.status_code == status, answer.text
    if code is not None:
        assert answer.json()["code"] == code
    legacy = client.post("/api/workflows/import", json={"name": "Old", "nodes": [{"id": "a", "type": "start"}]})
    assert legacy.status_code == 201  # exports of earlier releases name no format
