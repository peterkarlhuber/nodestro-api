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
from typing import Any
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


def test_secrets_reencrypt_with_v1_is_the_rollback_and_keeps_v1_values(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # NODESTRO_SECRETS_FORMAT=v1 (the default) with the keyring: re-encryption moves axs1 values back to v1 and leaves
    # v1 values alone (see test_server_obs.py for the whole rollback).
    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    _seed_v1_secret(schema)
    before = _stored(schema)
    capsys.readouterr()
    command.main(["secrets", "reencrypt"])
    report = json.loads(capsys.readouterr().out)
    assert (report["pending"], report["reencrypted"], report["active_kid"]) == (0, 0, "v1")
    assert _stored(schema) == before


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


# ---- identity on Axentra (NODESTRO_IDENTITY_BACKEND) ------------------------------------------------------------


def _seed_identity(schema: str) -> dict[str, str]:
    """A tenant with an owner and a viewer who are logged in (written by Nodestro's store alone)."""
    from nodestro_runtime.identity_store import IdentityStore

    database = PostgresDatabase(DSN, schema=schema)
    try:
        identity = IdentityStore(database)
        identity.create_tenant("acme", "Acme")
        owner = identity.login("https://login.example", "olga", email="olga@example.com", name="Olga")
        viewer = identity.login("https://login.example", "viktor")
        identity.set_role("acme", owner.id, "owner")
        identity.set_role("acme", viewer.id, "viewer")
        return {
            "owner": owner.id,
            "viewer": viewer.id,
            "token": identity.create_session(viewer.id, "acme"),
        }
    finally:
        database.close()


def test_the_identity_switch_follows_the_backfill(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nodestro_runtime.axentra_identity import AxentraIdentityStore
    from nodestro_runtime.identity_store import IdentityStore

    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    seeded = _seed_identity(schema)
    # Switched to axentra before the backfill: the server refuses to start, and says why.
    switched = command.load({**os.environ, "NODESTRO_IDENTITY_BACKEND": "axentra"})
    problems = command.check(switched)
    assert len(problems) == 1 and problems[0].startswith("identity.backfill_required")
    capsys.readouterr()
    with pytest.raises(SystemExit) as stopped:
        command.main(["axentra", "reconcile-identity"])
    assert stopped.value.code == 1
    differences = json.loads(capsys.readouterr().out)
    assert not differences["ok"] and "acme: not linked" in differences["differences"]["tenants"]
    command.main(["axentra", "backfill-identity", "--dry-run"])
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] and dry["users"]["created"] == 2 and dry["sessions"]["copied"] == 1
    command.main(["axentra", "backfill-identity"])
    done = json.loads(capsys.readouterr().out)
    assert done["memberships"]["added"] == 2 and done["sessions"]["copied"] == 1
    command.main(["axentra", "reconcile-identity"])
    output = capsys.readouterr().out
    assert json.loads(output)["ok"] is True
    assert seeded["token"] not in output + json.dumps(done)
    assert command.check(switched) == []
    # The API process of axentra mode lets Axentra run the login (one identity service).
    import nodestro_api.identity as identity_module

    flows: list[Any] = []
    original = identity_module.axentra_login_flow
    monkeypatch.setattr(
        identity_module, "axentra_login_flow", lambda *a, **k: flows.append(original(*a, **k)) or flows[-1]
    )
    monkeypatch.setenv("NODESTRO_IDENTITY_BACKEND", "axentra")
    command.create_server_app()
    monkeypatch.delenv("NODESTRO_IDENTITY_BACKEND")
    assert len(flows) == 1 and isinstance(flows[0], identity_module.AxentraLoginFlow)
    assert flows[0].service.redirect_uri("nodestro") == switched.settings.redirect_uri
    # Axentra answers now: the session from before the switch, the member commands, removal ends access.
    database = PostgresDatabase(DSN, schema=schema)
    services, identity = command._platform(switched, database)
    try:
        assert isinstance(identity, AxentraIdentityStore) and identity.mode == "axentra"
        assert identity.session(seeded["token"]).role == "viewer"  # type: ignore[union-attr]
        assert command.set_role(switched, "acme", seeded["viewer"], "editor").endswith("(was viewer)")
        assert identity.session(seeded["token"]).role == "editor"  # type: ignore[union-attr]
        assert sorted(line.split("\t")[1] for line in command.members(switched, "acme")) == ["editor", "owner"]
        with pytest.raises(NodestroError) as caught:
            command.remove_member(switched, "acme", "olga@example.com")
        assert caught.value.code == "identity.last_owner"
        command.remove_member(switched, "acme", seeded["viewer"])
        assert identity.session(seeded["token"]) is None
        assert any(line == "identity backend: axentra" for line in command.axentra_status(switched))
    finally:
        services.close()
        database.close()
    # Back to Nodestro's store (the rollback is a restart): its tables were kept current.
    database = PostgresDatabase(DSN, schema=schema)
    try:
        assert IdentityStore(database).role("acme", seeded["viewer"]) is None
    finally:
        database.close()
    command.main(["axentra", "reconcile-identity"])
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_the_identity_backfill_needs_axentra(schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _environment(tmp_path, NODESTRO_AXENTRA="off").items():
        monkeypatch.setenv(key, value)
    for arguments in (["axentra", "backfill-identity"], ["axentra", "reconcile-identity"]):
        with pytest.raises(SystemExit) as stopped:
            command.main(arguments)
        assert str(stopped.value).startswith("axentra.disabled")
    with pytest.raises(NodestroError) as caught:
        command.load({**os.environ, "NODESTRO_IDENTITY_BACKEND": "mirror"})
    assert caught.value.code == "profile.invalid"


def test_mirror_mode_writes_both_sides_from_the_member_commands(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in _environment(tmp_path, NODESTRO_IDENTITY_BACKEND="mirror").items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    loaded = command.load()
    command.create_tenant(loaded, "acme", "Acme")
    link = command.setup_link(loaded, "acme")
    assert "/api/auth/login?setup=" in link
    capsys.readouterr()
    command.main(["axentra", "reconcile-identity"])
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["counts"]["tenants"] == 1
    # Suspending writes both sides too.
    command.main(["suspend-tenant", "acme"])
    capsys.readouterr()
    command.main(["axentra", "reconcile-identity"])
    assert json.loads(capsys.readouterr().out)["ok"] is True


def _seed_axentra_login_state(schema: str, ring: str) -> None:
    """A value in one of Axentra's own keyring columns (an OIDC login start), under the key of ``ring``."""
    from datetime import datetime, timedelta, timezone

    from axentra.config.secrets import Keyring
    from axentra.config.secrets import SecretBox as AxentraSecretBox
    import psycopg
    from psycopg import sql

    box = AxentraSecretBox(Keyring.parse(ring))
    state = "ab" * 32
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        connection.execute(
            "INSERT INTO axentra_login_states (state_hash, provider, nonce, code_verifier, return_to, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                state,
                "default",
                box.encrypt("n", f"axentra_login_states:{state}:nonce"),
                box.encrypt("v", f"axentra_login_states:{state}:code_verifier"),
                "/",
                datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )


def _axentra_kids(schema: str) -> list[str]:
    import psycopg
    from psycopg import sql

    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        rows = connection.execute("SELECT nonce, code_verifier FROM axentra_login_states").fetchall()
    return sorted({value.split(".")[1] for row in rows for value in row})


def test_axentras_own_keyring_values_are_reencrypted_and_their_keys_checked_at_start(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SR-05: ``secrets reencrypt`` covered only Nodestro's documents. Axentra's own keyring columns (login states,
    webhook secrets, notification messages, the inbox) use the same keyring, so a removed old key silently lost them;
    and the start check did not know their key ids."""
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    old = _ring()
    for key, value in _environment(tmp_path, NODESTRO_AXENTRA_KEYRING=old).items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    _seed_axentra_login_state(schema, old)
    new = "k2:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")

    # The old key removed too early: only Axentra's values need it, and the start refuses.
    monkeypatch.setenv("NODESTRO_AXENTRA_KEYRING", new)
    with pytest.raises(NodestroError) as caught:
        command._ready(command.load())
    assert caught.value.code == "profile.invalid" and "k1 (2 values)" in str(caught.value)

    # The rotation: new key in front, old one behind; reencrypt moves Axentra's values too.
    monkeypatch.setenv("NODESTRO_AXENTRA_KEYRING", f"{new},{old}")
    command._ready(command.load()).close()
    capsys.readouterr()
    command.main(["secrets", "reencrypt", "--dry-run"])
    dry = json.loads(capsys.readouterr().out)
    assert dry["axentra"]["by_kid"] == {"k1": 2} and dry["pending"] == 2 and dry["nodestro_pending"] == 0
    command.main(["secrets", "reencrypt"])
    done = json.loads(capsys.readouterr().out)
    assert done["axentra"]["reencrypted"] == 2 and done["failed"] == []
    assert _axentra_kids(schema) == ["k2"]
    command.main(["secrets", "reencrypt", "--dry-run"])
    assert json.loads(capsys.readouterr().out)["pending"] == 0
    # --tenant leaves Axentra's columns (they belong to no Nodestro tenant).
    command.main(["secrets", "reencrypt", "--dry-run", "--tenant", "acme"])
    assert json.loads(capsys.readouterr().out)["axentra"] is None

    # Now the old key may go.
    monkeypatch.setenv("NODESTRO_AXENTRA_KEYRING", new)
    command._ready(command.load()).close()
