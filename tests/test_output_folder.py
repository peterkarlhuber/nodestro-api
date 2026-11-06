"""The result folder of a portable application, chosen on the web interface's settings page (OutputFolder)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.testing import ScriptedClient
import pytest
from test_export import _download, _export, _extract
from test_published_api import PUBLISHED, new_key, publish

from nodestro_api.output_folder import FILE, OutputFolder
from nodestro_api.serving import ApiError

from conftest import text
from test_flow import edge, save
from test_runs import agent_node

HEADERS = {"X-Nodestro-UI": "1", "Origin": "http://127.0.0.1:8800"}


def _host(default: Path | None) -> Any:
    return SimpleNamespace(output_dir=default, output_warning=None, output_check=None)


@pytest.fixture
def package(tmp_path: Path) -> SimpleNamespace:
    """A package folder like an unpacked portable export: code folders, data, logs and the default results."""
    root = tmp_path / "package"
    for name in ("app", "nodestro_api", "python", "config", "data", "logs", "results"):
        (root / name).mkdir(parents=True)
    return SimpleNamespace(root=root, data=root / "data", logs=root / "logs", default=(root / "results").resolve())


def _folder(package: SimpleNamespace, **options: Any) -> OutputFolder:
    return OutputFolder(
        _host(package.default), data_dir=package.data, project_dir=package.root, protected=[package.logs], **options
    )


def _code(call: Callable[[], Any]) -> str:
    with pytest.raises(ApiError) as caught:
        call()
    return caught.value.code


# ---- checks -------------------------------------------------------------------------------------------------------


def test_only_safe_absolute_folders_are_accepted(package: SimpleNamespace, tmp_path: Path) -> None:
    output = _folder(package)
    assert _code(lambda: output.check("relative/folder", create=True)) == "output.not_absolute"
    assert _code(lambda: output.check("", create=True)) == "output.invalid"
    assert _code(lambda: output.check("/tmp/a\nb", create=True)) == "output.invalid"
    if os.name == "posix":
        for system in ("/", "/etc", "/usr/local/share", "/var/lib", "/proc/self", "/bin/../etc"):
            assert _code(lambda system=system: output.check(system, create=True)) == "output.protected", system
    # The package's own folders: code, Python, settings, data (database, keys) and logs; the package root itself.
    for inside in ("app", "nodestro_api/x", "python", "config", "data", "data/secrets", "logs", "."):
        target = str(package.root / inside)
        assert _code(lambda target=target: output.check(target, create=True)) == "output.protected", inside
    # Normalized before the check: ".." cannot climb out of an allowed folder into a protected one.
    sneaky = str(package.root / "results" / ".." / "data")
    assert _code(lambda: output.check(sneaky, create=True)) == "output.protected"
    # A link to a protected folder is checked where it points.
    link = tmp_path / "link-to-data"
    link.symlink_to(package.data, target_is_directory=True)
    assert _code(lambda: output.check(str(link), create=True)) == "output.protected"
    if os.name == "posix":
        system_link = tmp_path / "etc-link"
        system_link.symlink_to("/etc", target_is_directory=True)
        assert _code(lambda: output.check(str(system_link / "nodestro"), create=True)) == "output.protected"
    # A file is not a folder; a missing folder needs consent.
    afile = tmp_path / "note.txt"
    afile.write_text("x")
    assert _code(lambda: output.check(str(afile), create=True)) == "output.not_a_folder"
    missing = tmp_path / "new" / "deeper"
    assert _code(lambda: output.check(str(missing), create=False)) == "output.missing"
    assert not missing.exists()
    assert output.check(str(missing), create=True) == missing.resolve() and missing.is_dir()
    # Another folder next to the default inside the package is fine (not code, not data).
    assert output.check(str(package.root / "ergebnisse"), create=True) == (package.root / "ergebnisse").resolve()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a POSIX user without root rights")
def test_a_folder_that_cannot_be_written_is_refused(package: SimpleNamespace, tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert _code(lambda: _folder(package).check(str(locked), create=False)) == "output.not_writable"
    finally:
        locked.chmod(0o700)


def test_a_write_error_is_reported(package: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Path, data: bytes) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_bytes", refuse)
    with pytest.raises(ApiError) as caught:
        _folder(package).check(str(tmp_path), create=False)
    assert caught.value.code == "output.not_writable" and caught.value.params["problem"] == "Permission denied"


def test_a_chosen_folder_swapped_for_a_link_is_not_written(package: SimpleNamespace, tmp_path: Path) -> None:
    output = _folder(package)
    chosen = tmp_path / "chosen"
    output.choose(str(chosen), create=True)
    assert output.host.output_check(chosen) is None
    chosen.rmdir()
    chosen.symlink_to(package.data, target_is_directory=True)
    assert output.host.output_check(chosen).code == "output.protected"


# ---- choice, persistence, restore --------------------------------------------------------------------------------


def test_choice_is_stored_reused_after_a_restart_and_restored(package: SimpleNamespace, tmp_path: Path) -> None:
    output = _folder(package, opener=lambda _folder: None)
    assert output.state() == {
        "enabled": True,
        "path": str(package.default),
        "warning": None,
        "source": "default",
        "default_path": str(package.default),
        "can_change": True,
        "can_open": True,
    }
    chosen = tmp_path / "Kunden" / "Ergebnisse 2026"
    state = output.choose(str(chosen), create=True)
    assert state["source"] == "chosen" and state["path"] == str(chosen.resolve())
    assert output.host.output_dir == chosen.resolve() and output.host.output_check is not None
    stored = json.loads((package.data / FILE).read_text("utf-8"))
    assert stored["path"] == str(chosen.resolve())
    if os.name == "posix":
        assert (package.data / FILE).stat().st_mode & 0o077 == 0
    # A restart: a new host and a new OutputFolder read the choice again.
    again = _folder(package)
    assert again.host.output_dir == chosen.resolve() and again.state()["source"] == "chosen"
    # Restore default: the file is gone, the host writes to the default again.
    state = again.reset()
    assert state["source"] == "default" and state["path"] == str(package.default)
    assert again.host.output_dir == package.default and again.host.output_check is None
    assert not (package.data / FILE).exists()
    assert _folder(package).state()["source"] == "default"


def test_a_stored_choice_that_became_unusable_falls_back_to_the_default(package: SimpleNamespace) -> None:
    (package.data / FILE).parent.mkdir(parents=True)
    (package.data / FILE).write_text(json.dumps({"path": str(package.root / "python" / "lib")}), "utf-8")
    output = _folder(package)
    state = output.state()
    assert state["source"] == "default" and state["path"] == str(package.default)
    assert state["warning"] and "python" in state["warning"]


def test_output_dir_from_the_environment_wins_and_cannot_be_changed(package: SimpleNamespace, tmp_path: Path) -> None:
    (package.data / FILE).parent.mkdir(parents=True)
    (package.data / FILE).write_text(json.dumps({"path": str(tmp_path)}), "utf-8")
    output = _folder(package, fixed=True)
    assert output.state()["source"] == "environment" and output.state()["can_change"] is False
    assert output.host.output_dir == package.default
    assert _code(lambda: output.choose(str(tmp_path), create=True)) == "output.fixed"
    assert _code(output.reset) == "output.fixed"


# ---- the folder browser ------------------------------------------------------------------------------------------


def test_browser_lists_folder_names_only(package: SimpleNamespace, tmp_path: Path) -> None:
    place = tmp_path / "place"
    (place / "Beta").mkdir(parents=True)
    (place / "alpha").mkdir()
    (place / ".hidden").mkdir()
    (place / "secret.txt").write_text("never listed")
    (place / "to-data").symlink_to(package.data, target_is_directory=True)
    output = _folder(package)
    view = output.browse(str(place))
    assert [item["name"] for item in view["folders"]] == ["alpha", "Beta", "to-data"]
    assert [item["usable"] for item in view["folders"]] == [True, True, False]
    assert "secret" not in json.dumps(view) and "never listed" not in json.dumps(view)
    assert view["path"] == str(place.resolve()) and view["parent"] == str(place.resolve().parent)
    assert {item["kind"] for item in view["places"]} >= {"home", "current"}
    assert _code(lambda: output.browse(str(place / "secret.txt"))) == "output.not_found"
    assert _code(lambda: output.browse("relative")) == "output.not_absolute"
    # The package root is listed, but its code folders are marked unusable.
    listed = {item["name"]: item["usable"] for item in output.browse(str(package.root))["folders"]}
    assert listed["results"] and not listed["app"] and not listed["data"]
    # New folders: plain names only.
    created = output.make_folder(str(place), "Neu 2026")
    assert created["path"] == str((place / "Neu 2026").resolve()) and (place / "Neu 2026").is_dir()
    for bad in ("", "..", ".x", "a/b", "a\\b", "c:d"):
        assert _code(lambda bad=bad: output.make_folder(str(place), bad)) == "output.bad_name", bad
    assert _code(lambda: output.make_folder(str(place), "alpha")) == "output.exists"
    assert _code(lambda: output.make_folder(str(package.data), "x")) == "output.protected"


# ---- the web interface: a real run with the deterministic double --------------------------------------------------


@pytest.fixture
def exported(make_app: Callable[..., TestClient], tmp_path: Path) -> SimpleNamespace:
    studio = make_app({"WRITER": [text("# Bericht\n\nAlles gut.")] * 3})
    workflow_id = save(
        studio,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER"),
            {"id": "out", "type": "output", "output": {"files": [{"format": "md", "name": "Bericht"}]}},
        ],
        [edge("start", "writer"), edge("writer", "out")],
        name="Bericht",
    )
    publication = publish(studio, workflow_id)["publication"]
    token = new_key(studio, publication["id"], ["run", "read"])
    project = _extract(_download(studio, _export(studio, publication["id"], web_ui=True)["id"]), tmp_path / "export")
    return SimpleNamespace(project=project, slug=publication["slug"], token=token)


@contextlib.contextmanager
def _app(project: Path, data_dir: Path, keys: str, *, opener: Any = None) -> Iterator[tuple[TestClient, Any]]:
    """The exported project's app with the operator's settings page of a portable package."""
    from nodestro_api.standalone import StandaloneSettings, create_app
    from nodestro_api.webui_server import UiOptions, WebUi

    holder: dict[str, Any] = {}

    def web_ui(host: Any, core: Any) -> Any:
        output = OutputFolder(host, data_dir=data_dir, project_dir=project, opener=opener)
        holder["ui"] = WebUi(host, UiOptions(language="de", control_token="c"), output=output)
        return holder["ui"]

    settings = StandaloneSettings.from_env({"DATA_DIR": str(data_dir), "API_KEYS": keys, "OUTPUT_DIR": "results"})
    fake = ScriptedClient({"WRITER": [text("# Bericht\n\nAlles gut.")] * 5})
    app = create_app(project, settings=settings, client_factory=lambda _provider: fake, web_ui=web_ui)
    with TestClient(app, base_url="http://127.0.0.1:8800") as client:
        yield client, holder["ui"]


def _sign_in_operator(client: TestClient, ui: Any) -> None:
    ui.sessions.add_launch_token("launch")
    assert client.get("/ui/launch?token=launch", follow_redirects=False).status_code == 303


def _run(client: TestClient, slug: str) -> str:
    started = client.post(f"{PUBLISHED}/{slug}/runs", json={"input": "Bericht"}, headers=HEADERS)
    assert started.status_code in (200, 201, 202), started.text
    run_id = str(started.json()["run_id"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if client.get(f"{PUBLISHED}/{slug}/runs/{run_id}").json()["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    return run_id


def _files_of(folder: Path, run_id: str, timeout: float = 5) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [path for path in folder.glob(f"*_{run_id}") if path.is_dir()]
        if found and any(found[0].iterdir()):
            return sorted(path.name for path in found[0].iterdir())
        time.sleep(0.05)
    return []


def test_the_settings_page_moves_future_result_files(exported: SimpleNamespace, tmp_path: Path) -> None:
    data = tmp_path / "data"
    default = (exported.project / "results").resolve()
    chosen = tmp_path / "Ablage" / "Sehr langer Ordnername für Ergebnisse"
    with _app(exported.project, data, "") as (client, ui):
        _sign_in_operator(client, ui)
        setup = client.get("/ui/api/setup").json()["output_dir"]
        assert setup["source"] == "default" and setup["path"] == str(default) and setup["can_change"]
        assert setup["can_open"] is False  # no file manager here (and the test client is not on loopback)
        first = _run(client, exported.slug)
        assert _files_of(default, first) == ["Bericht.md"]
        # Changing needs the interface's header and the same origin (as every setup action).
        assert client.put("/ui/api/setup/output", json={"path": str(chosen)}).status_code == 403
        evil = {**HEADERS, "Origin": "http://evil.example"}
        assert client.put("/ui/api/setup/output", json={"path": str(chosen)}, headers=evil).status_code == 403
        # Missing folder: the interface asks before it is created.
        missing = client.put("/ui/api/setup/output", json={"path": str(chosen)}, headers=HEADERS)
        assert missing.status_code == 409 and missing.json()["error"]["code"] == "output.missing"
        assert not chosen.exists()
        refused = client.put("/ui/api/setup/output", json={"path": str(data)}, headers=HEADERS)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "output.protected"
        saved = client.put("/ui/api/setup/output", json={"path": str(chosen), "create": True}, headers=HEADERS)
        assert saved.status_code == 200 and saved.json()["source"] == "chosen"
        assert saved.json()["path"] == str(chosen.resolve())
        # The next run writes there, at once (no restart); the earlier run's files stay where they are.
        second = _run(client, exported.slug)
        assert _files_of(chosen, second) == ["Bericht.md"]
        assert _files_of(default, first) == ["Bericht.md"] and not list(default.glob(f"*_{second}"))
        # The browser: names of folders only, for the operator.
        browse = client.get("/ui/api/setup/output/browse", params={"path": str(chosen.parent)})
        assert browse.status_code == 200 and [item["name"] for item in browse.json()["folders"]] == [chosen.name]
        made = client.post(
            "/ui/api/setup/output/folders", json={"parent": str(chosen.parent), "name": "Neu"}, headers=HEADERS
        )
        assert made.status_code == 201 and (chosen.parent / "Neu").is_dir()
        assert client.post("/ui/api/setup/output/open", headers=HEADERS).status_code == 404
    # After a restart the choice is still in effect.
    with _app(exported.project, data, "") as (client, ui):
        _sign_in_operator(client, ui)
        assert client.get("/ui/api/setup").json()["output_dir"]["path"] == str(chosen.resolve())
        third = _run(client, exported.slug)
        assert _files_of(chosen, third) == ["Bericht.md"]
        # Restore default.
        restored = client.delete("/ui/api/setup/output", headers=HEADERS)
        assert restored.status_code == 200 and restored.json()["source"] == "default"
        fourth = _run(client, exported.slug)
        assert _files_of(default, fourth) == ["Bericht.md"] and not list(chosen.glob(f"*_{fourth}"))
        assert _files_of(chosen, second) == ["Bericht.md"]  # nothing was moved back


def test_only_the_operator_may_see_or_change_the_result_folder(exported: SimpleNamespace, tmp_path: Path) -> None:
    from nodestro_api.serving import hash_secret, parse_token

    key_id, secret = parse_token(exported.token)  # type: ignore[misc]
    with _app(exported.project, tmp_path / "data", f"{key_id}:{hash_secret(secret)}:run+read") as (client, _ui):
        for method, path in (
            ("GET", "/ui/api/setup/output/browse"),
            ("PUT", "/ui/api/setup/output"),
            ("DELETE", "/ui/api/setup/output"),
            ("POST", "/ui/api/setup/output/folders"),
            ("POST", "/ui/api/setup/output/open"),
        ):
            assert client.request(method, path, headers=HEADERS).status_code == 401, path
        signed = client.post("/ui/api/session", json={"api_key": exported.token}, headers=HEADERS)
        assert signed.status_code == 200
        for method, path in (
            ("GET", "/ui/api/setup/output/browse"),
            ("PUT", "/ui/api/setup/output"),
            ("DELETE", "/ui/api/setup/output"),
            ("POST", "/ui/api/setup/output/folders"),
            ("POST", "/ui/api/setup/output/open"),
        ):
            answer = client.request(method, path, json={}, headers=HEADERS)
            assert answer.status_code == 403 and answer.json()["error"]["code"] == "ui.operator_only", path
        config = client.get("/ui/api/config").json()["output_dir"]
        assert config == {"enabled": True}


def test_the_folder_opens_only_for_a_browser_on_this_computer(exported: SimpleNamespace, tmp_path: Path) -> None:
    opened: list[Path] = []
    with _app(exported.project, tmp_path / "data", "", opener=opened.append) as (client, ui):
        _sign_in_operator(client, ui)
        # The test client is not on loopback: the button is hidden and the action refused.
        assert client.get("/ui/api/setup").json()["output_dir"]["can_open"] is False
        assert client.post("/ui/api/setup/output/open", headers=HEADERS).status_code == 404
        assert opened == []
        # A browser on this computer (the application's lifespan runs in the client above).
        local = TestClient(client.app, base_url="http://127.0.0.1:8800", client=("127.0.0.1", 50000))
        ui.sessions.add_launch_token("again")
        assert local.get("/ui/launch?token=again", follow_redirects=False).status_code == 303
        assert local.get("/ui/api/setup").json()["output_dir"]["can_open"] is True
        assert local.post("/ui/api/setup/output/open", headers=HEADERS).status_code == 200
        assert opened == [(exported.project / "results").resolve()]


def test_only_a_portable_package_carries_the_result_folder_choice() -> None:
    from nodestro_api.exporter import PORTABLE_ROOTS, runtime_modules

    assert "nodestro_api.output_folder" in runtime_modules(extra=PORTABLE_ROOTS)
    # A standard export has no operator session, so nothing there could change or restore the folder.
    assert "nodestro_api.output_folder" not in runtime_modules()
