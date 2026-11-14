"""TABRION MIG-NOD-9: recovery mode repairs the permissions of the desktop database only with the user's confirmation.

A desktop database that group or others may write is refused by the runtime; when it is ``fixable`` the API starts in
recovery mode: the regular guards, a dry run and a confirmed repair, nothing else. The target is always the data
directory's own ``nodestro.db`` and its side files; a file that changed since the dry run, a link, a hard link or a
file of another user is never touched. The diagnosis reports private-enough-to-open but readable files and restarts
into recovery mode on request; it never repairs in its own process.
"""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import stat
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime import storage
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.workspace import DataDirectoryLock
import pytest

from nodestro_api import recovery
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

pytestmark = pytest.mark.skipif(os.name != "posix", reason="file modes and owners are POSIX only")

SECRET = "s" * 43
PLAN = recovery.PLAN_PATH
REPAIR = recovery.REPAIR_PATH


@pytest.fixture(autouse=True)
def exit_request() -> Iterator[list[int]]:
    """The process' exit request, bound to a recorder instead of a uvicorn server."""
    stops: list[int] = []
    recovery.EXIT.bind(lambda: stops.append(recovery.EXIT.code or 0))
    yield stops
    recovery.EXIT.bind(None)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _data_dir(tmp_path: Path, mode: int | None = None) -> Path:
    """A data directory with a real desktop database (created by the API itself), then given ``mode``."""
    data = tmp_path / "data"
    with TestClient(
        create_app(AppConfig(data_dir=data), integrations=[], scheduler=False), base_url="http://127.0.0.1"
    ):
        pass
    if mode is not None:
        os.chmod(data / "nodestro.db", mode)
    return data


def _client(data: Path, *, secret: str | None = None, **config: Any) -> TestClient:
    app = create_app(AppConfig(data_dir=data, launch_secret=secret, **config), integrations=[], scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1")


def _repair(client: TestClient, **body: Any) -> Any:
    return client.post(REPAIR, json=body)


# ---- start: recovery mode instead of a refusal, never a repair ------------------------------------------------------


def test_a_refused_fixable_database_starts_recovery_mode_and_nothing_changes(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o664)
    content = (data / "nodestro.db").read_bytes()
    with _client(data) as client:
        assert client.app.state.recovery is True  # type: ignore[attr-defined]
        assert client.get("/api/health").json() == {"status": "ok", "mode": "recovery"}
        plan = client.get(PLAN).json()
        assert plan["origin"] == "refused"
        assert plan["repairable"] is True
        assert [(item["name"], item["status"], item["old_mode"], item["new_mode"]) for item in plan["files"]] == [
            ("nodestro.db", "not_confirmed", "0664", "0600")
        ]
        assert plan["command"] == f"chmod 600 {data / 'nodestro.db'}"
        assert len(plan["plan"]) == 64
    assert _mode(data / "nodestro.db") == 0o664  # starting and looking never change a mode
    assert (data / "nodestro.db").read_bytes() == content


def test_a_0644_database_opens_normally_and_keeps_its_mode(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o644)
    with _client(data) as client:
        assert not getattr(client.app.state, "recovery", False)
        assert client.get("/api/workflows").status_code == 200
    assert _mode(data / "nodestro.db") == 0o644


def test_every_other_route_is_closed_in_recovery_mode(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o664)
    with _client(data) as client:
        for path in (
            "/api/workflows",
            "/api/settings",
            "/api/meta",
            "/api/diagnostics",
            "/api/openapi.json",
            "/api/docs",
            "/api/error-codes",
            "/api/runs",
            "/api/published/x",
            "/api/app",
        ):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/workflows", json={"name": "x"}).status_code == 404
        assert client.post("/api/diagnostics/database-permissions/recovery", json={"confirm": True}).status_code == 404
        assert client.get(REPAIR).status_code == 405
        assert client.post(PLAN, json={"confirm": True}).status_code == 405
    routes = recovery.build_router(data / "nodestro.db", "refused").routes
    assert {getattr(route, "path", "") for route in routes} == {"/api/health", PLAN, REPAIR}


def test_a_database_that_cannot_be_repaired_still_stops_the_start(tmp_path: Path) -> None:
    target = _data_dir(tmp_path / "elsewhere", 0o664) / "nodestro.db"
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    (data / "nodestro.db").symlink_to(target)
    with pytest.raises(NodestroError) as caught:
        _client(data)
    assert caught.value.params == {"reason": "symlink", "fixable": False}
    assert _mode(target) == 0o664
    DataDirectoryLock(data).release()  # the refused start left the directory unlocked


def test_recovery_mode_is_for_the_desktop_only(tmp_path: Path) -> None:
    error = NodestroError("storage.unsafe_file", "x", reason="writable_by_others", fixable=True)
    assert recovery.recoverable(AppConfig(data_dir=tmp_path), error)
    assert not recovery.recoverable(AppConfig(data_dir=tmp_path, profile_kind="local-container"), error)
    assert not recovery.recoverable(AppConfig(data_dir=tmp_path), NodestroError("storage.unsafe_file", "x"))
    assert not recovery.requested(AppConfig(data_dir=tmp_path, recovery=True, profile_kind="local-container"))


def test_the_data_directory_stays_locked_while_recovery_mode_runs(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o664)
    with _client(data):
        with pytest.raises(NodestroError) as caught:
            DataDirectoryLock(data)
        assert caught.value.code == "workspace.in_use"
    DataDirectoryLock(data).release()


# ---- guards ---------------------------------------------------------------------------------------------------------


def test_recovery_mode_keeps_the_host_origin_and_launch_guards(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o664)
    with _client(data, secret=SECRET) as client:
        assert client.get("/api/health").status_code == 200  # the desktop shell waits for it
        for answer in (client.get(PLAN), _repair(client, confirm=True, plan="x" * 64)):
            assert answer.status_code == 401
            assert answer.json()["code"] == "access.launch_required"
        assert client.get("/api/launch", params={"token": SECRET}, follow_redirects=False).status_code == 200
        plan = client.get(PLAN).json()["plan"]
        rebound = client.get(PLAN, headers={"Host": "evil.example:8000"})
        assert rebound.status_code == 403 and rebound.json()["code"] == "request.host_not_allowed"
        for origin in ("https://evil.example", "null", "http://localhost:9999"):
            answer = client.post(REPAIR, json={"confirm": True, "plan": plan}, headers={"Origin": origin})
            assert answer.status_code == 403, origin
            assert answer.json()["code"] == "request.origin_not_allowed"
        assert client.get(PLAN).headers["x-content-type-options"] == "nosniff"
    assert _mode(data / "nodestro.db") == 0o664


# ---- the confirmed repair -------------------------------------------------------------------------------------------


def test_without_confirmation_nothing_changes(tmp_path: Path, exit_request: list[int]) -> None:
    data = _data_dir(tmp_path, 0o664)
    with _client(data) as client:
        plan = client.get(PLAN).json()["plan"]
        for body in ({}, {"plan": plan}, {"confirm": False, "plan": plan}):
            answer = client.post(REPAIR, json=body)
            assert answer.status_code == 400, body
            assert answer.json()["code"] == "recovery.not_confirmed"
        for loose in ("yes", 1, "true"):  # only a JSON true confirms
            assert client.post(REPAIR, json={"confirm": loose, "plan": plan}).status_code == 422, loose
    assert _mode(data / "nodestro.db") == 0o664
    assert exit_request == []


def test_the_confirmed_repair_narrows_the_modes_and_restarts(tmp_path: Path, exit_request: list[int]) -> None:
    data = _data_dir(tmp_path, 0o664)
    database = data / "nodestro.db"
    (data / "nodestro.db-wal").write_bytes(b"")
    os.chmod(data / "nodestro.db-wal", 0o644)
    content = database.read_bytes()
    with _client(data) as client:
        plan = client.get(PLAN).json()
        assert [(item["name"], item["old_mode"], item["new_mode"]) for item in plan["files"]] == [
            ("nodestro.db", "0664", "0600"),
            ("nodestro.db-wal", "0644", "0600"),
        ]
        answer = _repair(client, confirm=True, plan=plan["plan"])
        assert answer.status_code == 200
        result = answer.json()
        assert result["repaired"] is True and result["restart"] is True
        assert [(item["name"], item["status"], item["old_mode"], item["new_mode"]) for item in result["files"]] == [
            ("nodestro.db", "changed", "0664", "0600"),
            ("nodestro.db-wal", "changed", "0644", "0600"),
        ]
        assert exit_request == [recovery.RESTART_EXIT_CODE]
        again = _repair(client, confirm=True, plan=client.get(PLAN).json()["plan"])
        assert again.status_code == 409 and again.json()["code"] == "recovery.restarting"
    assert _mode(database) == 0o600 and _mode(data / "nodestro.db-wal") == 0o600
    assert database.read_bytes() == content  # the mode only, never the content
    with _client(data) as client:  # the regular start opens it now
        assert client.get("/api/workflows").status_code == 200


def test_the_runtime_result_is_shown_as_returned(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o444 | 0o020)  # r--rw-r-- : narrowed to 0400, nothing is added
    with _client(data) as client:
        plan = client.get(PLAN).json()
        assert [(item["old_mode"], item["new_mode"]) for item in plan["files"]] == [("0464", "0400")]
        assert _repair(client, confirm=True, plan=plan["plan"]).json()["files"][0]["new_mode"] == "0400"
    assert _mode(data / "nodestro.db") == 0o400


def test_a_file_swapped_after_the_dry_run_is_refused(tmp_path: Path, exit_request: list[int]) -> None:
    data = _data_dir(tmp_path, 0o664)
    database = data / "nodestro.db"
    with _client(data) as client:
        plan = client.get(PLAN).json()["plan"]
        replacement = data / "replacement"
        replacement.write_bytes(database.read_bytes())
        os.chmod(replacement, 0o664)
        os.replace(replacement, database)  # another file (another inode) under the same name
        answer = _repair(client, confirm=True, plan=plan)
        assert answer.status_code == 409 and answer.json()["code"] == "recovery.plan_changed"
        assert _mode(database) == 0o664
        os.chmod(database, 0o666)  # a mode changed since the dry run counts as well
        stale = client.get(PLAN).json()["plan"]
        os.chmod(database, 0o664)
        os.chmod(database, 0o660)
        assert _repair(client, confirm=True, plan=stale).json()["code"] == "recovery.plan_changed"
        assert _repair(client, confirm=True).json()["code"] == "recovery.plan_changed"
        assert _repair(client, confirm=True, plan="0" * 64).json()["code"] == "recovery.plan_changed"
    assert _mode(database) == 0o660
    assert exit_request == []


def test_a_linked_side_file_is_never_followed(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o644)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"")
    os.chmod(elsewhere, 0o644)
    (data / "nodestro.db-wal").symlink_to(elsewhere)
    with _client(data, recovery=True) as client:
        plan = client.get(PLAN).json()
        assert plan["origin"] == "requested" and plan["repairable"] is False
        assert ("nodestro.db-wal", "skipped", "symlink") in [
            (item["name"], item["status"], item["reason"]) for item in plan["files"]
        ]
        answer = _repair(client, confirm=True, plan=plan["plan"])
        assert answer.status_code == 409 and answer.json()["code"] == "recovery.not_repairable"
    assert _mode(elsewhere) == 0o644 and _mode(data / "nodestro.db") == 0o644


def test_a_hard_linked_database_is_never_touched(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o644)
    other_name = tmp_path / "other-name.db"
    os.link(data / "nodestro.db", other_name)
    with _client(data, recovery=True) as client:
        plan = client.get(PLAN).json()
        assert [(item["status"], item["reason"]) for item in plan["files"]] == [("skipped", "hard_link")]
        assert _repair(client, confirm=True, plan=plan["plan"]).json()["code"] == "recovery.not_repairable"
    assert _mode(other_name) == 0o644


def test_a_file_of_another_user_is_never_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _data_dir(tmp_path, 0o644)
    real = os.geteuid()
    monkeypatch.setattr(storage.os, "geteuid", lambda: real + 1)  # the files now belong to "someone else"
    with _client(data, recovery=True) as client:
        plan = client.get(PLAN).json()
        assert [(item["status"], item["reason"]) for item in plan["files"]] == [("skipped", "other_owner")]
        assert _repair(client, confirm=True, plan=plan["plan"]).json()["code"] == "recovery.not_repairable"
    assert _mode(data / "nodestro.db") == 0o644


def test_a_failed_repair_is_reported_with_the_manual_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_request: list[int]
) -> None:
    data = _data_dir(tmp_path, 0o664)

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(storage.os, "fchmod", refused)
    with _client(data) as client:
        plan = client.get(PLAN).json()["plan"]
        answer = _repair(client, confirm=True, plan=plan)
        assert answer.status_code == 500
        problem = answer.json()
        assert problem["code"] == "recovery.repair_failed"
        assert problem["params"]["command"] == f"chmod 600 {data / 'nodestro.db'}"
        assert problem["params"]["files"][0]["status"] == "failed"
    assert _mode(data / "nodestro.db") == 0o664
    assert exit_request == []


def test_a_swap_during_the_repair_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_request: list[int]
) -> None:
    data = _data_dir(tmp_path, 0o664)
    database = data / "nodestro.db"
    repair = storage.repair_desktop_database_modes

    def swapping(path: Any, *, confirmed: bool) -> list[storage.ModeRepair]:
        results = repair(path, confirmed=confirmed)
        if confirmed:  # another file of this user (another inode) takes the name right after the repair
            (data / "swapped").write_bytes(b"")
            os.chmod(data / "swapped", 0o600)
            os.replace(data / "swapped", database)
        return results

    with _client(data) as client:
        plan = client.get(PLAN).json()["plan"]
        monkeypatch.setattr(storage, "repair_desktop_database_modes", swapping)
        answer = _repair(client, confirm=True, plan=plan)
        assert answer.status_code == 500 and answer.json()["code"] == "recovery.repair_failed"
    assert exit_request == []


# ---- the diagnosis --------------------------------------------------------------------------------------------------


def _check(client: TestClient) -> dict[str, Any]:
    found = [item for item in client.get("/api/diagnostics").json()["checks"] if item["id"] == "database_permissions"]
    assert len(found) == 1
    return found[0]


def test_the_diagnosis_reports_a_readable_database_and_never_repairs_it(tmp_path: Path) -> None:
    data = _data_dir(tmp_path, 0o644)
    with _client(data) as client:
        check = _check(client)
        assert (check["status"], check["code"]) == ("warning", "not_private")
        assert check["params"]["files"][0]["old_mode"] == "0644"
        # The open database's -wal and -shm files have the database's mode (SQLite gives them the same).
        assert check["params"]["command"].startswith(f"chmod 600 {data / 'nodestro.db'}")
        assert check["params"]["restart"] is False and check["params"]["busy"] is False
    assert _mode(data / "nodestro.db") == 0o644
    os.chmod(data / "nodestro.db", 0o600)
    with _client(data) as client:
        assert (_check(client)["status"], _check(client)["code"]) == ("ok", "ok")


def test_the_diagnosis_restarts_into_recovery_mode_only_when_confirmed_and_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_request: list[int]
) -> None:
    data = _data_dir(tmp_path, 0o644)
    path = "/api/diagnostics/database-permissions/recovery"
    with _client(data) as client:
        answer = client.post(path, json={"confirm": False})
        assert answer.status_code == 400 and answer.json()["code"] == "recovery.not_confirmed"
        answer = client.post(path, json={"confirm": True})  # started from a terminal: no shell restarts it
        assert answer.status_code == 409 and answer.json()["code"] == "recovery.unavailable"
        monkeypatch.setattr(recovery, "shell_restarts", lambda: True)
        nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
        monkeypatch.setattr(nodestro, "background_work", lambda: ["run:r1"])
        assert _check(client)["params"]["busy"] is True
        answer = client.post(path, json={"confirm": True})
        assert answer.status_code == 409 and answer.json()["code"] == "recovery.busy"
        assert exit_request == []
        monkeypatch.setattr(nodestro, "background_work", lambda: [])
        assert _check(client)["params"]["restart"] is True
        answer = client.post(path, json={"confirm": True}, headers={"Origin": "https://evil.example"})
        assert answer.status_code == 403
        answer = client.post(path, json={"confirm": True})
        assert answer.status_code == 200 and answer.json() == {"restart": True}
        assert exit_request == [recovery.RECOVERY_EXIT_CODE]
    assert _mode(data / "nodestro.db") == 0o644  # the diagnosis never repairs in its own process
    # The shell starts the backend again with NODESTRO_RECOVERY: recovery mode, the workspace stays closed.
    with _client(data, recovery=True) as client:
        assert client.get("/api/workflows").status_code == 404
        plan = client.get(PLAN).json()
        assert plan["origin"] == "requested" and plan["repairable"] is True


def test_the_recovery_variable_is_read_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(recovery.RECOVERY_ENV, recovery.RECOVERY_VALUE)
    assert AppConfig.from_env().recovery is True
    monkeypatch.setenv(recovery.RECOVERY_ENV, "1")
    assert AppConfig.from_env().recovery is False


def test_an_exit_request_stops_the_bound_server() -> None:
    stops: list[str] = []
    request = recovery.ExitRequest()
    request.request(75)  # not bound: only logged
    request.bind(lambda: stops.append("stop"))
    assert request.code is None
    request.request(76)
    assert (request.code, stops) == (76, ["stop"])


def test_serve_ends_the_process_with_the_requested_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After the server stopped (the application's shutdown ran), the process ends with the code the desktop reads."""
    import argparse

    import uvicorn

    from nodestro_api import cli

    class Server:
        def __init__(self, config: Any) -> None:
            self.should_exit = False
            self.started = True

        async def startup(self, sockets: Any = None) -> None:
            return None

        def run(self) -> None:
            recovery.EXIT.request(recovery.RESTART_EXIT_CODE)
            assert self.should_exit  # the server stops as on Ctrl+C

    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(cli, "require_free_port", lambda args: None)
    args = argparse.Namespace(host="127.0.0.1", port=1, reload=False)
    with pytest.raises(SystemExit) as caught:
        cli.serve("nodestro_api.app:create_app", args, ready=lambda: None)
    assert caught.value.code == recovery.RESTART_EXIT_CODE
    assert recovery.EXIT.code is None  # unbound again
