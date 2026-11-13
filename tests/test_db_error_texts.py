"""Value-free database error texts of the server (TABRION MIG-NOD-3, finding B-06-2; the api part).

Canary tests: a value that must never reach an error text or a log line is planted where a real failure would put
it (pg_dump's error output, Axentra's storage path, a PostgreSQL error's ``DETAIL`` chained under a Nodestro error),
and the test checks it is gone while the useful facts (code, exit code, reason, SQLSTATE) stay. A control run without
the redaction shows the canary would otherwise appear.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import Any

from nodestro_runtime.problems import NodestroError
import pytest

from nodestro_api import server as command

CANARY = "canary-nod3api-7f3c"


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "NODESTRO_PROFILE": "server",
        "NODESTRO_DATA_DIR": str(tmp_path / "server"),
        "NODESTRO_DATABASE_URL": "host=127.0.0.1 dbname=none",
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.test",
        "NODESTRO_OIDC_ISSUER": "https://login.example",
        "NODESTRO_OIDC_CLIENT_ID": "nodestro-studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
        "NODESTRO_SECRET_KEY": base64.b64encode(os.urandom(32)).decode(),
    }


@pytest.mark.parametrize(
    ("stderr", "facts"),
    [
        (
            f'pg_dump: error: query failed: ERROR:  invalid input syntax for type integer: "{CANARY}"\n'
            f"DETAIL:  Key (email)=({CANARY}@example.invalid) already exists.\n",
            ["exit code 1"],
        ),
        (
            f'pg_dump: error: connection to server at "{CANARY}.db.internal" (10.0.0.9), port 5432 failed: FATAL:  '
            f'password authentication failed for user "{CANARY}"\n',
            ["exit code 1", "authentication failed"],
        ),
        (
            f'pg_dump: error: could not translate host name "{CANARY}.db.internal" to address: Name or service not '
            "known\n",
            ["exit code 1", "the database server is not reachable"],
        ),
        (
            f"pg_dump: error: query failed: ERROR:  42501: permission denied for table {CANARY}\n",
            ["exit code 1", "permission denied", "SQLSTATE 42501"],
        ),
        (
            f'pg_dump: error: could not open output file "/srv/{CANARY}/nodestro.dump": Permission denied\n',
            ["exit code 1", "the dump file cannot be written"],
        ),
    ],
)
def test_a_failed_pg_dump_names_no_value_of_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str, facts: list[str]
) -> None:
    """Item (1): ``storage.backup_failed`` after pg_dump keeps the exit code, a fixed reason and a SQLSTATE, never
    pg_dump's own text (server messages with values, host names, roles, paths)."""
    loaded = command.load(_environment(tmp_path))
    monkeypatch.setattr(command.shutil, "which", lambda name: f"/usr/bin/{name}")
    failed = subprocess.CompletedProcess(["pg_dump"], 1, stdout="", stderr=stderr)
    monkeypatch.setattr(command.subprocess, "run", lambda *args, **kwargs: failed)
    with pytest.raises(NodestroError) as caught:
        command.dump(loaded, tmp_path / "backups" / "nodestro.dump")
    text = str(caught.value)
    assert caught.value.code == "storage.backup_failed"
    assert CANARY not in text and CANARY not in repr(caught.value.params)
    assert "DETAIL" not in text and "ERROR:" not in text and "pg_dump:" not in text
    assert text.startswith("pg_dump failed (")
    for fact in facts:
        assert fact in text
    assert not (tmp_path / "backups" / "nodestro.dump").exists()


def test_pg_dump_failure_text_takes_only_a_sqlstate_shaped_token() -> None:
    """A word after ``ERROR:`` is no SQLSTATE unless it is five upper-case letters or digits with a digit."""
    assert "SQLSTATE" not in command._pg_dump_failure(1, "pg_dump: error: ERROR:  CANARY: x")
    assert "SQLSTATE" not in command._pg_dump_failure(1, "ERROR:  ABCDEF1: x")
    assert command._pg_dump_failure(2, "") == (
        "pg_dump failed (exit code 2); run pg_dump by hand with the same connection to see its output"
    )


def test_a_missing_axentra_storage_directory_is_reported_without_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Item (2): the error text leaves the storage path out; the operator's log names it."""
    lost = tmp_path / CANARY / "objects"
    storage = SimpleNamespace(backend="local", local_path=str(lost))
    services = SimpleNamespace(settings=SimpleNamespace(storage=storage), close=lambda: None)
    monkeypatch.setattr(command, "_axentra", lambda server: services)
    monkeypatch.setattr(command, "_stored_files", lambda server: 1)
    server: Any = SimpleNamespace(settings=SimpleNamespace(axentra=SimpleNamespace(files_backend="axentra")))
    with caplog.at_level(logging.ERROR, logger=command.logger.name), pytest.raises(NodestroError) as caught:
        command.archive_files(server, tmp_path / "backups")
    assert caught.value.code == "storage.backup_failed"
    assert "does not exist" in str(caught.value)
    assert CANARY not in str(caught.value) and str(tmp_path) not in str(caught.value)
    assert str(lost) in caplog.text  # the operator's log
    assert not (tmp_path / "backups").exists()


#: Logs a PostgreSQL error with a value in its DETAIL, chained under a Nodestro error, after uvicorn's own
#: dictConfig, through uvicorn's logger and through a Nodestro logger (root handler of configure_logging).
_LOG_SCRIPT = textwrap.dedent(
    """
    import logging, logging.config, os, sys
    import psycopg.errors
    from uvicorn.config import LOGGING_CONFIG
    from nodestro_runtime.problems import NodestroError
    from nodestro_api import server

    if sys.argv[1] in ("main", "json"):
        if sys.argv[1] == "json":
            os.environ["NODESTRO_LOG_FORMAT"] = "json"
        try:
            server.main(["worker-health"])  # installs the redaction and the server's logging, then exits (2)
        except SystemExit:
            pass
    elif sys.argv[1] == "factory":
        from nodestro_api.db_log_redaction import install_db_error_redaction
        from nodestro_api.observability import configure_logging

        install_db_error_redaction()  # what create_server_app does first
        configure_logging()
    else:  # the control: the server's logging without the redaction
        from nodestro_api.observability import configure_logging

        configure_logging()
    logging.config.dictConfig(LOGGING_CONFIG)  # uvicorn configures its loggers after the redaction was installed

    def fail():
        try:
            raise psycopg.errors.UniqueViolation(
                'duplicate key value violates unique constraint "users_email_key"\\n'
                "DETAIL:  Key (email)=(CANARY@example.invalid) already exists."
            )
        except psycopg.Error as exc:
            raise NodestroError("storage.migration_failed", "The migration failed") from exc

    for name in ("uvicorn.error", "nodestro_api.server"):
        try:
            fail()
        except NodestroError:
            logging.getLogger(name).exception("request failed (%s)", name)
    print("done", file=sys.stderr)
    """
).replace("CANARY", CANARY)


def _run_log_script(mode: str, tmp_path: Path) -> str:
    script = tmp_path / "log_script.py"
    script.write_text(_LOG_SCRIPT, encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NODESTRO_")}
    completed = subprocess.run(
        [sys.executable, "-I", str(script), mode],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=tmp_path,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr.rstrip().endswith("done"), completed.stderr
    return completed.stderr


@pytest.mark.parametrize("mode", ["main", "json", "factory"])
def test_server_logs_redact_chained_database_errors(mode: str, tmp_path: Path) -> None:
    """Item (3): ``nodestro-server`` (``main``) and the app factory install Tabrion's DbErrorRedactionFilter early;
    it survives uvicorn's dictConfig and covers uvicorn's loggers and the root handler: the traceback of a Nodestro
    error caused by a PostgreSQL error keeps the kind and the SQLSTATE, never the DETAIL value."""
    output = _run_log_script(mode, tmp_path)
    assert "request failed (uvicorn.error)" in output and "request failed (nodestro_api.server)" in output
    assert "storage.migration_failed" in output or "The migration failed" in output
    assert CANARY not in output
    assert "unique violation (SQLSTATE 23505)" in output
    if mode == "json":  # the root handler's JSON line carries the redacted traceback under "exception"
        line = next(item for item in output.splitlines() if item.startswith("{"))
        record = json.loads(line)
        assert record["logger"] == "nodestro_api.server"
        assert "unique violation (SQLSTATE 23505)" in record["exception"]


def test_server_logs_control_without_redaction_shows_the_canary(tmp_path: Path) -> None:
    """The control of the test above: without the redaction the same records carry the DETAIL value."""
    assert CANARY in _run_log_script("control", tmp_path)


def test_installing_the_redaction_twice_wraps_the_record_factory_once() -> None:
    from nodestro_api.db_log_redaction import DbErrorRedactionFilter, install_db_error_redaction

    before = logging.getLogRecordFactory()
    handler = logging.NullHandler()
    resort = list(logging.lastResort.filters) if logging.lastResort is not None else []
    try:
        install_db_error_redaction(handler)
        installed = logging.getLogRecordFactory()
        install_db_error_redaction(handler)
        assert logging.getLogRecordFactory() is installed and installed is not before
        assert sum(isinstance(item, DbErrorRedactionFilter) for item in handler.filters) == 1
    finally:
        logging.setLogRecordFactory(before)
        if logging.lastResort is not None:
            logging.lastResort.filters[:] = resort
