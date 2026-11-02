"""``nodestro-server`` observability and rollback safety: ``axentra status``, ``/api/metrics``, the inbox, the run
usage settlement of the worker and the start checks for stored secrets (M7, req. 4.5).

Needs ``NODESTRO_TEST_POSTGRES`` and the ``axentra`` package (Python 3.11+, ``nodestro-runtime[postgres]``).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
import json
import os
from pathlib import Path
import secrets
import uuid

from fastapi.testclient import TestClient
import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")
pytest.importorskip("axentra", reason="Axentra needs Python 3.11+")

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now
from nodestro_runtime.storage_postgres import PostgresDatabase

from nodestro_api import server as command
from nodestro_api.identity import ROLE_PERMISSIONS, SESSION_COOKIE
from nodestro_api.permissions import Permission

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")
SECRET_KEY = base64.b64encode(b"s" * 32).decode()
TOKEN = "m" * 48


def _key(kid: str) -> str:
    return f"{kid}:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


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
        "NODESTRO_AXENTRA_KEYRING": _key("k1"),
        "NODESTRO_AXENTRA_ALLOW_SUPERUSER": "1",
    }
    environ.update(changes)
    return {key: value for key, value in environ.items() if value != ""}


@pytest.fixture
def schema(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    if not DSN:
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    import psycopg
    from psycopg import sql

    name = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    monkeypatch.setenv("NODESTRO_DATABASE_SCHEMA", name)
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    yield name
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def _use(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> command.ServerEnvironment:
    for key in [key for key in os.environ if key.startswith("NODESTRO_") and key != "NODESTRO_DATABASE_SCHEMA"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    return command.load()


def _database(schema: str) -> PostgresDatabase:
    return PostgresDatabase(DSN, schema=schema)


def test_the_notified_roles_are_the_roles_that_may_run() -> None:
    from nodestro_runtime.axentra_notify import NOTIFY_ROLES

    assert set(NOTIFY_ROLES) == {role for role, granted in ROLE_PERMISSIONS.items() if Permission.RUN in granted}


# ---- secrets rollback ----------------------------------------------------------------------------------------


def _seed_axs1(schema: str, ring: str) -> None:
    from nodestro_runtime.axentra_platform import AxentraConfig, keyring_secret_box
    from nodestro_runtime.server_settings import SecretBox

    box = keyring_secret_box(
        AxentraConfig(enabled=True, keyring=ring, secrets_format="axs1"), SecretBox(base64.b64decode(SECRET_KEY))
    )
    database = _database(schema)
    try:
        database.store("acme").put_raw(
            "settings",
            "app",
            {"providers": {"openai": {"api_key": box.encrypt("acme", "providers.openai.api_key", "sk-1")}}},
        )
    finally:
        database.close()


def test_an_unsafe_switch_back_is_refused_until_the_values_are_moved_back(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ring = _key("k1")
    loaded = _use(monkeypatch, _environment(tmp_path, NODESTRO_AXENTRA_KEYRING=ring, NODESTRO_SECRETS_FORMAT="axs1"))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    _seed_axs1(schema, ring)
    assert command.check(loaded) == []
    # Switching Axentra off while axs1 values exist: refused, with the way back named.
    off = _use(monkeypatch, _environment(tmp_path, NODESTRO_AXENTRA="off", NODESTRO_AXENTRA_KEYRING=""))
    (problem,) = command.check(off)
    assert problem.startswith("profile.invalid") and "secrets reencrypt" in problem
    with pytest.raises(NodestroError) as caught:
        command._ready(off)
    assert caught.value.code == "profile.invalid"
    # A keyring without the key still in use: refused as well.
    rotated_wrong = _use(monkeypatch, _environment(tmp_path, NODESTRO_AXENTRA_KEYRING=_key("k2")))
    (problem,) = command.check(rotated_wrong)
    assert "lacks key ids" in problem and "k1 (1 values)" in problem
    status = command.axentra_status(rotated_wrong)
    assert any(line.startswith("secrets: NOT READABLE") for line in status)
    assert "stored secrets: axs1:k1 1" in status
    # The way back: v1 with the keyring still configured, re-encrypt, then switch off.
    back = _use(monkeypatch, _environment(tmp_path, NODESTRO_AXENTRA_KEYRING=ring, NODESTRO_SECRETS_FORMAT="v1"))
    assert command.check(back) == []
    capsys.readouterr()
    command.main(["secrets", "reencrypt"])
    report = json.loads(capsys.readouterr().out)
    assert (report["reencrypted"], report["failed"]) == (1, [])
    assert command.check(off) == []
    database = _database(schema)
    try:
        stored = database.store("acme").get_raw("settings", "app")["providers"]["openai"]["api_key"]
    finally:
        database.close()
    assert off.settings.secret_box().decrypt("acme", "providers.openai.api_key", stored) == "sk-1"


def test_reencryption_needs_axentra(schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _environment(tmp_path, NODESTRO_AXENTRA="off", NODESTRO_AXENTRA_KEYRING=""))
    with pytest.raises(SystemExit) as stopped:
        command.main(["secrets", "reencrypt"])
    assert str(stopped.value).startswith("secrets.keyring_required")


# ---- status ------------------------------------------------------------------------------------------------


def test_axentra_status_shows_the_embedding_state(schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ring = f"{_key('k2')},{_key('k1')}"
    loaded = _use(
        monkeypatch,
        _environment(
            tmp_path,
            NODESTRO_AXENTRA_KEYRING=ring,
            NODESTRO_AXENTRA_USAGE="1",
            NODESTRO_AXENTRA_RUN_USAGE="1",
            NODESTRO_IDENTITY_BACKEND="mirror",
            NODESTRO_METRICS_TOKEN=TOKEN,
        ),
    )
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    status = command.axentra_status(loaded)
    for line in (
        "axentra: on",
        "usage mirror: on",
        "run usage mirror: on",
        "notifications (inbox): off",
        "identity backend: mirror",
        "metrics endpoint: on",
        "keyring: k2 (active), k1",
        "axentra schema: current",
        "stored secrets: none",
        "reconcile backlog: usage 0, runs 0, audit 0",
        "identity backend ready: yes",
        "pending migrations: none",
    ):
        assert line in status, (line, status)
    assert any(line.startswith("nodestro schema: ") and line.endswith("(current)") for line in status)
    assert not any(ring.split(",")[0].split(":", 1)[1] in line for line in status)  # never a key
    assert TOKEN not in "\n".join(status)


# ---- the API process: metrics and the inbox ------------------------------------------------------------------


def test_metrics_need_the_token_and_hold_no_tenant_data(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _use(monkeypatch, _environment(tmp_path, NODESTRO_AXENTRA_AUDIT="1", NODESTRO_METRICS_TOKEN=TOKEN))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    app = command.create_server_app()
    # M6: the server profile serves no published workflow API, so no API key is ever checked there.
    from nodestro_api.permissions import _routes

    paths = [path for _, path, _, _ in _routes(app)]
    assert "/api/metrics" in paths and not any(path.startswith("/api/published") for path in paths)
    with TestClient(app, base_url="https://studio.test") as browser:
        assert browser.get("/api/metrics").status_code == 401
        assert browser.get("/api/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        answer = browser.get("/api/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
        assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert 'nodestro_db_pool_size{pool="nodestro"}' in answer.text
        assert 'nodestro_axentra_switch{switch="audit_mirror"} 1' in answer.text
        assert TOKEN not in answer.text and "acme" not in answer.text
        assert "/api/metrics" not in browser.get("/api/notifications").text  # no inbox without the switch
        assert browser.get("/api/notifications").status_code in (401, 404)
    without = _use(monkeypatch, _environment(tmp_path))
    assert not without.metrics.enabled
    with TestClient(command.create_server_app(), base_url="https://studio.test") as browser:
        assert browser.get("/api/metrics", headers={"Authorization": f"Bearer {TOKEN}"}).status_code in (401, 404)


def test_failed_runs_reach_the_members_inbox_and_the_worker_settles_run_usage(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_api.server_app import TenantFiles
    from nodestro_api.worker import ServerWorker

    loaded = _use(
        monkeypatch,
        _environment(
            tmp_path,
            NODESTRO_AXENTRA_USAGE="1",
            NODESTRO_AXENTRA_RUN_USAGE="1",
            NODESTRO_AXENTRA_NOTIFY="1",
            NODESTRO_IDENTITY_BACKEND="mirror",
        ),
    )
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    database = command._ready(loaded)
    services, identity = command._platform(loaded, database)
    services, notifications, _ = command._observed(loaded, database, services, identity)
    try:
        identity.create_tenant("acme", "Acme")
        owner = identity.login("https://login.example", "sub-owner", name="Owner")
        viewer = identity.login("https://login.example", "sub-viewer", name="Viewer")
        identity.set_role("acme", owner.id, "owner")
        identity.set_role("acme", viewer.id, "viewer")
        owner_token = identity.create_session(owner.id, "acme")
        viewer_token = identity.create_session(viewer.id, "acme")
        database.store("acme").put_raw(
            "runs",
            "run_bad",
            {
                "id": "run_bad",
                "workflow_id": "wf",
                "workflow_name": "Invoices",
                "status": "failed",
                "error_code": "run.interrupted",
                "finished_at": now().isoformat(),
                "usage": {
                    "requests": 2,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "estimated_cost_usd": 0.01,
                    "cost_complete": True,
                },
            },
        )
        worker = ServerWorker(TenantFiles(database=database, data_root=tmp_path / "data"), identity)
        worker.settlement = database.platform_events
        worker.notifications = notifications
        first = asyncio.run(worker.platform_duties())
        assert first["run_usage"]["written"] == 1 and first["notifications"]["sent"] == 1
        second = asyncio.run(worker.platform_duties())  # a second pass (or a restarted worker): nothing twice
        assert second["run_usage"]["written"] == 0 and second["notifications"]["sent"] == 0
        rows = database._query(
            "SELECT count(*), sum(quantity) FROM axentra_usage_events WHERE idempotency_key LIKE %s",
            ("nodestro:run:run_bad:nodestro.ai.requests",),
        )
        assert rows[0][0] == 1 and int(rows[0][1]) == 2
    finally:
        database.platform_events = None
        if services is not None:
            services.close()
        database.close()

    app = command.create_server_app()
    with TestClient(app, base_url="https://studio.test") as browser:
        assert browser.get("/api/notifications").status_code == 401
        browser.cookies.set(SESSION_COOKIE, owner_token, domain="studio.test")
        page = browser.get("/api/notifications").json()
        assert page["unread"] == 1 and [item["kind"] for item in page["items"]] == ["run_failed"]
        assert page["items"][0]["subject"] == "A run of Invoices failed"
        # SR-06: a malformed cursor is a 400 with a Nodestro error code (it was an unhandled 500).
        for cursor in _BAD_CURSORS:
            refused = browser.get("/api/notifications", params={"cursor": cursor})
            assert refused.status_code == 400, cursor
            assert refused.json()["code"] == "notifications.invalid_cursor" and cursor not in refused.text
        for good in _GOOD_CURSORS:  # every shape that passes the check is one PostgreSQL reads (SR-06a)
            assert browser.get("/api/notifications", params={"cursor": good}).status_code == 200, good
        if page.get("next_cursor"):
            assert browser.get("/api/notifications", params={"cursor": page["next_cursor"]}).status_code == 200
        marked = browser.post("/api/notifications/read", json={}, headers={"Origin": "https://studio.test"})
        assert marked.status_code == 200 and marked.json() == {"marked": 1}
        assert browser.get("/api/notifications").json()["unread"] == 0
        browser.cookies.set(SESSION_COOKIE, viewer_token, domain="studio.test")
        assert browser.get("/api/notifications").json() == {"items": [], "unread": 0, "next_cursor": None}


def _cursor(value: object) -> str:
    import base64
    import json

    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


_BAD_CURSORS = [
    "not-base64!!",
    _cursor({"a": 1}),
    _cursor(["2026-11-02T10:00:00+00:00"]),
    _cursor(["yesterday", "msg_x"]),
    _cursor(["2026-11-02T10:00:00", "msg_x"]),  # no time zone
    _cursor([1, 2]),
    _cursor(["2026-11-02T10:00:00+00:00", ""]),
    "A" * 600,
    # SR-06a: values Python's fromisoformat reads but PostgreSQL rejects (each was a 500), and lone surrogates.
    _cursor(["2026-11-02T10:00:00+00:00", "msg\x00x"]),  # a NUL byte in the id
    _cursor(["2026-11-02T10:00:00\x00+00:00", "msg_x"]),  # a NUL byte in the time
    _cursor(["2026-W45-1T10:00:00+00:00", "msg_x"]),  # an ISO week date
    _cursor(["2026-11-02T10:00:00+01:00:00.123456", "msg_x"]),  # an offset with microseconds
    _cursor(["2026-11-02T10:00:00,5+00:00", "msg_x"]),  # a comma fraction
    _cursor(["2026-11-02T10:00:00+00:00", "msg_\ud800"]),  # a lone surrogate in the id
    _cursor(["2026-11-02T10:00:00\ud800+00:00", "msg_x"]),  # a lone surrogate in the time
    _cursor(["\u0662\u0660\u0662\u0666-11-02T10:00:00+00:00", "msg_x"]),  # non-ASCII digits
    _cursor(["2026-11-02T10:00:00.1234567+00:00", "msg_x"]),  # more than microseconds
    _cursor(["2026-11-31T10:00:00+00:00", "msg_x"]),  # no such day
    _cursor(["2026-11-02 10:00:00.123456+00:00", "msg_x"]),  # a space instead of T (not Axentra's form)
    _cursor(["2026-11-02T10:00Z", "msg_x"]),  # no seconds
    _cursor(["2026-11-02T10:00:00.5-05", "msg_x"]),  # an hour-only offset
    _cursor(["2026-11-02T10:00:00+05:30:15", "msg_x"]),  # an offset with seconds
]

#: Cursors in the shapes Axentra writes (``datetime.isoformat()`` of an aware value) and accepts back.
_GOOD_CURSORS = [
    _cursor(["2026-11-02T10:00:00+00:00", "msg_x"]),
    _cursor(["2026-11-02T10:00:00.123456+00:00", "msg_01JBX4Z6Q5ZC1RSV3E7D1W2K3M"]),
    _cursor(["2026-11-02T10:00:00Z", "msg_x"]),
    _cursor(["2026-11-02T10:00:00.5-05:00", "msg_x"]),
]


def test_a_notification_cursor_is_checked_before_axentra_reads_it() -> None:
    from nodestro_runtime.problems import NodestroError

    from nodestro_api.api_notifications import check_cursor

    check_cursor(None)
    check_cursor("")
    check_cursor(_cursor(["2026-11-02T10:00:00.123456+00:00", "msg_1"]))
    for cursor in _GOOD_CURSORS:
        check_cursor(cursor)
    for cursor in [*_BAD_CURSORS, "\x00", "\ud800", "eyJ\ud800"]:  # raw NUL bytes and lone surrogates too
        with pytest.raises(NodestroError) as caught:
            check_cursor(cursor)
        assert caught.value.code == "notifications.invalid_cursor"
        assert str(caught.value) == "The cursor is not valid; start again without it"  # the input is never echoed
        assert caught.value.params == {}
