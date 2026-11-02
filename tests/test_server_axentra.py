"""``nodestro-server`` with the Axentra platform services switched on (``NODESTRO_AXENTRA=on``).

Needs ``NODESTRO_TEST_POSTGRES`` and the ``axentra`` package (Python 3.11+, ``nodestro-runtime[postgres]``).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
import json
import os
from pathlib import Path
import secrets
import shutil
import uuid

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")
pytest.importorskip("axentra", reason="Axentra needs Python 3.11+")

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.server_settings import ENCRYPTED_PREFIX, SecretBox
from nodestro_runtime.storage_postgres import PostgresDatabase

from nodestro_api import server as command

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")
SECRET_KEY = base64.b64encode(b"s" * 32).decode()


def _ring() -> str:
    return "k1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _environment(tmp_path: Path, **changes: str) -> dict[str, str]:
    environ = {
        "NODESTRO_PROFILE": "server",
        "NODESTRO_DATA_DIR": str(tmp_path / "server"),
        "NODESTRO_DATABASE_URL": DSN or "host=127.0.0.1 dbname=none",
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.test",
        "NODESTRO_OIDC_ISSUER": "https://login.example",
        "NODESTRO_OIDC_CLIENT_ID": "nodestro-studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
        "NODESTRO_SECRET_KEY": SECRET_KEY,
        "NODESTRO_AXENTRA": "on",
        "NODESTRO_AXENTRA_KEYRING": _ring(),
        # The test database role is a superuser; production uses a dedicated role (RUNBOOK).
        "NODESTRO_AXENTRA_ALLOW_SUPERUSER": "1",
    }
    environ.update(changes)
    return environ


@pytest.fixture
def schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    if not DSN:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    import psycopg
    from psycopg import sql

    name = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    monkeypatch.setenv("NODESTRO_DATABASE_SCHEMA", name)
    yield name
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def test_a_new_server_migrates_both_schemas_and_is_ready(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = command.load(_environment(tmp_path))
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    assert command.check(loaded)  # nothing migrated yet
    assert command.migrate(loaded, backup_dir=None, backup_taken=False) == 0  # new database: no backup needed
    assert command.check(loaded) == []
    status = command.axentra_status(loaded)
    assert status[0] == "axentra: on" and status[-1] == "pending migrations: none"
    command._ready(loaded).close()


def test_switching_axentra_on_later_needs_one_backup(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    without = command.load(_environment(tmp_path, NODESTRO_AXENTRA="off"))
    command.migrate(without, backup_dir=None, backup_taken=False)
    loaded = command.load(_environment(tmp_path))
    problems = command.check(loaded)
    assert problems and problems[0].startswith("storage.schema_mismatch: Axentra")
    with pytest.raises(NodestroError) as caught:
        command._ready(loaded)
    assert caught.value.code == "storage.schema_mismatch"
    with pytest.raises(NodestroError) as caught:
        command.migrate(loaded, backup_dir=None, backup_taken=False)
    assert caught.value.code == "storage.backup_required"
    if shutil.which("pg_dump") is None:
        command.migrate(loaded, backup_dir=None, backup_taken=True)
    else:
        command.migrate(loaded, backup_dir=tmp_path / "backups", backup_taken=False)
        dumps = list((tmp_path / "backups").iterdir())
        assert len(dumps) == 1 and dumps[0].name.startswith("pre-migration-axentra-")
    assert command.check(loaded) == []
    assert command.check(without) == []  # switching it off again needs nothing (Axentra's tables just stay)


def _seed_v1_secret(schema: str) -> None:
    database = PostgresDatabase(DSN, schema=schema)
    try:
        box = SecretBox(base64.b64decode(SECRET_KEY))
        database.store("acme").put_raw(
            "settings",
            "app",
            {"providers": {"openai": {"api_key": box.encrypt("acme", "providers.openai.api_key", "sk-1")}}},
        )
    finally:
        database.close()


def _stored(schema: str) -> str:
    database = PostgresDatabase(DSN, schema=schema)
    try:
        return str(database.store("acme").get_raw("settings", "app")["providers"]["openai"]["api_key"])
    finally:
        database.close()


def test_secrets_reencrypt_moves_v1_values_to_the_keyring(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environment(tmp_path, NODESTRO_SECRETS_FORMAT="axs1")
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    _seed_v1_secret(schema)
    capsys.readouterr()
    command.main(["secrets", "reencrypt", "--dry-run"])
    dry = json.loads(capsys.readouterr().out)
    assert (dry["pending"], dry["reencrypted"], dry["failed"]) == (1, 0, [])
    assert _stored(schema).startswith(ENCRYPTED_PREFIX)
    command.main(["secrets", "reencrypt"])
    done = json.loads(capsys.readouterr().out)
    assert done["reencrypted"] == 1 and _stored(schema).startswith("axs1.k1.")
    box = command.load().settings.secret_box()
    assert box.decrypt("acme", "providers.openai.api_key", _stored(schema)) == "sk-1"


def test_secrets_reencrypt_is_refused_while_new_values_are_still_written_as_v1(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as stopped:
        command.main(["secrets", "reencrypt"])
    assert str(stopped.value).startswith("secrets.keyring_required")


def test_an_unreadable_secret_fails_the_command_and_stays(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in _environment(tmp_path, NODESTRO_SECRETS_FORMAT="axs1").items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    _seed_v1_secret(schema)
    # Another server key: the stored value cannot be decrypted and must not be destroyed.
    monkeypatch.setenv("NODESTRO_SECRET_KEY", base64.b64encode(b"x" * 32).decode())
    before = _stored(schema)
    capsys.readouterr()
    with pytest.raises(SystemExit) as stopped:
        command.main(["secrets", "reencrypt"])
    assert stopped.value.code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == ["acme/settings/app:providers.openai.api_key"]
    assert _stored(schema) == before


def test_axentra_reconcile_needs_a_mirror_switched_on(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as stopped:
        command.main(["axentra", "reconcile"])
    assert str(stopped.value).startswith("axentra.mirrors_off")


def test_the_mirrors_attach_to_the_server_and_reconcile_writes_older_records(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nodestro_runtime.tool_approvals import ToolApprovalEvent, record_audit

    for key, value in _environment(tmp_path, NODESTRO_AXENTRA_AUDIT="1", NODESTRO_AXENTRA_USAGE="1").items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    loaded = command.load()
    command.create_tenant(loaded, "acme", "Acme")
    database = PostgresDatabase(DSN, schema=schema)
    try:
        record_audit(database.store("acme"), ToolApprovalEvent(kind="decided", actor="u_1", run_id="run_1"))
    finally:
        database.close()
    capsys.readouterr()
    command.main(["axentra", "reconcile", "--dry-run"])
    dry = json.loads(capsys.readouterr().out)
    assert (dry["audit_missing"], dry["written"]) == (1, 0)
    command.main(["axentra", "reconcile"])
    done = json.loads(capsys.readouterr().out)
    assert (done["audit_missing"], done["written"], done["failed"]) == (1, 1, 0)
    command.main(["axentra", "reconcile"])
    assert json.loads(capsys.readouterr().out)["audit_missing"] == 0
    # The API and worker processes attach the mirror: a new event is mirrored in its own transaction.
    database = PostgresDatabase(DSN, schema=schema)
    services = command._attach_mirrors(loaded, database)
    try:
        assert services is not None and database.platform_events is not None
        record_audit(database.store("acme"), ToolApprovalEvent(kind="pre_granted", actor="u_1", pre_grant_id="tpg_1"))
        actions = [
            row[0]
            for row in database._query(
                "SELECT action FROM axentra_audit_events WHERE action LIKE %s ORDER BY occurred_at", ("nodestro.%",)
            )
        ]
        assert actions == ["nodestro.tool_approval.decided", "nodestro.tool_approval.pre_granted"]
        assert any(line == "audit mirror: on" for line in command.axentra_status(loaded))
    finally:
        database.platform_events = None
        services.close()
        database.close()
