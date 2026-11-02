"""The desktop app's secrets arrive through a pipe (``--secrets-fd``), never in the environment (review MS-1)."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Iterator
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
import pytest

from nodestro_api import cli, handoff, launch
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

SECRET = "a1" * 32
KEY = base64.b64encode(b"v" * 32).decode()
PAYLOAD = json.dumps({"launch_secret": SECRET, "vault_key": KEY}).encode()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    handoff.reset()
    monkeypatch.setattr(launch, "_taken", launch._UNSET)
    monkeypatch.delenv(launch.ENV_VAR, raising=False)
    monkeypatch.delenv(handoff.VAULT_KEY_ENV, raising=False)
    yield
    handoff.reset()


def pipe_with(payload: bytes) -> int:
    """The read end of a pipe that holds ``payload``; the write end is closed."""
    read_end, write_end = os.pipe()
    os.write(write_end, payload)
    os.close(write_end)
    return read_end


def closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_a_valid_handoff_is_read_and_the_descriptor_closed() -> None:
    fd = pipe_with(PAYLOAD)
    found = handoff.read(fd)
    assert (found.launch_secret, found.vault_key) == (SECRET, KEY)
    assert closed(fd)
    # Neither value shows in a representation (logs, tracebacks).
    assert SECRET not in repr(found) and KEY not in repr(found)


def test_missing_and_empty_values_are_none() -> None:
    assert handoff.parse(b'{"launch_secret": "' + SECRET.encode() + b'"}') == handoff.Handoff(SECRET, None)
    assert handoff.parse(b'{"launch_secret": "", "vault_key": null}') == handoff.Handoff(None, None)
    assert handoff.parse(b"{}") == handoff.Handoff()


@pytest.mark.parametrize(
    "payload",
    [b"", b"not json", b"[1]", b'"text"', b'{"launch_secret": 5}', b'{"other": "x"}', b"\xff\xfe"],
)
def test_a_malformed_handoff_is_refused_and_the_descriptor_still_closed(payload: bytes) -> None:
    fd = pipe_with(payload)
    with pytest.raises(NodestroError) as caught:
        handoff.read(fd)
    assert caught.value.code == "access.handoff_invalid"
    assert closed(fd)


def test_the_refusal_never_quotes_the_secrets() -> None:
    with pytest.raises(NodestroError) as caught:
        handoff.parse(json.dumps({"launch_secret": SECRET, "vault_key": KEY, "extra": SECRET}).encode())
    assert SECRET not in str(caught.value) and KEY not in str(caught.value)


def test_a_missing_or_unusable_descriptor_is_refused() -> None:
    fd = pipe_with(PAYLOAD)
    os.close(fd)
    for unusable in (fd, 1, 2, -1):
        with pytest.raises(NodestroError) as caught:
            handoff.read(unusable)
        assert caught.value.code == "access.handoff_invalid"


def test_an_oversized_handoff_is_refused(tmp_path: Path) -> None:
    big = tmp_path / "big"
    big.write_bytes(b" " * (handoff.MAX_BYTES + 1))
    fd = os.open(big, os.O_RDONLY)
    with pytest.raises(NodestroError, match="more than"):
        handoff.read(fd)
    assert closed(fd)


def test_the_handoff_replaces_the_environment_and_stays_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(launch.ENV_VAR, "e" * 40)
    monkeypatch.setenv(handoff.VAULT_KEY_ENV, base64.b64encode(b"e" * 32).decode())
    handoff.receive(pipe_with(PAYLOAD))
    assert launch.ENV_VAR not in os.environ and handoff.VAULT_KEY_ENV not in os.environ
    config = AppConfig.from_env()
    assert (config.launch_secret, config.vault_key) == (SECRET, KEY)
    assert SECRET not in repr(config) and KEY not in repr(config)
    assert all(SECRET not in value and KEY not in value for value in os.environ.values())
    with pytest.raises(NodestroError, match="already received"):
        handoff.receive(pipe_with(PAYLOAD))


def test_the_operators_vault_key_is_taken_out_of_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(handoff.VAULT_KEY_ENV, KEY)
    assert AppConfig.from_env().vault_key == KEY
    assert handoff.VAULT_KEY_ENV not in os.environ
    assert AppConfig.from_env().vault_key == KEY


def test_without_a_descriptor_nothing_is_read() -> None:
    assert handoff.receive(None) is None
    assert handoff.received() is None
    assert AppConfig.from_env().vault_key is None


def test_the_workspace_seals_tokens_with_the_handed_over_key(tmp_path: Path) -> None:
    app = create_app(AppConfig(data_dir=tmp_path / "data", vault_key=KEY), integrations=[], scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/mcp/status").json()["token_vault"] is True
    assert handoff.VAULT_KEY_ENV not in os.environ


def test_a_malformed_vault_key_stops_the_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handoff.receive(pipe_with(json.dumps({"launch_secret": SECRET, "vault_key": "short"}).encode()))
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    args = argparse.Namespace(host="127.0.0.1", port=1, reload=False)
    with pytest.raises(SystemExit, match="NODESTRO_VAULT_KEY"):
        cli.serve("nodestro_api.app:create_app", args)


def test_reload_is_refused_after_a_handoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handoff.receive(pipe_with(json.dumps({"vault_key": KEY}).encode()))
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    args = argparse.Namespace(host="127.0.0.1", port=1, reload=True)
    with pytest.raises(SystemExit, match="--secrets-fd"):
        cli.serve("nodestro_api.app:create_app", args)


def test_the_command_line_stops_on_a_bad_handoff(tmp_path: Path) -> None:
    args = cli.parser("nodestro-api", "").parse_args(["--secrets-fd", str(pipe_with(b"nope"))])
    with pytest.raises(SystemExit, match="not a JSON object"):
        cli.prepare(args)


# The backend as the desktop app starts it, in its own process: the secrets on a pipe, then a child process.
_BACKEND = textwrap.dedent(
    """
    import json, os, subprocess, sys

    from nodestro_api import cli
    from nodestro_api.context import AppConfig

    fd = sys.argv[1]
    args = cli.parser("nodestro", "").parse_args(["--secrets-fd", fd, "--data-dir", sys.argv[2], "--port", "1"])
    cli.prepare(args)
    config = AppConfig.from_env()
    own = {}
    for name in ("environ", "cmdline"):
        path = f"/proc/self/{name}"
        own[name] = open(path, "rb").read().decode("latin-1") if os.path.exists(path) else ""
    child = subprocess.run(
        [sys.executable, "-c", sys.argv[3], fd],
        close_fds=False,
        capture_output=True,
        text=True,
        check=True,
    )
    print(json.dumps({
        "launch_secret": config.launch_secret,
        "vault_key": config.vault_key,
        "environ": dict(os.environ),
        "own": own,
        "child": json.loads(child.stdout),
    }))
    """
)

# What a process the backend starts can see: its environment, the handoff descriptor, its standard input.
_CHILD = textwrap.dedent(
    """
    import json, os, sys

    fd = int(sys.argv[1])
    try:
        os.fstat(fd)
        inherited = fd != 0
    except OSError:
        inherited = False
    stdin = os.read(0, 4096).decode("latin-1") if fd == 0 else ""
    print(json.dumps({"environ": dict(os.environ), "inherited": inherited, "stdin": stdin}))
    """
)


def _run_backend(tmp_path: Path, fd: str, **popen: object) -> dict[str, object]:
    env = {name: value for name, value in os.environ.items() if name not in (launch.ENV_VAR, handoff.VAULT_KEY_ENV)}
    process = subprocess.Popen(
        [sys.executable, "-c", _BACKEND, fd, str(tmp_path / "data"), _CHILD],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen,  # type: ignore[arg-type]
    )
    # communicate() writes the payload and closes the pipe (Python 3.10 cannot flush a pipe closed by hand).
    out, err = process.communicate(PAYLOAD if process.stdin is not None else None, timeout=120)
    assert process.returncode == 0, err.decode()
    return json.loads(out)  # type: ignore[no-any-return]


def _assert_secrets_only_in_memory(found: dict[str, object]) -> None:
    assert (found["launch_secret"], found["vault_key"]) == (SECRET, KEY)
    child = found["child"]
    assert isinstance(child, dict)
    assert child["inherited"] is False
    for seen in (found["environ"], found["own"], child["environ"], child["stdin"]):
        text = json.dumps(seen)
        assert SECRET not in text and KEY not in text


def test_the_backend_and_its_children_never_see_the_secrets_on_a_descriptor(tmp_path: Path) -> None:
    fd = pipe_with(PAYLOAD)
    try:
        found = _run_backend(tmp_path, str(fd), pass_fds=(fd,))
    finally:
        os.close(fd)
    _assert_secrets_only_in_memory(found)


def test_the_backend_and_its_children_never_see_the_secrets_on_standard_input(tmp_path: Path) -> None:
    # As the desktop app starts it (``--secrets-fd 0``): the child's standard input is the null device.
    found = _run_backend(tmp_path, "0", stdin=subprocess.PIPE)
    _assert_secrets_only_in_memory(found)
    child = found["child"]
    assert isinstance(child, dict) and child["stdin"] == ""
