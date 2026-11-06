"""The API of local models (Ollama) and of the semantic knowledge index."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
import textwrap
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

MakeApp = Callable[..., TestClient]

FAKE_OLLAMA = textwrap.dedent(
    """\
    #!{python}
    import http.server, json, os
    host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
    TAGS = {{"models": [
        {{"name": "qwen3.5:4b", "model": "qwen3.5:4b", "size": 3300000000,
          "digest": "d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04", "details": {{}}}}]}}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def reply(self, body, status=200):
            data = json.dumps(body).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            if self.path == "/api/version": self.reply({{"version": "0.40.14"}})
            elif self.path == "/api/tags": self.reply(TAGS)
            else: self.reply({{"error": "not found"}}, 404)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{{}}")
            if self.path == "/api/show" and body.get("model") == "qwen3.5:4b":
                self.reply({{"capabilities": ["completion", "tools", "vision"], "model_info": {{}}}})
            elif self.path == "/api/generate" and body.get("model") == "qwen3.5:4b":
                self.reply({{"eval_count": 50, "eval_duration": 5000000000}})
            else: self.reply({{"error": "model not found"}}, 404)
        def do_DELETE(self):
            self.reply({{}})
    http.server.ThreadingHTTPServer((host, int(port)), H).serve_forever()
    """
)


def _ctx(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


def test_the_overview_works_without_a_runtime(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NODESTRO_OLLAMA_BINARY", raising=False)
    client = make_app(local_defaults=True)
    overview = client.get("/api/local-models").json()
    assert overview["manageable"] is True
    assert overview["status"]["mode"] == "managed" and overview["status"]["runtime_installed"] is False
    assert overview["status"]["running"] is False and overview["status"]["pinned_version"] == "0.40.14"
    assert [entry["name"] for entry in overview["catalog"]][:2] == ["qwen3.5:4b", "qwen3.5:9b"]
    assert all(entry["installed"] is False for entry in overview["catalog"])
    built = next(entry for entry in overview["catalog"] if entry["name"] == "mdenseon:307m")
    assert [item["role"] for item in built["build_inputs"]] == ["weights", "template"]
    assert all(len(item["sha256"]) == 64 and item["url"].startswith("https://") for item in built["build_inputs"])
    assert all(entry["build_inputs"] == [] for entry in overview["catalog"] if entry["name"] != "mdenseon:307m")
    assert all(isinstance(entry["fits"], bool) for entry in overview["catalog"])
    tasks = {item["task"]: item for item in overview["recommendations"]}
    assert tasks["embedding"]["model"] == "ollama:qwen3-embedding:0.6b"
    assert overview["hardware"]["cpus"] >= 1
    recommended = client.get("/api/local-models/recommend", params={"task": "classify"}).json()
    assert recommended["model"] == "ollama:qwen3.5:4b"
    assert client.get("/api/local-models/recommend", params={"task": "dance"}).status_code == 400
    refused = client.post("/api/local-models/runtime", json={})
    assert refused.status_code == 400 and refused.json()["code"] == "local.archive_or_download"
    missing = client.post("/api/local-models/runtime", json={"archive": "/no/such/ollama.tgz"})
    assert missing.status_code == 400 and missing.json()["code"] == "local.file_missing"
    # The settings show the new defaults of a new installation.
    settings = client.get("/api/settings").json()
    assert settings["default_model"] == "ollama:qwen3.5:4b" and settings["assistant_model"] == "ollama:qwen3.5:9b"
    assert settings["model_defaults"] == "local"


def test_downloads_are_refused_offline(make_app: MakeApp) -> None:
    client = make_app()
    assert client.put("/api/settings", json={"strict_offline": True}).json()["strict_offline"] is True
    refused = client.post("/api/local-models/runtime", json={"download": True})
    assert refused.status_code == 400 and refused.json()["code"] == "local.offline_download"
    # mDenseOn is built from its pinned upstream files: its download is refused too, an offline build names both files.
    built = client.post("/api/local-models/pull", json={"model": "mdenseon:307m"})
    assert built.status_code == 400 and built.json()["code"] == "local.offline_download"
    partial = client.post("/api/local-models/import", json={"kind": "build", "path": "/x", "name": "mdenseon:307m"})
    assert partial.status_code == 400 and partial.json()["code"] == "local.name_missing"
    missing = client.post(
        "/api/local-models/import",
        json={"kind": "build", "path": "/no/weights", "template": "/no/template", "name": "mdenseon:307m"},
    )
    assert missing.status_code == 400 and missing.json()["code"] == "local.file_missing"


def test_cloud_assistants_and_price_checks_are_refused_offline(make_app: MakeApp) -> None:
    """The strict offline mode refuses every assistant call with a cloud model before a request goes out, not only
    workflow runs: assistant, Workflow Architect, Agent Architect, tool generation; and the manual price check."""
    client = make_app()  # cloud defaults (the scripted client answers Anthropic)
    conversation = client.post("/api/assistant/conversations", json={}).json()["id"]
    assert client.put("/api/settings", json={"strict_offline": True}).json()["strict_offline"] is True
    refused = [
        client.post(f"/api/assistant/conversations/{conversation}/messages", json={"content": "Hi"}),
        client.post("/api/architect/jobs", json={"description": "A blog post", "language": "en"}),
        client.post("/api/agents/design", json={"description": "Summarizes text"}),
        client.post("/api/tools/jobs", json={"requirement": "Add two numbers"}),
    ]
    for response in refused:
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "check.offline_cloud_model", response.text
        assert response.json()["params"]["model"].startswith("anthropic:")
    assert not client.fake.messages.calls  # type: ignore[attr-defined]
    prices = client.post("/api/pricing/sync")
    assert prices.status_code == 400 and prices.json()["code"] == "pricing.offline_sync"


@pytest.fixture
def managed(make_app: MakeApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    if sys.platform == "win32":
        pytest.skip("the fake Ollama binary is a shebang script")
    binary = tmp_path / "bin" / "ollama"
    binary.parent.mkdir()
    binary.write_text(FAKE_OLLAMA.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("NODESTRO_OLLAMA_BINARY", str(binary))
    return make_app()


def test_the_managed_server_starts_lists_and_stops(managed: TestClient) -> None:
    deadline = time.monotonic() + 15  # the application starts it in the background
    while not managed.get("/api/local-models").json()["status"]["running"]:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    overview = managed.get("/api/local-models").json()
    assert overview["status"]["runtime_bundled"] is True and overview["status"]["base_url"].startswith("http://127.0")
    models = overview["status"]["models"]
    assert models == [
        {
            "name": "qwen3.5:4b",
            "size": 3300000000,
            "digest": "d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04",
            "kind": "chat",
            "capabilities": ["completion", "tools", "vision"],
            "context_length": None,
            "embedding_length": None,
            "catalog": True,
            "verified": True,
            "speed": None,
        }
    ]
    catalog = {entry["name"]: entry for entry in overview["catalog"]}
    assert catalog["qwen3.5:4b"]["installed"] and catalog["qwen3.5:4b"]["verified"]
    providers = {item["id"]: item for item in managed.get("/api/settings").json()["providers"]}
    assert providers["ollama"]["configured"] and providers["ollama"]["local_server"]
    # The managed server starts with Nodestro's context window; the hints say what this computer can do.
    assert overview["status"]["context_length"] == 32768 and overview["status"]["context_source"] == "configured"
    assert isinstance(overview["status"]["accelerated"], bool)
    codes = {hint["code"] for hint in overview["advice"]}
    assert "local.context_small" not in codes and "local.context_unknown" not in codes
    # The speed is measured, never estimated: a short request, stored with the model.
    job = managed.post("/api/local-models/benchmark", json={"model": "qwen3.5:4b"})
    assert job.status_code == 202 and job.json()["kind"] == "benchmark"
    deadline = time.monotonic() + 15
    while (state := managed.get(f"/api/local-models/jobs/{job.json()['id']}").json())["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert state["state"] == "done" and state["message"] == "10.0 tokens/s", state
    speed = managed.get("/api/local-models").json()["status"]["models"][0]["speed"]
    assert speed["tokens_per_second"] == 10.0
    missing = managed.post("/api/local-models/benchmark", json={"model": "nope:1b"})
    assert missing.status_code in (400, 404) and missing.json()["code"] == "local.model_missing"
    assert managed.delete("/api/local-models/models/qwen3.5:4b").status_code == 204
    invalid = managed.delete("/api/local-models/models/qwen3.5:cloud")
    assert invalid.status_code == 400
    assert managed.post("/api/local-models/stop").json()["running"] is False
    assert managed.put("/api/local-models/autostart", json={"enabled": False}).json()["autostart"] is False
    assert managed.post("/api/local-models/start").json()["running"] is True


def test_the_semantic_index_routes(make_app: MakeApp) -> None:
    client = make_app()
    client.post("/api/knowledge", files={"files": ("a.md", b"Refunds within 30 days.", "text/markdown")})
    status = client.get("/api/knowledge/index").json()
    assert status["state"] == "off" and status["mode"] == "fts" and status["chunks"] == 1
    refused = client.post("/api/knowledge/index/rebuild", json={})
    assert refused.status_code == 400 and refused.json()["code"] == "knowledge.embedding_model_missing"
    vector = client.get("/api/knowledge/search", params={"q": "money back", "mode": "vector"})
    assert vector.status_code == 400 and vector.json()["code"] == "knowledge.vectors_missing"
    hits = client.get("/api/knowledge/search", params={"q": "refunds"}).json()
    assert hits[0]["ranks"] == {"fts": 1}
    settings = client.put("/api/settings", json={"knowledge_retrieval": "hybrid"}).json()
    assert settings["embedding_model"] == "qwen3-embedding:0.6b"  # chosen with the mode
    assert client.get("/api/knowledge/index").json()["mode"] == "hybrid"
    assert client.delete("/api/knowledge/index").json()["state"] == "off"


def test_usage_reports_local_requests(make_app: MakeApp) -> None:
    client = make_app()
    total = client.get("/api/usage").json()["total"]
    assert total["local_requests"] == 0


def test_a_folder_that_is_no_offline_package_has_its_own_code(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UX handover B3: not agent.OllamaError with an English text, but local.invalid_package (translated)."""
    monkeypatch.delenv("NODESTRO_OLLAMA_BINARY", raising=False)
    client = make_app(local_defaults=True)
    folder = tmp_path / "not-a-package"
    folder.mkdir()
    (folder / "readme.txt").write_text("x")
    answer = client.post("/api/local-models/import", json={"kind": "package", "path": str(folder)})
    assert answer.status_code == 400, answer.text
    assert answer.json()["code"] == "local.invalid_package"
    assert answer.json()["params"]["reason"] == "invalid_package"


def _finished(client: TestClient, job: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while (state := client.get(f"/api/local-models/jobs/{job['id']}").json())["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return state


def test_the_setup_reports_its_steps_and_one_runs_at_a_time(managed: TestClient) -> None:
    started = managed.post("/api/local-models/setup", json={})
    assert started.status_code == 202, started.text
    job = started.json()
    assert job["kind"] == "setup" and [step["id"] for step in job["steps"]] == [
        "check",
        "prerequisites",
        "download",
        "verify",
        "install",
        "configure",
        "start",
        "reachable",
        "hardware",
        "ready",
    ]
    again = managed.post("/api/local-models/setup", json={})
    if again.status_code == 202:  # the first one was already done
        _finished(managed, again.json())
    else:
        assert again.status_code == 409 and again.json()["code"] == "local.job_running"
    state = _finished(managed, job)
    assert state["state"] == "done", state
    steps = {step["id"]: step for step in state["steps"]}
    assert steps["check"]["note"] == "bundled" and steps["download"]["state"] == "skipped"
    assert steps["start"]["state"] == "done" and steps["reachable"]["detail"].startswith("Ollama 0.40.14 at http://127")
    assert steps["hardware"]["note"].startswith("gpu_") and steps["ready"]["note"] == "ready"
    # A reload of the interface finds the job with its steps (and the server running).
    listed = {item["id"]: item for item in managed.get("/api/local-models/jobs").json()}
    assert listed[job["id"]]["steps"] == state["steps"]
    assert managed.get("/api/local-models").json()["status"]["running"] is True


def test_a_users_own_server_is_checked_but_never_stopped(make_app: MakeApp) -> None:
    client = make_app()
    saved = client.put("/api/settings", json={"providers": {"ollama": {"base_url": "http://127.0.0.1:9/v1"}}})
    assert saved.status_code == 200, saved.text
    assert client.get("/api/local-models").json()["status"]["mode"] == "external"
    refused = client.post("/api/local-models/stop")
    assert refused.status_code == 409 and refused.json()["code"] == "local.not_managed"
    state = _finished(client, client.post("/api/local-models/setup", json={}).json())
    steps = {step["id"]: step for step in state["steps"]}
    # Nothing answers on port 9: the check of the address fails, nothing was started or configured.
    assert state["state"] == "failed" and state["error_code"] == "local.unreachable" and state["retryable"] is True
    assert steps["start"]["state"] == "skipped" and steps["start"]["note"] == "not_managed"
    assert steps["reachable"]["state"] == "failed"


def test_a_job_that_runs_already_is_a_conflict(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """API-062: ``local.job_running`` answers 409 like ``/stop`` does for the same conflict; the code stays."""
    from nodestro_runtime.problems import NodestroError

    monkeypatch.delenv("NODESTRO_OLLAMA_BINARY", raising=False)
    client = make_app(local_defaults=True)
    # Every other runtime error stays a 400.
    missing = client.post("/api/local-models/runtime", json={"archive": "/no/such/ollama.tgz"})
    assert missing.status_code == 400, missing.text
    assert missing.json()["code"] == "local.file_missing"

    def busy(*_: Any, **__: Any) -> None:
        raise NodestroError("local.job_running", "qwen3.5:4b is already being processed", job="job-1")

    monkeypatch.setattr(_ctx(client).local_models, "pull", busy)
    monkeypatch.setattr(_ctx(client).local_models, "setup", busy)
    for answer in (
        client.post("/api/local-models/pull", json={"model": "qwen3.5:4b"}),
        client.post("/api/local-models/setup", json={}),
    ):
        assert answer.status_code == 409, answer.text
        assert answer.json()["code"] == "local.job_running" and answer.json()["params"] == {"job": "job-1"}
