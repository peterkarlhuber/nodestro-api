"""A real portable package on the computer the test runs on: build it (Python, libraries, launcher; no local models),
unpack it, run its self-test, start it, sign in to the web interface, run the workflow through the API, stop it and
start it again. Slow and needs the network (uv downloads Python and wheels), so it runs only with
NODESTRO_PORTABLE_SMOKE=1 (CI runs it on Linux, Windows and macOS: .github/workflows/portable.yml).

The connection proof (NAL-A2): the workflow's agent uses an OpenAI-compatible model served by a tiny fake endpoint on
127.0.0.1 (:class:`_FakeModel`) and the built-in tool ``search_web`` bound to a ``search`` connection with the
deterministic ``test`` provider; no outside service is contacted. The studio creates the connection and sets its
secret through its API; the package gets the connection's settings from ``<DATA_DIR>/secrets/connections.json`` (what
``configure`` writes) and its secret from ``NODESTRO_CONNECTION_<ID>_SECRET``, and refuses runs without it
(``connection.missing``). Neither secret may appear in an answer, an event, a log, a backup or the package."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.request
import zipfile

from fastapi.testclient import TestClient
import pytest

from test_flow import edge, save, wait

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


#: The connection secrets of the proof: one set through the studio's API, one through the package's environment.
STUDIO_SECRET = "studio-search-secret-7f3a9c"
PACKAGE_SECRET = "package-search-secret-4d81e2"
#: What the fake model asks the search provider.
QUERY = "portable proof"


class _FakeModel:
    """A tiny OpenAI-compatible chat completions endpoint on 127.0.0.1 (standard library only, so it runs the same on
    every platform). A conversation whose last message is not a tool result gets a call of ``search_web``; one that
    ends with a tool result gets the final text. Every request body is kept in :attr:`requests`."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # quiet test output
                return

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                with owner._lock:
                    owner.requests.append(body)
                if not self.path.endswith("/chat/completions"):
                    self.send_error(404)
                    return
                payload = owner.answer(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-model", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> _FakeModel:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    @staticmethod
    def answer(body: dict[str, Any]) -> bytes:
        messages = body.get("messages") or [{}]
        if messages[-1].get("role") == "tool":
            message: dict[str, Any] = {"role": "assistant", "content": "The search found what was asked for."}
            finish = "stop"
        else:
            call = {"name": "search_web", "arguments": json.dumps({"query": QUERY})}
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function"}]}
            message["tool_calls"][0]["function"] = call
            finish = "tool_calls"
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        base = {"id": "chatcmpl-proof", "created": 0, "model": body.get("model", "")}
        if not body.get("stream"):
            choice = {"index": 0, "message": message, "finish_reason": finish}
            return json.dumps({**base, "object": "chat.completion", "choices": [choice], "usage": usage}).encode()
        delta = dict(message)
        if "tool_calls" in delta:
            delta["tool_calls"] = [{**delta["tool_calls"][0], "index": 0}]
        chunks = [
            {"index": 0, "delta": delta, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": finish},
        ]
        events = [{**base, "object": "chat.completion.chunk", "choices": [chunk]} for chunk in chunks]
        events.append({**base, "object": "chat.completion.chunk", "choices": [], "usage": usage})
        return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode() + b"data: [DONE]\n\n"

    def tool_results(self) -> list[str]:
        """The tool results the model was sent, oldest first."""
        with self._lock:
            requests = list(self.requests)
        found: list[str] = []
        for body in requests:
            last = (body.get("messages") or [{}])[-1]
            if last.get("role") == "tool":
                found.append(str(last.get("content") or ""))
        return found


def _no_secret(secret: str, texts: dict[str, str | bytes]) -> None:
    for where, text in texts.items():
        found = secret.encode() in text if isinstance(text, bytes) else secret in text
        assert not found, f"the connection secret appears in {where}"


def _zip_members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {f"backup {info.filename}": archive.read(info) for info in archive.infolist() if not info.is_dir()}


def _studio_connection_proof(studio: TestClient, model: _FakeModel, workflow_id: str, connection_id: str) -> None:
    """The studio: without a secret the run starts and ``search_web`` reports ``connection.secret_missing`` to the
    model (the studio has no start check for connections); with the secret set through the API the tool calls the
    test provider and the run's source ledger holds its hits. The secret is in no answer, event, export or backup."""
    started = studio.post(f"/api/workflows/{workflow_id}/runs", json={"values": {"topic": "ask"}})
    assert started.status_code == 201, started.text
    unset = wait(studio, started.json()["id"])
    assert unset["status"] == "succeeded", unset.get("error")
    assert unset["sources"] == []
    assert "connection.secret_missing" in model.tool_results()[-1]
    stored = studio.put(f"/api/connections/{connection_id}/secret", json={"secret": STUDIO_SECRET})
    assert stored.status_code == 200 and stored.json()["secret_set"] is True, stored.text
    started = studio.post(f"/api/workflows/{workflow_id}/runs", json={"values": {"topic": "ask"}})
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    done = wait(studio, run_id)
    assert done["status"] == "succeeded", done.get("error")
    result = model.tool_results()[-1]
    assert "Provider: test" in result and QUERY in result, result
    assert done["sources"] and all(item["provider"] == "test" for item in done["sources"]), done["sources"]
    seen: dict[str, str | bytes] = {
        "the run": studio.get(f"/api/runs/{run_id}").text,
        "the run events": studio.get(f"/api/runs/{run_id}/events").text,
        "the run export": studio.get(f"/api/runs/{run_id}/export").text,
        "the workflow export": studio.get(f"/api/workflows/{workflow_id}/export").text,
        "the connection list": studio.get("/api/connections").text,
        "the connection": studio.get(f"/api/connections/{connection_id}").text,
        "the connection status": studio.get("/api/connections/status").text,
        "the connection events": studio.get("/api/connections/events").text,
        "the secret answer": stored.text,
    }
    backup = studio.get("/api/backup")
    assert backup.status_code == 200, backup.text
    seen.update(_zip_members(backup.content))
    _no_secret(STUDIO_SECRET, seen)


def test_a_portable_package_runs_on_this_computer(make_app: Callable[..., TestClient], tmp_path: Path) -> None:
    with _FakeModel() as model:
        _build_and_run(make_app, tmp_path, model)


def _build_and_run(make_app: Callable[..., TestClient], tmp_path: Path, model: _FakeModel) -> None:
    from nodestro_runtime.providers import endpoint_client

    from nodestro_api import portable, portable_connections
    from nodestro_api.portable_build import host_platform

    # The studio's model client talks to the fake endpoint (the package reaches it through COMPATIBLE_BASE_URL).
    studio = make_app(client=endpoint_client(model.url, None))
    capabilities = {"tools": "supported", "streaming": "supported"}
    configured = studio.put(
        "/api/settings", json={"providers": {"compatible": {"base_url": model.url, "capabilities": capabilities}}}
    )
    assert configured.status_code == 200, configured.text
    created = studio.post("/api/connections", json={"type": "search", "provider": "test", "name": "Proof search"})
    assert created.status_code == 201, created.text
    connection_id = str(created.json()["id"])
    agent = {
        "name": "Agent",
        "system_prompt": "Answer",
        "model": "compatible:proof-model",
        "effort": None,  # nothing is declared about reasoning for the fake endpoint
        "tools": ["search_web"],
        "tool_settings": {"search_web": {"connection_id": connection_id}},
    }
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
            {"id": "agent", "type": "agent", "label": "Agent", "agent": agent},
            {"id": "out", "type": "output"},
        ],
        # "Smoke" takes the default branch without a model; "ask" reaches the agent and its search_web call.
        [edge("start", "route"), edge("route", "agent", "ask"), edge("route", "out", "default"), edge("agent", "out")],
        name="Portable smoke",
        budget_usd=1.0,
    )
    _studio_connection_proof(studio, model, workflow_id, connection_id)
    published = studio.post(f"/api/workflows/{workflow_id}/api/publish", json={})
    assert published.status_code == 201, published.text
    publication = published.json()["publication"]
    options = {
        "kind": "portable",
        "web_ui": True,
        "language": "de",
        # CI can build another platform than the host Python's (Windows ARM64 from an emulated x64 Python).
        "platform": os.environ.get("NODESTRO_PORTABLE_PLATFORM") or host_platform(),
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
    assert job["state"] == "done", f"{job.get('error_code')}: {job.get('error')} (step {job.get('step')})"
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
    # TABRION MIG-NOD-3: with a runtime that requires tabrion, it was built from the pinned, hash-checked archive.
    from nodestro_api import exporter
    from nodestro_api.portable_build import tabrion_pin

    portable = json.loads((folder / "PORTABLE.json").read_text(encoding="utf-8"))
    if exporter._runtime_needs_tabrion():
        pin = tabrion_pin()
        assert portable["tabrion"] == {"commit": pin.commit, "archive_sha256": pin.sha256}
        assert (folder / "licenses" / "tabrion.txt").is_file()
    else:
        assert portable["tabrion"] is None
    starter = [str(folder / "start.cmd")] if windows else [str(folder / "start.sh")]
    # The libraries of the web tools (orkexa's extra "web") are part of the package.
    for library in ("trafilatura", "charset_normalizer", "lxml", "httpx"):
        assert next(folder.rglob(f"{library}/__init__.py"), None) is not None, f"{library} is not in the package"
    _no_secret(STUDIO_SECRET, {"the package": archive.content})
    # The package reads nothing of the studio's environment (its data folder, its keys) and of the test's Python.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "VIRTUAL_ENV", "NODESTRO_", "ANTHROPIC_", "OPENAI_", "COMPATIBLE_"))
    }
    # A provider key from the environment (the environment wins over the settings page). It is never used (the
    # agent's model is the fake endpoint's), but a run only starts when every provider of the release has a key.
    environment["ANTHROPIC_API_KEY"] = "sk-ant-portable-smoke-not-a-real-key"
    # The OpenAI-compatible endpoint of the package's environment wins over the exported base URL.
    environment["COMPATIBLE_BASE_URL"] = model.url
    port = _free_port()
    environment["NODESTRO_PORT"] = str(port)
    # The connection as "configure" stores it, without its secret: that comes from the environment (and wins).
    data_dir = portable.Settings(folder).data_dir
    entry = {"type": "search", "provider": "test", "name": "Proof search", "settings": {}}
    portable_connections.save(data_dir, {connection_id: entry})
    secret_variable = portable_connections.secret_variable(connection_id)
    environment[secret_variable] = PACKAGE_SECRET

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
    started: list[str] = []
    answers: dict[str, str | bytes] = {}
    # Start, run (the smoke run and the agent's search), stop; then start again without the connection's secret:
    # the runs survive the restart, and a new run is refused because the connection is missing.
    for attempt in (1, 2):
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
            if attempt == 1:
                for topic in ("Smoke", "ask"):
                    code, run_answer = _post(f"{base}/api/published/{slug}/runs", {"values": {"topic": topic}}, token)
                    assert code == 202, run_answer
                    started.append(str(run_answer["run_id"]))
                ask_run = started[1]

                def done(started: list[str] = started) -> bool:
                    for run_id in started:
                        body = json.loads(_get(f"{base}/api/published/{slug}/runs/{run_id}", token)[1])
                        if body["status"] not in ("succeeded", "failed", "cancelled"):
                            return False
                    return True

                _wait_for(done, 120, "the runs")
                result = model.tool_results()[-1]
                assert "Provider: test" in result and QUERY in result, result
                answers["the run"] = _get(f"{base}/api/published/{slug}/runs/{ask_run}", token)[1]
                answers["the run result"] = _get(f"{base}/api/published/{slug}/runs/{ask_run}/result", token)[1]
                events = _get(f"{base}/api/published/{slug}/runs/{ask_run}/events?text=true", token, timeout=30)[1]
                assert b"event: end" in events, events[-500:]
                answers["the run events"] = events
            else:
                code, refused = _post(f"{base}/api/published/{slug}/runs", {"values": {"topic": "Smoke"}}, token)
                error = refused.get("error")
                assert code == 503 and isinstance(error, dict), (code, refused)
                assert error["code"] == "connection.missing" and error["params"] == {"count": 1}, refused
                answers["the refusal"] = json.dumps(refused)
            runs = json.loads(_get(f"{base}/api/published/{slug}/runs", token)[1])
            assert len(runs) == 2 and all(item["status"] == "succeeded" for item in runs), runs
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
        environment.pop(secret_variable, None)  # the second start has the connection's settings, but no secret
    # The agent's run recorded the test provider's hits in its source ledger (the published API does not show it).
    from nodestro_runtime.schemas import Run
    from nodestro_runtime.storage import Store

    store = Store(data_dir / "runtime.db")
    try:
        stored = store.get("runs", started[1], Run)
    finally:
        store.close()
    assert stored is not None and stored.status == "succeeded", stored
    assert stored.sources and all(item.provider == "test" for item in stored.sources), stored.sources
    # Neither the answers, the events, the logs nor anything the package wrote to its data folder holds the secret.
    for attempt in (1, 2):
        answers[f"start log {attempt}"] = (tmp_path / f"start{attempt}.log").read_bytes()
    for path in data_dir.rglob("*"):
        if path.is_file():
            answers[str(path.relative_to(folder))] = path.read_bytes()
    _no_secret(PACKAGE_SECRET, answers)


def _port_open(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0
