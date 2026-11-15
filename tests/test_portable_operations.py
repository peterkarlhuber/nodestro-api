"""Operating a portable package (NAL-C3): the folder model, the check of program/ at each start and with ``verify``,
backup and restore, update and rollback, taking over an older package, and installing a newer web interface.

The packages here are small stand-ins with the real folder model; tests/test_portable_smoke.py runs the same commands
on a real package on six platforms."""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tarfile
from typing import Any
import zipfile

from fastapi.testclient import TestClient
import pytest
from test_export import _download, _export, _extract
from test_portable import published  # noqa: F401 - the fixture

from nodestro_api import portable, portable_build, portable_ops, ui_package
from nodestro_api.app_contract import CONTRACT_VERSIONS
from nodestro_api.portable_ops import PackageError

WEBUI = Path(portable_build.__file__).resolve().parent / "webui"
SLUG = "offer-writer"


def _program(folder: Path, *, slug: str = SLUG, release: int = 1, platform: str = "linux-x86_64") -> Path:
    """A program folder with a few files, its PORTABLE.json (layout 2) and its SHA256SUMS."""
    folder.mkdir(parents=True)
    (folder / "nodestro_api").mkdir()
    (folder / "nodestro_api" / "portable.py").write_text(f"# release {release}\n", encoding="utf-8")
    (folder / "workflow").mkdir()
    (folder / "workflow" / "release.json").write_text(json.dumps({"release": release}), encoding="utf-8")
    (folder / "models").mkdir()
    (folder / "models" / "orkexa-models.json").write_text("{}", encoding="utf-8")
    info = {
        "format": "nodestro-portable",
        "format_version": 1,
        "layout_version": 2,
        "name": "Offer writer",
        "slug": slug,
        "release_number": release,
        "platform": platform,
        "platform_label": platform,
    }
    (folder / "PORTABLE.json").write_text(json.dumps(info), encoding="utf-8")
    (folder / "SHA256SUMS").write_text(portable_build.checksums(folder), encoding="utf-8")
    return folder


def _package(root: Path, **options: Any) -> Path:
    """A package folder of layout 2: program/, start script, config/ and some data of a person."""
    _program(root / "program", **options)
    (root / "start.sh").write_text("#!/bin/sh\n# v1\n", encoding="utf-8")
    (root / "README.md").write_text("readme 1\n", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "settings.toml").write_text('[web]\nlanguage = "de"\n', encoding="utf-8")
    data = root / "data"
    for name, content in {
        "runtime.db": b"runs",
        "api-keys.txt": b"key:hash:run+read\n",
        "secrets/providers.json": b'{"anthropic": {"api_key": "sk-secret"}}',
        "run/instance.json": b"{}",
        "models/blob": b"copy of a model",
        "ollama/server.log": b"log",
    }.items():
        (data / name).parent.mkdir(parents=True, exist_ok=True)
        (data / name).write_bytes(content)
    (root / "results").mkdir()
    (root / "results" / "offer.txt").write_text("result", encoding="utf-8")
    return root


def _zip(folder: Path, target: Path, top: str = "package") -> Path:
    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, f"{top}/{path.relative_to(folder).as_posix()}")
    return target


def _settings(root: Path) -> portable.Settings:
    return portable.Settings(root)


# ---- the folder model and the integrity check -------------------------------------------------------------------


def test_the_launcher_finds_the_package_around_its_program(tmp_path: Path) -> None:
    root = _package(tmp_path / "app")
    assert portable_ops.package_root(root / "program") == root
    assert portable_ops.program_of(root) == root / "program"
    settings = _settings(root)
    assert settings.program == root / "program" and settings.data_dir == root / "data"
    # The old layout: the program files are the package folder itself.
    old = tmp_path / "old"
    old.mkdir()
    (old / "PORTABLE.json").write_text(json.dumps({"slug": SLUG, "format_version": 1}), encoding="utf-8")
    assert portable_ops.package_root(old) == old and portable_ops.program_of(old) == old


def test_verify_finds_changed_missing_and_extra_files(tmp_path: Path) -> None:
    program = _program(tmp_path / "program")
    assert portable_ops.verify_program(program) == []
    # Python's bytecode caches and the model server's index are written at run time: not problems.
    (program / "nodestro_api" / "__pycache__").mkdir()
    (program / "nodestro_api" / "__pycache__" / "portable.cpython-313.pyc").write_bytes(b"\x00")
    (program / "models" / "manifests-v2").mkdir()
    (program / "models" / "manifests-v2" / "model").write_text("{}", encoding="utf-8")
    assert portable_ops.verify_program(program) == []
    (program / "nodestro_api" / "portable.py").write_text("# edited\n", encoding="utf-8")
    (program / "workflow" / "release.json").unlink()
    (program / "workflow" / "stray.json").write_text("{}", encoding="utf-8")
    assert portable_ops.verify_program(program) == [
        "changed: nodestro_api/portable.py",
        "missing: workflow/release.json",
        "extra: workflow/stray.json",
    ]


def test_model_files_are_only_checked_for_presence_at_the_start(tmp_path: Path) -> None:
    program = _program(tmp_path / "program")
    (program / "models" / "orkexa-models.json").write_text('{"edited": 1}', encoding="utf-8")
    assert portable_ops.verify_program(program, skip=portable_ops.SKIPPED_AT_START) == []
    assert portable_ops.verify_program(program) == ["changed: models/orkexa-models.json"]


@pytest.mark.skipif(os.name != "posix", reason="symbolic links")
def test_verify_refuses_links_out_of_the_program(tmp_path: Path) -> None:
    program = _program(tmp_path / "program")
    (program / "python").mkdir()
    (program / "python" / "python3").symlink_to("python3.13")  # inside: as in the bundled Python
    assert portable_ops.verify_program(program) == []
    (program / "python" / "evil").symlink_to(tmp_path)
    assert portable_ops.verify_program(program) == ["link: python/evil"]


def test_a_damaged_sums_file_is_a_problem(tmp_path: Path) -> None:
    program = _program(tmp_path / "program")
    (program / "SHA256SUMS").write_text("nonsense\n" + "0" * 64 + "  ../outside\n", encoding="utf-8")
    problems = portable_ops.verify_program(program)
    assert problems[:2] == ["changed: SHA256SUMS", "changed: SHA256SUMS"]
    (program / "SHA256SUMS").unlink()
    assert portable_ops.verify_program(program) == ["missing: SHA256SUMS"]


def test_the_start_refuses_a_changed_program(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = _package(tmp_path / "app")
    settings = _settings(root)
    console = portable.Console("en")
    assert portable._program_intact(settings, console)
    (root / "program" / "nodestro_api" / "portable.py").write_text("# edited\n", encoding="utf-8")
    assert not portable._program_intact(settings, console)
    assert "changed: nodestro_api/portable.py" in caplog.text
    arguments = portable.parser().parse_args(["start", "--no-browser"])
    assert portable.start(arguments, settings) == 4


def test_verify_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _package(tmp_path / "app")
    assert portable.verify_command(_settings(root)) == 0
    (root / "program" / "workflow" / "release.json").write_text("{}", encoding="utf-8")
    assert portable.verify_command(_settings(root)) == 1
    assert "changed: workflow/release.json" in capsys.readouterr().out


def test_commands_of_the_folder_model_need_layout_2_and_a_stopped_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_path / "old"
    old.mkdir()
    assert portable.rollback_command(portable.Settings(old)) == 2
    root = _package(tmp_path / "app")
    monkeypatch.setattr(portable, "_running", lambda _settings: True)
    arguments = portable.parser().parse_args(["backup", str(tmp_path / "b.zip")])
    assert portable.backup_command(arguments, _settings(root)) == 2
    assert not (tmp_path / "b.zip").exists()
    assert "Beende sie zuerst" in capsys.readouterr().out
    assert portable.verify_command(_settings(root)) == 0  # verify is allowed while it runs


# ---- backup and restore ----------------------------------------------------------------------------------------


def _names(target: Path) -> set[str]:
    with zipfile.ZipFile(target) as archive:
        return set(archive.namelist())


def test_a_backup_holds_settings_and_data_but_secrets_and_results_only_on_request(tmp_path: Path) -> None:
    root = _package(tmp_path / "app")
    settings = _settings(root)
    info = portable.package_info(settings.program)
    plain = portable_ops.backup(root, settings.data_dir, settings.output_dir, tmp_path / "plain.zip", info)
    assert plain["secrets"] is False and plain["results"] is False and plain["slug"] == SLUG
    assert _names(tmp_path / "plain.zip") == {
        "BACKUP.json",
        "config/settings.toml",
        "data/runtime.db",
        "data/api-keys.txt",
    }
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "plain.zip").stat().st_mode) == 0o600
    full = tmp_path / "full.zip"
    portable_ops.backup(
        root, settings.data_dir, settings.output_dir, full, info, include_secrets=True, include_results=True
    )
    assert {"data/secrets/providers.json", "results/offer.txt"} <= _names(full)
    assert not any(name.startswith(("data/run/", "data/models/", "data/ollama/")) for name in _names(full))


def test_the_backup_command_warns_about_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _package(tmp_path / "app")
    arguments = portable.parser().parse_args(["backup", str(tmp_path / "b.zip"), "--include-secrets"])
    assert portable.backup_command(arguments, _settings(root)) == 0
    assert "unverschlüsselt" in capsys.readouterr().out
    assert "data/secrets/providers.json" in _names(tmp_path / "b.zip")


def test_restore_replaces_data_keeps_secrets_and_saves_the_current_state(tmp_path: Path) -> None:
    root = _package(tmp_path / "app")
    settings = _settings(root)
    info = portable.package_info(settings.program)
    target = tmp_path / "backup.zip"
    portable_ops.backup(root, settings.data_dir, settings.output_dir, target, info, include_results=True)
    (settings.data_dir / "runtime.db").write_bytes(b"later runs")
    (settings.data_dir / "added.txt").write_text("later", encoding="utf-8")
    (root / "results" / "offer.txt").write_text("changed", encoding="utf-8")
    (root / "results" / "other.txt").write_text("kept", encoding="utf-8")
    meta = portable_ops.restore(root, settings.data_dir, settings.output_dir, target, info)
    assert (settings.data_dir / "runtime.db").read_bytes() == b"runs"
    assert not (settings.data_dir / "added.txt").exists()
    # The backup had no secrets: the current ones stay.
    assert (settings.data_dir / "secrets" / "providers.json").is_file()
    assert (root / "results" / "offer.txt").read_text(encoding="utf-8") == "result"
    assert (root / "results" / "other.txt").read_text(encoding="utf-8") == "kept"
    saved = Path(meta["saved"])
    assert saved.parent == root / "backups" and "data/added.txt" in _names(saved)
    assert "data/secrets/providers.json" in _names(saved)
    assert not list(root.glob(".restore-*"))


def test_restore_refuses_a_backup_of_another_application(tmp_path: Path) -> None:
    other = _package(tmp_path / "other", slug="another-app")
    settings = _settings(other)
    target = tmp_path / "other.zip"
    portable_ops.backup(other, settings.data_dir, None, target, portable.package_info(settings.program))
    root = _package(tmp_path / "app")
    mine = _settings(root)
    with pytest.raises(PackageError) as refused:
        portable_ops.restore(root, mine.data_dir, None, target, portable.package_info(mine.program))
    assert refused.value.key == "backup_other_app"
    assert (mine.data_dir / "runtime.db").read_bytes() == b"runs"


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("../escape.txt", "archive_unsafe"),
        ("/etc/passwd", "archive_unsafe"),
        ("data/..\\..\\x", "archive_unsafe"),
        ("C:/Windows/x", "archive_unsafe"),
        ("program/nodestro_api/portable.py", "archive_unexpected"),
    ],
)
def test_restore_checks_every_path_before_it_changes_anything(tmp_path: Path, name: str, key: str) -> None:
    root = _package(tmp_path / "app")
    settings = _settings(root)
    target = tmp_path / "bad.zip"
    meta = {"format": "nodestro-portable-backup", "format_version": 1, "slug": SLUG}
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("BACKUP.json", json.dumps(meta))
        archive.writestr("data/runtime.db", b"other")
        archive.writestr(name, b"x")
    with pytest.raises(PackageError) as refused:
        portable_ops.restore(root, settings.data_dir, None, target, portable.package_info(settings.program))
    assert refused.value.key == key
    assert (settings.data_dir / "runtime.db").read_bytes() == b"runs"
    assert not (tmp_path / "escape.txt").exists()


def test_restore_refuses_links_and_unknown_backups(tmp_path: Path) -> None:
    root = _package(tmp_path / "app")
    settings = _settings(root)
    info = portable.package_info(settings.program)
    target = tmp_path / "link.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(
            "BACKUP.json", json.dumps({"format": "nodestro-portable-backup", "format_version": 1, "slug": SLUG})
        )
        link = zipfile.ZipInfo("data/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc")
    with pytest.raises(PackageError) as refused:
        portable_ops.restore(root, settings.data_dir, None, target, info)
    assert refused.value.key == "archive_unsafe"
    newer = tmp_path / "newer.zip"
    with zipfile.ZipFile(newer, "w") as archive:
        archive.writestr("BACKUP.json", json.dumps({"format": "nodestro-portable-backup", "format_version": 9}))
    with pytest.raises(PackageError) as refused:
        portable_ops.restore(root, settings.data_dir, None, newer, info)
    assert refused.value.key == "backup_version"
    (tmp_path / "plain.txt").write_text("no zip", encoding="utf-8")
    with pytest.raises(PackageError) as refused:
        portable_ops.restore(root, settings.data_dir, None, tmp_path / "plain.txt", info)
    assert refused.value.key == "backup_unreadable"


# ---- update and rollback ---------------------------------------------------------------------------------------


def _new_package(tmp_path: Path, **options: Any) -> Path:
    folder = tmp_path / "new"
    _program(folder / "program", release=2, **options)
    (folder / "start.sh").write_text("#!/bin/sh\n# v2\n", encoding="utf-8")
    (folder / "README.md").write_text("readme 2\n", encoding="utf-8")
    (folder / "config").mkdir()
    (folder / "config" / "settings.toml").write_text('[web]\nlanguage = "en"\n', encoding="utf-8")
    return folder


@pytest.mark.parametrize("as_zip", [True, False])
def test_update_replaces_the_program_keeps_the_data_and_rollback_goes_back(tmp_path: Path, as_zip: bool) -> None:
    root = _package(tmp_path / "app")
    settings = _settings(root)
    new = _new_package(tmp_path)
    source = _zip(new, tmp_path / "new.zip") if as_zip else new
    record = portable_ops.update(root, settings.data_dir, settings.output_dir, source)
    assert record["from"] == 1 and record["to"] == 2
    assert portable.package_info(root / "program")["release_number"] == 2
    assert portable.package_info(root / "program.previous")["release_number"] == 1
    assert portable_ops.verify_program(root / "program") == []
    # The person's things are untouched: settings, data, secrets, results.
    assert (root / "config" / "settings.toml").read_text(encoding="utf-8") == '[web]\nlanguage = "de"\n'
    assert (settings.data_dir / "runtime.db").read_bytes() == b"runs"
    assert (settings.data_dir / "secrets" / "providers.json").is_file()
    assert (root / "results" / "offer.txt").is_file()
    assert (root / "start.sh").read_text(encoding="utf-8").endswith("# v2\n")
    assert (root / "README.md").read_text(encoding="utf-8") == "readme 2\n"
    saved = root / "backups" / record["backup"]
    assert "data/runtime.db" in _names(saved) and "data/secrets/providers.json" not in _names(saved)
    assert not list(root.glob(".update-*"))
    assert new.is_dir()  # an unpacked package given as the source is copied, never moved
    back = portable_ops.rollback(root)
    assert back == {"release_number": 1, "backup": record["backup"]}
    assert portable.package_info(root / "program.previous")["release_number"] == 2
    assert portable_ops.rollback(root)["release_number"] == 2  # and forward again


def test_on_windows_the_start_script_puts_the_new_program_in_place(tmp_path: Path) -> None:
    root = _package(tmp_path / "app")
    (root / "start.cmd").write_text("old", encoding="utf-8")
    new = _new_package(tmp_path)
    (new / "start.cmd").write_text("new", encoding="utf-8")
    settings = _settings(root)
    portable_ops.update(root, settings.data_dir, None, new, swap_now=False)
    # The running Python's folder is not renamed: the new program waits in program.next, start.cmd beside it.
    assert portable.package_info(root / "program")["release_number"] == 1
    assert portable.package_info(root / "program.next")["release_number"] == 2
    assert (root / "start.cmd").read_text(encoding="utf-8") == "old"
    assert (root / "start.cmd.new").read_text(encoding="utf-8") == "new"
    assert portable_ops.apply_pending(root) and not portable_ops.apply_pending(root)
    assert portable.package_info(root / "program")["release_number"] == 2
    assert portable_ops.rollback(root, swap_now=False)["release_number"] == 1
    assert (root / "program.next").is_dir() and not (root / "program.previous").exists()
    assert portable_ops.apply_pending(root)
    assert portable.package_info(root / "program")["release_number"] == 1
    assert portable.package_info(root / "program.previous")["release_number"] == 2
    script = portable_build.start_scripts(portable_build.PLATFORMS["windows-x86_64"])["start.cmd"][0]
    assert 'move "program.next" "program"' in script and 'move /y "start.cmd.new" "start.cmd"' in script


def test_update_refuses_another_application_platform_or_a_damaged_package(tmp_path: Path) -> None:
    root = _package(tmp_path / "app")
    settings = _settings(root)
    for options, key in (
        ({"slug": "another-app"}, "update_other_app"),
        ({"platform": "windows-x86_64"}, "update_other_platform"),
    ):
        new = _new_package(tmp_path, **options)
        with pytest.raises(PackageError) as refused:
            portable_ops.update(root, settings.data_dir, None, new)
        assert refused.value.key == key
        shutil.rmtree(new)
    new = _new_package(tmp_path)
    (new / "program" / "nodestro_api" / "portable.py").write_text("# broken copy\n", encoding="utf-8")
    with pytest.raises(PackageError) as refused:
        portable_ops.update(root, settings.data_dir, None, new)
    assert refused.value.key == "update_damaged"
    assert portable.package_info(root / "program")["release_number"] == 1
    assert not (root / "program.previous").exists() and not list(root.glob(".update-*"))
    with pytest.raises(PackageError) as refused:
        portable_ops.update(root, settings.data_dir, None, tmp_path / "app" / "results")
    assert refused.value.key == "update_not_a_package"
    with pytest.raises(PackageError) as refused:
        portable_ops.rollback(root)
    assert refused.value.key == "rollback_nothing"


@pytest.mark.skipif(os.name != "posix", reason="symbolic links")
def test_update_refuses_a_link_out_of_the_package(tmp_path: Path) -> None:
    root = _package(tmp_path / "app")
    new = _new_package(tmp_path)
    target = _zip(new, tmp_path / "new.zip")
    with zipfile.ZipFile(target, "a") as archive:
        link = zipfile.ZipInfo("package/program/python/evil")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../../../outside")
    with pytest.raises(PackageError) as refused:
        portable_ops.update(root, _settings(root).data_dir, None, target)
    assert refused.value.key == "archive_unsafe"
    assert portable.package_info(root / "program")["release_number"] == 1


def test_update_and_rollback_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _package(tmp_path / "app")
    new = _new_package(tmp_path)
    arguments = portable.parser().parse_args(["update", str(new)])
    assert portable.update_command(arguments, _settings(root)) == 0
    assert portable.rollback_command(_settings(root)) == 0
    assert portable.package_info(root / "program")["release_number"] == 1


# ---- taking over an older package ------------------------------------------------------------------------------


def _old_package(folder: Path, *, slug: str = SLUG) -> Path:
    """A package of the first layout: program files, settings and data side by side."""
    folder.mkdir()
    (folder / "PORTABLE.json").write_text(json.dumps({"slug": slug, "name": "Offer writer"}), encoding="utf-8")
    (folder / "config").mkdir()
    (folder / "config" / "settings.toml").write_text(
        '[data]\ndata_dir = "data"\noutput_dir = "ergebnisse"\n', encoding="utf-8"
    )
    (folder / "data" / "secrets").mkdir(parents=True)
    (folder / "data" / "runtime.db").write_bytes(b"old runs")
    (folder / "data" / "secrets" / "providers.json").write_text("{}", encoding="utf-8")
    (folder / "data" / "run").mkdir()
    (folder / "data" / "run" / "instance.json").write_text("{}", encoding="utf-8")
    (folder / "ergebnisse").mkdir()
    (folder / "ergebnisse" / "old.txt").write_text("old result", encoding="utf-8")
    return folder


def test_adopt_copies_the_settings_and_data_of_an_old_package(tmp_path: Path) -> None:
    old = _old_package(tmp_path / "old")
    root = tmp_path / "app"
    _program(root / "program", release=2)
    (root / "config").mkdir()
    (root / "config" / "settings.toml").write_text("# new defaults\n", encoding="utf-8")
    arguments = portable.parser().parse_args(["adopt", str(old)])
    assert portable.adopt_command(arguments, _settings(root)) == 0
    settings = _settings(root)
    assert settings.data_dir == root / "data" and (settings.data_dir / "runtime.db").read_bytes() == b"old runs"
    assert (settings.data_dir / "secrets" / "providers.json").is_file()
    assert not (settings.data_dir / "run").exists()
    assert (root / "ergebnisse" / "old.txt").is_file()
    assert (root / "config" / "settings.toml.package").read_text(encoding="utf-8") == "# new defaults\n"
    assert (old / "data" / "runtime.db").is_file()  # the old package is not changed
    # A second time: this package has data now.
    with pytest.raises(PackageError) as refused:
        portable_ops.adopt(root, old, relative={"data_dir": "data"})
    assert refused.value.key == "adopt_has_data"


def test_adopt_refuses_another_application(tmp_path: Path) -> None:
    old = _old_package(tmp_path / "old", slug="another-app")
    root = tmp_path / "app"
    _program(root / "program")
    with pytest.raises(PackageError) as refused:
        portable_ops.adopt(root, old, relative={"data_dir": "data"})
    assert refused.value.key == "adopt_other_app"
    with pytest.raises(PackageError) as refused:
        portable_ops.adopt(root, tmp_path / "nothing", relative={})
    assert refused.value.key == "adopt_not_a_package"


# ---- the web interface -----------------------------------------------------------------------------------------


def _artifact(tmp_path: Path, *, edit: dict[str, Any] | None = None, tamper: bool = False) -> tuple[Path, str]:
    """An interface artifact as nodestro-app-ui builds it: manifest.json and the files at the top level."""
    manifest = json.loads((WEBUI / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(edit or {})
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:

        def add(name: str, data: bytes) -> None:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))

        add("manifest.json", json.dumps(manifest).encode())
        for item in manifest["files"]:
            data = (WEBUI / item["path"]).read_bytes()
            add(item["path"], data + (b"/* changed */" if tamper and item["path"] == "app.js" else b""))
    target = tmp_path / f"nodestro-app-ui-{manifest['version']}.tgz"
    target.write_bytes(gzip.compress(buffer.getvalue()))
    return target, ui_package.sha256(target.read_bytes())


def test_ui_install_checks_hash_files_and_contract(tmp_path: Path) -> None:
    root = _package(tmp_path / "app")
    artifact, digest = _artifact(tmp_path, edit={"version": "0.2.0"})
    with pytest.raises(PackageError) as refused:
        portable_ops.install_ui(root, artifact, "0" * 64, CONTRACT_VERSIONS)
    assert refused.value.key == "ui_hash_mismatch"
    with pytest.raises(PackageError) as refused:
        portable_ops.install_ui(root, artifact, "short", CONTRACT_VERSIONS)
    assert refused.value.key == "ui_hash_needed"
    (tmp_path / "changed").mkdir()
    changed, changed_digest = _artifact(tmp_path / "changed", tamper=True)
    with pytest.raises(PackageError) as refused:
        portable_ops.install_ui(root, changed, changed_digest, CONTRACT_VERSIONS)
    assert refused.value.key == "ui_invalid" and "app.js" in str(refused.value)
    (tmp_path / "future").mkdir()
    future, future_digest = _artifact(tmp_path / "future", edit={"contract": {"min": 7, "max": 9}})
    with pytest.raises(PackageError) as refused:
        portable_ops.install_ui(root, future, future_digest, CONTRACT_VERSIONS)
    assert "contract" in str(refused.value)
    assert portable_ops.installed_ui(root, CONTRACT_VERSIONS) is None
    found = portable_ops.install_ui(root, artifact, digest.upper(), CONTRACT_VERSIONS)
    assert found["version"] == "0.2.0"
    assert portable_ops.installed_ui(root, CONTRACT_VERSIONS) == root / "ui"
    # A changed file of the installed interface stops the start.
    (root / "ui" / "app.css").write_text("/* edited */", encoding="utf-8")
    with pytest.raises(PackageError) as refused:
        portable_ops.installed_ui(root, CONTRACT_VERSIONS)
    assert refused.value.key == "ui_damaged"
    assert not portable._program_intact(_settings(root), portable.Console("en"))
    assert portable_ops.reset_ui(root) and not (root / "ui").exists()
    assert not portable_ops.reset_ui(root)


@pytest.mark.parametrize("name", ["../escape.js", "/abs.js", ".hidden.js", "a\\b.js"])
def test_ui_artifacts_with_unsafe_paths_are_refused(tmp_path: Path, name: str) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo(name)
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    into = tmp_path / "into"
    into.mkdir()
    with pytest.raises(ui_package.UiPackageError):
        ui_package.unpack_artifact(buffer.getvalue(), into)
    assert not any(into.iterdir())


def test_ui_artifacts_with_links_are_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("app.js")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "/etc/passwd"
        archive.addfile(entry)
    with pytest.raises(ui_package.UiPackageError, match="not a regular file"):
        ui_package.unpack_artifact(buffer.getvalue(), tmp_path)
    with pytest.raises(ui_package.UiPackageError, match="not a readable"):
        ui_package.unpack_artifact(b"not an archive", tmp_path)


def test_the_installed_interface_is_served(tmp_path: Path) -> None:
    from nodestro_api import webui_server

    artifact, digest = _artifact(tmp_path, edit={"version": "0.2.0"})
    root = _package(tmp_path / "app")
    portable_ops.install_ui(root, artifact, digest, CONTRACT_VERSIONS)
    try:
        webui_server.use_ui_dir(root / "ui")
        assert webui_server.ui_dir() == root / "ui"
        assert webui_server._static("app.js") == (root / "ui" / "app.js").resolve()
        assert webui_server._static("../program/PORTABLE.json") is None
    finally:
        webui_server.use_ui_dir(None)
    assert webui_server.ui_dir() == webui_server.WEBUI_DIR


def test_the_vendoring_script_serves_the_backends_contract_versions() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        import vendor_app_ui
    finally:
        sys.path.pop(0)
    assert set(CONTRACT_VERSIONS) == vendor_app_ui.SERVED_CONTRACTS


# ---- the build -------------------------------------------------------------------------------------------------


def test_start_scripts_run_the_program_folder() -> None:
    for platform_id in ("windows-x86_64", "linux-x86_64", "macos-arm64"):
        for content, _mode in portable_build.start_scripts(portable_build.PLATFORMS[platform_id]).values():
            assert "program/python/bin/python3 -I" in content or 'program\\python\\python.exe" -I' in content


def test_bytecode_is_not_exported_and_the_interface_is_described(tmp_path: Path) -> None:
    program = tmp_path / "program"
    shutil.copytree(WEBUI, program / "nodestro_api" / "webui")
    (program / "lib" / "__pycache__").mkdir(parents=True)
    (program / "lib" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (program / "lib" / "x.py").write_text("", encoding="utf-8")
    portable_build.strip_bytecode(program)
    assert not (program / "lib" / "__pycache__").exists() and (program / "lib" / "x.py").is_file()
    described = portable_build.embedded_ui(program)
    manifest = json.loads((WEBUI / "manifest.json").read_text(encoding="utf-8"))
    assert described == {
        "version": manifest["version"],
        "contract": manifest["contract"],
        "manifest_sha256": ui_package.sha256((WEBUI / "manifest.json").read_bytes()),
    }
    assert portable_build.embedded_ui(tmp_path / "none") is None


# ---- an export without an interface (variant C) ----------------------------------------------------------------


def test_an_export_without_the_interface_has_no_interface_routes(published: Any, tmp_path: Path) -> None:  # noqa: F811
    studio, publication = published()
    record = _export(studio, publication["id"], web_ui=False, language="de")
    project = _extract(_download(studio, record["id"]), tmp_path / "export")
    assert not (project / "nodestro_api" / "webui" / "index.html").exists()
    with _project_app(project, tmp_path / "data") as client:
        paths = {getattr(route, "path", "") for route in client.app.routes}  # type: ignore[attr-defined]
        assert not any(path == "/" or path.startswith("/ui") for path in paths), sorted(paths)
        assert client.get("/").status_code == 404
        assert client.get("/ui/assets/app.js").status_code == 404
        assert client.get("/health").status_code == 200
        assert client.get("/api/app").status_code == 200


@contextlib.contextmanager
def _project_app(project: Path, data_dir: Path) -> Iterator[TestClient]:
    """The exported project's own application (its modules, not the installed ones), as ``WEB_UI=on`` asks."""
    packages = ("app", "nodestro_runtime", "nodestro_api")

    def ours(name: str) -> bool:
        return any(name == package or name.startswith(f"{package}.") for package in packages)

    installed = {name: module for name, module in sys.modules.items() if ours(name)}
    for name in installed:
        del sys.modules[name]
    sys.path.insert(0, str(project))
    try:
        from nodestro_api.standalone import StandaloneSettings, create_app

        settings = StandaloneSettings.from_env({"DATA_DIR": str(data_dir), "API_KEYS": "", "WEB_UI": "on"})
        with TestClient(create_app(project, settings=settings), base_url="http://127.0.0.1:8800") as client:
            yield client
    finally:
        sys.path.remove(str(project))
        for name in [name for name in sys.modules if ours(name)]:
            del sys.modules[name]
        sys.modules.update(installed)


def test_the_standalone_host_puts_relative_folders_under_its_home(tmp_path: Path) -> None:
    from nodestro_api.standalone import resolve_output_dir

    found, warning = resolve_output_dir(tmp_path / "home", "results")
    assert found == (tmp_path / "home" / "results").resolve() and warning is None


def test_a_portable_package_carries_the_modules_of_its_commands() -> None:
    from nodestro_api import exporter

    modules = exporter.runtime_modules(None, exporter.PORTABLE_ROOTS)
    assert {"nodestro_api.portable_ops", "nodestro_api.ui_package", "nodestro_api.app_contract"} <= set(modules)
