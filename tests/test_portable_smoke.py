"""A real portable package on the computer the test runs on: build it (Python, libraries, launcher; no local models),
unpack it, run its self-test, start it, sign in to the web interface, run the workflow through the API, stop it and
start it again. Slow and needs the network (uv downloads Python and wheels), so it runs only with
NODESTRO_PORTABLE_SMOKE=1 (CI runs it on Linux, Windows and macOS: .github/workflows/portable.yml)."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

from fastapi.testclient import TestClient
import pytest

from test_flow import edge, save

pytestmark = pytest.mark.skipif(
    os.environ.get("NODESTRO_PORTABLE_SMOKE") != "1", reason="builds a real package (NODESTRO_PORTABLE_SMOKE=1)"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _get(url: str, token: str | None = None, timeout: float = 10) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _post(url: str, body: dict[str, object], token: str) -> tuple[int, dict[str, object]]:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _wait_for(check: Callable[[], bool], seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):  # not listening yet
            if check():
                return
        time.sleep(1)
    raise AssertionError(f"{what} did not happen within {seconds:.0f} s")


def test_a_portable_package_runs_on_this_computer(make_app: Callable[..., TestClient], tmp_path: Path) -> None:
    from nodestro_api.portable_build import host_platform

    studio = make_app({})
    workflow_id = save(
        studio,
        [
            {
                "id": "start",
                "type": "start",
                "start": {"fields": [{"name": "topic", "label": "Topic", "type": "text", "required": True}]},
            },
            {
                "id": "route",
                "type": "condition",
                "condition": {"rules": [{"id": "ask", "field": "topic", "operator": "equals", "value": "ask"}]},
            },
            {"id": "agent", "type": "agent", "label": "Agent", "agent": {"name": "Agent", "system_prompt": "Answer"}},
            {"id": "out", "type": "output"},
        ],
        # The agent is never reached in this test (no provider key on the runner): "Smoke" takes the default branch.
        [edge("start", "route"), edge("route", "agent", "ask"), edge("route", "out", "default"), edge("agent", "out")],
        name="Portable smoke",
        budget_usd=1.0,
    )
    published = studio.post(f"/api/workflows/{workflow_id}/api/publish", json={})
    assert published.status_code == 201, published.text
    publication = published.json()["publication"]
    options = {
        "kind": "portable",
        "web_ui": True,
        "language": "de",
        "platform": host_platform(),
        "confirm_distribution": True,
        "output_dir": "ergebnisse",
    }
    checked = studio.post(f"/api/publications/{publication['id']}/export/check", json={"options": options})
    assert checked.status_code == 200, checked.text
    check = checked.json()
    assert check["ok"], check["problems"]
    assert not check["portable"]["local_models"]
    started = studio.post(f"/api/publications/{publication['id']}/export-jobs", json={"options": options})
    assert started.status_code == 202, started.text
    job_id = started.json()["id"]
    job: dict[str, object] = {}

    def finished() -> bool:
        job.update(studio.get(f"/api/export-jobs/{job_id}").json())
        return job["state"] != "running"

    _wait_for(finished, 1500, "the build")
    assert job["state"] == "done", job
    archive = studio.get(f"/api/exports/{job['export_id']}/download")
    assert archive.status_code == 200
    target = tmp_path / "zip" / "package.zip"
    target.parent.mkdir()
    target.write_bytes(archive.content)
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(target) as package:
        root = package.namelist()[0].split("/")[0]
        package.extractall(unpacked)
        modes = {info.filename: info.external_attr >> 16 for info in package.infolist()}
    folder = unpacked / root
    windows = sys.platform == "win32"
    if not windows:  # ZipFile.extractall keeps neither modes nor symbolic links: restore them as unzip would
        for name, mode in modes.items():
            path = unpacked / name
            if stat.S_ISLNK(mode):
                link = path.read_text(encoding="utf-8")
                path.unlink()
                path.symlink_to(link)
            elif mode and path.is_file():
                path.chmod(mode & 0o777)
    for name in ("README.md", "openapi.json", "SHA256SUMS", "PORTABLE.json", "config/settings.toml"):
        assert (folder / name).is_file(), name
    starter = [str(folder / "start.cmd")] if windows else [str(folder / "start.sh")]
    # The package reads nothing of the studio's environment (its data folder, its keys) and of the test's Python.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "VIRTUAL_ENV", "NODESTRO_", "ANTHROPIC_", "OPENAI_"))
    }
    # A provider key from the environment (the environment wins over the settings page). It is never used: the
    # agent sits on a branch the test does not take, but a run only starts when every provider has a key.
    environment["ANTHROPIC_API_KEY"] = "sk-ant-portable-smoke-not-a-real-key"
    port = _free_port()
    environment["NODESTRO_PORT"] = str(port)

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*starter, *args], cwd=folder, env=environment, capture_output=True, text=True, timeout=600, check=False
        )

    test = run("check")
    assert test.returncode == 0, test.stdout + test.stderr
    created = run("keys", "new", "--name", "smoke", "--scopes", "run,read")
    assert created.returncode == 0, created.stderr
    token = created.stdout.splitlines()[1].strip()
    base = f"http://127.0.0.1:{port}"
    slug = publication["slug"]
    for attempt in (1, 2):  # start, run, stop; then the same again: data survives the restart
        log = (tmp_path / f"start{attempt}.log").open("w")
        process = subprocess.Popen(
            [*starter, "start", "--no-browser"], cwd=folder, env=environment, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            _wait_for(lambda: _get(f"{base}/health", timeout=2)[0] == 200, 180, "the start")
            status, page = _get(f"{base}/")
            assert status == 200 and b"/ui/assets/app.js" in page
            assert _get(f"{base}/ui/assets/i18n/pt-BR.json")[0] == 200
            assert _get(f"{base}/api/published/{slug}/runs")[0] == 401
            code, run_answer = _post(f"{base}/api/published/{slug}/runs", {"values": {"topic": "Smoke"}}, token)
            assert code == 202, run_answer
            run_id = str(run_answer["run_id"])

            def done(run_id: str = run_id) -> bool:
                body = json.loads(_get(f"{base}/api/published/{slug}/runs/{run_id}", token)[1])
                return bool(body["status"] in ("succeeded", "failed", "cancelled"))

            _wait_for(done, 120, "the run")
            runs = json.loads(_get(f"{base}/api/published/{slug}/runs", token)[1])
            assert len(runs) == attempt and all(item["status"] == "succeeded" for item in runs), runs
        finally:
            stopped = run("stop")
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                stuck = f"the application did not stop: {stopped.stdout}{stopped.stderr}"
            else:
                stuck = ""
            log.close()  # an exception from the try block above is not replaced by one from here
        assert not stuck, stuck
        assert not _port_open(port), "the port is still in use after stop"


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0
