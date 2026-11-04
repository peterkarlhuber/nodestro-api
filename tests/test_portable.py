"""The portable export: the analysis of a release's models and network needs, the provider keys on the target
computer, result folders, the web interface's sessions and security, the package's settings, scripts, archive and
README, and the six README languages (no downloads: the parts that need Python builds, wheels or models are covered
by the end-to-end scripts of the export work, see docs/portable-export.md)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tomllib
from typing import Any
import zipfile

from fastapi.testclient import TestClient
import pytest
from test_export import _download, _export, _extract
from test_published_api import form_workflow, new_key, publish

from nodestro_api import exporter, portable, portable_build
from nodestro_api.export_models import model_uses, network_needs
from nodestro_api.portable_secrets import ProviderSecrets, path_of
from nodestro_api.standalone import copy_result_files, resolve_output_dir, safe_file_name

from conftest import text
from test_flow import edge
from test_runs import agent_node

LANGUAGES = ("de", "en", "es", "fr", "it", "pt-BR")
PLACEHOLDER = re.compile(r"\{[A-Za-z_]+\}")


# ---- models and network --------------------------------------------------------------------------------------


def _release_bundle(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> Any:
    """A stand-in for a release bundle: the main workflow only, no tools."""
    from nodestro_runtime.schemas import Workflow

    workflow = Workflow.model_validate({"name": "Modelle", "nodes": nodes, "edges": edges})

    class Bundle:
        def __init__(self) -> None:
            self.workflow = workflow
            self.server_tools: list[str] = []
            self.mcp_tools: dict[str, Any] = {}
            self.tools: dict[str, Any] = {}
            self.tool_trust: dict[str, str] = {}

        def all_workflows(self) -> list[Any]:
            return [workflow]

    return Bundle()


def test_model_uses_name_every_model_with_its_role() -> None:
    writer = agent_node("writer", "WRITER")
    writer["agent"]["model"] = "ollama:qwen3.5:4b"
    writer["agent"]["fallbacks"] = ["ollama:ministral-3:8b"]
    writer["agent"]["quality"] = {
        "enabled": True,
        "criteria": [{"name": "short", "description": "Short"}],
        "judge_model": "anthropic:claude-haiku-4-5",
    }
    bundle = _release_bundle(
        [{"id": "start", "type": "start"}, writer, {"id": "out", "type": "output"}],
        [edge("start", "writer"), edge("writer", "out")],
    )
    uses = {use.model: use for use in model_uses(bundle)}
    assert uses["ollama:qwen3.5:4b"].roles == ["agent"] and uses["ollama:qwen3.5:4b"].local
    assert uses["ollama:ministral-3:8b"].roles == ["fallback"]
    assert uses["anthropic:claude-haiku-4-5"].roles == ["judge"] and not uses["anthropic:claude-haiku-4-5"].local
    # Local models first, then the cloud.
    assert [use.local for use in model_uses(bundle)] == [True, True, False]
    needs = network_needs(bundle, uses.values())
    assert [(need.kind, need.name) for need in needs] == [("cloud_model", "anthropic:claude-haiku-4-5")]
    # The embedding model counts only when an agent searches its knowledge semantically.
    assert all(use.roles != ["embedding"] for use in model_uses(bundle, embedding_model="qwen3-embedding:0.6b"))


# ---- provider keys on the target computer --------------------------------------------------------------------


def test_provider_keys_stay_on_the_target_and_the_environment_wins(tmp_path: Path) -> None:
    environ: dict[str, str] = {}
    store = ProviderSecrets(tmp_path, ["anthropic", "openai", "ollama", "unknown"], environ=environ)
    assert [item["id"] for item in store.status()] == ["anthropic", "openai", "ollama"]
    status = store.set("anthropic", {"api_key": "sk-ant-test-secret"})
    assert status["configured"] and status["source"] == "settings"
    assert "sk-ant-test-secret" not in json.dumps(store.status())  # never a value
    assert environ["ANTHROPIC_API_KEY"] == "sk-ant-test-secret"
    file = path_of(tmp_path)
    if os.name == "posix":
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    # A fresh process reads the file; a variable set outside wins and cannot be changed here.
    outside = {"OPENAI_API_KEY": "sk-from-the-secret-store"}
    again = ProviderSecrets(tmp_path, ["anthropic", "openai"], environ=outside)
    again.apply()
    assert outside["ANTHROPIC_API_KEY"] == "sk-ant-test-secret"
    openai = next(item for item in again.status() if item["id"] == "openai")
    assert openai["source"] == "environment" and not openai["editable"]
    with pytest.raises(Exception) as refused:
        again.set("openai", {"api_key": "sk-other"})
    assert getattr(refused.value, "status", None) == 409
    for bad, code in (({"api_key": "a\nb"}, "provider.key_invalid"), ({}, "provider.nothing_to_store")):
        with pytest.raises(Exception) as invalid:
            again.set("anthropic", bad)
        assert getattr(invalid.value, "code", "") == code
    again.remove("anthropic")
    assert "ANTHROPIC_API_KEY" not in outside and "anthropic" not in json.loads(file.read_text())


def test_endpoint_addresses_refuse_credentials(tmp_path: Path) -> None:
    store = ProviderSecrets(tmp_path, ["vllm"], environ={})
    with pytest.raises(Exception) as refused:
        store.set("vllm", {"base_url": "http://user:secret@10.0.0.5:8000/v1"})
    # Refused either as an invalid address or as credentials in the address; never stored.
    assert getattr(refused.value, "code", "") in ("provider.base_url_invalid", "provider.base_url_credentials")
    assert not path_of(tmp_path).exists()
    store.set("vllm", {"base_url": "http://10.0.0.5:8000/v1"})
    assert store.status()[0]["base_url"] == "http://10.0.0.5:8000/v1"


# ---- result folders ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\a:b?.txt", "a_b_.txt"),
        ("CON.txt", "_CON.txt"),
        ("lpt1", "_lpt1"),
        ("  .hidden.  ", "hidden"),
        ("", "result"),
        ("x" * 300 + ".md", "x" * 117 + ".md"),
    ],
)
def test_safe_file_names(given: str, expected: str) -> None:
    assert safe_file_name(given) == expected


def test_output_folder_falls_back_when_the_configured_one_cannot_be_used(tmp_path: Path) -> None:
    assert resolve_output_dir(tmp_path, "") == (None, None)
    folder, warning = resolve_output_dir(tmp_path, "ergebnisse")
    assert folder == (tmp_path / "ergebnisse").resolve() and warning is None
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    folder, warning = resolve_output_dir(tmp_path, str(blocker / "below"))
    assert folder == (tmp_path / "results").resolve() and warning and str(blocker) in warning


def test_result_files_are_copied_per_run_without_overwriting(tmp_path: Path) -> None:
    class Entry:
        def __init__(self, entry_id: str, name: str) -> None:
            self.id, self.name, self.error_code = entry_id, name, None

    class Files:
        def list(self, _run_id: str) -> list[Entry]:
            return [Entry("1", "Text.md"), Entry("2", "text.md"), Entry("3", "../evil.md")]

        def content(self, _run_id: str, entry_id: str) -> tuple[Any, bytes]:
            return None, entry_id.encode()

    from nodestro_runtime.schemas import Run

    run = Run.model_construct(id="run_1", finished_at=None)
    written = copy_result_files(Files(), run, tmp_path)  # type: ignore[arg-type]
    assert [path.name for path in written] == ["Text.md", "text (2).md", "evil.md"]
    assert all(path.parent.parent == tmp_path and path.parent.name.endswith("_run_1") for path in written)
    second = copy_result_files(Files(), run, tmp_path)  # type: ignore[arg-type]
    assert len(second) == 3 and len(list(written[0].parent.iterdir())) >= 3


@pytest.fixture
def published(make_app: Callable[..., TestClient]) -> Callable[..., tuple[TestClient, dict[str, Any]]]:
    def factory(**workflow: Any) -> tuple[TestClient, dict[str, Any]]:
        client = make_app({"WRITER": [text("Angebot für ACME")] * 5})
        publication = publish(client, form_workflow(client, **workflow))["publication"]
        return client, publication

    return factory


# ---- the web interface -------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _ui_app(project: Path, data_dir: Path, client: Any, keys: str) -> Iterator[tuple[TestClient, Any]]:
    """The exported project's app with the web interface of a portable package (operator link, provider setup)."""
    packages = ("app", "nodestro_runtime", "nodestro_api")

    def ours(name: str) -> bool:
        return any(name == package or name.startswith(f"{package}.") for package in packages)

    installed = {name: module for name, module in sys.modules.items() if ours(name)}
    for name in installed:
        del sys.modules[name]
    sys.path.insert(0, str(project))
    holder: dict[str, Any] = {}
    try:
        from nodestro_api.standalone import StandaloneSettings, create_app
        from nodestro_api.webui_server import UiOptions, WebUi

        secrets_store = ProviderSecrets(data_dir, ["anthropic"], environ={})

        def web_ui(host: Any, core: Any) -> Any:
            holder["ui"] = WebUi(
                host,
                UiOptions(language="de", control_token="control-token", output_dir=str(project / "results")),
                provider_status=secrets_store.status,
                set_provider=secrets_store.set,
                remove_provider=secrets_store.remove,
            )
            return holder["ui"]

        settings = StandaloneSettings.from_env({"DATA_DIR": str(data_dir), "API_KEYS": keys})
        app = create_app(project, settings=settings, client_factory=lambda _provider: client, web_ui=web_ui)
        with TestClient(app, base_url="http://127.0.0.1:8800") as test_client:
            yield test_client, holder["ui"]
    finally:
        sys.path.remove(str(project))
        for name in [name for name in sys.modules if ours(name)]:
            del sys.modules[name]
        sys.modules.update(installed)


@pytest.fixture
def ui_project(published: Callable[..., tuple[TestClient, dict[str, Any]]], tmp_path: Path) -> Iterator[Any]:
    studio, publication = published()
    record = _export(studio, publication["id"], web_ui=True, language="de")
    project = _extract(_download(studio, record["id"]), tmp_path / "export")
    yield studio, publication, project


def _key_line(token: str) -> str:
    from nodestro_api.serving import hash_secret, parse_token

    key_id, secret = parse_token(token)  # type: ignore[misc]
    return f"{key_id}:{hash_secret(secret)}:run+read"


def test_the_export_ships_the_interface_and_its_contract(ui_project: Any) -> None:
    _studio, _publication, project = ui_project
    webui = project / "nodestro_api" / "webui"
    for name in ("index.html", "app.js", "app.css", "markdown.js", "brand/nodestro-logo.png"):
        assert (webui / name).is_file(), name
    for language in LANGUAGES:
        strings = json.loads((webui / "i18n" / f"{language}.json").read_text("utf-8"))
        assert set(strings) == set(json.loads((webui / "i18n" / "en.json").read_text("utf-8"))), language
    html = (webui / "index.html").read_text("utf-8")
    # No external resource: no CDN, no inline script.
    assert "http://" not in html.replace("http://www.w3.org", "") and "https://" not in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    document = json.loads((project / "openapi.json").read_text("utf-8"))
    assert document["openapi"].startswith("3.") and any(path.endswith("/runs") for path in document["paths"])


def test_web_interface_sessions_and_security(ui_project: Any, tmp_path: Path) -> None:
    studio, publication, project = ui_project
    token = new_key(studio, publication["id"], ["run", "read"])
    scripted = studio.app.state.scripted if hasattr(studio.app.state, "scripted") else None
    with _ui_app(project, tmp_path / "data", scripted, _key_line(token)) as (client, ui):
        page = client.get("/")
        assert page.status_code == 200
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY"
        assert client.get("/ui/assets/app.js").status_code == 200
        for path in ("/ui/assets/../webui_server.py", "/ui/assets/%2e%2e/standalone.py", "/ui/assets/i18n/none.json"):
            assert client.get(path).status_code == 404, path
        boot = client.get("/ui/api/bootstrap").json()
        assert boot["session"] is None and boot["language"] == "de" and "pt-BR" in boot["languages"]
        assert client.get("/ui/api/config").status_code == 401
        # The operator's one-time link.
        ui.sessions.add_launch_token("launch-1")
        answer = client.get("/ui/launch?token=launch-1", follow_redirects=False)
        assert answer.status_code in (302, 303) and answer.headers["location"] == "/"
        cookie = answer.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        config = client.get("/ui/api/config").json()
        assert config["output_dir"]["path"] and config["fields"][0]["name"] == "customer"
        setup = client.get("/ui/api/setup").json()
        assert setup["providers"][0]["id"] == "anthropic" and "sk-" not in json.dumps(setup)
        # Changes need the interface's header and the same origin.
        assert client.put("/ui/api/setup/providers/anthropic", json={"api_key": "sk-ant-x"}).status_code == 403
        headers = {"X-Nodestro-UI": "1", "Origin": "http://evil.example"}
        assert (
            client.put("/ui/api/setup/providers/anthropic", json={"api_key": "sk"}, headers=headers).status_code == 403
        )
        headers["Origin"] = "http://127.0.0.1:8800"
        saved = client.put("/ui/api/setup/providers/anthropic", json={"api_key": "sk-ant-x"}, headers=headers)
        assert saved.status_code == 200 and "sk-ant-x" not in saved.text and saved.json()["configured"]
        assert client.get("/ui/launch?token=launch-1", follow_redirects=False).headers["location"] != "/"
        # The cookie also authenticates the published API (reading needs no header) ...
        assert client.get(f"/api/published/{publication['slug']}/runs").status_code == 200
        # ... but starting a run with it needs the header as well.
        start = f"/api/published/{publication['slug']}/runs"
        assert client.post(start, json={"values": {"customer": "ACME"}}).status_code == 403
        client.delete("/ui/api/session", headers=headers)
        # Somebody else signs in with an API key: no operator pages.
        assert client.post("/ui/api/session", json={"api_key": "ndk_wrong"}, headers=headers).status_code == 401
        signed = client.post("/ui/api/session", json={"api_key": token}, headers=headers)
        assert signed.status_code == 200 and signed.json() == {"scopes": ["run", "read"], "operator": False, "name": ""}
        assert client.get("/ui/api/setup").status_code == 403
        assert "path" not in client.get("/ui/api/config").json()["output_dir"]
        # The launcher's control endpoints answer only with the control token.
        assert client.post("/ui/control/launch").status_code == 404


# ---- the package -----------------------------------------------------------------------------------------------


def test_settings_file_is_valid_and_maps_to_the_application(tmp_path: Path) -> None:
    options = exporter.ExportOptions(kind="portable", web_ui=True, language="fr", offline=True, output_dir="C:\\Out")
    text_ = portable_build.settings_toml(options, context_length=16384)
    values = tomllib.loads(text_)
    assert values["server"]["host"] == "127.0.0.1" and values["server"]["port"] == 8800
    assert values["web"]["language"] == "fr" and values["models"]["offline"] is True
    assert values["models"]["context_length"] == 16384 and values["data"]["output_dir"] == "C:\\Out"
    settings = portable.Settings(tmp_path, values)
    environment = settings.environment()
    assert environment["OUTPUT_DIR"] == "C:\\Out" and environment["WEB_UI"] == "on"
    assert environment["UI_LANGUAGE"] == "fr" and environment["DATA_DIR"] == str(tmp_path / "data")


def test_start_scripts_per_platform() -> None:
    windows = portable_build.start_scripts(portable_build.PLATFORMS["windows-x86_64"])
    assert set(windows) == {"start.cmd", "stop.cmd", "configure.cmd"}
    assert all("\r\n" in content for content, _mode in windows.values())
    assert "python\\python.exe" in windows["start.cmd"][0] and " -I " in windows["start.cmd"][0]
    mac = portable_build.start_scripts(portable_build.PLATFORMS["macos-arm64"])
    assert "Start.command" in mac and mac["start.sh"][1] & 0o111
    linux = portable_build.start_scripts(portable_build.PLATFORMS["linux-x86_64"])
    assert "Start.command" not in linux and linux["start.sh"][0].startswith("#!/bin/sh")


def test_archive_keeps_modes_and_checksums(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    (stage / "python" / "bin").mkdir(parents=True)
    (stage / "python" / "bin" / "python3.13").write_bytes(b"\x7fELF")
    (stage / "python" / "bin" / "python3.13").chmod(0o755)
    (stage / "python" / "bin" / "python3").symlink_to("python3.13")
    (stage / "README.md").write_text("hello")
    sums = portable_build.checksums(stage)
    assert "README.md" in sums and "python/bin/python3.13" in sums

    class Quiet:
        def bytes(self, *_args: Any, **_kwargs: Any) -> None: ...

        def check(self) -> None: ...

        def __getattr__(self, _name: str) -> Any:
            return lambda *args, **kwargs: None

    target = tmp_path / "out.zip"
    portable_build.write_zip(stage, "app-v1-linux-x86_64", target, Quiet(), posix=True)  # type: ignore[arg-type]
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert all(name.startswith("app-v1-linux-x86_64/") for name in names)
        info = archive.getinfo("app-v1-linux-x86_64/python/bin/python3.13")
        assert (info.external_attr >> 16) & 0o111
        link = archive.getinfo("app-v1-linux-x86_64/python/bin/python3")
        assert stat.S_ISLNK(link.external_attr >> 16) and archive.read(link) == b"python3.13"


def test_portable_packages_include_multipart_support() -> None:
    assert any(item.startswith("python-multipart") for item in exporter._APP_PACKAGES)


# ---- README in six languages -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["standard", "portable"])
@pytest.mark.parametrize("language", LANGUAGES)
def test_readme_templates_have_the_same_placeholders(kind: str, language: str) -> None:
    reference = exporter.readme_template(kind, "en")
    path = exporter.README_DIR / f"{kind}.{language}.md"
    assert path.is_file(), f"{path.name} is missing"
    translated = path.read_text(encoding="utf-8")
    assert sorted(PLACEHOLDER.findall(translated)) == sorted(PLACEHOLDER.findall(reference))
    assert [line.count("|") for line in translated.splitlines() if line.startswith("|")] == [
        line.count("|") for line in reference.splitlines() if line.startswith("|")
    ]
    strings = exporter.readme_strings(language)
    english = exporter.readme_strings("en")
    assert set(json.loads((exporter.README_DIR / f"strings.{language}.json").read_text("utf-8"))) == set(english)
    for key, value in english.items():
        assert sorted(PLACEHOLDER.findall(strings[key])) == sorted(PLACEHOLDER.findall(value)), (language, key)


@pytest.mark.parametrize("language", LANGUAGES)
def test_exported_readme_is_complete(
    published: Callable[..., tuple[TestClient, dict[str, Any]]], tmp_path: Path, language: str
) -> None:
    studio, publication = published()
    record = _export(studio, publication["id"], language=language)
    project = _extract(_download(studio, record["id"]), tmp_path / language)
    readme = (project / "README.md").read_text("utf-8")
    leftovers = set(PLACEHOLDER.findall(readme)) - {"{run_id}", "{file_id}"}
    assert not leftovers, leftovers


def test_portable_readme_parts_are_filled() -> None:
    from nodestro_api.export_models import LocalModelInfo, NetworkNeed

    class Check:
        def __init__(self) -> None:
            self.providers = ["ollama", "anthropic"]
            self.local_models = [
                LocalModelInfo(name="qwen3.5:4b", roles=["agent"], size=3_400_000_000, license="Apache-2.0")
            ]
            self.bundled_models = ["qwen3.5:4b"]
            self.network_needs = [
                NetworkNeed(kind="cloud_model", name="anthropic:claude-haiku-4-5", detail="Anthropic")
            ]

    for language in LANGUAGES:
        options = exporter.ExportOptions(kind="portable", language=language, web_ui=True)
        text_ = portable_build.fill_readme(
            exporter.readme_template("portable", language),
            options,
            Check(),
            portable_build.PLATFORMS["windows-x86_64"],
            {"version": "0.40.14", "bytes": 3_400_000_000},
            python_version="3.13.9",
            context_length=32768,
        )
        assert "qwen3.5:4b" in text_ and "Windows" in text_ and "ANTHROPIC_API_KEY" in text_
        assert "anthropic:claude-haiku-4-5" in text_ and "Apache-2.0" in text_
        for token in ("{platform}", "{models_section}", "{providers_section}", "{network_section}", "{ui_line}"):
            assert token not in text_, (language, token)


def test_launcher_keys_are_stored_as_hashes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = portable.Settings(tmp_path, {"data": {"data_dir": "data"}})
    args = portable.parser().parse_args(["keys", "new", "--name", "CRM", "--scopes", "run,read"])
    assert portable.keys(args, settings) == 0
    token = capsys.readouterr().out.splitlines()[1].strip()
    stored = (tmp_path / "data" / "api-keys.txt").read_text()
    assert token.split("_")[-1] not in stored and stored.count(":") >= 3 and "run+read:CRM" in stored
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "data" / "api-keys.txt").stat().st_mode) == 0o600
    from nodestro_api.standalone import parse_keys

    assert parse_keys(stored.strip())[0].scopes == ["run", "read"]


def test_zip_of_a_standard_export_is_unchanged_by_portable_options(
    published: Callable[..., tuple[TestClient, dict[str, Any]]],
) -> None:
    studio, publication = published()
    record = _export(studio, publication["id"])
    with zipfile.ZipFile(io.BytesIO(_download(studio, record["id"]))) as archive:
        names = {name.split("/", 1)[1] for name in archive.namelist()}
    assert "app/main.py" in names and "openapi.json" in names
    assert not any(name.startswith(("python/", "ollama/", "models/", "config/settings.toml")) for name in names)
    assert not any(
        name.startswith("nodestro_api/webui/") and not name.startswith("nodestro_api/webui/i18n") for name in names
    )
    refused = studio.post(f"/api/publications/{publication['id']}/exports", json={"options": {"kind": "portable"}})
    assert refused.status_code in (400, 409, 422)
    _ = text


def test_check_works_on_a_fresh_installation_without_a_local_model_folder(
    published: Callable[..., tuple[TestClient, dict[str, Any]]],
) -> None:
    studio, publication = published()
    options = {"kind": "portable", "platform": portable_build.host_platform(), "confirm_distribution": True}
    checked = studio.post(f"/api/publications/{publication['id']}/export/check", json={"options": options})
    assert checked.status_code == 200, checked.text
    assert checked.json()["portable"]["local_models"] == []


@pytest.mark.parametrize(
    ("model", "with_google"), [("anthropic:claude-haiku-4-5", False), ("gemini:gemini-3-flash", True)]
)
def test_the_google_sdk_is_packaged_only_for_a_release_that_uses_gemini(model: str, with_google: bool) -> None:
    writer = agent_node("writer", "WRITER")
    writer["agent"]["model"] = model
    bundle = _release_bundle(
        [{"id": "start", "type": "start"}, writer, {"id": "out", "type": "output"}],
        [edge("start", "writer"), edge("writer", "out")],
    )
    bundle.python = []
    direct, _ = portable_build.requirements(exporter.ExportOptions(kind="portable"), bundle)
    assert any(item.startswith("google-genai") for item in direct) is with_google
    assert any(item.startswith("anthropic") for item in direct)


def test_the_portable_check_has_no_git_reference_and_names_downloads_by_code(
    published: Callable[..., tuple[TestClient, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UX handover B5: a portable package always bundles orkexa (no "choose the Git reference" error); downloads are
    named by codes the studio can translate."""
    studio, publication = published()
    monkeypatch.setattr(
        portable_build,
        "downloads",
        lambda ctx, platform, local_models: [
            portable_build.Download(what="python", size=10, cached=False),
            portable_build.Download(what="libraries", size=10, cached=False),
        ],
    )
    url = f"/api/publications/{publication['id']}/export/check"
    options: dict[str, Any] = {"kind": "portable", "platform": portable_build.host_platform(), "allow_downloads": False}
    codes = {item["code"]: item for item in studio.post(url, json={"options": options}).json()["problems"]}
    assert "export.portable_distribution" in codes and "export.distribution_unconfirmed" not in codes
    assert codes["export.download_needed"]["params"]["downloads"] == ["python", "libraries"]
    options.update(confirm_distribution=True, allow_downloads=True)
    codes = {item["code"]: item for item in studio.post(url, json={"options": options}).json()["problems"]}
    assert codes["export.downloads"]["params"]["downloads"] == ["python", "libraries"]
    assert codes["export.downloads"]["params"]["items"] == "python, libraries"  # kept for older clients
    # The source project still asks before copying orkexa.
    standard = studio.post(url, json={"options": {"orkexa": "bundle"}}).json()["problems"]
    assert "export.distribution_unconfirmed" in {item["code"] for item in standard}
