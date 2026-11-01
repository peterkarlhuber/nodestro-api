"""``nodestro-server``: settings refused before anything starts, migrations, readiness, the API process."""

from __future__ import annotations

import base64
from collections.abc import Iterator
import os
from pathlib import Path
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage_postgres import POSTGRES_SCHEMA_VERSION, PostgresDatabase

from nodestro_api import server as command

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


def _environment(tmp_path: Path, **changes: str) -> dict[str, str]:
    environ = {
        "NODESTRO_PROFILE": "server",
        "NODESTRO_DATA_DIR": str(tmp_path / "server"),
        "NODESTRO_DATABASE_URL": DSN or "host=127.0.0.1 dbname=none",
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.test",
        "NODESTRO_OIDC_ISSUER": "https://login.example",
        "NODESTRO_OIDC_CLIENT_ID": "nodestro-studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
        "NODESTRO_SECRET_KEY": base64.b64encode(os.urandom(32)).decode(),
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


def test_an_incomplete_or_desktop_environment_is_refused_with_every_problem(tmp_path: Path) -> None:
    with pytest.raises(NodestroError) as error:
        command.load({"NODESTRO_PROFILE": "desktop"})
    assert error.value.code == "profile.invalid"
    fields = [item["field"] for item in error.value.params["problems"]]
    assert fields[0] == "NODESTRO_PROFILE" and "NODESTRO_DATABASE_URL" in fields and "NODESTRO_SECRET_KEY" in fields
    with pytest.raises(NodestroError) as error:
        command.load(_environment(tmp_path, NODESTRO_PUBLIC_ORIGIN="http://studio.example.com"))
    assert [item["field"] for item in error.value.params["problems"]] == ["NODESTRO_PUBLIC_ORIGIN"]
    loaded = command.load(_environment(tmp_path))
    assert loaded.profile.kind == "server" and loaded.settings.public_origin == "https://studio.test"
    assert "client-secret" not in repr(loaded)


def test_model_endpoints_and_worker_timing_come_from_the_environment(tmp_path: Path) -> None:
    loaded = command.load(
        _environment(
            tmp_path,
            NODESTRO_MODEL_ENDPOINTS="https://models.example.com, http://localhost:9000",
            NODESTRO_WORKER_LEASE_SECONDS="5",
            NODESTRO_WORKER_REAP_SECONDS="2.5",
            NODESTRO_WORKER_CLEANUP_SECONDS="300",
        )
    )
    assert loaded.endpoints == ("https://models.example.com", "http://localhost:9000")
    assert loaded.worker == {"lease": 5.0, "reap_every": 2.5, "cleanup_every": 300.0}
    with pytest.raises(NodestroError) as error:
        command.load(
            _environment(tmp_path, NODESTRO_MODEL_ENDPOINTS="models.example.com", NODESTRO_WORKER_POLL_SECONDS="0")
        )
    fields = [item["field"] for item in error.value.params["problems"]]
    assert fields == ["NODESTRO_MODEL_ENDPOINTS", "NODESTRO_WORKER_POLL_SECONDS"]


def test_worker_capacity_comes_from_the_environment_and_is_validated(tmp_path: Path) -> None:
    from nodestro_api.worker import WorkerSettings

    assert command.load(_environment(tmp_path)).capacity == {}
    assert (WorkerSettings.max_runs, WorkerSettings.max_waiting) == (2, 1)
    loaded = command.load(_environment(tmp_path, NODESTRO_WORKER_MAX_RUNS="4", NODESTRO_WORKER_MAX_WAITING="3"))
    assert loaded.capacity == {"max_runs": 4, "max_waiting": 3}
    for changes, field in (
        ({"NODESTRO_WORKER_MAX_RUNS": "two"}, "NODESTRO_WORKER_MAX_RUNS"),
        ({"NODESTRO_WORKER_MAX_RUNS": "0"}, "NODESTRO_WORKER_MAX_RUNS"),
        # every held run waiting would leave no slot to execute in
        ({"NODESTRO_WORKER_MAX_RUNS": "2", "NODESTRO_WORKER_MAX_WAITING": "2"}, "NODESTRO_WORKER_MAX_RUNS"),
        ({"NODESTRO_WORKER_MAX_WAITING": "-1"}, "NODESTRO_WORKER_MAX_RUNS"),
    ):
        with pytest.raises(NodestroError) as error:
            command.load(_environment(tmp_path, **changes))
        assert [item["field"] for item in error.value.params["problems"]] == [field], changes


def test_the_command_stops_with_one_sentence() -> None:
    environ = {key: value for key, value in os.environ.items() if not key.startswith("NODESTRO_")}
    stopped = subprocess.run(
        [sys.executable, "-c", "from nodestro_api.server import main; main(['check'])"],
        capture_output=True,
        text=True,
        env={**environ, "NODESTRO_PROFILE": "server"},
        check=False,
    )
    assert stopped.returncode == 1
    assert stopped.stderr.startswith("profile.invalid: The server cannot start:")
    assert "Traceback" not in stopped.stderr


def test_migrate_check_and_tenant_setup(schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = command.load(_environment(tmp_path))
    problems = command.check(loaded)
    assert any("schema version 0" in item for item in problems)  # not migrated yet
    assert command.migrate(loaded, backup_dir=None, backup_taken=False) == 0  # a new database needs no backup
    database = PostgresDatabase(DSN, schema=schema)
    try:
        assert database.schema_version == POSTGRES_SCHEMA_VERSION
    finally:
        database.close()
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    assert command.check(loaded) == []
    command.create_tenant(loaded, "acme", "Acme")
    link = command.setup_link(loaded, "acme")
    assert link.startswith("https://studio.test/api/auth/login?setup=")


def test_without_docker_the_server_is_not_ready(schema: str, tmp_path: Path) -> None:
    loaded = command.load(_environment(tmp_path, NODESTRO_DOCKER="/nonexistent/docker"))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    assert [item for item in command.check(loaded) if item.startswith("sandbox.unavailable: Docker is not reachable")]
    with pytest.raises(NodestroError) as error:
        command._ready(loaded)
    assert error.value.code == "sandbox.unavailable"


def test_the_api_process_serves_the_studio_and_requires_a_login(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>studio</html>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    for key, value in _environment(tmp_path, NODESTRO_STATIC_DIR=str(static)).items():
        monkeypatch.setenv(key, value)
    command.migrate(command.load(), backup_dir=None, backup_taken=False)
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    app = command.create_server_app()
    with TestClient(app, base_url="https://studio.test") as browser:
        assert browser.get("/api/health").status_code == 200
        assert browser.get("/workflows").text == "<html>studio</html>"  # the single-page app
        assert browser.get("/assets/app.js").text == "console.log(1)"
        assert browser.get("/api/workflows").status_code == 401  # no session, no data
        assert browser.get("/api/nothing-here").status_code in (401, 404)
        assert browser.get("/api/auth/me").status_code == 401
    app.state.tenants  # noqa: B018 - built


def test_a_schema_that_is_not_current_needs_a_backup(schema: str, tmp_path: Path) -> None:
    database = PostgresDatabase(DSN, schema=schema)
    try:
        from nodestro_runtime.storage_postgres import postgres_migrations

        steps = postgres_migrations()
        database.migrate(migrations={version: steps[version] for version in sorted(steps)[:1]})
    finally:
        database.close()
    loaded = command.load(_environment(tmp_path))
    with pytest.raises(NodestroError) as error:
        command.migrate(loaded, backup_dir=None, backup_taken=False)
    assert error.value.code == "storage.backup_failed" and "--backup-dir" in str(error.value)  # nothing changed
    assert command.migrate(loaded, backup_dir=None, backup_taken=True) == 1


def test_migrate_writes_a_pg_dump_backup_first(schema: str, tmp_path: Path) -> None:
    import shutil

    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump is not installed")
    database = PostgresDatabase(DSN, schema=schema)
    try:
        from nodestro_runtime.storage_postgres import postgres_migrations

        steps = postgres_migrations()
        database.migrate(migrations={version: steps[version] for version in sorted(steps)[:1]})
    finally:
        database.close()
    loaded = command.load(_environment(tmp_path))
    command.migrate(loaded, backup_dir=tmp_path / "backups", backup_taken=False)
    dumps = list((tmp_path / "backups").iterdir())
    assert len(dumps) == 1 and dumps[0].name.startswith("pre-migration-1-to-") and dumps[0].stat().st_size > 0


def test_the_backup_command_writes_a_pg_dump(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    if shutil.which("pg_dump") is None:
        pytest.skip("pg_dump is not installed")
    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    command.main(["backup", str(tmp_path / "backups")])
    written = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert written.parent == tmp_path / "backups" and written.name.startswith("nodestro-") and written.suffix == ".dump"
    assert written.read_bytes().startswith(b"PGDMP")  # pg_dump's custom format


def test_operators_manage_members_roles_and_sessions(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A-08 / D-03: members are listed, added, changed and removed, and sessions revoked, without SQL."""
    from nodestro_runtime.identity_store import IdentityStore

    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    command.main(["create-tenant", "acme", "Acme"])
    database = PostgresDatabase(DSN, schema=schema)
    try:
        identity = IdentityStore(database)
        owner = identity.login("https://login.example", "owner-sub", email="owner@example.com", name="Olga")
        identity.set_role("acme", owner.id, "owner")
        # Bob logged in once (a login alone grants nothing); his e-mail address is verified.
        bob = identity.login("https://login.example", "bob-sub", email="Bob@Example.com")
        capsys.readouterr()

        command.main(["set-role", "acme", "bob@example.com", "editor"])
        assert capsys.readouterr().out.strip() == f"{bob.id} is editor in acme"
        assert identity.role("acme", bob.id) == "editor"
        command.main(["set-role", "acme", "bob-sub", "viewer"])  # the subject at the configured provider
        assert capsys.readouterr().out.strip() == f"{bob.id} is viewer in acme (was editor)"
        command.main(["members", "acme"])
        listed = capsys.readouterr().out.strip().splitlines()
        assert listed == sorted(
            [
                f"{owner.id}\towner\towner@example.com\tOlga\towner-sub",
                f"{bob.id}\tviewer\tBob@Example.com\t-\tbob-sub",
            ]
        )

        session = identity.create_session(bob.id, "acme")
        command.main(["revoke-sessions", bob.id])
        assert capsys.readouterr().out.strip() == f"ended 1 sessions of {bob.id}"
        assert identity.session(session) is None and identity.role("acme", bob.id) == "viewer"

        session = identity.create_session(bob.id, "acme")
        command.main(["remove-member", "acme", "bob@example.com"])
        assert capsys.readouterr().out.strip() == f"removed {bob.id} from acme"
        assert identity.role("acme", bob.id) is None and identity.session(session) is None

        for argv, code in (
            (["set-role", "acme", "nobody@example.com", "viewer"], "identity.user_unknown"),
            (["set-role", "other", bob.id, "viewer"], "identity.tenant_unknown"),
            (["remove-member", "acme", bob.id], "identity.not_a_member"),
            (["remove-member", "acme", owner.id], "identity.last_owner"),
            (["set-role", "acme", owner.id, "admin"], "identity.last_owner"),
        ):
            with pytest.raises(SystemExit) as stopped:
                command.main(argv)
            assert str(stopped.value.code).startswith(f"{code}:"), (argv, stopped.value.code)
        assert identity.role("acme", owner.id) == "owner"
        with pytest.raises(SystemExit):
            command.main(["set-role", "acme", bob.id, "superuser"])  # not a role
    finally:
        database.close()


def test_a_shared_e_mail_address_needs_the_user_id(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_runtime.identity_store import IdentityStore

    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    command.main(["create-tenant", "acme", "Acme"])
    database = PostgresDatabase(DSN, schema=schema)
    try:
        identity = IdentityStore(database)
        identity.login("https://login.example", "one", email="team@example.com")
        identity.login("https://other.example", "two", email="team@example.com")
        with pytest.raises(SystemExit) as stopped:
            command.main(["set-role", "acme", "team@example.com", "viewer"])
        assert str(stopped.value.code).startswith("identity.user_ambiguous:")
        assert identity.members("acme") == []
    finally:
        database.close()
