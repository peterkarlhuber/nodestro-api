"""The standalone FastAPI export: contents, secrets, determinism, the generated project's own tests
and the same results as the studio for the same release (simulated model, no API calls)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import zipfile

from fastapi.testclient import TestClient
import nodestro_runtime
import pytest
from test_published_api import PUBLISHED, auth, finish, form_workflow, new_key, publish

from nodestro_api import exporter

from conftest import ScriptedClient, text
from test_flow import edge, save
from test_runs import agent_node


def _export(client: TestClient, publication_id: str, **options: Any) -> dict[str, Any]:
    response = client.post(f"/api/publications/{publication_id}/exports", json={"options": options})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _download(client: TestClient, export_id: str) -> bytes:
    response = client.get(f"/api/exports/{export_id}/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    return response.content


def _extract(data: bytes, target: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(target)
        root = archive.namelist()[0].split("/")[0]
    return target / root


def _knowledge(client: TestClient, name: str, content: bytes) -> str:
    response = client.post("/api/knowledge", files=[("files", (name, content, "text/plain"))])
    assert response.status_code in (200, 201), response.text
    data = response.json()
    return str((data[0] if isinstance(data, list) else data)["id"])


@pytest.fixture
def exported(make_app: Callable[..., TestClient]) -> Callable[..., tuple[TestClient, dict[str, Any]]]:
    def factory(**workflow: Any) -> tuple[TestClient, dict[str, Any]]:
        client = make_app({"WRITER": [text("Angebot für ACME")] * 5})
        client.put("/api/settings", json={"api_key": "sk-ant-api03-very-secret-studio-key"})
        publication = publish(client, form_workflow(client, **workflow))["publication"]
        return client, publication

    return factory


def test_export_check_names_what_is_and_is_not_included(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    file_id = _knowledge(client, "preise.txt", b"Preisliste 2026")
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node(
                "writer",
                "WRITER",
                knowledge={"file_ids": [file_id], "mode": "context"},
                memory={"mode": "persistent", "space": "Kunden"},
            ),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "writer"), edge("writer", "out")],
        name="Mit Wissen",
    )
    publication = publish(client, workflow_id)["publication"]
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}}).json()
    codes = {item["code"]: item["level"] for item in check["problems"]}
    assert codes["export.knowledge_excluded"] == "warning"
    assert codes["export.memory_empty"] == "info"
    assert codes["export.private_repository"] == "warning"
    assert {item["kind"] for item in check["resources"]} == {"knowledge", "memory"}
    assert "nodestro_runtime/engine.py" in check["files"]
    assert "nodestro_api/standalone.py" in check["files"]
    # The 25 modules of Nodestro 0.1, the extension contract the knowledge base uses for OCR, the SQLite tables
    # module, and the sandbox policy with tool provenance and the operating profile it reads, and the built-in
    # Linux sandbox the policy can choose (runtime 45e7774).
    modules = check["runtime_modules"]
    assert len(modules) == 36 and "nodestro_runtime.extensions" in modules
    assert {"nodestro_runtime.builtin_sandbox", "nodestro_runtime.sandbox_confine"} <= set(modules)
    # Costs in an export are priced like in the studio (PRICING-1): the price catalog travels with the engine.
    assert {"nodestro_runtime.cost_tracking", "nodestro_runtime.pricing"} <= set(modules)
    assert {"nodestro_runtime.sandbox_policy", "nodestro_runtime.tool_provenance"} <= set(modules)
    assert not any(path.startswith("workflow/knowledge/") for path in check["files"])
    included = client.post(
        f"/api/publications/{publication['id']}/export/check",
        json={"options": {"knowledge": [file_id], "memory": ["Kunden"]}},
    ).json()
    assert f"workflow/knowledge/{file_id}/data" in included["files"]
    assert "workflow/memory/Kunden.json" in included["files"]
    bundle = client.post(
        f"/api/publications/{publication['id']}/export/check", json={"options": {"orkexa": "bundle"}}
    ).json()
    assert bundle["ok"] is False
    assert bundle["problems"][0]["code"] == "export.distribution_unconfirmed"
    refused = client.post(f"/api/publications/{publication['id']}/exports", json={"options": {"orkexa": "bundle"}})
    assert refused.status_code == 400


def test_export_contains_no_secrets_and_is_reproducible(exported: Callable[..., Any]) -> None:
    client, publication = exported(approval=True)
    token = new_key(client, publication["id"])
    first = _export(client, publication["id"])
    second = _export(client, publication["id"])
    data = _download(client, first["id"])
    assert data == _download(client, second["id"])
    assert first["sha256"] == second["sha256"]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        everything = b"".join(archive.read(name) for name in names)
    assert b"sk-ant-api03" not in everything
    assert token.split("_", 2)[2].encode() not in everything
    root = names[0].split("/")[0]
    assert root == "angebot-generator-api"
    for path in (
        "README.md",
        "pyproject.toml",
        "requirements.lock",
        ".env.example",
        "Dockerfile",
        "app/main.py",
        "app/keys.py",
        "app/schemas.py",
        "RUNTIME.json",
        "nodestro_api/standalone.py",
        "nodestro_api/serving.py",
        "nodestro_runtime/engine.py",
        "workflow/release.json",
        "workflow/manifest.json",
        "examples/client.py",
        "examples/curl.sh",
        "tests/test_api.py",
    ):
        assert f"{root}/{path}" in names, path
    assert not any(name.endswith(("/context.py", "/api.py", "/workspace.py", "/app.py")) for name in names)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read(f"{root}/workflow/manifest.json"))
        readme = archive.read(f"{root}/README.md").decode()
    assert manifest["secrets_included"] is False
    assert manifest["orkexa"]["private_repository_access_needed"] is True
    assert manifest["providers"] == ["anthropic"]
    assert "orkexa" in readme and "API_KEYS" in readme and "ANTHROPIC_API_KEY" in readme
    assert "This workflow has 1 approval step." in readme
    history = client.get(f"/api/publications/{publication['id']}/exports").json()
    assert [item["id"] for item in history] == [second["id"], first["id"]]


def test_runtime_is_copied_unchanged(exported: Callable[..., Any], tmp_path: Path) -> None:
    client, publication = exported()
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    runtime = json.loads((project / "RUNTIME.json").read_text())
    dirs = {"nodestro_runtime": Path(nodestro_runtime.__file__).parent, "nodestro_api": Path(exporter.__file__).parent}
    modules = {name.replace(".", "/") + ".py" for name in exporter.runtime_modules()}
    assert set(runtime["files"]) == modules | {"nodestro_runtime/__init__.py", "nodestro_api/__init__.py"}
    for name in runtime["files"]:
        package, _, file = name.partition("/")
        assert (project / name).read_bytes() == (dirs[package] / file).read_bytes()
    assert set(runtime["packages"]) == {"nodestro-runtime", "nodestro-api"}
    assert runtime["packages"]["nodestro-runtime"]["version"] == nodestro_runtime.__version__


def test_bundled_library_mode(exported: Callable[..., Any], tmp_path: Path) -> None:
    client, publication = exported()
    record = _export(client, publication["id"], orkexa="bundle", confirm_distribution=True)
    project = _extract(_download(client, record["id"]), tmp_path)
    vendor = project / "vendor" / "orkexa"
    assert (vendor / "LICENSE").is_file() and "MIT" in (vendor / "LICENSE").read_text()
    assert (vendor / "src" / "orkexa" / "__init__.py").is_file()
    assert 'orkexa = { path = "vendor/orkexa" }' in (project / "pyproject.toml").read_text()
    assert [path.name for path in (project / "vendor").iterdir()] == ["orkexa"]
    assert "git+" not in (project / "requirements.lock").read_text()


def _blocked_studio(tmp_path: Path) -> Path:
    """A directory whose studio packages refuse to import: the export must not need them."""
    for name in ("nodestro", "nodestro_studio", "nodestro_examples"):
        blocker = tmp_path / "blocker" / name
        blocker.mkdir(parents=True)
        (blocker / "__init__.py").write_text(f"raise ImportError('the exported project must not import {name}')\n")
    return tmp_path / "blocker"


@pytest.mark.parametrize("approval", [False, True])
def test_generated_project_passes_its_own_tests(exported: Callable[..., Any], tmp_path: Path, approval: bool) -> None:
    client, publication = exported(approval=approval)
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_blocked_studio(tmp_path)), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert "13 passed" in result.stdout, result.stdout[-2000:]


def test_key_tool_of_the_export(exported: Callable[..., Any], tmp_path: Path) -> None:
    client, publication = exported()
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    env = {**os.environ, "PYTHONPATH": str(_blocked_studio(tmp_path))}
    result = subprocess.run(
        [sys.executable, "-m", "app.keys", "new", "--name", "CRM", "--scopes", "run,read"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    token = next(line for line in lines if line.startswith("ndk_"))
    entry = lines[-1]
    key_id, digest, scopes, name = entry.split(":")
    assert token.split("_")[1] == key_id and scopes == "run+read" and name == "CRM"
    assert token.split("_", 2)[2] not in entry and len(digest) == 64


# ---- the same release in the studio and in the export ------------------------------------------


@contextlib.contextmanager
def _exported_app(project: Path, data_dir: Path, client: Any, keys: str) -> Iterator[TestClient]:
    """The exported project's app, imported in this process from the project's own copies of the runtime."""
    packages = ("app", "nodestro_runtime", "nodestro_api")

    def ours(name: str) -> bool:
        return any(name == package or name.startswith(f"{package}.") for package in packages)

    installed = {name: module for name, module in sys.modules.items() if ours(name)}
    for name in installed:
        del sys.modules[name]
    sys.path.insert(0, str(project))
    try:
        from nodestro_api.standalone import StandaloneSettings, create_app

        assert project in Path(sys.modules["nodestro_runtime.engine"].__file__ or "").resolve().parents
        settings = StandaloneSettings.from_env({"DATA_DIR": str(data_dir), "API_KEYS": keys})
        with TestClient(create_app(project, settings=settings, client_factory=lambda _provider: client)) as test_client:
            yield test_client
    finally:
        sys.path.remove(str(project))
        for name in [name for name in sys.modules if ours(name)]:
            del sys.modules[name]
        sys.modules.update(installed)


def _comparable(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": run["status"],
        "output": run["output"],
        "steps": sorted((step["node_id"], step["status"]) for step in run.get("steps", [])),
    }


def test_studio_and_export_give_the_same_results(make_app: Callable[..., TestClient], tmp_path: Path) -> None:
    script = {"WRITER": [text("Angebot für ACME")], "SEND": [text("Gesendet: ACME")]}
    client = make_app(script)
    workflow_id = save(
        client,
        [
            {
                "id": "start",
                "type": "start",
                "start": {"fields": [{"name": "customer", "label": "Kunde", "type": "text", "required": True}]},
            },
            agent_node("writer", "WRITER", task_template="Angebot für {{customer}}"),
            {
                "id": "check",
                "type": "approval",
                "label": "Prüfen",
                "approval": {"instructions": "Ok?", "on_reject": "stop"},
            },
            agent_node("send", "SEND", task_template="{{Writer}}"),
            {"id": "out", "type": "output"},
        ],
        [
            edge("start", "writer"),
            edge("writer", "check"),
            edge("check", "send", "approved"),
            edge("send", "out"),
        ],
        name="Vergleich",
    )
    publication = publish(client, workflow_id)["publication"]
    token = new_key(client, publication["id"])
    body = {"values": {"customer": "ACME"}}

    def drive(api: TestClient, key: str) -> dict[str, Any]:
        run_id = api.post(f"{PUBLISHED}/vergleich/runs", json=body, headers=auth(key)).json()["run_id"]
        paused = finish(api, "vergleich", key, run_id)
        assert paused["status"] == "awaiting_approval", paused
        decided = api.post(
            f"{PUBLISHED}/vergleich/runs/{run_id}/approve",
            json={"decision": "approved", "comment": "ok"},
            headers=auth(key),
        )
        assert decided.status_code == 200, decided.text
        deadline = time.monotonic() + 15
        while finish(api, "vergleich", key, run_id)["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        result = api.get(f"{PUBLISHED}/vergleich/runs/{run_id}/result", headers=auth(key)).json()
        return {**finish(api, "vergleich", key, run_id), "output": result["output"]}

    studio = drive(client, token)
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    from nodestro_api.serving import hash_secret
    from nodestro_api.serving import new_key as make_token

    key_id, secret, export_token = make_token()
    fake = ScriptedClient(script)
    with _exported_app(
        project, tmp_path / "export-data", fake, f"{key_id}:{hash_secret(secret)}:run+read+approve"
    ) as api:
        standalone = drive(api, export_token)
    assert _comparable(studio) == _comparable(standalone)
    assert len(_comparable(studio)["steps"]) >= 4
    assert standalone["output"] == "Gesendet: ACME"
    # The same prompts reached the model.
    studio_calls = [(call.get("system"), call.get("messages")) for call in client.fake.messages.calls]  # type: ignore[attr-defined]
    export_calls = [(call.get("system"), call.get("messages")) for call in fake.messages.calls]
    assert studio_calls == export_calls


def test_a_local_orkexa_installation_exports_the_pinned_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    # orkexa has no release tags: without a Git installation the export uses the revision Nodestro pins.
    original = exporter.metadata.distribution

    class Local:
        def read_text(self, name: str) -> str | None:
            return (
                json.dumps({"url": "file:///home/user/orkexa", "dir_info": {}}) if name == "direct_url.json" else None
            )

    monkeypatch.setattr(exporter.metadata, "distribution", lambda name: Local() if name == "orkexa" else original(name))
    pinned = next(line for line in exporter.metadata.requires("nodestro-runtime") or [] if line.startswith("orkexa"))
    line = exporter._orkexa_git()
    assert line.endswith(pinned.split(" @ ", 1)[1]) and "@v" not in line


def test_the_exported_api_chooses_its_tool_sandbox_from_the_environment() -> None:
    from pydantic import ValidationError

    from nodestro_api.standalone import StandaloneSettings

    assert StandaloneSettings.from_env({}).tool_sandbox == "subprocess"
    assert StandaloneSettings.from_env({"TOOL_SANDBOX": "docker"}).tool_sandbox == "docker"
    with pytest.raises(ValidationError):
        StandaloneSettings.from_env({"TOOL_SANDBOX": "unsafe"})
