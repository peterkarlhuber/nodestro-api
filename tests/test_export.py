"""The standalone FastAPI export: contents, secrets, determinism, the generated project's own tests
and the same results as the studio for the same release (simulated model, no API calls)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
import io
import json
import os
from pathlib import Path
import re
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
    assert "export.private_repository" not in codes  # orkexa's repository is public (DX handover B1)
    assert {item["kind"] for item in check["resources"]} == {"knowledge", "memory"}
    assert "nodestro_runtime/engine.py" in check["files"]
    assert "nodestro_api/standalone.py" in check["files"]
    # The 25 modules of Nodestro 0.1, the extension contract the knowledge base uses for OCR, the SQLite tables
    # module, and the sandbox policy with tool provenance and the operating profile it reads, and the built-in
    # Linux sandbox the policy can choose (runtime 45e7774), and the per-call tool approval rules the engine
    # applies (tool_policy, tool_approvals; runtime 8597544), output contracts and the orchestrator node
    # (contracts, orchestrator; runtime 40e04b4), and the agent checkpoints of runs paused at a tool call
    # (tool_checkpoints; runtime c86781b), and the tool call journal and the sandbox cancel binding that continuing
    # and cancelling runs use (tool_journal, sandbox_cancel; backend audit B-02, B-03), and the run budget decisions
    # the engine pauses runs for (run_budget; BUDGET-1).
    modules = check["runtime_modules"]
    # + nodestro_runtime.run_chat (LRC-1); + nodestro_runtime.docker_status (Docker-first plan R2);
    # + nodestro_runtime.run_budget (BUDGET-1); + nodestro_runtime.local_models (the catalog of local models the
    # preflight and the model catalog read; it starts nothing, and the managed server's service stays out);
    # + nodestro_runtime.value_checks (key figures the engine checks against tool results; runtime #108)
    expected = {"nodestro_runtime.extensions", "nodestro_runtime.docker_status", "nodestro_runtime.run_budget"}
    expected.add("nodestro_runtime.value_checks")
    # + nodestro_api.export_models (every model of the release, incl. orchestrators) and nodestro_api.webui_server
    # (the optional web interface of an export).
    expected |= {"nodestro_api.export_models", "nodestro_api.webui_server"}
    # + nodestro_api.body_limit and nodestro_runtime.traces (its trace limit): the exported application limits
    # request bodies like the studio (backend audit 3, API3-02).
    expected |= {"nodestro_api.body_limit", "nodestro_runtime.traces"}
    # + nodestro_runtime.reasoning (the live reasoning events the engine passes on for every provider).
    expected.add("nodestro_runtime.reasoning")
    # + nodestro_runtime.model_profile and nodestro_runtime.output_limits: the agent builder checks options and
    # output limits by the model's profile.
    expected |= {"nodestro_runtime.model_profile", "nodestro_runtime.output_limits"}
    # + nodestro_api.permissions: the exported application checks every route's protection when it is built
    # (MUP-ONA A5, PS-5).
    expected.add("nodestro_api.permissions")
    # + the service connections and tool specs of NAL-A1 (ADR-0001): nodestro_runtime.connections, .tool_spec and
    # .net_address (the address rules shared with MCP, so no MCP module comes along), nodestro_api.portable_connections
    # (the connections entered on the target computer) and nodestro_api.portable_secrets (its owner-only file writer).
    added = {
        "nodestro_runtime.connections",
        "nodestro_runtime.tool_spec",
        "nodestro_runtime.net_address",
        "nodestro_api.portable_connections",
        "nodestro_api.portable_secrets",
    }
    expected |= added
    # + the app API contract of NAL-C1: nodestro_api.app_contract (models, form mapping, backend id, header) and
    # nodestro_api.app_discovery (GET /api/app).
    expected |= {"nodestro_api.app_contract", "nodestro_api.app_discovery"}
    assert not any(module.startswith("nodestro_runtime.mcp.") for module in modules)
    # + nodestro_runtime.error_texts with nodestro-runtime 9e0a73d and later (TABRION MIG-NOD-3; it imports tabrion,
    # which the export then names by its Git commit): counted from the installed runtime, before and after the bump.
    with_error_texts = (Path(nodestro_runtime.__file__).parent / "error_texts.py").is_file()
    assert ("nodestro_runtime.error_texts" in modules) is with_error_texts
    assert len(modules) == 65 + with_error_texts and expected | {"nodestro_runtime.local_models"} <= set(modules)
    assert "nodestro_runtime.local_service" not in modules
    # The audit/usage hook protocol is exported (tool approvals and the usage log import it); the Axentra
    # integration itself is server-only and never part of an export.
    assert "nodestro_runtime.platform_events" in modules and "nodestro_runtime.axentra_platform" not in modules
    assert "nodestro_runtime.bedrock" in modules  # the exported runtime knows Amazon Bedrock
    assert "nodestro_runtime.tool_checkpoints" in modules
    assert {"nodestro_runtime.tool_journal", "nodestro_runtime.sandbox_cancel"} <= set(modules)
    assert {"nodestro_runtime.contracts", "nodestro_runtime.orchestrator"} <= set(modules)
    assert {"nodestro_runtime.tool_policy", "nodestro_runtime.tool_approvals"} <= set(modules)
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
    assert manifest["orkexa"]["network_access_needed"] is True
    assert manifest["tabrion"] == {"requirement": exporter._tabrion_git()}
    assert "private_repository_access_needed" not in manifest["orkexa"]
    assert manifest["providers"] == ["anthropic"]
    assert "orkexa" in readme and "API_KEYS" in readme and "ANTHROPIC_API_KEY" in readme
    # Never advise a token in the git configuration (it would be stored in plain text in ~/.gitconfig).
    assert "x-access-token" not in readme and "insteadOf" not in readme and "private" not in readme
    assert "This workflow has 1 approval step." in readme
    history = client.get(f"/api/publications/{publication['id']}/exports").json()
    assert [item["id"] for item in history] == [second["id"], first["id"]]


def test_an_export_can_be_deleted_with_its_file(exported: Callable[..., Any]) -> None:
    """API-048: deleting an export removes its record and its ZIP (unknown ids answer 404)."""
    client, publication = exported()
    first, second = _export(client, publication["id"]), _export(client, publication["id"])
    data_dir = client.app.state.nodestro.config.data_dir  # type: ignore[attr-defined]
    assert (data_dir / "exports" / f"{first['id']}.zip").is_file()
    assert client.delete(f"/api/exports/{first['id']}").status_code == 204
    assert not (data_dir / "exports" / f"{first['id']}.zip").exists()
    assert [item["id"] for item in client.get(f"/api/publications/{publication['id']}/exports").json()] == [
        second["id"]
    ]
    assert client.get(f"/api/exports/{first['id']}/download").status_code == 404
    assert client.delete(f"/api/exports/{first['id']}").status_code == 404
    assert client.delete("/api/exports/exp_unknown").status_code == 404
    (data_dir / "exports" / f"{second['id']}.zip").unlink()  # a file already gone does not block the delete
    assert client.delete(f"/api/exports/{second['id']}").status_code == 204


@pytest.mark.parametrize("language", ["en", "de", "es", "fr", "it", "pt-BR"])
@pytest.mark.parametrize("kind", ["standard", "portable"])
def test_a_readme_without_approval_steps_has_no_approval_part(kind: str, language: str) -> None:
    """API-053: the approval section (standard) or sentence and example (portable) is left out in every language;
    the rest of the template stays."""
    template = exporter.readme_template(kind, language)
    shortened = exporter._without_approvals(template)
    assert "{approvals}" in template and "{approvals}" not in shortened and "{curl_approve}" not in shortened
    assert shortened.count("```") % 2 == 0
    kept = set(re.findall(r"\{\w+\}", template)) - {"{approvals}", "{curl_approve}"}
    assert kept <= set(re.findall(r"\{\w+\}", shortened))
    assert len(template) - len(shortened) < 1200


def test_an_export_without_approvals_says_none(exported: Callable[..., Any], tmp_path: Path) -> None:
    client, publication = exported()
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    readme = (project / "README.md").read_text()
    assert "## Approvals" not in readme and "/approve" not in readme and "approval step" not in readme
    assert "Starting memory: none" in readme and "Tools: none" in readme and ": -" not in readme


def test_runtime_is_copied_unchanged(exported: Callable[..., Any], tmp_path: Path) -> None:
    client, publication = exported()
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    runtime = json.loads((project / "RUNTIME.json").read_text())
    dirs = {"nodestro_runtime": Path(nodestro_runtime.__file__).parent, "nodestro_api": Path(exporter.__file__).parent}
    modules = {name.replace(".", "/") + ".py" for name in exporter.runtime_modules()}
    # The vendored Swagger UI of /docs is copied unchanged as well (backend audit 3, API3-01).
    swagger = {f"nodestro_api/swagger_ui/{name}" for name in exporter._SWAGGER_FILES}
    assert set(runtime["files"]) == modules | swagger | {"nodestro_runtime/__init__.py", "nodestro_api/__init__.py"}
    for name in runtime["files"]:
        package, _, file = name.partition("/")
        assert (project / name).read_bytes() == (dirs[package] / file).read_bytes()
    assert set(runtime["packages"]) == {"nodestro-runtime", "nodestro-api"}
    assert runtime["packages"]["nodestro-runtime"]["version"] == nodestro_runtime.__version__


@pytest.mark.parametrize(
    ("language", "build", "by_id"),
    [
        ("en", "build once from Orkexa's repository", "never by the tag"),
        ("de", "einmal aus Orkexas", "nie über den Tag"),
    ],
)
def test_readme_says_how_to_build_the_sandbox_image(
    exported: Callable[..., Any], tmp_path: Path, language: str, build: str, by_id: str
) -> None:
    """``TOOL_SANDBOX=docker`` needs Orkexa's image: the README gives the build and check commands (Orkexa's own
    README does not mention the image) and says that containers run the checked image's id."""
    client, publication = exported()
    project = _extract(_download(client, _export(client, publication["id"], language=language)["id"]), tmp_path)
    readme = (project / "README.md").read_text()
    assert "docker build -t orkexa-sandbox -f docker/sandbox.Dockerfile ." in readme
    assert "python -m orkexa.dynamic.docker --self-test" in readme
    assert build in readme and by_id in readme
    assert "Orkexa's README" not in readme and "Orkexas README" not in readme


@pytest.mark.parametrize("language", ["en", "de", "es", "fr", "it", "pt-BR"])
def test_the_dockerfile_is_pinned_and_the_readme_shows_the_container_with_mounts(
    exported: Callable[..., Any], tmp_path: Path, language: str
) -> None:
    """API-027/API-064: the export's base image is pinned by the digest the runtime reviewed, and the README shows a
    container run that keeps the data and the result folder (``OUTPUT_DIR``) on the host."""
    import sys

    from nodestro_runtime.container_images import BASE_IMAGES

    client, publication = exported()
    project = _extract(_download(client, _export(client, publication["id"], language=language)["id"]), tmp_path)
    dockerfile = (project / "Dockerfile").read_text()
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert f"FROM {BASE_IMAGES[minor]}\n" in dockerfile and "@sha256:" in BASE_IMAGES[minor]
    assert "ENV DATA_DIR=/data" in dockerfile
    readme = (project / "README.md").read_text()
    assert "| `OUTPUT_DIR` |" in readme
    assert "`DATA_DIR=/data`" in readme and "`OUTPUT_DIR=/results`" in readme
    assert '-v "$PWD/data:/data" -v "$PWD/results:/results" workflow-api' in readme


def test_bundled_library_mode(exported: Callable[..., Any], tmp_path: Path) -> None:
    client, publication = exported()
    record = _export(client, publication["id"], orkexa="bundle", confirm_distribution=True)
    project = _extract(_download(client, record["id"]), tmp_path)
    vendor = project / "vendor" / "orkexa"
    assert (vendor / "LICENSE").read_text() == exporter._library_license()
    assert (vendor / "src" / "orkexa" / "__init__.py").is_file()
    assert 'orkexa = { path = "vendor/orkexa" }' in (project / "pyproject.toml").read_text()
    assert [path.name for path in (project / "vendor").iterdir()] == ["orkexa"]  # never a copy of tabrion
    # The only Git line is Tabrion's (TABRION MIG-NOD-3: no vendoring), when the runtime needs it.
    git_lines = [line for line in (project / "requirements.lock").read_text().splitlines() if "git+" in line]
    assert git_lines == ([exporter._tabrion_git()] if exporter._runtime_needs_tabrion() else [])


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
    # Without approval steps the approval test is skipped, not passed (API-053).
    expected = "13 passed" if approval else "12 passed, 1 skipped"
    assert expected in result.stdout, result.stdout[-2000:]


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


def _fake_orkexa(monkeypatch: pytest.MonkeyPatch, direct_url: dict[str, Any] | None) -> None:
    original = exporter.metadata.distribution

    class Fake:
        def read_text(self, name: str) -> str | None:
            return json.dumps(direct_url) if name == "direct_url.json" and direct_url is not None else None

    monkeypatch.setattr(exporter.metadata, "distribution", lambda name: Fake() if name == "orkexa" else original(name))


def test_a_git_orkexa_installation_exports_its_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    # orkexa has no release tags: the export names the exact commit the installation came from (PEP 610).
    commit = "0123456789abcdef0123456789abcdef01234567"
    url = "https://github.com/peterkarlhuber/orkexa.git"
    _fake_orkexa(monkeypatch, {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}})
    assert exporter._orkexa_git() == f"{exporter._LIBRARY} @ git+{url}@{commit}"


@pytest.mark.parametrize(
    "direct_url",
    [
        None,  # installed from an index or a wheel: no direct_url.json
        {"url": "file:///home/user/orkexa", "dir_info": {}},  # a local checkout
        {"url": "file:///home/user/orkexa", "vcs_info": {"vcs": "git", "commit_id": "0" * 40}},  # not on GitHub
    ],
)
def test_an_orkexa_without_a_github_commit_cannot_be_exported_by_reference(
    monkeypatch: pytest.MonkeyPatch, direct_url: dict[str, Any] | None
) -> None:
    # The runtime only declares a version range for orkexa; the exact commit lives in the studio lock. There is no
    # fallback to guess one: the export refuses with an explanation instead of naming a tag that does not exist.
    _fake_orkexa(monkeypatch, direct_url)
    with pytest.raises(RuntimeError, match="studio lock"):
        exporter._orkexa_git()


def _tabrion_lines(text: str) -> list[str]:
    return [line.strip().strip('",') for line in text.splitlines() if re.match(r'\s*"?tabrion\b', line)]


def test_the_export_names_tabrion_by_its_git_commit_never_by_name(exported: Callable[..., Any], tmp_path: Path) -> None:
    """TABRION MIG-NOD-3 / DC-1: the copied runtime modules import tabrion (runtime 9e0a73d and later); the project
    names it only as ``tabrion @ git+https://github.com/...@<full commit>`` in pyproject.toml and requirements.lock,
    never as a bare name an index could answer."""
    if not exporter._runtime_needs_tabrion():
        pytest.skip("the installed nodestro-runtime does not require tabrion (before 9e0a73d)")
    line = exporter._tabrion_git()
    assert line is not None and re.fullmatch(r"tabrion @ git\+https://github\.com/\S+@[0-9a-f]{40}", line)
    client, publication = exported()
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}}).json()
    assert line in check["dependencies"]
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    for name in ("pyproject.toml", "requirements.lock"):
        assert _tabrion_lines((project / name).read_text()) == [line], name


def _fake_tabrion(monkeypatch: pytest.MonkeyPatch, direct_url: dict[str, Any] | None) -> None:
    original = exporter.metadata.distribution

    class Fake:
        def read_text(self, name: str) -> str | None:
            return json.dumps(direct_url) if name == "direct_url.json" and direct_url is not None else None

    monkeypatch.setattr(exporter, "_runtime_needs_tabrion", lambda: True)
    monkeypatch.setattr(exporter.metadata, "distribution", lambda name: Fake() if name == "tabrion" else original(name))


def test_a_git_tabrion_installation_exports_its_full_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    commit = "d5821faa8742dea012abda3b663549c038d76543"
    url = "https://github.com/peterkarlhuber/tabrion.git"
    _fake_tabrion(monkeypatch, {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}})
    assert exporter._tabrion_git() == f"tabrion @ git+{url}@{commit}"


@pytest.mark.parametrize(
    "direct_url",
    [
        None,  # installed from an index or a wheel: a bare name, which an export must never repeat (DC-1)
        {"url": "file:///home/user/tabrion", "dir_info": {"editable": True}},  # a local checkout
        {"url": "file:///home/user/tabrion", "vcs_info": {"vcs": "git", "commit_id": "0" * 40}},  # not on GitHub
        {"url": "https://github.com/peterkarlhuber/tabrion.git", "vcs_info": {"vcs": "git", "commit_id": "d5821fa"}},
        # a URL that would break out of the requirement line or the TOML string (review of MIG-NOD-3, item 4)
        {"url": 'https://github.com/a/b" x', "vcs_info": {"vcs": "git", "commit_id": "0" * 40}},
        {"url": "https://github.com/a/b@evil", "vcs_info": {"vcs": "git", "commit_id": "0" * 40}},
        {"url": "https://github.com/a/b/../c", "vcs_info": {"vcs": "git", "commit_id": "0" * 40}},
    ],
)
def test_a_tabrion_without_a_full_github_commit_refuses_the_export(
    monkeypatch: pytest.MonkeyPatch, direct_url: dict[str, Any] | None
) -> None:
    _fake_tabrion(monkeypatch, direct_url)
    with pytest.raises(RuntimeError, match="tabrion is not installed from a GitHub commit"):
        exporter._tabrion_git()


def test_an_older_runtime_needs_no_tabrion_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exporter.metadata, "requires", lambda name: ["orkexa[all]>=0.2.0.dev0,<0.3"])
    assert exporter._runtime_needs_tabrion() is False and exporter._tabrion_git() is None
    monkeypatch.setattr(exporter.metadata, "requires", lambda name: ['tabrion[testing]>=0.1; extra == "dev"'])
    assert exporter._runtime_needs_tabrion() is False


def test_the_exported_api_chooses_its_tool_sandbox_from_the_environment() -> None:
    from pydantic import ValidationError

    from nodestro_api.standalone import StandaloneSettings

    assert StandaloneSettings.from_env({}).tool_sandbox == "subprocess"
    assert StandaloneSettings.from_env({"TOOL_SANDBOX": "docker"}).tool_sandbox == "docker"
    with pytest.raises(ValidationError):
        StandaloneSettings.from_env({"TOOL_SANDBOX": "unsafe"})


def test_the_exported_api_proves_docker_before_untrusted_tools_run(
    exported: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_runtime import docker_status
    from nodestro_runtime.sandbox_policy import isolation_of
    from nodestro_runtime.testing import StaticDockerMonitor
    from orkexa.dynamic import DockerSandbox

    from nodestro_api.standalone import StandaloneHost, StandaloneSettings

    client, publication = exported()
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    created: list[dict[str, Any]] = []
    monitor = StaticDockerMonitor()

    def monitor_for(sandbox: Callable[[], DockerSandbox], **options: Any) -> StaticDockerMonitor:
        created.append({"sandbox": sandbox(), **options})
        return monitor

    monkeypatch.setattr(docker_status, "monitor_for", monitor_for)
    data = tmp_path / "export-data"
    host = StandaloneHost(project, StandaloneSettings.from_env({"DATA_DIR": str(data), "TOOL_SANDBOX": "docker"}))
    try:
        owner = docker_status.installation_owner(data)
        assert created and created[0]["owner"] == owner and created[0]["sandbox"].owner == owner
        assert monitor.reaps == [False]  # expired containers only
        made = created[0]["sandbox"]
        assert isinstance(made, DockerSandbox) and isolation_of(made) == "none"  # refused until proven
    finally:
        host.close()


def test_an_installation_without_test_tools_exports(
    exported: Callable[..., tuple[TestClient, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The server and desktop images install no pytest: the check and the export must not fail with a 500.
    real = exporter._version

    def without_pytest(name: str) -> str:
        if name == "pytest":
            raise exporter.metadata.PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(exporter, "_version", without_pytest)
    client, publication = exported()
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}})
    assert check.status_code == 200 and check.json()["ok"] is True
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path)
    pyproject = (project / "pyproject.toml").read_text()
    assert '"pytest>=8",' in pyproject  # the floor its generated tests need, not a pin
    assert f'"httpx=={real("httpx")}",' in pyproject  # an installed test tool stays pinned


def test_the_module_analysis_is_kept_between_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """UX handover B2: the export check parsed every runtime module on each call (about a second)."""
    first = exporter.runtime_modules()
    parsed: list[str] = []
    original = exporter.ast.parse
    monkeypatch.setattr(exporter.ast, "parse", lambda source, *a, **k: parsed.append("x") or original(source, *a, **k))
    assert exporter.runtime_modules() == first
    assert parsed == []
    # A changed file of a package (a development checkout) is analysed again.
    folder = tmp_path / "pkg"
    folder.mkdir()
    (folder / "a.py").write_text("x = 1\n")
    before = exporter._source_stamp({"nodestro_api": folder})
    os.utime(folder / "a.py", ns=(1, 10**18))
    assert exporter._source_stamp({"nodestro_api": folder}) != before
