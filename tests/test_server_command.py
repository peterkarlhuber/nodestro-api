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


def test_the_command_prints_one_problem_per_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """API-053: ``nodestro-server check`` with several profile problems prints each on its own line."""
    for name in list(os.environ):
        if name.startswith("NODESTRO_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NODESTRO_PROFILE", "server")
    with pytest.raises(SystemExit) as stopped:
        command.main(["check"])
    lines = str(stopped.value.code).splitlines()
    assert lines[0].startswith("profile.invalid: The server cannot start: ") and len(lines) >= 3
    assert all(line.startswith("- ") for line in lines[1:])
    assert any("NODESTRO_DATABASE_URL" in line for line in lines[1:])
    assert command._error_lines(NodestroError("storage.x", "Broken")) == "storage.x: Broken"


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

        command.main(["set-role", "acme", bob.id, "editor"])
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
        command.main(["remove-member", "acme", bob.id])
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

        # A tenant is suspended and resumed without SQL; its sessions are refused meanwhile.
        session = identity.create_session(owner.id, "acme")
        command.main(["suspend-tenant", "acme"])
        assert capsys.readouterr().out.strip() == "tenant acme is suspended"
        assert not identity.tenant_active("acme")
        with pytest.raises(NodestroError):
            identity.session(session)
        command.main(["resume-tenant", "acme"])
        assert capsys.readouterr().out.strip() == "tenant acme is active"
        assert identity.session(session) is not None
        with pytest.raises(SystemExit) as stopped:
            command.main(["suspend-tenant", "other"])
        assert str(stopped.value.code).startswith("identity.tenant_unknown:")
    finally:
        database.close()


def test_an_e_mail_address_alone_never_changes_a_users_access(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SR-10: an e-mail address is the latest claim of an identity provider. Someone whose provider lets them set
    any address could otherwise be given the role meant for the address's owner."""
    from nodestro_runtime.identity_store import IdentityStore

    for key, value in _environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    command.main(["migrate"])
    command.main(["create-tenant", "acme", "Acme"])
    database = PostgresDatabase(DSN, schema=schema)
    try:
        identity = IdentityStore(database)
        owner = identity.login("https://login.example", "owner-sub", email="owner@example.com")
        identity.set_role("acme", owner.id, "owner")
        # The person meant never logged in; someone else claims the address at the provider.
        claimer = identity.login("https://login.example", "claimer-sub", email="ceo@example.com", name="Mallory")
        session = identity.create_session(owner.id, "acme")
        for argv in (
            ["set-role", "acme", "ceo@example.com", "owner"],
            ["remove-member", "acme", "owner@example.com"],
            ["revoke-sessions", "owner@example.com"],
        ):
            with pytest.raises(SystemExit) as stopped:
                command.main(argv)  # no terminal: never confirmed
            message = str(stopped.value.code)
            assert message.startswith("identity.user_confirm:"), argv
            assert "subject" in message and "https://login.example" in message
        assert f"{owner.id}: provider https://login.example, subject owner-sub" in str(stopped.value.code)
        assert identity.role("acme", claimer.id) is None and identity.role("acme", owner.id) == "owner"
        assert identity.session(session) is not None

        # On a terminal the operator sees the identity and types the user id; anything else changes nothing.
        shown: list[str] = []
        monkeypatch.setattr(command.sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr(command.sys.stderr, "isatty", lambda: True, raising=False)
        for answer in ("yes", owner.id, ""):
            monkeypatch.setattr("builtins.input", lambda prompt, answer=answer: shown.append(prompt) or answer)
            with pytest.raises(SystemExit) as stopped:
                command.main(["set-role", "acme", "ceo@example.com", "viewer"])
            assert str(stopped.value.code).startswith("identity.user_confirm:")
        assert identity.role("acme", claimer.id) is None
        assert "Mallory" in capsys.readouterr().err and len(shown) == 3
        monkeypatch.setattr("builtins.input", lambda prompt: claimer.id)
        command.main(["set-role", "acme", "ceo@example.com", "viewer"])
        assert capsys.readouterr().out.strip() == f"{claimer.id} is viewer in acme"
        assert identity.role("acme", claimer.id) == "viewer"
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
        message = str(stopped.value.code)
        assert message.startswith("identity.user_ambiguous:")
        assert "https://login.example, subject one" in message and "https://other.example, subject two" in message
        assert identity.members("acme") == []
    finally:
        database.close()


def test_a_backup_as_a_role_that_row_level_security_limits_names_the_owner_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pg_dump as the runtime role fails on Axentra's forced row-level security: the error says what to set."""
    import subprocess as process

    loaded = command.load(_environment(tmp_path))
    stderr = (
        "pg_dump: error: query failed: ERROR:  query would be affected by row-level security policy for table "
        '"axentra_files"\nHINT:  To disable the policy for the table\'s owner, use ALTER TABLE NO FORCE ROW LEVEL '
        "SECURITY.\n"
    )
    monkeypatch.setattr(command.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        command.subprocess, "run", lambda *args, **kwargs: process.CompletedProcess(args, 1, stdout="", stderr=stderr)
    )
    with pytest.raises(NodestroError) as caught:
        command.dump(loaded, tmp_path / "backups" / "nodestro.dump")
    assert caught.value.code == "storage.backup_failed"
    assert "NODESTRO_DATABASE_MIGRATION_URL (the owner role)" in str(caught.value)
    assert not (tmp_path / "backups" / "nodestro.dump").exists()


def test_check_warns_about_a_superuser_runtime_role(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DX-31: in Nodestro's own mode (one role) a superuser or BYPASSRLS runtime role is reported as a warning (api and
    worker accept it); with Axentra or a separate migration role it is refused (tests/test_server_roles.py)."""
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    loaded = command.load(_environment(tmp_path))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    warnings: list[str] = []
    assert command.check(loaded, warnings=warnings) == []
    assert len(warnings) == 1 and warnings[0].startswith("storage.role_unsafe") and "is a superuser" in warnings[0]


def test_check_warns_about_bedrock_roles_for_unknown_tenants(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-008: a typo in NODESTRO_BEDROCK_TENANT_ROLES is a warning that names the tenant id, never the role."""
    import json

    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    monkeypatch.setattr(command, "_superuser_role", lambda _server, _database: None)
    role = {"role_arn": "arn:aws:iam::123456789012:role/nodestro-alpha", "external_id": "external-alpha-0001"}
    roles = json.dumps({"alpha": role, "aplha": role})
    loaded = command.load(_environment(tmp_path, NODESTRO_BEDROCK_TENANT_ROLES=roles))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    command.create_tenant(loaded, "alpha", "Alpha")
    warnings: list[str] = []
    assert command.check(loaded, warnings=warnings) == []
    assert len(warnings) == 1 and warnings[0].startswith("bedrock.tenant_unknown"), warnings
    assert "aplha" in warnings[0] and "alpha," not in warnings[0]
    assert "arn:aws" not in warnings[0] and "external-alpha" not in warnings[0]
    command.create_tenant(loaded, "aplha", "Typo")
    warnings.clear()
    assert command.check(loaded, warnings=warnings) == [] and warnings == []


@pytest.mark.real_identity_check
def test_check_reads_the_identity_provider(tmp_path: Path) -> None:
    """DX-31: check verifies the issuer's discovery document (reachable, same issuer, https endpoints)."""
    import httpx
    import stub_oidc

    loaded = command.load(_environment(tmp_path, NODESTRO_OIDC_ISSUER=stub_oidc.ISSUER))
    provider = stub_oidc.StubProvider()
    assert command.check_identity_provider(loaded, httpx.Client(transport=provider.transport())) is None
    other = stub_oidc.StubProvider(issuer="https://other.example")

    def wrong_issuer(request: httpx.Request) -> httpx.Response:
        answer = other.transport().handle_request(
            httpx.Request("GET", "https://other.example/.well-known/openid-configuration")
        )
        answer.read()
        return httpx.Response(200, json=answer.json())

    found = command.check_identity_provider(loaded, httpx.Client(transport=httpx.MockTransport(wrong_issuer)))
    assert found is not None and found.startswith("identity.provider:") and "other.example" in found

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    found = command.check_identity_provider(loaded, httpx.Client(transport=httpx.MockTransport(down)))
    assert found is not None and found.startswith("identity.provider:") and stub_oidc.ISSUER in found


@pytest.mark.real_identity_check
def test_check_can_skip_the_identity_provider(schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker's health check runs ``check --skip-identity-provider``: no request reaches the provider."""
    import httpx

    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    loaded = command.load(_environment(tmp_path))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    assert command.check(loaded, http=client, identity_provider=False) == []
    assert calls == []
    found = command.check(loaded, http=client)
    assert len(found) == 1 and found[0].startswith("identity.provider:") and len(calls) == 1
    assert command.parser().parse_args(["check", "--skip-identity-provider"]).skip_identity_provider


def test_conversations_from_before_owners_get_the_owner_their_files_name(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-057: ``assistant backfill-owners`` sets the owner where every file of a conversation was uploaded by one
    person; without files or with files of several people a conversation stays visible to the tenant."""
    from nodestro_runtime.input_files import COLLECTION, InputFile
    from nodestro_runtime.schemas import now

    from nodestro_api.assistant import Conversation
    from nodestro_api.assistant_files import ConversationFile

    loaded = command.load(_environment(tmp_path))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    command.create_tenant(loaded, "alpha", "Alpha")
    database = command._database(loaded)
    store = database.store("alpha")

    def chat(conversation_id: str, uploaders: list[str], owner: str | None = None) -> None:
        files = []
        for index, who in enumerate(uploaders):
            file_id = f"in_{conversation_id[-4:]}{index:08d}"
            info = {"name": f"f{index}.txt", "format": "txt", "group": "text", "media_type": "text/plain", "size": 1}
            store.put(
                COLLECTION,
                file_id,
                InputFile(
                    id=file_id, blob="b", sha256="0" * 64, created_at=now(), origin="assistant", uploaded_by=who, **info
                ),
            )
            files.append(ConversationFile(id=file_id, **info))
        store.put(
            "assistant_conversations", conversation_id, Conversation(id=conversation_id, files=files, owner=owner)
        )

    chat("chat_one1", ["alice", "alice"])
    chat("chat_two2", ["alice", "bob"])
    chat("chat_none", [])
    chat("chat_mine", ["carol"], owner="dave")
    database.close()
    dry = command.backfill_conversation_owners(loaded, dry_run=True, tenant=None)
    assert dry["tenants"]["alpha"] == {"without_owner": 3, "assigned": 1, "ambiguous": 1, "unknown": 1}
    report = command.backfill_conversation_owners(loaded, dry_run=False, tenant="alpha")
    assert report["tenants"]["alpha"]["assigned"] == 1
    database = command._database(loaded)
    try:
        owners = {item.id: item.owner for item in database.store("alpha").list("assistant_conversations", Conversation)}
    finally:
        database.close()
    assert owners == {"chat_one1": "alice", "chat_two2": None, "chat_none": None, "chat_mine": "dave"}
    again = command.backfill_conversation_owners(loaded, dry_run=False, tenant=None)
    assert again["tenants"]["alpha"] == {"without_owner": 2, "assigned": 0, "ambiguous": 1, "unknown": 1}
    with pytest.raises(NodestroError) as unknown:
        command.backfill_conversation_owners(loaded, dry_run=True, tenant="nobody")
    assert unknown.value.code == "identity.tenant_unknown"
    parsed = command.parser().parse_args(["assistant", "backfill-owners", "--dry-run", "--tenant", "alpha"])
    assert (parsed.assistant_command, parsed.dry_run, parsed.tenant) == ("backfill-owners", True, "alpha")


def test_operator_commands_are_in_the_tenants_access_audit(schema: str, tmp_path: Path) -> None:
    """API-004: create-tenant, set-role, suspend/resume, remove-member and revoke-sessions are recorded in the
    tenant's own trail with the actor ``operator``."""
    from nodestro_runtime.identity_store import IdentityStore

    from nodestro_api import access_audit

    loaded = command.load(_environment(tmp_path))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    command.create_tenant(loaded, "acme", "Acme")
    database = PostgresDatabase(DSN, schema=schema)
    try:
        user = IdentityStore(database).login("https://login.example", "alice", email="alice@example.com")
        command.set_role(loaded, "acme", user.id, "editor")
        command.set_role(loaded, "acme", user.id, "admin")
        command.set_tenant_status(loaded, "acme", "suspended")
        command.set_tenant_status(loaded, "acme", "active")
        command.revoke_sessions(loaded, user.id)
        command.remove_member(loaded, "acme", user.id)
        found = access_audit.events(database.store("acme"))
    finally:
        database.close()
    assert [event.kind for event in reversed(found)] == [
        "tenant_created",
        "role_changed",
        "role_changed",
        "tenant_status_changed",
        "tenant_status_changed",
        "sessions_revoked",
        "member_removed",
    ]
    assert {event.actor for event in found} == {"operator"}
    changed = next(event for event in found if event.kind == "role_changed")
    assert (changed.target_id, changed.details) == (user.id, {"before": "editor", "role": "admin"})
