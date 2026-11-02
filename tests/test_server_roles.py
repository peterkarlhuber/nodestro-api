"""``nodestro-server`` with separate database roles: ``migrate`` (and the backup) as the owner, ``api``/``worker`` as
a runtime role without superuser, ``BYPASSRLS`` or table ownership; Axentra switched on without
``NODESTRO_AXENTRA_ALLOW_SUPERUSER``.

Needs ``NODESTRO_TEST_POSTGRES`` (a superuser: it creates the two roles and a schema owned by the owner) and the
``axentra`` package.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import shutil
import subprocess

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")
pytest.importorskip("axentra", reason="Axentra needs Python 3.11+")

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage_postgres import POSTGRES_SCHEMA_VERSION
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from nodestro_api import server as command

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


@dataclass
class Setup:
    schema: str
    owner: str
    runtime: str
    owner_url: str
    runtime_url: str


def _url(user: str, password: str) -> str:
    parts = conninfo_to_dict(DSN)
    parts.update(user=user, password=password)
    return make_conninfo("", **parts)


@pytest.fixture
def roles(monkeypatch: pytest.MonkeyPatch) -> Iterator[Setup]:
    """An owner role (owns the schema; BYPASSRLS so that pg_dump can read Axentra's forced-RLS tables) and a runtime
    role (no privileges yet), as the RUNBOOK sets them up."""
    if not DSN:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    suffix = secrets.token_hex(4)
    password = secrets.token_hex(16)
    found = Setup(
        schema=f"nodestro_test_{suffix}",
        owner=f"nd_owner_{suffix}",
        runtime=f"nd_runtime_{suffix}",
        owner_url=_url(f"nd_owner_{suffix}", password),
        runtime_url=_url(f"nd_runtime_{suffix}", password),
    )
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER BYPASSRLS PASSWORD {}").format(
                sql.Identifier(found.owner), sql.Literal(password)
            )
        )
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(found.runtime), sql.Literal(password)
            )
        )
        admin.execute(
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                sql.Identifier(found.schema), sql.Identifier(found.owner)
            )
        )
    monkeypatch.setenv("NODESTRO_DATABASE_SCHEMA", found.schema)
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    yield found
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(found.schema)))
        for role in (found.runtime, found.owner):
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _environment(tmp_path: Path, **changes: str) -> dict[str, str]:
    environ = {
        "NODESTRO_PROFILE": "server",
        "NODESTRO_DATA_DIR": str(tmp_path / "server"),
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.test",
        "NODESTRO_OIDC_ISSUER": "https://login.example",
        "NODESTRO_OIDC_CLIENT_ID": "nodestro-studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
        "NODESTRO_SECRET_KEY": base64.b64encode(b"s" * 32).decode(),
        "NODESTRO_AXENTRA": "on",
        "NODESTRO_AXENTRA_KEYRING": "k1:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
        "NODESTRO_AXENTRA_AUDIT": "1",
        "NODESTRO_AXENTRA_USAGE": "1",
    }
    environ.update(changes)
    return environ


def _split(roles: Setup, tmp_path: Path, **changes: str) -> command.ServerEnvironment:
    return command.load(
        _environment(
            tmp_path,
            NODESTRO_DATABASE_URL=roles.runtime_url,
            NODESTRO_DATABASE_MIGRATION_URL=roles.owner_url,
            **changes,
        )
    )


def test_migrate_as_owner_then_serve_as_the_runtime_role(roles: Setup, tmp_path: Path) -> None:
    loaded = _split(roles, tmp_path)
    assert loaded.settings.separate_migration_role
    assert command.migrate(loaded, backup_dir=None, backup_taken=False) == 0
    # migrate granted the runtime role its privileges: the server is ready without ALLOW_SUPERUSER.
    assert command.check(loaded) == []
    report = command.database_roles(loaded)
    assert report["runtime"]["role"] == roles.runtime and report["runtime"]["restricted"]
    assert report["runtime"]["table_owner"] is False and report["owner"]["table_owner"] is True
    assert report["owner_can_back_up"] is True
    database = command._ready(loaded)
    try:
        services, identity = command._platform(loaded, database)
        try:
            # Axentra runs on Nodestro's pool, as the runtime role, with the mirrors attached.
            assert services.platform.db.owns_pool is False
            assert services.platform.db.role_info() == {"superuser": False, "bypass_rls": False, "table_owner": False}
            identity.create_tenant("acme", "ACME")
            assert [tenant.id for tenant in identity.tenants()] == ["acme"]
        finally:
            database.platform_events = None
            services.close()
    finally:
        database.close()
    # A second run has nothing to do and grants again (idempotent).
    assert command.migrate(loaded, backup_dir=None, backup_taken=False) == POSTGRES_SCHEMA_VERSION


def test_the_backup_runs_as_the_owner_and_reads_axentras_tables(roles: Setup, tmp_path: Path) -> None:
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        pytest.skip("pg_dump is not installed")
    loaded = _split(roles, tmp_path)
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    database = command._ready(loaded)
    try:
        services, identity = command._platform(loaded, database)
        try:
            identity.create_tenant("acme", "ACME")
        finally:
            services.close()
    finally:
        database.close()
    dump = command.dump(loaded, tmp_path / "backup.dump")
    listing = subprocess.run(["pg_restore", "--list", str(dump)], capture_output=True, text=True, check=True).stdout
    assert "TABLE DATA" in listing and "axentra_tenants" in listing and "nodestro_tenants" in listing


def test_the_owner_role_is_refused_at_runtime(roles: Setup, tmp_path: Path) -> None:
    owner_only = command.load(_environment(tmp_path, NODESTRO_DATABASE_URL=roles.owner_url))
    assert not owner_only.settings.separate_migration_role
    command.migrate(owner_only, backup_dir=None, backup_taken=False)
    problems = command.check(owner_only)
    assert len(problems) == 1 and problems[0].startswith("storage.role_unsafe") and "owns tables" in problems[0]
    with pytest.raises(NodestroError) as caught:
        command._ready(owner_only)
    assert caught.value.code == "storage.role_unsafe"
    with pytest.raises(NodestroError) as caught:
        command.grant_runtime_role(owner_only)
    assert caught.value.code == "storage.role_unsafe"  # no separate owner connection configured
    # The explicit opt-in (development only) still works.
    allowed = command.load(
        _environment(tmp_path, NODESTRO_DATABASE_URL=roles.owner_url, NODESTRO_AXENTRA_ALLOW_SUPERUSER="1")
    )
    assert command.check(allowed) == []


def test_the_runtime_role_cannot_migrate(roles: Setup, tmp_path: Path) -> None:
    runtime_only = command.load(_environment(tmp_path, NODESTRO_DATABASE_URL=roles.runtime_url))
    # Without USAGE on the owner's schema it cannot even see it, let alone create tables in it.
    with pytest.raises((NodestroError, psycopg.Error)):
        command.migrate(runtime_only, backup_dir=None, backup_taken=False)


def test_migrate_refuses_a_runtime_role_that_bypasses_rls(roles: Setup, tmp_path: Path) -> None:
    loaded = command.load(
        _environment(tmp_path, NODESTRO_DATABASE_URL=DSN, NODESTRO_DATABASE_MIGRATION_URL=roles.owner_url)
    )
    with pytest.raises(NodestroError) as caught:
        command.migrate(loaded, backup_dir=None, backup_taken=False)
    assert caught.value.code == "storage.role_unsafe"
    with psycopg.connect(DSN, autocommit=True) as admin:  # nothing was migrated
        found = admin.execute("SELECT to_regclass(%s)", (f"{roles.schema}.nodestro_documents",)).fetchone()
    assert found == (None,)


def test_the_db_commands(
    roles: Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environment(
        tmp_path, NODESTRO_DATABASE_URL=roles.runtime_url, NODESTRO_DATABASE_MIGRATION_URL=roles.owner_url
    )
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    command.main(["db", "grant-runtime-role"])
    assert '"restricted": true' in capsys.readouterr().out
    command.main(["db", "roles"])
    assert f'"role": "{roles.runtime}"' in capsys.readouterr().out


def test_an_owner_without_bypassrls_cannot_back_up_axentras_tables(roles: Setup, tmp_path: Path) -> None:
    """Why the RUNBOOK gives the owner role BYPASSRLS: Axentra's tables FORCE row-level security, which binds their
    owner too, and pg_dump refuses to dump a table whose rows a policy would filter. (An owner can switch RLS off
    anyway, so BYPASSRLS gives it nothing it could not take; the runtime role never has it.)"""
    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump is not installed")
    loaded = _split(roles, tmp_path)
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("ALTER ROLE {} NOBYPASSRLS").format(sql.Identifier(roles.owner)))
    assert command.database_roles(loaded)["owner_can_back_up"] is False
    with pytest.raises(NodestroError) as caught:
        command.dump(loaded, tmp_path / "backup.dump")
    assert caught.value.code == "storage.backup_failed" and "row-level security" in str(caught.value)
