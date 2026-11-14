"""Tabrion in a portable package (TABRION MIG-NOD-3): built from the pinned, hash-checked source archive.

``portable_locks/tabrion.txt`` pins the source archive of one Tabrion commit by SHA-256; :func:`install_tabrion`
downloads it once (cache), checks the hash on every use, builds the wheel offline with the hash-pinned hatchling of
``portable_locks/build.txt`` and installs it with ``--no-index``. The pin must be the commit the installation (the
studio lock) uses. The full build downloads from GitHub and PyPI: ``NODESTRO_NETWORK_TESTS=1``.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any

from nodestro_runtime.problems import NodestroError
import pytest

from nodestro_api import exporter, portable_build

COMMIT = "d5821faa8742dea012abda3b663549c038d76543"
ARCHIVE = f"https://github.com/peterkarlhuber/tabrion/archive/{COMMIT}.tar.gz"


def _ctx(tmp_path: Path) -> Any:
    return SimpleNamespace(config=SimpleNamespace(data_dir=tmp_path / "data"))


def _progress() -> Any:
    return SimpleNamespace(check=lambda: None, cancel=threading.Event())


def test_the_shipped_pin_names_one_archive_by_full_commit_and_sha256() -> None:
    pin = portable_build.tabrion_pin()
    assert re.fullmatch(r"[0-9a-f]{40}", pin.commit) and re.fullmatch(r"[0-9a-f]{64}", pin.sha256)
    assert pin.url == f"https://github.com/peterkarlhuber/tabrion/archive/{pin.commit}.tar.gz"


def test_the_pin_is_the_commit_of_the_installed_tabrion() -> None:
    """The pin and the export's Git line (the installed tabrion, from the studio lock) name the same commit: a lock
    bump that moves tabrion must update portable_locks/tabrion.txt too."""
    if not exporter._runtime_needs_tabrion():
        pytest.skip("the installed nodestro-runtime does not require tabrion (before 9e0a73d)")
    try:
        line = exporter._tabrion_git()
    except RuntimeError:
        pytest.skip("tabrion is not installed from a GitHub commit here (CI installs it from the studio lock)")
    assert line is not None
    assert line.rpartition("@")[2] == portable_build.tabrion_pin().commit


def test_the_build_backend_lock_pins_every_package_by_hash() -> None:
    text = (portable_build.TABRION_LOCKS / "build.txt").read_text(encoding="utf-8")
    entries = re.split(r"\n(?=[A-Za-z])", "\n".join(line for line in text.splitlines() if not line.startswith("#")))
    entries = [entry for entry in entries if entry.strip()]
    assert any(entry.startswith("hatchling==") for entry in entries)
    for entry in entries:
        assert re.match(r"[A-Za-z0-9_.-]+==[^\s\\]+", entry), entry
        assert "--hash=sha256:" in entry, entry


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"tabrion @ {ARCHIVE}\n",  # no hash
        f"tabrion @ {ARCHIVE} --hash=sha256:{'a' * 63}\n",  # short hash
        f"tabrion @ {ARCHIVE.replace(COMMIT, COMMIT[:7])} --hash=sha256:{'a' * 64}\n",  # short commit
        f"tabrion @ https://example.com/tabrion/archive/{COMMIT}.tar.gz --hash=sha256:{'a' * 64}\n",  # other host
        f"tabrion @ {ARCHIVE} --hash=sha256:{'a' * 64}\ntabrion @ {ARCHIVE} --hash=sha256:{'b' * 64}\n",  # two
    ],
)
def test_a_malformed_pin_fails_the_build(text: str) -> None:
    with pytest.raises(NodestroError) as caught:
        portable_build.tabrion_pin(text)
    assert caught.value.code == "export.failed"


def test_a_continued_line_is_one_pin() -> None:
    pin = portable_build.tabrion_pin(f"# comment\ntabrion @ {ARCHIVE} \\\n    --hash=sha256:{'c' * 64}\n")
    assert (pin.commit, pin.sha256) == (COMMIT, "c" * 64)


def test_no_tabrion_for_an_older_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exporter, "_tabrion_git", lambda: None)
    assert (
        portable_build.install_tabrion(_ctx(tmp_path), tmp_path / "site", tmp_path, _progress(), allow_downloads=True)
        is None
    )
    assert not (tmp_path / "site").exists()


def test_a_pin_of_another_commit_than_the_installed_one_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "0" * 40
    monkeypatch.setattr(
        exporter, "_tabrion_git", lambda: f"tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@{other}"
    )
    with pytest.raises(NodestroError) as caught:
        portable_build.install_tabrion(_ctx(tmp_path), tmp_path / "site", tmp_path, _progress(), allow_downloads=True)
    assert caught.value.code == "export.failed" and "another Tabrion commit" in str(caught.value)


def _pinned(monkeypatch: pytest.MonkeyPatch, digest: str) -> None:
    line = f"tabrion @ git+https://github.com/peterkarlhuber/tabrion.git@{COMMIT}"
    monkeypatch.setattr(exporter, "_tabrion_git", lambda: line)
    monkeypatch.setattr(
        portable_build, "tabrion_pin", lambda: portable_build.TabrionPin(url=ARCHIVE, commit=COMMIT, sha256=digest)
    )


def test_a_cached_archive_with_another_hash_fails_the_build_and_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pinned(monkeypatch, hashlib.sha256(b"the real archive").hexdigest())
    cached = tmp_path / "data" / "export-cache" / "tabrion" / f"{COMMIT}.tar.gz"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"a tampered archive")
    with pytest.raises(NodestroError) as caught:
        portable_build.install_tabrion(_ctx(tmp_path), tmp_path / "site", tmp_path, _progress(), allow_downloads=False)
    assert caught.value.code == "export.failed" and "SHA-256" in str(caught.value)
    assert not cached.exists() and not (tmp_path / "site").exists()


def test_without_downloads_a_missing_archive_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _pinned(monkeypatch, "a" * 64)
    with pytest.raises(NodestroError) as caught:
        portable_build.install_tabrion(_ctx(tmp_path), tmp_path / "site", tmp_path, _progress(), allow_downloads=False)
    assert caught.value.code == "export.download_needed" and caught.value.params.get("what") == "tabrion"


@pytest.mark.skipif(
    os.environ.get("NODESTRO_NETWORK_TESTS") != "1", reason="set NODESTRO_NETWORK_TESTS=1 (GitHub, PyPI)"
)
def test_the_wheel_is_built_from_the_pinned_archive_and_imports(tmp_path: Path) -> None:
    """The real steps: download the archive, check its SHA-256, build the wheel with the hash-pinned hatchling
    (no build isolation), install it with --no-index into a target folder, import tabrion.errors from there."""
    if exporter._tabrion_git() is None:
        pytest.skip("the installed nodestro-runtime does not require tabrion")
    site = tmp_path / "site"
    work = tmp_path / "work"
    work.mkdir()
    pin = portable_build.install_tabrion(_ctx(tmp_path), site, work, _progress(), allow_downloads=True)
    assert pin is not None and pin == portable_build.tabrion_pin()
    assert (work / "tabrion-LICENSE").is_file()
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import tabrion, tabrion.errors; "
            "print(tabrion.__file__, tabrion.__version__)",
            str(site),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.startswith(str(site)) and " 0.1." in probe.stdout
    # The cached archive is reused offline, checked again.
    again = portable_build.install_tabrion(_ctx(tmp_path), tmp_path / "site2", work, _progress(), allow_downloads=False)
    assert again == pin
