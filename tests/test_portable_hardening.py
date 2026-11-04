"""Hardening of the exported application, its web interface and the portable launcher (backend audit round 3:
API3-01 to API3-05): the API reference without a CDN, request body limits before the key check, the launcher's
network rule for ``start --host``, the one-time sign-in link kept out of the log file and the exporting computer's
folder kept out of the bundled Python."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import re
from typing import Any

from fastapi.testclient import TestClient
import pytest
from test_portable import _key_line, _ui_app, published, ui_project  # noqa: F401  (fixtures)
from test_published_api import new_key

from nodestro_api import portable, portable_build


def test_the_exported_api_reference_loads_nothing_from_elsewhere(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    studio, publication, project = request.getfixturevalue("ui_project")
    assert (project / "nodestro_api" / "swagger_ui" / "swagger-ui-bundle.js").is_file()
    token = new_key(studio, publication["id"], ["run", "read"])
    with _ui_app(project, tmp_path / "data", None, _key_line(token)) as (client, _ui):
        page = client.get("/docs")
        assert page.status_code == 200
        assert not re.search(r"(src|href)=\"(https?:)?//", page.text), page.text
        assert "<script>" not in page.text  # no inline script
        policy = page.headers["content-security-policy"]
        assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy
        for name in ("swagger-ui-bundle.js", "swagger-ui.css", "nodestro-init.js", "favicon-32x32.png"):
            assert client.get(f"/docs/assets/{name}").status_code == 200, name
        for name in ("LICENSE", "VENDORED.md", "..%2Fstandalone.py", "nope.js"):
            assert client.get(f"/docs/assets/{name}").status_code == 404, name
        assert client.get("/openapi.json").status_code == 200


def test_bodies_are_limited_before_the_key_is_checked(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    studio, publication, project = request.getfixturevalue("ui_project")
    token = new_key(studio, publication["id"], ["run", "read"])
    with _ui_app(project, tmp_path / "data", None, _key_line(token)) as (client, ui):
        headers = {"X-Nodestro-UI": "1", "Content-Type": "application/json"}
        # The sign-in reads at most 4 KiB (no session, no key yet).
        big = b'{"api_key": "' + b"A" * 8192 + b'"}'
        answer = client.post("/ui/api/session", content=big, headers=headers)
        assert answer.status_code == 413 and answer.json()["error"]["code"] == "request.too_large"
        # A body that FastAPI parses before the key check (approve, resume): the studio's JSON limit, in the
        # published APIs' error format.
        approve = f"/api/published/{publication['slug']}/runs/run_x/approve"
        answer = client.post(approve, content=b"[" + b"1," * (3 * 1024 * 1024) + b"1]", headers=headers)
        assert answer.status_code == 413 and answer.json()["error"]["code"] == "request.too_large"
        # The provider setup of the operator reads at most 16 KiB.
        ui.sessions.add_launch_token("launch-1")
        client.get("/ui/launch?token=launch-1", follow_redirects=False)
        setup = "/ui/api/setup/providers/anthropic"
        headers["Origin"] = "http://127.0.0.1:8800"
        answer = client.put(setup, content=b'{"api_key": "' + b"A" * 20000 + b'"}', headers=headers)
        assert answer.status_code == 413
        # Ordinary sizes still work.
        assert client.put(setup, json={"api_key": "sk-ant-x"}, headers=headers).status_code == 200


def test_the_network_listener_limits_small_published_bodies() -> None:

    from nodestro_api.network import network_app

    class Core:
        def __getattr__(self, name: str) -> Any:  # the probe never reaches the core
            raise AssertionError(name)

    with TestClient(network_app(Core()), raise_server_exceptions=False) as client:  # type: ignore[arg-type]
        answer = client.post(
            "/api/published/x/runs/run_x/resume",
            content=b"[" + b"1," * (3 * 1024 * 1024) + b"1]",
            headers={"Content-Type": "application/json"},
        )
        assert answer.status_code == 413
        assert answer.json()["error"]["code"] == "request.too_large"


def test_start_host_argument_obeys_the_network_rule(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = portable.Settings(tmp_path, {"data": {"data_dir": str(tmp_path / "data"), "log_dir": str(tmp_path)}})
    served: list[str] = []
    monkeypatch.setattr(portable, "_serve", lambda args, settings, console, host, *rest: served.append(host) or 0)
    monkeypatch.setattr(portable, "_choose_port", lambda host, port: port)
    for key in ("API_KEYS", "NODESTRO_HOST"):
        monkeypatch.delenv(key, raising=False)
    for key, value in settings.environment().items():  # start() sets them; restored after the test
        monkeypatch.setenv(key, value)
    # The network without TLS is refused, whether it comes from the settings file or from --host.
    assert portable.start(argparse.Namespace(no_browser=True, host="0.0.0.0", port=None), settings) == 2
    assert served == []
    # This computer only is fine.
    assert portable.start(argparse.Namespace(no_browser=True, host="127.0.0.1", port=None), settings) == 0
    assert served == ["127.0.0.1"]


def test_the_one_time_sign_in_link_is_not_logged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "application.log"
    handler = logging.FileHandler(log)
    root = logging.getLogger()
    root.addHandler(handler)
    level = root.level
    root.setLevel(logging.INFO)
    try:
        portable.Console("en").say("launch_link", log=False, url="http://127.0.0.1:8800/ui/launch?token=SECRET")
        portable.Console("en").say("ready", url="http://127.0.0.1:8800", logs=str(tmp_path))
    finally:
        handler.close()
        root.removeHandler(handler)
        root.setLevel(level)
    assert "SECRET" in capsys.readouterr().out  # the person at the computer still sees it
    text = log.read_text(encoding="utf-8")
    assert "SECRET" not in text and "127.0.0.1:8800" in text
    source = Path(portable.__file__).read_text(encoding="utf-8")
    assert 'say("launch_link", log=False' in source


def test_the_bundled_python_does_not_name_the_exporting_computer(tmp_path: Path) -> None:
    source = tmp_path / "home" / "alice" / "data" / "export-cache" / "python" / "cpython-3.13.9-linux-x86_64-gnu"
    target = tmp_path / "package" / "python"
    config = target / "lib" / "python3.13" / "_sysconfigdata__linux_x86_64-linux-gnu.py"
    config.parent.mkdir(parents=True)
    config.write_text(f"build_time_vars = {{'prefix': '{source}', 'LIBDIR': '{source}/lib', 'CC': 'cc'}}\n")
    other = target / "lib" / "python3.13" / "os.py"
    other.write_text("# untouched\n")
    changed = portable_build.neutral_python_paths(target, source)
    assert changed == [config]
    text = config.read_text()
    assert str(tmp_path) not in text and "'/install/lib'" in text
    assert other.read_text() == "# untouched\n"
    # A cache that was moved since uv installed it: the prefix written in the file is replaced too.
    moved = tmp_path / "old" / "home" / "bob" / "cpython"
    config.write_text(f'build_time_vars = {{"prefix": "{moved}", "exec_prefix": "{moved}", "LIBDIR": "{moved}/lib"}}\n')
    assert portable_build.neutral_python_paths(target, source) == [config]
    assert str(tmp_path) not in config.read_text() and '"/install/lib"' in config.read_text()


@pytest.mark.parametrize("folder", ["Ergebnisse/😀 Berichte", "a\x7fb", 'C:\\Daten\\"Ablage"\n#x = 1', "results"])
def test_the_settings_file_reads_back_any_output_folder(folder: str) -> None:
    """``config/settings.toml`` takes the export's output folder as a TOML string (API3-09): characters outside the
    Basic Multilingual Plane must not become JSON surrogate escapes, which TOML refuses."""
    import tomllib

    from nodestro_api.exporter import ExportOptions

    text = portable_build.settings_toml(ExportOptions(kind="portable", output_dir=folder), context_length=32768)
    assert tomllib.loads(text)["data"]["output_dir"] == folder.strip()


@pytest.mark.skipif(not hasattr(__import__("os"), "getuid"), reason="POSIX file modes")
def test_the_data_and_log_folders_are_owner_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """API3-04: on a shared Unix computer other users cannot read runs, results or the log."""
    import os
    import stat

    package = tmp_path / "package"
    package.mkdir()
    (package / "logs").mkdir(mode=0o755)  # from an older version
    (package / "logs").chmod(0o755)
    elsewhere = tmp_path / "shared"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    settings = portable.Settings(package, {"data": {"data_dir": "data", "log_dir": "logs"}})
    monkeypatch.setattr(portable, "_serve", lambda *args: 0)
    monkeypatch.setattr(portable, "_choose_port", lambda host, port: port)
    for key in ("API_KEYS", "NODESTRO_HOST", "DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in settings.environment().items():  # start() sets them; restored after the test
        monkeypatch.setenv(key, value)
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    old_umask = os.umask(0o022)
    try:
        assert portable.start(argparse.Namespace(no_browser=True, host=None, port=None), settings) == 0
    finally:
        os.umask(old_umask)
        for handler in root.handlers[:]:
            if handler not in handlers:
                handler.close()
                root.removeHandler(handler)
        root.setLevel(level)

    def mode(path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    assert mode(package / "data") == 0o700
    assert mode(package / "logs") == 0o700
    assert mode(package / "logs" / "application.log") == 0o600
    # A folder the person named outside the package keeps the modes its owner gave it.
    portable._private_folder(elsewhere, package)
    assert mode(elsewhere) == 0o755
    portable._private_folder(elsewhere / "new", package)
    assert mode(elsewhere / "new") == 0o700
