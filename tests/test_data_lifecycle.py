"""The product-neutral backup core, export everything and delete everything (N-15).

- A backup made on one computer restores identically on another: two processes with their own data directories and
  their own keys for the data directory's secrets (``tests/backup_machine.py``).
- A backup of the format before N-15 (``tests/fixtures/backup-v1.zip``, made by the release before it, without a
  ``product`` field) stays readable and restorable.
- Delete everything leaves no data in the data directory; export everything holds no secret; both and a restore are
  audited, need the ``install`` permission, the CSRF value when it is switched on, and an explicit confirmation.
"""

from __future__ import annotations

from collections.abc import Callable
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any
import zipfile

from fastapi.testclient import TestClient
from launch_matrix import ORIGIN, SECRET, product_app
import pytest
from test_data import _fill

from nodestro_api import backup
from nodestro_api.context import AppConfig
from nodestro_api.data_lifecycle import KEPT
from nodestro_api.launch import CSRF_HEADER, LaunchHardening
from nodestro_api.permissions import Permission, Principal

from conftest import text

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "backup-v1.zip"
CANARIES = (b"CANARY-PROVIDER-KEY", b"CANARY-CONNECTION-SECRET", b"CANARY-KNOWLEDGE", b"CANARY-MEMORY")
CONFIRM = {"confirm": "delete all data"}


def _machine(command: str, data_dir: Path, key: str, archive: Path, out: Path) -> dict[str, Any]:
    """Run one computer of the acceptance test as a process of its own; returns what it wrote."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])}
    done = subprocess.run(
        [sys.executable, str(HERE / "backup_machine.py"), command, str(data_dir), key * 44, str(archive), str(out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    result: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return result


def _without(document: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in fields}


def test_a_backup_from_one_computer_restores_identically_on_another(tmp_path: Path) -> None:
    archive = tmp_path / "backup.zip"
    first = _machine("fill", tmp_path / "a", "a", archive, tmp_path / "a.json")
    second = _machine("restore", tmp_path / "b", "b", archive, tmp_path / "b.json")
    a, b = first["state"], second["state"]

    # What the acceptance names, as the API shows it: conversations, agents, knowledge, grants and logs.
    for name in ("conversations", "templates", "knowledge", "search", "grants", "denies", "activity", "runs"):
        assert a["api"][name], name
        assert b["api"][name] == a["api"][name], name
    # And as the database holds it: every document, memory, stored files, the search index, tool and input files.
    assert b["tables"] == a["tables"] and all(a["tables"].values())
    files = {name: digest for name, digest in a["files"].items() if not name.endswith(".lock")}
    assert b["files"] == files and any(name.startswith("inputs/blobs/") for name in files)
    differs = {
        name
        for name in set(a["documents"]) | set(b["documents"])
        if a["documents"].get(name) != b["documents"].get(name)
    }
    assert differs == {"settings", "service_connections", "access_audit"}
    # Only the secrets differ: each computer keeps its own provider key; the connection's secret was left out.
    settings_a, settings_b = a["documents"]["settings"]["app"], b["documents"]["settings"]["app"]
    assert _without(settings_a, "providers") == _without(settings_b, "providers")
    [(connection_id, connection)] = a["documents"]["service_connections"].items()
    assert connection["secret_sealed"] and "secret_sealed" not in b["documents"]["service_connections"][connection_id]
    assert _without(connection, "secret_sealed") == b["documents"]["service_connections"][connection_id]
    # The restore is audited on the second computer; nothing else was added.
    assert a["documents"].get("access_audit", {}) == {}
    assert [event["kind"] for event in b["documents"]["access_audit"].values()] == ["backup_restored"]

    # Secrets are left out and asked for again: the connection's secret; the provider key is this computer's.
    assert a["secrets"]["anthropic"] == "sk-ant-CANARY-PROVIDER-KEY-1"
    assert b["secrets"] == {"anthropic": "sk-ant-OTHER-COMPUTER-KEY", "connections": {connection_id: False}}
    expected = [f"connection:{connection_id}"]
    assert second["inspected"]["secrets_to_reenter"] == expected == second["restored"]["secrets_to_reenter"]
    assert second["inspected"]["product"] == "nodestro"
    with zipfile.ZipFile(archive) as opened:
        manifest = json.loads(opened.read("manifest.json"))
        content = b"".join(opened.read(name) for name in opened.namelist())
    assert manifest["product"] == "nodestro"
    assert manifest["secrets_excluded"] == sorted(["provider:anthropic", f"connection:{connection_id}"])
    assert b"CANARY" not in content and b"nodestro:enc:" not in content
    if os.name == "posix":
        # Restored files are private, as those they replace.
        for path in [tmp_path / "b" / "inputs", *(tmp_path / "b" / "inputs").rglob("*"), tmp_path / "b" / "backups"]:
            assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, path


def _upload(client: TestClient, data: bytes) -> Any:
    return client.post("/api/backup/inspect", files={"file": ("backup.zip", data, "application/zip")})


def test_a_backup_from_before_the_product_field_stays_readable(make_app: Callable[..., TestClient]) -> None:
    data = FIXTURE.read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format"] == "nodestro-backup" and manifest["format_version"] == 1 and "product" not in manifest

    client = make_app({"ONE": [text("Done.")]})
    client.put("/api/settings", json={"providers": {"anthropic": {"api_key": "sk-ant-this-computer-1"}}})
    info = _upload(client, data)
    assert info.status_code == 200, info.text
    checked = info.json()
    assert checked["product"] == "nodestro" and checked["secrets_to_reenter"] == []
    assert checked["contents"] | {"conversations": 0} == manifest["contents"]
    restored = client.post("/api/backup/restore", json={"id": checked["id"], "confirm": True})
    assert restored.status_code == 200, restored.text
    [workflow] = client.get("/api/workflows").json()
    assert len(client.get("/api/runs").json()) == 1
    assert [item["agent"]["name"] for item in client.get("/api/agents/templates").json()] == ["Helper"]
    assert client.get("/api/memory/Team/file", params={"path": "/memories/a.md"}).json()["content"] == "Team knowledge"
    assert [item["name"] for item in client.get("/api/knowledge").json()] == ["note.txt"]
    assert client.get("/api/tools/double_it").json()["trust"] == "untrusted"
    assert client.get("/api/organize").json()["items"][f"workflow:{workflow['id']}"]["favorite"] is True
    assert client.get("/api/settings").json()["default_budget_usd"] == 3.5
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    assert nodestro.provider_settings("anthropic")["api_key"] == "sk-ant-this-computer-1"

    # A backup from before N-15 does not name the secrets it left out: nothing to ask for (documented limit).
    other = make_app()
    assert _upload(other, data).json()["secrets_to_reenter"] == []


def test_a_product_names_its_backups_and_refuses_another_products(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    nodestro.config.product_id = "example-product"
    refused = _upload(client, FIXTURE.read_bytes())  # a backup without the field is the studio's
    assert (refused.status_code, refused.json()["code"]) == (400, "backup.product_mismatch")
    assert refused.json()["params"] == {"found": "nodestro", "expected": "example-product"}
    own = client.get("/api/backup").content
    with zipfile.ZipFile(io.BytesIO(own)) as archive:
        assert json.loads(archive.read("manifest.json"))["product"] == "example-product"
    assert _upload(client, own).json()["product"] == "example-product"
    studio = make_app()
    assert _upload(studio, own).json()["code"] == "backup.product_mismatch"
    with pytest.raises(ValueError):
        backup.BackupProfile(product="Not A Product", data_dir=Path("."))
    with pytest.raises(ValueError):
        backup.BackupProfile(product="example", data_dir=Path("."), components=("database",))


def test_a_profile_without_a_component_neither_writes_nor_replaces_it(
    make_app: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_app({"ONE": [text("a")]})
    _fill(client)
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    profile = backup.BackupProfile(product="nodestro", data_dir=nodestro.config.data_dir, components=("inputs",))
    target = tmp_path / "no-tools.zip"
    backup.create_backup(nodestro, target, profile)
    with zipfile.ZipFile(target) as archive:
        assert not [name for name in archive.namelist() if name.startswith("tools/")]
        assert json.loads(archive.read("manifest.json"))["components"] == ["inputs"]
    info = backup.inspect_backup(nodestro, target, target.name, profile)
    backup.restore_backup(nodestro, info.id, profile)
    assert client.get("/api/tools/double_it").status_code == 200  # the tool files were left as they are


def _canaries(client: TestClient) -> None:
    _fill(client)
    client.post("/api/knowledge", files={"files": ("secret.txt", b"CANARY-KNOWLEDGE in a file", "text/plain")})
    client.put("/api/memory/Team/file", json={"path": "/memories/b.md", "content": "CANARY-MEMORY"})
    connection = client.post("/api/connections", json={"type": "search", "provider": "test", "name": "Search"}).json()
    client.put(f"/api/connections/{connection['id']}/secret", json={"secret": "CANARY-CONNECTION-SECRET"})
    client.put("/api/settings", json={"providers": {"anthropic": {"api_key": "sk-ant-CANARY-PROVIDER-KEY-1"}}})


def test_delete_everything_leaves_no_data_in_the_data_directory(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("a")]})
    _canaries(client)
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    data_dir = Path(nodestro.config.data_dir)
    # A restore leaves a safety backup; other things lie around too, a link to a file outside among them.
    info = _upload(client, client.get("/api/backup").content).json()
    client.post("/api/backup/restore", json={"id": info["id"], "confirm": True})
    (data_dir / "exports").mkdir(exist_ok=True)
    (data_dir / "exports" / "old.zip").write_bytes(b"CANARY-MEMORY in an export")
    outside = data_dir.parent / "outside.txt"
    outside.write_text("stays", encoding="utf-8")
    (data_dir / "link").symlink_to(outside)
    assert any(canary in path.read_bytes() for path in data_dir.rglob("*") if path.is_file() for canary in CANARIES)

    for body in ({"confirm": "yes"}, {"confirm": "DELETE ALL DATA"}):
        answer = client.post("/api/data/delete-all", json=body)
        assert (answer.status_code, answer.json()["code"]) == (400, "data.not_confirmed")
    assert client.post("/api/data/delete-all", json={}).status_code == 422
    assert client.post("/api/data/delete-all", json={"confirm": True}).status_code == 422
    assert client.get("/api/workflows").json()  # nothing changed yet

    deleted = client.post("/api/data/delete-all", json=CONFIRM)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["contents"]["workflows"] == 1 and deleted.json()["removed_entries"] >= 4

    # Nothing of the data is left in any file of the data directory, the database's log included.
    left = sorted(path.name for path in data_dir.iterdir())
    assert set(left) <= KEPT, left
    for path in data_dir.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            for canary in (*CANARIES, b"sk-ant", b"nodestro:enc:", b"double_it"):
                assert canary not in content, (path.name, canary)
    assert outside.read_text(encoding="utf-8") == "stays"  # a link is removed, never followed

    # The workspace starts over; the deletion is the audit trail's only entry.
    assert client.get("/api/workflows").json() == []
    assert client.get("/api/knowledge").json() == []
    assert client.get("/api/connections").json() == []
    assert client.get("/api/memory").json() == []
    assert client.get("/api/tools/double_it").status_code == 404
    assert nodestro.provider_settings("anthropic").get("api_key") in (None, "")
    [event] = client.get("/api/access-audit").json()
    assert (event["kind"], event["actor"], event["target_type"], event["target_id"]) == (
        "data_deleted",
        "local",
        "data",
        "nodestro",
    )
    created = client.post("/api/workflows", json={"name": "New", "nodes": [], "edges": []})
    assert created.status_code == 201, created.text


def test_delete_everything_waits_for_background_work(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app()
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    client.post("/api/workflows", json={"name": "Kept", "nodes": [], "edges": []})
    monkeypatch.setattr(nodestro, "background_work", lambda: ["run:run_x"])
    answer = client.post("/api/data/delete-all", json=CONFIRM)
    assert (answer.status_code, answer.json()["code"]) == (400, "data.busy")
    assert answer.json()["params"] == {"work": ["run:run_x"]}
    assert len(client.get("/api/workflows").json()) == 1
    assert client.get("/api/access-audit").json() == []


def test_export_everything_is_readable_and_holds_no_secret(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("a")]})
    _canaries(client)
    answer = client.get("/api/data/export")
    assert answer.status_code == 200 and answer.headers["content-type"] == "application/zip"
    assert 'filename="nodestro-export-' in answer.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(answer.content)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        entries = {name: archive.read(name) for name in names}
    assert (manifest["format"], manifest["format_version"], manifest["product"]) == ("nodestro-export", 1, "nodestro")
    assert set(manifest["files"]) == set(names) - {"manifest.json"}
    # Without a key for the data directory, the connection's secret stays in this process only (tested above).
    assert manifest["secrets_excluded"] == ["provider:anthropic"]
    workflows = [json.loads(line) for line in entries["documents/workflows.jsonl"].splitlines()]
    assert [row["data"]["name"] for row in workflows] == ["Test"]
    memory = [json.loads(line) for line in entries["memory.jsonl"].splitlines()]
    assert "CANARY-MEMORY" in {row["content"] for row in memory} and len(memory) == 2
    files = [json.loads(line) for line in entries["stored-files.jsonl"].splitlines()]
    assert {entries[row["file"]] for row in files} >= {b"CANARY-KNOWLEDGE in a file"}
    assert "tools/double_it/v1.json" in names
    secrets = b"".join(content for name, content in entries.items())
    for secret in (b"CANARY-PROVIDER-KEY", b"CANARY-CONNECTION-SECRET", b"nodestro:enc:", b"sk-ant"):
        assert secret not in secrets
    for local in ("api_keys", "api_idempotency", "local_vault"):
        assert f"documents/{local}.jsonl" not in names
    [event] = client.get("/api/access-audit").json()
    assert event["kind"] == "data_exported" and event["details"]["file_name"].startswith("nodestro-export-")


def test_export_delete_and_restore_need_the_install_permission(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    admin = Principal(id="usr_admin", permissions=frozenset(Permission) - {Permission.INSTALL})
    client.app.state.principal_resolver = lambda _connection: admin  # type: ignore[attr-defined]
    for method, path, body in (
        ("get", "/api/data/export", None),
        ("post", "/api/data/delete-all", CONFIRM),
        ("post", "/api/backup/restore", {"id": "000000000000", "confirm": True}),
    ):
        answer = getattr(client, method)(path, **({"json": body} if body else {}))
        assert (answer.status_code, answer.json()["code"]) == (403, "access.forbidden"), path
        assert answer.json()["params"]["permission"] == "install"


def test_delete_everything_needs_the_csrf_value_in_a_product(tmp_path: Path) -> None:
    """In an embedding product (router group ``data``, its own product id) with the CSRF switch on."""
    app = product_app(
        tmp_path / "data",
        groups=("data",),
        launch_secret=SECRET,
        hardening=LaunchHardening(csrf=True),
        product_id="example-product",
    )
    with TestClient(app, base_url=ORIGIN) as client:
        from test_launch_hardening import launched

        csrf = launched(client)["csrf"]
        refused = client.post("/api/data/delete-all", json=CONFIRM, headers={"origin": ORIGIN})
        assert (refused.status_code, refused.json()["code"]) == (403, "access.csrf_invalid")
        deleted = client.post("/api/data/delete-all", json=CONFIRM, headers={"origin": ORIGIN, CSRF_HEADER: csrf})
        assert deleted.status_code == 200, deleted.text
        exported = client.get("/api/data/export")
        assert 'filename="example-product-export-' in exported.headers["content-disposition"]


def test_the_studio_keeps_its_product_id() -> None:
    assert AppConfig(data_dir=Path(".")).product_id == backup.LEGACY_PRODUCT == "nodestro"
