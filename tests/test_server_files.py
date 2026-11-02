"""``nodestro-server files ...`` and backups with input file bytes in Axentra's storage (``NODESTRO_FILES_BACKEND``).

The command and restore-drill cases need ``NODESTRO_TEST_POSTGRES``, ``pg_dump``/``pg_restore`` and the ``axentra``
package. Each drill uses databases of its own (created and dropped here), so a dump never contains another test's
schema.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
import os
from pathlib import Path
import secrets
import shutil
import tarfile
from typing import Any
import uuid

from fastapi.testclient import TestClient
import pytest
from test_inputs import upload

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


# ---- downloads (no database) ----------------------------------------------------------------------------


class _InertBlobs:
    """The volume blobs of a desktop test app, reporting every blob as kept inert (as Axentra's policy would)."""

    def __init__(self, blobs: Any) -> None:
        self.blobs = blobs

    def __getattr__(self, name: str) -> Any:
        return getattr(self.blobs, name)

    def __contains__(self, blob: object) -> bool:
        return blob in self.blobs

    def inert(self, blob: str) -> bool:
        return True


def test_an_inert_file_is_downloaded_as_opaque_bytes(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    stored = upload(client, "page.html", b"<html><body>hi</body></html>")["file"]
    before = client.get(f"/api/inputs/{stored['id']}/content")
    assert before.headers["content-type"].startswith("text/html")  # the volume backend: unchanged
    inputs = client.app.state.nodestro.inputs  # type: ignore[attr-defined]
    inputs.blobs = _InertBlobs(inputs.blobs)
    after = client.get(f"/api/inputs/{stored['id']}/content")
    assert after.status_code == 200 and after.content == b"<html><body>hi</body></html>"
    assert after.headers["content-type"] == "application/octet-stream"
    assert after.headers["content-disposition"].startswith("attachment;")
    assert after.headers["x-content-type-options"] == "nosniff"


# ---- commands and the restore drill (PostgreSQL) ----------------------------------------------------------


def _ring() -> str:
    return "k1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _url(name: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parts = conninfo_to_dict(DSN)
    parts["dbname"] = name
    return make_conninfo("", **parts)


@pytest.fixture
def databases(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], str]]:
    """``new()`` -> the URL of a fresh, empty database (dropped afterwards)."""
    if not DSN:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    pytest.importorskip("axentra", reason="needs the axentra package (the server extra)")
    import psycopg
    from psycopg import sql

    monkeypatch.delenv("NODESTRO_DATABASE_SCHEMA", raising=False)
    created: list[str] = []

    def new() -> str:
        name = f"nodestro_files_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return _url(name)

    yield new
    with psycopg.connect(DSN, autocommit=True) as connection:
        for name in created:
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _environment(tmp_path: Path, url: str, **changes: str) -> dict[str, str]:
    config = tmp_path / "axentra.toml"
    if not config.exists():
        config.write_text(f'[storage]\nbackend = "local"\nlocal_path = "{tmp_path / "objects"}"\n')
    environ = {
        "NODESTRO_PROFILE": "server",
        "NODESTRO_DATA_DIR": str(tmp_path / "server"),
        "NODESTRO_DATABASE_URL": url,
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.test",
        "NODESTRO_OIDC_ISSUER": "https://login.example",
        "NODESTRO_OIDC_CLIENT_ID": "nodestro-studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
        "NODESTRO_SECRET_KEY": base64.b64encode(b"s" * 32).decode(),
        "NODESTRO_AXENTRA": "on",
        "NODESTRO_AXENTRA_KEYRING": RING,
        "NODESTRO_AXENTRA_ALLOW_SUPERUSER": "1",
        "NODESTRO_AXENTRA_CONFIG": str(config),
        "NODESTRO_FILES_BACKEND": "axentra",
    }
    environ.update(changes)
    return environ


RING = _ring()


def _volume_upload(url: str, data_root: Path, tenant: str, data: bytes, name: str) -> Any:
    from nodestro_runtime.input_files import InputFiles
    from nodestro_runtime.storage_postgres import PostgresDatabase

    database = PostgresDatabase(url)
    try:
        return InputFiles(database.store(tenant), data_root / "tenants" / tenant / "inputs").add(data, name=name)
    finally:
        database.close()


def _read(command: Any, server: Any, tenant: str, file_id: str) -> bytes:
    from nodestro_runtime.input_files import InputFiles

    with command._files_tools(server) as files:
        root = server.profile.data_dir / "tenants" / tenant / "inputs"
        return InputFiles(files.database.store(tenant), root, blobs=files.blobs(tenant, root)).data(file_id)


def test_files_commands_need_the_switch(databases: Callable[[], str], tmp_path: Path) -> None:
    from nodestro_runtime.problems import NodestroError

    from nodestro_api import server as command

    loaded = command.load(_environment(tmp_path, databases(), NODESTRO_FILES_BACKEND="volume"))
    with pytest.raises(NodestroError) as caught:
        command.files_command(loaded, "migrate")
    assert caught.value.code == "files.backend_off"
    assert command.archive_files(loaded, tmp_path / "backups") is None
    assert "files backend: volume" in command.axentra_status(loaded)


def test_a_relative_local_directory_is_refused(
    databases: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_api import server as command

    (tmp_path / "axentra.toml").write_text('[storage]\nbackend = "local"\nlocal_path = "./var/storage"\n')
    loaded = command.load(_environment(tmp_path, databases()))
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    problems = command.check(loaded)
    assert problems and problems[0].startswith("profile.invalid: Axentra's storage.local_path must be an absolute")


@pytest.mark.skipif(shutil.which("pg_restore") is None, reason="needs pg_dump and pg_restore")
def test_migration_and_the_restore_drill(
    databases: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from nodestro_api import server as command

    url = databases()
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    before = command.load(_environment(tmp_path, url, NODESTRO_FILES_BACKEND="volume"))
    command.migrate(before, backup_dir=None, backup_taken=False)
    command.create_tenant(before, "acme", "Acme")
    data_root = before.profile.data_dir
    old = _volume_upload(url, data_root, "acme", b"uploaded before the switch", "old.txt")

    loaded = command.load(_environment(tmp_path, url))
    assert command.check(loaded) == []
    assert _read(command, loaded, "acme", old.id) == b"uploaded before the switch"  # the volume fallback
    planned = command.files_command(loaded, "migrate", dry_run=True)
    assert planned["pending"] == 2 and planned["copied"] == 0
    copied = command.files_command(loaded, "migrate", tenant="acme")
    assert copied["copied"] == 2 and copied["ok"]
    verified = command.files_command(loaded, "verify")
    assert verified["verified"] == 2 and verified["pending"] == 0 and verified["ok"]
    with pytest.raises(Exception) as refused:
        command.files_command(loaded, "remove-source")
    assert getattr(refused.value, "code", "") == "files.source_removal_off"

    # A file uploaded after the switch lives only in Axentra's storage.
    from nodestro_runtime.input_files import InputFiles

    with command._files_tools(loaded) as files:
        root = data_root / "tenants" / "acme" / "inputs"
        new = InputFiles(files.database.store("acme"), root, blobs=files.blobs("acme", root)).add(
            b"uploaded after the switch", name="new.txt"
        )

    # The backup: the database dump plus an archive of Axentra's local storage directory.
    dump = command.dump(loaded, tmp_path / "backups" / "nodestro.dump")
    archive = command.archive_files(loaded, tmp_path / "backups")
    assert archive is not None and archive.name.startswith("nodestro-files-") and dump.is_file()

    # Disaster: the storage directory and the database are lost. Restore both into new ones.
    shutil.rmtree(tmp_path / "objects")
    restored_url = databases()
    subprocess.run(["pg_restore", "--no-owner", f"--dbname={restored_url}", str(dump)], check=True)
    (tmp_path / "objects").mkdir()
    with tarfile.open(archive) as unpacked:
        unpacked.extractall(tmp_path / "objects", filter="data")
    restored = command.load(_environment(tmp_path, restored_url))
    assert command.check(restored) == []
    drill = command.files_command(restored, "verify")
    assert drill["ok"] and drill["verified"] == 4 and drill["orphans"] == []
    assert _read(command, restored, "acme", old.id) == b"uploaded before the switch"
    assert _read(command, restored, "acme", new.id) == b"uploaded after the switch"

    # Rollback: copy the Axentra-only files back to the volume, then the volume backend reads everything.
    exported = command.files_command(restored, "export-volume")
    assert exported["copied"] == 2 and exported["ok"]  # the new file and its extraction
    from nodestro_runtime.storage_postgres import PostgresDatabase

    database = PostgresDatabase(restored_url)
    try:
        volume = InputFiles(database.store("acme"), data_root / "tenants" / "acme" / "inputs")
        assert volume.data(new.id) == b"uploaded after the switch"
    finally:
        database.close()


def test_api_and_worker_contexts_read_the_same_files_through_axentra(
    databases: Callable[[], str], tmp_path: Path
) -> None:
    from nodestro_api import server as command
    from nodestro_api.server_app import TenantFiles

    url = databases()
    loaded = command.load(_environment(tmp_path, url))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    command.create_tenant(loaded, "acme", "Acme")
    with command._files_tools(loaded) as files:
        api = TenantFiles(database=files.database, data_root=tmp_path / "api-volume", files=files).open("acme")
        # The worker has no shared volume here at all: with Axentra's storage it needs none for new files.
        worker = TenantFiles(database=files.database, data_root=tmp_path / "worker-volume", files=files).open("acme")
        try:
            assert type(api.inputs.blobs).__name__ == "AxentraBlobs"
            item = api.inputs.add(b"# Brief\n\nRead me.\n", name="brief.md")
            assert worker.inputs.data(item.id) == b"# Brief\n\nRead me.\n"
            assert worker.inputs.extracted(item.id).text.startswith("# Brief")
        finally:
            api.close()
            worker.close()


def _gzip_name(archive: Path) -> str:
    """The file name in an archive's gzip header (FNAME)."""
    header = archive.read_bytes()
    assert header[:2] == b"\x1f\x8b" and header[3] & 0x08, "the gzip header names no file"
    return header[10 : header.index(b"\0", 10)].decode("latin-1")


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="needs pg_dump")
def test_a_backup_before_the_first_upload(
    databases: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh installation (no upload yet, the storage directory not created yet) is backed up with an empty
    archive; once Axentra stored a file, a missing directory is an error again."""
    from nodestro_runtime.problems import NodestroError

    from nodestro_api import server as command

    url = databases()
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    loaded = command.load(_environment(tmp_path, url))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    shutil.rmtree(tmp_path / "objects", ignore_errors=True)
    command.dump(loaded, tmp_path / "backups" / "nodestro.dump")
    archive = command.archive_files(loaded, tmp_path / "backups")
    assert archive is not None and _gzip_name(archive) == archive.name.removesuffix(".gz")
    with tarfile.open(archive) as unpacked:
        assert unpacked.getnames() == []
    assert not list((tmp_path / "backups").glob("*.partial"))

    command.create_tenant(loaded, "acme", "Acme")
    with command._files_tools(loaded) as files:  # the start creates the directory (owner only)
        assert (tmp_path / "objects").is_dir()
        root = loaded.profile.data_dir / "tenants" / "acme" / "inputs"
        from nodestro_runtime.input_files import InputFiles

        InputFiles(files.database.store("acme"), root, blobs=files.blobs("acme", root)).add(b"stored", name="a.txt")
    archived = command.archive_files(loaded, tmp_path / "later")
    assert archived is not None and _gzip_name(archived) == archived.name.removesuffix(".gz")
    with tarfile.open(archived) as unpacked:
        assert any(member.isfile() for member in unpacked.getmembers())
    shutil.rmtree(tmp_path / "objects")
    with pytest.raises(NodestroError) as caught:
        command.archive_files(loaded, tmp_path / "lost")
    assert caught.value.code == "storage.backup_failed" and "does not exist" in str(caught.value)


def test_files_cleanup_removes_what_verify_reports(
    databases: Callable[[], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An object left by a cut-off copy makes ``files verify`` exit with 2 until ``files cleanup`` removed it."""
    import json

    from nodestro_runtime.storage_postgres import PostgresDatabase

    from nodestro_api import server as command

    url = databases()
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    for key, value in _environment(tmp_path, url).items():
        monkeypatch.setenv(key, value)
    loaded = command.load(_environment(tmp_path, url))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    command.create_tenant(loaded, "acme", "Acme")
    with command._files_tools(loaded) as files:
        stray = files.upload(
            "acme", b"cut off", blob="0" * 16, name="a.txt", media_type="text/plain", origin="migration"
        )
    database = PostgresDatabase(url)
    try:
        database._write(
            "UPDATE axentra_files SET created_at = created_at - interval '2 hours' WHERE id = %s",
            (stray.axentra_file,),
        )
    finally:
        database.close()
    capsys.readouterr()
    with pytest.raises(SystemExit) as exited:
        command.main(["files", "verify"])
    assert exited.value.code == 2
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"] and [entry["file"] for entry in report["orphans"]] == [stray.axentra_file]
    command.main(["files", "cleanup", "--dry-run"])
    assert json.loads(capsys.readouterr().out)["removed"] == 0
    command.main(["files", "cleanup"])
    assert json.loads(capsys.readouterr().out)["removed"] == 1
    command.main(["files", "verify"])
    assert json.loads(capsys.readouterr().out)["ok"]
