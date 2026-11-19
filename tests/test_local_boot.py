"""Nodestro Local Docker's key file (LD-020): ``nodestro-local boot`` reads it as root, drops to the studio's user and
hands the key over through a pipe; ``nodestro-local serve`` refuses to start without it or as root.

The end-to-end tests of nodestro-local-docker run the same path in the real container (rootful and rootless Docker).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from nodestro_runtime.problems import NodestroError
import pytest

from nodestro_api import cli, handoff, local_boot, local_container
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

KEY = "k" * 43
BROWSER_HOST = "nodestro-0123456789.localhost"


def new_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture(autouse=True)
def fresh_handoff(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("NODESTRO_VAULT_KEY", raising=False)
    handoff.reset()
    yield
    handoff.reset()


# ---- the key file -------------------------------------------------------------------------------------------------


def test_a_key_file_is_read(tmp_path: Path) -> None:
    key = new_key()
    (tmp_path / "vault.key").write_text(key + "\n")
    assert local_boot.read_key_file(tmp_path / "vault.key") == key


@pytest.mark.parametrize(
    ("content", "reason"),
    [(b"", "does not hold a key"), (b"short", "does not hold a key"), (b"\xff" * 44, "does not hold a key")],
)
def test_a_file_without_a_key_is_refused_without_quoting_it(tmp_path: Path, content: bytes, reason: str) -> None:
    (tmp_path / "vault.key").write_bytes(content)
    with pytest.raises(NodestroError, match=reason) as refused:
        local_boot.read_key_file(tmp_path / "vault.key")
    assert refused.value.code == "secrets.vault_key_file"
    if content:
        assert content.decode("latin-1") not in str(refused.value)


def test_links_directories_large_and_missing_files_are_refused(tmp_path: Path) -> None:
    key = tmp_path / "vault.key"
    key.write_text(new_key())
    (tmp_path / "link").symlink_to(key)
    (tmp_path / "large").write_text("A" * (local_boot.MAX_KEY_BYTES + 1))
    (tmp_path / "dir").mkdir()
    for name, reason in (
        ("link", "cannot be read"),
        ("large", "too large"),
        ("dir", "not a regular file"),
        ("missing", "launcher"),
    ):
        with pytest.raises(NodestroError, match=reason):
            local_boot.read_key_file(tmp_path / name)


# ---- dropping root ------------------------------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a user other than root")
def test_boot_refuses_to_run_without_root() -> None:
    with pytest.raises(NodestroError) as refused:
        local_boot.drop_privileges()
    assert refused.value.code == "secrets.boot_not_root"


@pytest.mark.skipif(not sys.platform.startswith("linux") or os.geteuid() != 0, reason="needs root on Linux")
def test_root_is_dropped_for_good() -> None:
    # In a child process: this one keeps running as root for the other tests.
    script = (
        "import os, json; from nodestro_api import local_boot; local_boot.drop_privileges(); "
        "status = dict(l.split(':', 1) for l in open('/proc/self/status').read().splitlines() if ':' in l); "
        "print(json.dumps({'ids': [os.getresuid(), os.getresgid()], 'groups': os.getgroups(), "
        "'caps': {n: int(status[n].strip(), 16) for n in ('CapPrm', 'CapEff', 'CapAmb', 'CapInh')}}))"
    )
    done = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True)
    found = json.loads(done.stdout)
    assert found["ids"] == [[10001] * 3, [10001] * 3] and found["groups"] == []
    assert found["caps"] == {"CapPrm": 0, "CapEff": 0, "CapAmb": 0, "CapInh": 0}


def test_serve_gets_the_key_through_a_pipe_never_the_environment_or_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = new_key()
    started: list[list[str]] = []

    def execv(path: str, argv: list[str]) -> None:
        started.append(argv)

    monkeypatch.setattr(local_boot.os, "execv", execv)
    local_boot._serve(key)
    (argv,) = started
    assert key not in " ".join(argv) and key not in json.dumps(dict(os.environ))
    assert argv[-3:-1] == ["serve", "--secrets-fd"]
    if "tini" in argv[0]:
        assert argv[1] == "--"
    fd = int(argv[-1])
    assert os.get_inheritable(fd)
    received = handoff.read(fd)
    assert received.vault_key == key and received.launch_secret is None


# ---- serve refuses to run without the key -------------------------------------------------------------------------


def _local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    access = tmp_path / "secrets" / "access-key"
    access.parent.mkdir()
    local_container.create_access_key(access)
    local_container.create_browser_host(access.parent / local_container.BROWSER_HOST_FILE)
    monkeypatch.setenv(local_container.ENV_KEY_FILE, str(access))
    monkeypatch.delenv(local_container.ENV_BROWSER_HOST, raising=False)
    monkeypatch.setenv("NODESTRO_PROFILE", "local-container")
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))


def test_serve_refuses_to_start_without_the_handed_over_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _local_env(tmp_path, monkeypatch)
    # An operator's variable does not replace the key file.
    monkeypatch.setenv("NODESTRO_VAULT_KEY", new_key())
    with pytest.raises(SystemExit, match="key file on the host"):
        cli.serve("nodestro_api.app:create_app", argparse.Namespace(host="0.0.0.0", port=1, reload=False))
    assert not (tmp_path / "data" / "nodestro.db").exists()


def test_serve_refuses_to_run_as_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _local_env(tmp_path, monkeypatch)
    read, write = os.pipe()
    os.write(write, json.dumps({"vault_key": new_key()}).encode())
    os.close(write)
    handoff.receive(read)
    monkeypatch.setattr(local_container.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(SystemExit, match="does not run as root"):
        cli.serve("nodestro_api.app:create_app", argparse.Namespace(host="0.0.0.0", port=1, reload=False))


def test_the_local_container_refuses_a_key_that_does_not_fit(tmp_path: Path) -> None:
    def config(key: str) -> AppConfig:
        return AppConfig(
            data_dir=tmp_path / "data",
            host="0.0.0.0",
            profile_kind="local-container",
            access_key=KEY,
            browser_host=BROWSER_HOST,
            host_port=8765,
            vault_key=key,
        )

    first = new_key()
    create_app(config(first), integrations=[], scheduler=False).state.nodestro.close()
    with pytest.raises(NodestroError) as refused:
        create_app(config(new_key()), integrations=[], scheduler=False)
    assert refused.value.code == "secrets.vault_key_mismatch"
    create_app(config(first), integrations=[], scheduler=False).state.nodestro.close()


def test_a_restore_keeps_the_current_key_check() -> None:
    from nodestro_api import backup

    # A backup from before a key rotation must not bring back the old key's check value.
    assert "local_vault" in backup._LOCAL_COLLECTIONS
