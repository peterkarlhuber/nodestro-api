"""Export a published release as a standalone FastAPI project (a ZIP file).

The project is generated deterministically, without any AI request:

- ``nodestro_runtime/`` and ``nodestro_api/``: the installed packages ``nodestro-runtime`` (engine,
  tools, knowledge, memory, result files ...) and the HTTP layer of published workflows from
  ``nodestro-api`` (``serving``, ``standalone``), copied byte for byte from this installation,
  with their versions, source commits and SHA-256 in ``RUNTIME.json``. Which modules belong to
  it is computed from their imports (:func:`runtime_modules`); the studio services (architect,
  assistant, settings, publishing) never do. So the export runs the same engine, in the same
  version, as the studio: no second implementation, nothing rewritten.
- ``workflow/release.json`` and ``manifest.json``: the immutable release and what was exported.
- ``app/main.py``, ``config.py``, ``schemas.py``, ``keys.py``: a thin application around the runtime,
  configuration from environment variables, the request models as readable code, a key tool.
- ``pyproject.toml`` and ``requirements.lock``: exact versions of everything this installation
  runs with, plus the approved libraries of the workflow's tools.
- ``tests/``: tests with a simulated model (no cost), ``examples/`` and a README.

The agent library ``orkexa`` is not on PyPI. The user chooses how the export gets it: ``git``
names the exact commit of its repository (installing needs access to it while it is private),
``bundle`` copies the library's source into ``vendor/`` (only after confirming the right to pass
it on; the MIT license text travels with it). Both are stated in the README and the manifest.

The agents' providers (Anthropic, OpenAI, Gemini, OpenAI-compatible endpoints) are configured
with environment variables in the export; the manifest names the providers, the capabilities
declared for endpoints and base URLs without credentials. Knowledge files and memory are data,
not configuration: the user picks which are copied into the project. Nothing secret is
exported: no API keys of any provider, no credentials in URLs, no settings.
"""

from __future__ import annotations

import ast
from datetime import datetime
import hashlib
from importlib import metadata
import io
import json
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Any, Literal
import zipfile

from nodestro_runtime import __version__ as runtime_version
from nodestro_runtime.builtin_tools import BUILTIN_TOOLS
from nodestro_runtime.engine import start_fields
from nodestro_runtime.problems import CheckIssue, NodestroError
from nodestro_runtime.providers import PROVIDERS, base_url_variable, has_credentials, provider_of
from nodestro_runtime.release import ReleaseBundle, current_runtime
from nodestro_runtime.schemas import InputField, new_id, now
from orkexa import __version__ as library_version
from packaging.markers import default_environment
from packaging.requirements import Requirement
from pydantic import BaseModel, Field

from . import __version__
from .api_examples import KEY_VARIABLE, examples
from .publishing import Publication, PublicationEvent, Release
from .serving import ApiLimits, ServedRelease

if TYPE_CHECKING:
    from nodestro_runtime.storage import Store

    from .context import Nodestro

EXPORTS = "api_exports"
GUIDES = "api_guides"
#: The packages an export copies: distribution -> import package.
RUNTIME_PACKAGES = {"nodestro-runtime": "nodestro_runtime", "nodestro-api": "nodestro_api"}
#: The modules an exported project starts from; everything they import (within these packages) is added.
RUNTIME_ROOTS = (
    "nodestro_api.standalone",
    "nodestro_api.serving",
    "nodestro_runtime.release",
    "nodestro_runtime.engine",
    "nodestro_runtime.reports",
    "nodestro_runtime.run_views",
)
#: Studio services that must never end up in an export.
STUDIO_ONLY = frozenset(
    {
        "nodestro_api.app",
        "nodestro_api.api",
        "nodestro_api.context",
        "nodestro_api.publishing",
        "nodestro_api.exporter",
        "nodestro_api.architect",
        "nodestro_api.assistant",
        "nodestro_api.network",
        "nodestro_api.api_keys",
        "nodestro_api.backup",
        "nodestro_runtime.workspace",
        "nodestro_runtime.launcher",
        "nodestro_runtime.preflight",
        "nodestro_runtime.python_envs",
    }
)
_FIXED_TIME = (2026, 1, 1, 0, 0, 0)
NODESTRO_LICENSE = """MIT License

Copyright (c) 2026 Peter Karlhuber

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


# ---- options, checks, records -----------------------------------------------------------


class ExportOptions(BaseModel):
    #: ``git``: install orkexa from its repository at the exact commit; ``bundle``: copy its source
    #: into the project.
    orkexa: Literal["git", "bundle"] = "git"
    #: Required for ``bundle``: the user may pass the library's source on.
    confirm_distribution: bool = False
    #: Knowledge files (ids) whose content is copied into the project.
    knowledge: list[str] = Field(default_factory=list)
    #: Persistent memory spaces whose current content becomes the project's starting memory.
    memory: list[str] = Field(default_factory=list)
    #: Language of the README (German or English).
    language: Literal["de", "en"] = "en"
    #: An accepted AI-written guide to add as ``docs/AI_GUIDE.md`` (see :mod:`nodestro_api.api_guide`).
    guide_id: str | None = None


class ExportResource(BaseModel):
    kind: Literal["knowledge", "memory"]
    id: str
    name: str
    size: int = 0
    used_by: list[str] = Field(default_factory=list)
    available: bool = True
    included: bool = False


class ExportCheck(BaseModel):
    ok: bool
    problems: list[CheckIssue] = Field(default_factory=list)
    resources: list[ExportResource] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    tool_libraries: list[str] = Field(default_factory=list)
    runtime_modules: list[str] = Field(default_factory=list)
    runtime: dict[str, str] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list)


class ExportRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("exp"))
    publication_id: str
    release_id: str
    release_number: int
    created_at: datetime = Field(default_factory=now)
    options: ExportOptions
    file_name: str
    size: int
    sha256: str
    files: int
    warnings: list[str] = Field(default_factory=list)
    runtime: dict[str, str] = Field(default_factory=dict)


# ---- the runtime -------------------------------------------------------------------------


def _package_dirs() -> dict[str, Path]:
    """Where the copied packages are installed (the import package's directory)."""
    import nodestro_runtime

    return {
        "nodestro_runtime": Path(nodestro_runtime.__file__).resolve().parent,
        "nodestro_api": Path(__file__).resolve().parent,
    }


class _Imports(ast.NodeVisitor):
    """Imports of a module within the copied packages, without those only used for type checking."""

    def __init__(self, package: str, dirs: dict[str, Path]) -> None:
        self.package = package
        self.dirs = dirs
        self.found: set[str] = set()

    def _add(self, package: str, name: str) -> None:
        if (self.dirs[package] / f"{name}.py").is_file():
            self.found.add(f"{package}.{name}")

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            for item in node.orelse:
                self.visit(item)
            return
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 1:
            package, module = self.package, node.module
        elif node.level == 0 and node.module and node.module.split(".")[0] in self.dirs:
            package, _, module = node.module.partition(".")
        else:
            return
        if module:
            self._add(package, module.split(".")[0])
        else:
            for alias in node.names:
                self._add(package, alias.name)


def runtime_modules(dirs: dict[str, Path] | None = None) -> list[str]:
    """The modules an exported project needs (``package.module``), from :data:`RUNTIME_ROOTS` and their imports."""
    dirs = dirs or _package_dirs()
    seen: set[str] = set()
    queue = list(RUNTIME_ROOTS)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        package, _, module = name.partition(".")
        source = dirs[package] / f"{module}.py"
        if not source.is_file():
            raise FileNotFoundError(source)
        seen.add(name)
        visitor = _Imports(package, dirs)
        visitor.visit(ast.parse(source.read_text(encoding="utf-8")))
        queue += sorted(visitor.found - seen)
    leaked = seen & STUDIO_ONLY
    if leaked:
        raise RuntimeError(f"Studio modules would be exported: {sorted(leaked)}")
    return sorted(seen)


def runtime_sources() -> dict[str, dict[str, str]]:
    """Version and source of each copied package: the Git commit it was installed from (PEP 610), if known."""
    found: dict[str, dict[str, str]] = {}
    for dist in RUNTIME_PACKAGES:
        entry = {"version": _version(dist)}
        try:
            direct = metadata.distribution(dist).read_text("direct_url.json")
        except metadata.PackageNotFoundError:
            direct = None
        if direct:
            data = json.loads(direct)
            commit = (data.get("vcs_info") or {}).get("commit_id")
            if commit:
                entry["commit"] = commit
                entry["repository"] = str(data.get("url", ""))
            elif (data.get("dir_info") or {}).get("editable"):
                entry["source"] = "editable"
        found[dist] = entry
    return found


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- dependencies ------------------------------------------------------------------------

#: What the exported application itself needs (orkexa with its provider SDKs comes separately).
_APP_PACKAGES = ("fastapi", "uvicorn[standard]", "pydantic", "pypdf")
#: The agent library with every provider SDK (Anthropic, OpenAI, Gemini) and PDF support.
_LIBRARY = "orkexa[all,pdf]"
_DEV_PACKAGES = ("pytest", "httpx")


def _version(name: str) -> str:
    return metadata.version(name)


def _closure(names: list[str]) -> dict[str, str]:
    """``{distribution: version}`` of the installed requirements of ``names`` (with extras, markers)."""
    environment = default_environment()
    found: dict[str, str] = {}
    queue: list[tuple[str, set[str]]] = []
    for item in names:
        requirement = Requirement(item)
        queue.append((requirement.name, set(requirement.extras)))
    seen: set[tuple[str, frozenset[str]]] = set()
    while queue:
        name, extras = queue.pop()
        key = re.sub(r"[-_.]+", "-", name).lower()
        marker = (key, frozenset(extras))
        if marker in seen:
            continue
        seen.add(marker)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        found[key] = dist.version
        for raw in dist.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is not None:
                base: dict[str, Any] = dict(environment)
                applies = any(requirement.marker.evaluate({**base, "extra": extra}) for extra in (extras or {""}))
                if not applies:
                    continue
            queue.append((requirement.name, set(requirement.extras)))
    return found


def _orkexa_git() -> str:
    """The commit this installation's orkexa came from (PEP 610), else the commit Nodestro pins.

    orkexa has no release tags, so a local or editable installation falls back to the exact
    revision in nodestro-runtime's requirements instead of a tag that does not exist.
    """
    try:
        direct = metadata.distribution("orkexa").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        direct = None
    if direct:
        data = json.loads(direct)
        commit = (data.get("vcs_info") or {}).get("commit_id")
        if commit and "github.com" in str(data.get("url", "")):
            return f"{_LIBRARY} @ git+{data['url']}@{commit}"
    for line in metadata.requires("nodestro-runtime") or []:
        requirement = Requirement(line)
        if requirement.name == "orkexa" and requirement.url:
            return f"{_LIBRARY} @ {requirement.url}"
    raise RuntimeError("nodestro-runtime's requirements do not pin orkexa")


def tool_libraries(bundle: ReleaseBundle) -> list[str]:
    """Pinned packages of the release's approved tool libraries (from the locks)."""
    from nodestro_runtime.python_envs import parse_lock

    # Packages the runtime itself brings (pydantic, ...) keep the runtime's pinned version.
    runtime = set(_closure([*_APP_PACKAGES, _LIBRARY]))
    pins: dict[str, str] = {}
    for lock in bundle.python:
        for package in parse_lock(lock.lock):
            key = re.sub(r"[-_.]+", "-", package.key).lower()
            if key not in runtime:
                pins.setdefault(key, f"{package.name}=={package.version}")
    return sorted(pins.values())


def dependencies(options: ExportOptions, bundle: ReleaseBundle) -> tuple[list[str], list[str], str]:
    """Direct requirements (pinned), the full lock lines and the orkexa line."""
    agent_line = f"{_LIBRARY} @ ./vendor/orkexa" if options.orkexa == "bundle" else _orkexa_git()
    direct = [f"{Requirement(name).name}{_extras(name)}=={_version(Requirement(name).name)}" for name in _APP_PACKAGES]
    closure = _closure([*_APP_PACKAGES, _LIBRARY])
    closure.pop("orkexa", None)
    lock = [f"{name}=={version}" for name, version in sorted(closure.items())]
    tools = tool_libraries(bundle)
    return direct + tools, sorted(set(lock) | set(tools)), agent_line


def _extras(name: str) -> str:
    extras = sorted(Requirement(name).extras)
    return f"[{','.join(extras)}]" if extras else ""


# ---- checks ------------------------------------------------------------------------------


def release_providers(bundle: ReleaseBundle) -> list[str]:
    """The providers the release's agents and judges use (bare Claude ids: Anthropic)."""
    found: set[str] = set()
    for model in bundle.models:
        try:
            found.add(provider_of(model))
        except NodestroError:
            continue
    return sorted(found)


def _issue(code: str, message: str, level: Literal["error", "warning", "info"] = "info", **params: Any) -> CheckIssue:
    return CheckIssue(code=code, message=message, level=level, params=params)


def check(ctx: Nodestro, publication: Publication, release: Release, options: ExportOptions) -> ExportCheck:
    """What the export would contain and what to know before downloading it; nothing is written."""
    bundle = release.bundle
    problems: list[CheckIssue] = []
    resources: list[ExportResource] = []
    for ref in bundle.knowledge:
        available = ref.id in ctx.knowledge.store
        included = ref.id in options.knowledge
        resources.append(
            ExportResource(
                kind="knowledge",
                id=ref.id,
                name=ref.name,
                size=ref.size,
                used_by=ref.used_by,
                available=available,
                included=included and available,
            )
        )
        if included and not available:
            problems.append(
                _issue("export.knowledge_missing", f"{ref.name} no longer exists", level="error", name=ref.name)
            )
        elif not included:
            problems.append(
                _issue(
                    "export.knowledge_excluded",
                    f"{ref.name} is not included: the agents of the export run without it",
                    level="warning",
                    name=ref.name,
                )
            )
        elif available and ctx.knowledge.store.info(ref.id).sha256 != ref.sha256:
            problems.append(
                _issue(
                    "export.knowledge_changed",
                    f"{ref.name} changed since publishing; the export contains the current file",
                    level="warning",
                    name=ref.name,
                )
            )
    spaces = {info.name: info for info in ctx.memory.all()}
    for space_ref in bundle.memory:
        info = spaces.get(space_ref.space)
        resources.append(
            ExportResource(
                kind="memory",
                id=space_ref.space,
                name=space_ref.space,
                size=info.size if info else 0,
                used_by=space_ref.used_by,
                included=space_ref.space in options.memory,
            )
        )
        problems.append(
            _issue(
                "export.memory_included" if space_ref.space in options.memory else "export.memory_empty",
                f"Memory {space_ref.space}: "
                + (
                    "its current content is the starting memory"
                    if space_ref.space in options.memory
                    else "starts empty"
                ),
                level="info",
                space=space_ref.space,
            )
        )
    foreign = sorted(set(bundle.builtin_tools) - {tool.name for tool in BUILTIN_TOOLS})
    if foreign:
        problems.append(
            _issue(
                "export.integration_tools",
                f"Tools of installed integrations cannot be exported yet: {', '.join(foreign)}",
                level="error",
                tools=", ".join(foreign),
            )
        )
    if options.orkexa == "bundle" and not options.confirm_distribution:
        problems.append(
            _issue(
                "export.distribution_unconfirmed",
                "Confirm that you may pass on the source code of orkexa, or choose the Git reference",
                level="error",
            )
        )
    if options.orkexa == "git":
        problems.append(
            _issue(
                "export.private_repository",
                "Installing the export needs read access to the repository peterkarlhuber/orkexa while it is private",
                level="warning",
            )
        )
    else:
        problems.append(
            _issue(
                "export.bundled_library",
                "The source code of orkexa is copied into the project (MIT license included)",
                level="info",
            )
        )
    published_library = bundle.runtime.get("orkexa") or "?"
    if bundle.runtime.get("nodestro") != runtime_version or bundle.runtime.get("orkexa") != library_version:
        problems.append(
            _issue(
                "export.runtime_differs",
                "This release was published with another Nodestro or agent library version; "
                "the export contains the current runtime",
                level="warning",
                published=f"{bundle.runtime.get('nodestro')} / {published_library}",
                current=f"{runtime_version} / orkexa {library_version}",
            )
        )
    for provider in release_providers(bundle):
        provider_info = PROVIDERS.get(provider)
        if provider_info is None:
            continue
        variables = provider_info.env_vars or [f"{provider.upper()}_API_KEY"]
        if provider_info.base_url:
            variables = [base_url_variable(provider), *variables]
        problems.append(
            _issue(
                "export.provider",
                f"{provider_info.label}: configure {' / '.join(variables)} in the export's environment",
                level="info",
                provider=provider,
                variables=" / ".join(variables),
            )
        )
        stored = ctx.provider_settings(provider)
        url = str(stored.get("base_url") or "")
        if url and has_credentials(url):
            problems.append(
                _issue(
                    "export.base_url_withheld",
                    f"The base URL of {provider_info.label} contains credentials and is not exported; "
                    f"set {base_url_variable(provider)} in the export",
                    level="warning",
                    provider=provider,
                    variable=base_url_variable(provider),
                )
            )
    if bundle.python:
        problems.append(
            _issue(
                "export.tool_libraries",
                "The approved tool libraries are installed into the project's environment",
                level="info",
                packages=", ".join(tool_libraries(bundle)),
            )
        )
    if ctx.settings().sandbox == "docker":
        problems.append(
            _issue(
                "export.sandbox_process",
                "The export runs tools in a separate process (the Docker sandbox is a studio setting)",
                level="info",
            )
        )
    if bundle.approvals():
        problems.append(
            _issue(
                "export.approvals",
                "Approvals are decided through the API of the export (permission approve); "
                "the studio does not see the export's runs",
                level="info",
            )
        )
    if bundle.server_tools:
        problems.append(
            _issue(
                "export.server_tools",
                f"Server tools ({', '.join(bundle.server_tools)}) are called through the Anthropic API",
                level="info",
                tools=", ".join(bundle.server_tools),
            )
        )
    if options.guide_id:
        guide = ctx.store.get_raw(GUIDES, options.guide_id)
        if guide is None or not guide.get("accepted") or guide.get("release_id") != release.id:
            problems.append(
                _issue("export.guide_not_accepted", "The AI guide must be reviewed and accepted first", level="error")
            )
    direct, _, agent_line = dependencies(options, bundle)
    order = {"error": 0, "warning": 1, "info": 2}
    problems.sort(key=lambda item: order[item.level])
    files = sorted(_project_files(ctx, publication, release, options, dry_run=True))
    return ExportCheck(
        ok=not any(item.level == "error" for item in problems),
        problems=problems,
        resources=resources,
        dependencies=[*direct, agent_line],
        tool_libraries=tool_libraries(bundle),
        runtime_modules=runtime_modules(),
        runtime={"nodestro": runtime_version, "orkexa": library_version},
        files=files,
    )


# ---- generation ---------------------------------------------------------------------------


def project_name(publication: Publication) -> str:
    return f"{publication.slug}-api"


def _python_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def schemas_source(bundle: ReleaseBundle) -> str:
    """``app/schemas.py``: the request body as plain Pydantic classes (generated, for reading and clients).

    The running API validates with the runtime's own models (:func:`nodestro_api.serving.input_model`);
    a test of the exported project checks that both describe the same fields.
    """
    fields = start_fields(bundle.workflow)
    lines = [
        '"""The request body of this API as plain Pydantic models (generated by Nodestro, do not edit).',
        "",
        "The running API validates requests with the runtime's own models; tests/test_api.py checks",
        "that these classes describe the same fields. Use them in your own client code.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from datetime import date",
        "from typing import Literal",
        "",
        "from pydantic import BaseModel, ConfigDict, Field",
        "",
        "",
        "class FileInput(BaseModel):",
        '    model_config = ConfigDict(extra="forbid")',
        "",
        "    name: str = Field(min_length=1, max_length=200)",
        "    media_type: str | None = None",
        "    content_base64: str = Field(min_length=1)",
        "",
        "",
    ]
    if not fields:
        lines += [
            "class RunRequest(BaseModel):",
            '    model_config = ConfigDict(extra="forbid")',
            "",
            '    input: str = Field(default="", max_length=200_000)',
            "    files: list[FileInput] = Field(default_factory=list)",
            "",
        ]
        return "\n".join(lines)
    lines += ["class Values(BaseModel):", '    model_config = ConfigDict(extra="forbid")', ""]
    for field in fields:
        lines.append(f"    {field.name}: {_field_annotation(field)}")
    required = any(field.required and field.default in (None, "", []) for field in fields)
    lines += [
        "",
        "",
        "class RunRequest(BaseModel):",
        '    model_config = ConfigDict(extra="forbid")',
        "",
        f"    values: Values{'' if required else ' | None = None'}",
        '    input: str = Field(default="", max_length=200_000)',
        "    files: list[FileInput] = Field(default_factory=list)",
        "",
    ]
    return "\n".join(lines)


def _field_annotation(field: InputField) -> str:
    types = {"text": "str", "textarea": "str", "number": "int | float", "date": "date", "boolean": "bool"}
    if field.type == "select":
        options = [option for option in field.options if option.strip()]
        annotation = f"Literal[{', '.join(_python_literal(option) for option in options)}]" if options else "str"
    elif field.type == "file":
        annotation = "FileInput | list[FileInput]"
    else:
        annotation = types[field.type]
    description = _python_literal(field.description or field.label or field.name)
    required = field.required and field.default in (None, "", [])
    if required:
        return f"{annotation} = Field(description={description})"
    return f"{annotation} | None = Field(default=None, description={description})"


_MAIN = '''"""The API of the exported workflow {name!r} (generated by Nodestro, release v{number}).

Start:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from pathlib import Path

from nodestro_api.standalone import create_app

from .config import load_environment

PROJECT_DIR = Path(__file__).resolve().parent.parent

load_environment(PROJECT_DIR / ".env")
app = create_app(PROJECT_DIR)
'''

_CONFIG = '''"""Configuration from environment variables (generated by Nodestro).

Every setting is read from the environment; a .env file next to pyproject.toml is read first
without overriding variables that are already set. See .env.example and the README.

ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY (or GOOGLE_API_KEY)
                      The keys of the AI providers the agents use (only the ones the workflow needs;
                      see the README). Without them, starting a run answers 503.
<PROVIDER>_BASE_URL   Base URL of an OpenAI-compatible endpoint, Ollama or vLLM (COMPATIBLE_, OLLAMA_,
                      VLLM_BASE_URL), with an optional <PROVIDER>_API_KEY.
API_KEYS              Who may call this API: entries <id>:<sha256>:<scopes>[:<name>], separated by ";".
                      Create entries with: python -m app.keys new --name "My app" --scopes run,read
DATA_DIR              Where runs, approvals, files and memory are stored (default: ./data).
API_DOCS              local (default: /docs only from this computer), on, off.
RUN_RETENTION_DAYS    Delete runs older than this many days at start (0: keep everything).
TOOL_TIMEOUT_SECONDS  Time limit of one tool call (default 60).
TOOL_SANDBOX          subprocess (default): only tools written by a person in the tool editor run, as a
                      process of this server. docker: every own tool runs in an isolated container
                      (needs Docker); AI-generated tools run only with docker.
LIMIT_<NAME>          Override a limit published with the release, e.g. LIMIT_DAILY_BUDGET_USD=10,
                      LIMIT_MAX_CONCURRENT_RUNS=4, LIMIT_RUN_BUDGET_USD=0.5, LIMIT_SYNC_ENABLED=true.
"""

import os
from pathlib import Path

from nodestro_api.standalone import StandaloneSettings


def load_environment(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\\""))


def settings() -> StandaloneSettings:
    return StandaloneSettings.from_env()
'''

_KEYS = '''"""Create API keys for this project (generated by Nodestro).

    python -m app.keys new --name "CRM" --scopes run,read
    python -m app.keys hash ndk_...

The key is printed once; only the line for API_KEYS (id, SHA-256 of the secret, permissions)
belongs into your configuration. Permissions: run (start, cancel, continue), read (status,
result, events, files, usage), approve (decide approvals).
"""

import argparse

from nodestro_api.serving import SCOPES, hash_secret, new_key, parse_token


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.keys")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("new", help="create a new key")
    create.add_argument("--name", default="")
    create.add_argument("--scopes", default="run,read")
    digest = commands.add_parser("hash", help="the API_KEYS entry of an existing key")
    digest.add_argument("token")
    digest.add_argument("--scopes", default="run,read")
    digest.add_argument("--name", default="")
    args = parser.parse_args()
    scopes = [scope.strip() for scope in args.scopes.split(",") if scope.strip() in SCOPES]
    if not scopes:
        parser.error(f"--scopes: choose from {', '.join(SCOPES)}")
    if args.command == "new":
        key_id, secret, token = new_key()
        print("Key (shown once, give it to the client):")
        print(f"  {token}")
    else:
        parsed = parse_token(args.token)
        if parsed is None:
            parser.error("not a key of the form ndk_<id>_<secret>")
        key_id, secret = parsed
    print("Add to API_KEYS (separate several entries with ;):")
    print(f"  {key_id}:{hash_secret(secret)}:{'+'.join(scopes)}{':' + args.name if args.name else ''}")


if __name__ == "__main__":
    main()
'''

_CONFTEST = '''"""Test helpers: a simulated model that answers every agent without calling any provider (no cost).

The simulation uses Orkexa's scripted provider: it stands in for every provider (Anthropic, OpenAI,
Gemini, OpenAI-compatible endpoints) while the tests run, so no key and no network are needed.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
import sys
import types
import typing
from typing import Any

from fastapi.testclient import TestClient
from orkexa import ModelCapabilities
from orkexa.core.types import ModelRequest, ModelResponse, TextPart, TokenUsage
from orkexa.providers import register_provider
from orkexa.providers.fake import FakeModel, FakeProvider, response
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from nodestro_api.serving import hash_secret, new_key  # noqa: E402
from nodestro_api.standalone import StandaloneSettings, create_app, parse_keys  # noqa: E402

ANSWER = "Simulated answer of the test model."


def example(annotation: Any) -> Any:
    """A value of the given type, for structured outputs of the simulated model."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        options = [item for item in typing.get_args(annotation) if item is not type(None)]
        return example(options[0]) if options else None
    if origin is list:
        return []
    if annotation is bool:
        return True
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0
    if annotation is datetime.date:
        return datetime.date(2026, 1, 31)
    if hasattr(annotation, "model_fields"):
        return annotation(**{name: example(info.annotation) for name, info in annotation.model_fields.items()})
    return ANSWER


class SimulatedModel(FakeModel):
    """Answers every request: the text ANSWER, or an example value of the requested output format."""

    def next(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        usage = TokenUsage(input_tokens=100, output_tokens=20)
        output = request.output_format
        if output is not None and hasattr(output, "model_fields"):
            value = example(output)
            return response([TextPart(text=value.model_dump_json())], parsed=value, usage=usage)
        return response([TextPart(text=ANSWER)], usage=usage)


class SimulatedProvider(FakeProvider):
    """Stands in for a real provider during the tests: takes its options, supports every feature."""

    def __init__(self, config: Any = None, asynchronous: bool = False) -> None:
        super().__init__(config, asynchronous)
        if not isinstance(self._client, FakeModel):
            self._client = SimulatedModel()

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities.all("supported")

    def check_options(self, options: Any) -> None:
        return None


SIMULATED_PROVIDERS = ("anthropic", "openai", "gemini", "compatible", "ollama", "vllm")


@pytest.fixture(autouse=True, scope="session")
def simulated_providers() -> None:
    """Every provider answers from the simulation in this test session (nothing leaves the computer)."""
    for name in SIMULATED_PROVIDERS:
        register_provider(name, SimulatedProvider, replace=True)


def make_key(scopes: str = "run+read+approve") -> tuple[str, str]:
    """A test key: (token, API_KEYS entry)."""
    key_id, secret, token = new_key()
    return token, f"{key_id}:{hash_secret(secret)}:{scopes}:test"


@pytest.fixture
def api(tmp_path: Path):
    """``api(**env)`` -> (TestClient, tokens by scope set); data lives in a temporary directory."""
    clients: list[TestClient] = []

    def factory(data_dir: Path | None = None, **env: str) -> tuple[TestClient, dict[str, str]]:
        tokens: dict[str, str] = {}
        entries = []
        for scopes in ("run+read+approve", "read", "run+read"):
            token, entry = make_key(scopes)
            tokens[scopes] = token
            entries.append(entry)
        environ = {"DATA_DIR": str(data_dir or tmp_path / "data"), "API_KEYS": ";".join(entries), **env}
        settings = StandaloneSettings.from_env(environ)
        simulated = SimulatedModel()
        app = create_app(PROJECT_DIR, settings=settings, client_factory=lambda _provider: simulated)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, tokens

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


__all__ = ["api", "headers", "make_key", "parse_keys", "SimulatedModel", "os"]
'''

_TEST_API = '''"""Tests of the exported API with a simulated model: nothing is paid, nothing leaves this computer."""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from conftest import PROJECT_DIR, headers

MANIFEST = json.loads((PROJECT_DIR / "workflow" / "manifest.json").read_text(encoding="utf-8"))
RELEASE = json.loads((PROJECT_DIR / "workflow" / "release.json").read_text(encoding="utf-8"))
SLUG = MANIFEST["slug"]
BASE = f"/api/published/{SLUG}"
EXAMPLE = json.loads((PROJECT_DIR / "examples" / "request.json").read_text(encoding="utf-8"))


def finish(client: Any, token: str, run_id: str, *, approve: bool = True, timeout: float = 60) -> dict[str, Any]:
    """Follow a run until it ends; waiting approvals are approved through the API."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"{BASE}/runs/{run_id}", headers=headers(token)).json()
        if status["status"] == "awaiting_approval" and approve:
            for approval in status["approvals"]:
                answer = client.post(
                    f"{BASE}/runs/{run_id}/approve",
                    headers=headers(token),
                    json={"approval_id": approval["approval_id"], "decision": "approved", "comment": "test"},
                )
                assert answer.status_code == 200, answer.text
            continue
        if status["status"] not in ("queued", "running"):
            if status["status"] != "awaiting_approval" or not approve:
                return status
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def test_app_starts_with_the_published_version(api):
    client, tokens = api()
    info = client.get(BASE, headers=headers(tokens["read"]))
    assert info.status_code == 200, info.text
    assert info.json()["version"] == MANIFEST["release_number"]
    assert client.get("/health").json() == {"status": "ok"}


def test_release_matches_its_manifest():
    from nodestro_runtime.release import ReleaseBundle

    bundle = ReleaseBundle.model_validate(RELEASE)
    assert bundle.digest() == MANIFEST["release_digest"]


def test_routes_and_openapi(api):
    client, _ = api()
    document = client.get("/openapi.json").json()
    for path in ("/runs", "/runs/{run_id}", "/runs/{run_id}/result", "/runs/{run_id}/approve"):
        assert f"{BASE}{path}" in document["paths"]
    assert "WorkflowOutput" in document["components"]["schemas"]


def test_authentication_is_required(api):
    client, tokens = api()
    assert client.post(f"{BASE}/runs", json=EXAMPLE).status_code == 401
    wrong = client.post(f"{BASE}/runs", json=EXAMPLE, headers=headers("ndk_000000000000_" + "x" * 43))
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "auth.invalid"
    read_only = client.post(f"{BASE}/runs", json=EXAMPLE, headers=headers(tokens["read"]))
    assert read_only.status_code == 403
    assert client.get("/api/published/other-api", headers=headers(tokens["read"])).status_code == 404


def test_invalid_requests_get_structured_errors(api):
    client, tokens = api()
    answer = client.post(f"{BASE}/runs", json={"unexpected": 1}, headers=headers(tokens["run+read"]))
    assert answer.status_code == 422
    error = answer.json()["error"]
    assert error["code"] == "request.invalid"
    assert error["params"]["errors"]
    missing = client.get(f"{BASE}/runs/run_000000000000", headers=headers(tokens["read"]))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "run.not_found"


def test_run_with_simulated_model(api):
    client, tokens = api()
    token = tokens["run+read+approve"]
    started = client.post(f"{BASE}/runs", json=EXAMPLE, headers=headers(token))
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    status = finish(client, token, run_id)
    assert status["status"] == "succeeded", status
    result = client.get(f"{BASE}/runs/{run_id}/result", headers=headers(token)).json()
    assert result["status"] == "succeeded"
    assert result["usage"]["requests"] >= 0
    files = client.get(f"{BASE}/runs/{run_id}/files", headers=headers(token))
    assert files.status_code == 200
    for entry in files.json():
        if not entry["error_code"]:
            download = client.get(f"{BASE}/runs/{run_id}/files/{entry['id']}", headers=headers(token))
            assert download.status_code == 200
    usage = client.get(f"{BASE}/usage", headers=headers(token)).json()
    assert usage["runs"] == 1


def test_retries_with_the_same_idempotency_key_start_one_run(api):
    client, tokens = api()
    token = tokens["run+read+approve"]
    key = {"Idempotency-Key": "order-1", **headers(token)}
    first = client.post(f"{BASE}/runs", json=EXAMPLE, headers=key)
    second = client.post(f"{BASE}/runs", json=EXAMPLE, headers=key)
    assert first.json()["run_id"] == second.json()["run_id"]
    assert second.headers.get("Idempotent-Replayed") == "true"
    finish(client, token, first.json()["run_id"])


def test_cost_limit_refuses_new_runs(api):
    client, tokens = api(LIMIT_DAILY_BUDGET_USD="0")
    answer = client.post(f"{BASE}/runs", json=EXAMPLE, headers=headers(tokens["run+read"]))
    assert answer.status_code == 429
    assert answer.json()["error"]["code"] == "limit.daily_budget"


def test_pending_approvals_survive_a_restart(api, tmp_path: Path):
    if not MANIFEST.get("approvals"):
        return  # this workflow has no approval steps
    data_dir = tmp_path / "restart"
    client, tokens = api(data_dir=data_dir)
    token = tokens["run+read+approve"]
    run_id = client.post(f"{BASE}/runs", json=EXAMPLE, headers=headers(token)).json()["run_id"]
    status = finish(client, token, run_id, approve=False)
    assert status["status"] == "awaiting_approval"
    client.__exit__(None, None, None)
    again, tokens = api(data_dir=data_dir)
    # New keys after the restart: the same data directory, a fresh configuration.
    token = tokens["run+read+approve"]
    assert again.get(f"{BASE}/runs/{run_id}", headers=headers(token)).json()["status"] == "awaiting_approval"
    assert finish(again, token, run_id)["status"] == "succeeded"


def test_request_models_match_the_runtime():
    from app import schemas
    from nodestro_runtime.schemas import Workflow
    from nodestro_api.serving import input_model

    runtime = input_model(Workflow.model_validate(RELEASE["workflow"])).model_json_schema()
    generated = schemas.RunRequest.model_json_schema()
    assert set(runtime["properties"]) == set(generated["properties"])
    assert set(runtime.get("required", [])) == set(generated.get("required", []))
    if "Values" in generated.get("$defs", {}):
        values = next(item for name, item in runtime["$defs"].items() if name.endswith("Values"))
        assert set(values["properties"]) == set(generated["$defs"]["Values"]["properties"])
        assert set(values.get("required", [])) == set(generated["$defs"]["Values"].get("required", []))
'''

_TEST_RUNTIME = '''"""The runtime packages are exactly the ones Nodestro exported (see RUNTIME.json)."""

import hashlib
import json
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
INFO = json.loads((PROJECT_DIR / "RUNTIME.json").read_text(encoding="utf-8"))


def test_runtime_files_are_unchanged():
    for name, digest in INFO["files"].items():
        assert hashlib.sha256((PROJECT_DIR / name).read_bytes()).hexdigest() == digest, name


def test_the_copied_runtime_is_the_one_in_use():
    import nodestro_api.standalone
    import nodestro_runtime.engine

    for module in (nodestro_runtime.engine, nodestro_api.standalone):
        assert PROJECT_DIR in Path(module.__file__).resolve().parents, module.__name__


def test_runtime_does_not_need_the_studio():
    for package in ("nodestro_runtime", "nodestro_api"):
        for file in (PROJECT_DIR / package).glob("*.py"):
            assert f"{package}/{file.name}" in INFO["files"], file.name
            assert "nodestro_studio" not in file.read_text(encoding="utf-8"), file.name
'''


def _upload_section(curl: str | None, german: bool) -> str:
    """The README part on sending files as ``multipart/form-data`` (only for forms with file fields)."""
    if not curl:
        return ""
    if german:
        intro = (
            "Dateien gehen als Base64 im JSON-Körper oder als `multipart/form-data`: ein Teil je Datei, benannt nach "
            "seinem Feld, die übrigen Werte als JSON im Teil `values`. Hochgeladene Dateien gelten nur für diesen Lauf."
        )
    else:
        intro = (
            "Files travel as Base64 in the JSON body or as `multipart/form-data`: one part per file, named after its "
            "field, the other values as JSON in the part `values`. Uploaded files belong to that run only."
        )
    return f"\n{intro}\n\n```bash\n{curl}\n```\n"


def _approval_steps(count: int, german: bool) -> str:
    if german:
        return f"{count} Freigabeschritt" if count == 1 else f"{count} Freigabeschritte"
    return f"{count} approval step" if count == 1 else f"{count} approval steps"


def _readme(
    publication: Publication,
    release: Release,
    options: ExportOptions,
    served: ServedRelease,
    tools: list[str],
    included: list[str],
    excluded: list[str],
    memory: list[str],
    base_urls: dict[str, str] | None = None,
) -> str:
    sample = examples(served, "http://127.0.0.1:8000")
    german = options.language == "de"
    name = project_name(publication)
    bundle = release.bundle
    agent_git = options.orkexa == "git"
    text = _README_DE if german else _README_EN
    providers = release_providers(bundle)
    replacements = {
        "{title}": publication.name,
        "{name}": name,
        "{slug}": publication.slug,
        "{number}": str(release.number),
        "{version}": str(release.workflow_version or "-"),
        "{digest}": release.digest[:16],
        "{published}": release.created_at.date().isoformat(),
        "{nodestro}": runtime_version,
        "{library}": library_version,
        "{python}": f"{sys.version_info.major}.{sys.version_info.minor}",
        "{request}": json.dumps(sample.request, indent=2, ensure_ascii=False),
        "{curl_start}": sample.curl["start"],
        "{curl_status}": sample.curl["status"],
        "{curl_result}": sample.curl["result"],
        "{curl_events}": sample.curl["events"],
        "{curl_approve}": sample.curl["approve"],
        "{upload}": _upload_section(sample.curl.get("upload"), german),
        "{key_variable}": KEY_VARIABLE,
        "{approvals}": _approval_steps(bundle.approvals(), german),
        "{limits}": "\n".join(
            f"| `LIMIT_{key.upper()}` | `{value}` |" for key, value in publication.limits.model_dump().items()
        ),
        "{agent_install}": (_AGENT_GIT_DE if german else _AGENT_GIT_EN)
        if agent_git
        else (_AGENT_BUNDLE_DE if german else _AGENT_BUNDLE_EN),
        "{tools}": ", ".join(f"`{item}`" for item in tools) or "-",
        "{knowledge_in}": ", ".join(included) or "-",
        "{knowledge_out}": ", ".join(excluded) or "-",
        "{memory}": ", ".join(memory) or "-",
        "{guide}": ("\n- [docs/AI_GUIDE.md](docs/AI_GUIDE.md)" if options.guide_id else ""),
        "{providers}": ", ".join(PROVIDERS[item].label for item in providers if item in PROVIDERS) or "-",
        "{provider_rows}": _provider_rows(providers, german, base_urls),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


_AGENT_GIT_EN = """`orkexa` is not on PyPI; it is installed from its GitHub repository at the exact commit Nodestro
used (see `pyproject.toml`). While the repository is private, installing needs read access to
`github.com/peterkarlhuber/orkexa`, e.g. a token:

```bash
git config --global url."https://x-access-token:<TOKEN>@github.com/peterkarlhuber/orkexa".insteadOf \\
  "https://github.com/peterkarlhuber/orkexa"
```

Without that access the installation fails. Export again with "include the library" if the project has
to install without it."""

_AGENT_GIT_DE = """`orkexa` gibt es nicht auf PyPI; es wird aus seinem GitHub-Repository installiert, genau in dem
Commit, den Nodestro verwendet hat (siehe `pyproject.toml`). Solange das Repository privat ist, brauchst du
für die Installation Lesezugriff auf `github.com/peterkarlhuber/orkexa`, zum Beispiel mit einem Token:

```bash
git config --global url."https://x-access-token:<TOKEN>@github.com/peterkarlhuber/orkexa".insteadOf \\
  "https://github.com/peterkarlhuber/orkexa"
```

Ohne diesen Zugriff schlägt die Installation fehl. Exportiere erneut mit „Library mitliefern“, wenn das
Projekt ohne Zugriff installierbar sein muss."""

_AGENT_BUNDLE_EN = """`orkexa` is included as source code in `vendor/orkexa` (MIT license, see its `LICENSE`) and
installed from there; no access to its repository is needed. The person who exported this project
confirmed that they may pass the library on. Keep that in mind before sharing the project."""

_AGENT_BUNDLE_DE = """`orkexa` liegt als Quellcode in `vendor/orkexa` (MIT-Lizenz, siehe dortige `LICENSE`) und
wird von dort installiert; ein Zugriff auf das Repository ist nicht nötig. Beim Export wurde bestätigt,
dass die Library weitergegeben werden darf. Beachte das, bevor du das Projekt weitergibst."""


def _provider_rows(providers: list[str], german: bool, base_urls: dict[str, str] | None) -> str:
    """The README table rows of the providers' environment variables."""
    rows: list[str] = []
    for provider in providers:
        info = PROVIDERS.get(provider)
        if info is None:
            continue
        if info.needs_key:
            names = " / ".join(f"`{name}`" for name in info.env_vars)
            text = (
                f"Der Schlüssel für {info.label}. Ohne ihn antwortet ein Start mit 503."
                if german
                else f"The key for {info.label}. Without it, starting a run answers 503."
            )
            rows.append(f"| {names} | {text} |")
            continue
        default = (base_urls or {}).get(provider) or info.default_base_url
        shown = f" (`{default}`)" if default else ""
        if german:
            rows.append(f"| `{base_url_variable(provider)}` | Basis-URL von {info.label}{shown}. |")
            rows.append(
                f"| `{provider.upper()}_API_KEY` | Schlüssel für {info.label}, falls der Server einen verlangt. |"
            )
        else:
            rows.append(f"| `{base_url_variable(provider)}` | The base URL of {info.label}{shown}. |")
            rows.append(f"| `{provider.upper()}_API_KEY` | A key for {info.label}, if the server needs one. |")
    return "\n".join(rows)


_README_EN = """# {title}: workflow API

This project serves the Nodestro workflow **{title}** as an HTTP API, independent of Nodestro Studio.
It was exported from release **v{number}** (workflow version v{version}, published {published},
digest `{digest}`) and contains:

- `workflow/release.json`: the published workflow, its sub-workflows and the code of its tools, frozen;
- `nodestro_runtime/` and `nodestro_api/`: the execution engine of Nodestro (nodestro-runtime {nodestro}) and
  the HTTP layer of published workflows, copied unchanged (versions, source commits and checksums in
  `RUNTIME.json`), so the workflow runs exactly as in the studio: parallel agents, decisions, approvals,
  sub-workflows, tools, budgets and result files;
- `app/`: the application (`main.py`), its configuration (`config.py`), the request models (`schemas.py`)
  and a tool to create API keys (`keys.py`);
- `tests/`: tests with a simulated model (no cost) and `examples/`.{guide}

Nothing is sent to Nodestro. The API needs access to the AI providers of its agents ({providers}) and its
own API keys for its clients.

## Requirements

- Python {python} (the versions in `requirements.lock` were taken from Nodestro running on Python {python})
- Access to {providers} (see "Configure")

### orkexa (agent library)

{agent_install}

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
# or with uv:  uv sync   (creates uv.lock; needs network access)
cp .env.example .env
```

## Configure

All settings come from environment variables or `.env` (see `app/config.py`):

| Variable | Meaning |
| --- | --- |
{provider_rows}
| `API_KEYS` | The keys of your clients (hashes only, see below). Empty: every request is refused. |
| `DATA_DIR` | Where runs, approvals, files and memory are stored (default `./data`). |
| `API_DOCS` | `local` (default: `/docs` only from this computer), `on` or `off`. |
| `RUN_RETENTION_DAYS` | Delete runs older than this many days at start (0 keeps everything). |
| `TOOL_SANDBOX` | `subprocess` (default) or `docker`, see below. |

`TOOL_SANDBOX=subprocess` runs only tools a person wrote in the tool editor, as a process of this server.
`TOOL_SANDBOX=docker` runs every own tool in an isolated container (needs Docker and the `orkexa-sandbox`
image). AI-generated tools run only with `docker`; without it, runs that use them are refused.

### API keys

```bash
python -m app.keys new --name "My app" --scopes run,read
```

prints a key (give it to the client, it is shown once) and the line for `API_KEYS`. Permissions:
`run` (start, cancel, continue), `read` (status, result, events, files, usage), `approve` (decide
approvals). Separate several entries with `;`. Keys of Nodestro are never part of an export.

### Limits

The limits of the published API apply; override them with:

| Variable | Exported value |
| --- | --- |
{limits}

`LIMIT_DAILY_BUDGET_USD` stops new runs once the runs of the last 24 hours cost that much;
`LIMIT_RUN_BUDGET_USD` stops a single run. Exceeded limits answer `429` with a code.

## Start

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API then answers on this computer only. To serve other computers, listen on `0.0.0.0` **behind HTTPS**
(e.g. a reverse proxy, or `--ssl-certfile`/`--ssl-keyfile`): keys must not travel unencrypted.
Interactive documentation: http://127.0.0.1:8000/docs

## Use

Runs are asynchronous: starting answers `202` with a `run_id`; then ask for the status, follow the events
and fetch the result.

```bash
export {key_variable}=ndk_...
{curl_start}
```

The request body (`examples/request.json`):

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Status values: `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. Sending the
same `Idempotency-Key` again returns the run that already started, so retries never start a run twice.
The events are Server-Sent Events; after a lost connection, reconnect with `Last-Event-ID`; the first event is
always the complete current status. `examples/client.py` does all of this in Python.

## Approvals

This workflow has {approvals}. A run that reaches one pauses with the status
`awaiting_approval`; the status lists what to review. A key with the permission `approve` decides:

```bash
{curl_approve}
```

`approval_id` may be left out when only one approval waits. Every decision is stored with the run (who, when,
comment); a second decision on the same approval answers `409`. Waiting runs survive restarts.

## Result files and data

Result files the workflow writes are listed under `/runs/{run_id}/files`. Everything is stored in
`DATA_DIR/runtime.db` (runs with their steps, usage and cost, approvals, files, uploads, memory) and
`DATA_DIR/tools`. Knowledge included in the export: {knowledge_in}. Not included (the agents run without
it): {knowledge_out}. Starting memory: {memory}. Tools: {tools}.

## Errors and costs

Errors have the form `{"error": {"status", "code", "message", "params"}}`. Every run records tokens and
estimated cost (status and result: `usage`); `/api/published/{slug}/usage` sums them up. A run that was
interrupted by a restart is marked `failed` (`run.interrupted`) and can be continued with
`POST /runs/{run_id}/resume`.

## Tests

```bash
pip install pytest httpx
pytest
```

The tests use a simulated model: they cost nothing and check the routes, validation, keys, limits,
idempotency, a complete run, approvals across a restart and that the runtime is unchanged.
"""

_README_DE = """# {title}: Workflow-API

Dieses Projekt stellt den Nodestro-Workflow **{title}** als HTTP-API bereit, unabhängig von Nodestro Studio.
Es wurde aus der Veröffentlichung **v{number}** exportiert (Workflow-Version v{version}, veröffentlicht am
{published}, Prüfsumme `{digest}`) und enthält:

- `workflow/release.json`: den veröffentlichten Workflow, seine Unterworkflows und den Code seiner Tools,
  unveränderlich;
- `nodestro_runtime/` und `nodestro_api/`: die Ausführungs-Engine von Nodestro (nodestro-runtime {nodestro})
  und die HTTP-Schicht veröffentlichter Workflows, unverändert kopiert (Versionen, Quell-Commits und
  Prüfsummen in `RUNTIME.json`). Der Workflow läuft daher genau wie im Studio: parallele Agenten,
  Entscheidungen, Freigaben, Unterworkflows, Tools, Budgets und Ergebnisdateien;
- `app/`: die Anwendung (`main.py`), ihre Konfiguration (`config.py`), die Anfragemodelle (`schemas.py`)
  und ein Werkzeug für API-Schlüssel (`keys.py`);
- `tests/`: Tests mit einem simulierten Modell (kostenlos) und `examples/`.{guide}

Nichts wird an Nodestro gesendet. Die API braucht Zugang zu den KI-Anbietern ihrer Agenten ({providers})
und eigene API-Schlüssel für ihre Clients.

## Voraussetzungen

- Python {python} (die Versionen in `requirements.lock` stammen aus Nodestro unter Python {python})
- Zugang zu {providers} (siehe „Konfiguration“)

### orkexa (Agenten-Library)

{agent_install}

## Installation

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
# oder mit uv:  uv sync   (erzeugt uv.lock; braucht Netzwerkzugriff)
cp .env.example .env
```

## Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen oder `.env` (siehe `app/config.py`):

| Variable | Bedeutung |
| --- | --- |
{provider_rows}
| `API_KEYS` | Die Schlüssel deiner Clients (nur Hashes, siehe unten). Leer: Jede Anfrage wird abgelehnt. |
| `DATA_DIR` | Ablage für Läufe, Freigaben, Dateien und Memory (Standard `./data`). |
| `API_DOCS` | `local` (Standard: `/docs` nur von diesem Rechner), `on` oder `off`. |
| `RUN_RETENTION_DAYS` | Läufe, die älter sind, werden beim Start gelöscht (0 behält alles). |
| `TOOL_SANDBOX` | `subprocess` (Standard) oder `docker`, siehe unten. |

`TOOL_SANDBOX=subprocess` führt nur Tools aus, die eine Person im Tool-Editor geschrieben hat, als Prozess
dieses Servers. `TOOL_SANDBOX=docker` führt jedes eigene Tool in einem isolierten Container aus (braucht Docker
und das Image `orkexa-sandbox`). KI-generierte Tools laufen nur mit `docker`; ohne werden Läufe, die sie nutzen,
abgelehnt.

### API-Schlüssel

```bash
python -m app.keys new --name "Meine App" --scopes run,read
```

gibt einen Schlüssel aus (für den Client, er wird nur einmal angezeigt) und die Zeile für `API_KEYS`.
Rechte: `run` (starten, abbrechen, fortsetzen), `read` (Status, Ergebnis, Ereignisse, Dateien, Nutzung),
`approve` (Freigaben entscheiden). Mehrere Einträge trennst du mit `;`. Schlüssel aus Nodestro werden nie
exportiert.

### Limits

Es gelten die Limits der veröffentlichten API; du überschreibst sie mit:

| Variable | Exportierter Wert |
| --- | --- |
{limits}

`LIMIT_DAILY_BUDGET_USD` stoppt neue Läufe, sobald die Läufe der letzten 24 Stunden so viel gekostet haben;
`LIMIT_RUN_BUDGET_USD` stoppt einen einzelnen Lauf. Überschrittene Limits antworten mit `429` und einem Code.

## Starten

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Die API antwortet dann nur auf diesem Rechner. Für andere Rechner lauschst du auf `0.0.0.0` **hinter HTTPS**
(z. B. ein Reverse Proxy oder `--ssl-certfile`/`--ssl-keyfile`): Schlüssel dürfen nicht unverschlüsselt
übertragen werden. Interaktive Dokumentation: http://127.0.0.1:8000/docs

## Verwenden

Läufe sind asynchron: Der Start antwortet mit `202` und einer `run_id`; danach fragst du den Status ab,
folgst den Ereignissen und holst das Ergebnis.

```bash
export {key_variable}=ndk_...
{curl_start}
```

Der Anfragekörper (`examples/request.json`):

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Status: `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. Sendest du denselben
`Idempotency-Key` erneut, kommt der bereits gestartete Lauf zurück; Wiederholungen starten also nie einen
zweiten Lauf. Die Ereignisse sind Server-Sent Events; nach einem Verbindungsabbruch verbindest du dich mit
`Last-Event-ID` neu, und das erste Ereignis ist immer der vollständige aktuelle Status. `examples/client.py`
zeigt alles in Python.

## Freigaben

Dieser Workflow hat {approvals}. Ein Lauf, der einen erreicht, pausiert mit dem Status
`awaiting_approval`; der Status zeigt, was zu prüfen ist. Ein Schlüssel mit dem Recht `approve` entscheidet:

```bash
{curl_approve}
```

`approval_id` kann entfallen, wenn nur eine Freigabe wartet. Jede Entscheidung wird mit dem Lauf gespeichert
(wer, wann, Kommentar); eine zweite Entscheidung zur selben Freigabe antwortet mit `409`. Wartende Läufe
überstehen Neustarts.

## Ergebnisdateien und Daten

Ergebnisdateien des Workflows stehen unter `/runs/{run_id}/files`. Alles liegt in `DATA_DIR/runtime.db`
(Läufe mit Schritten, Nutzung und Kosten, Freigaben, Dateien, Uploads, Memory) und `DATA_DIR/tools`. Im
Export enthaltenes Wissen: {knowledge_in}. Nicht enthalten (die Agenten laufen ohne): {knowledge_out}.
Start-Memory: {memory}. Tools: {tools}.

## Fehler und Kosten

Fehler haben die Form `{"error": {"status", "code", "message", "params"}}`. Jeder Lauf speichert Tokens und
geschätzte Kosten (Status und Ergebnis: `usage`); `/api/published/{slug}/usage` fasst sie zusammen. Ein
Lauf, der durch einen Neustart unterbrochen wurde, ist `failed` (`run.interrupted`) und lässt sich mit
`POST /runs/{run_id}/resume` fortsetzen.

## Tests

```bash
pip install pytest httpx
pytest
```

Die Tests verwenden ein simuliertes Modell: Sie kosten nichts und prüfen Routen, Validierung, Schlüssel,
Limits, Idempotenz, einen vollständigen Lauf, Freigaben über einen Neustart und die Unverändertheit der
Runtime.
"""


def _env_example(publication: Publication, providers: list[str], base_urls: dict[str, str]) -> str:
    lines: list[str] = []
    for provider in providers:
        info = PROVIDERS.get(provider)
        if info is None:
            continue
        if info.needs_key:
            lines.append(f"{info.env_vars[0]}=")
        else:
            lines.append(f"{base_url_variable(provider)}={base_urls.get(provider) or info.default_base_url or ''}")
            lines.append(f"# {provider.upper()}_API_KEY=")
    keys = "\n".join(lines)
    return f"""# Configuration of {project_name(publication)} (see README). Never commit real keys.
{keys}
# Create entries with: python -m app.keys new --name "My app" --scopes run,read
API_KEYS=
DATA_DIR=./data
API_DOCS=local
RUN_RETENTION_DAYS=0
TOOL_SANDBOX=subprocess
# LIMIT_DAILY_BUDGET_USD={publication.limits.daily_budget_usd}
# LIMIT_MAX_CONCURRENT_RUNS={publication.limits.max_concurrent_runs}
"""


def _pyproject(
    publication: Publication, release: Release, direct: list[str], agent_line: str, bundle_mode: bool
) -> str:
    deps = "\n".join(f'    "{item}",' for item in [*direct, agent_line])
    dev = "\n".join(f'    "{name}=={_version(name)}",' for name in _DEV_PACKAGES)
    sources = '\n[tool.uv.sources]\norkexa = { path = "vendor/orkexa" }\n' if bundle_mode else ""
    return f"""# Generated by Nodestro {__version__} from release v{release.number} of {publication.slug}.
[project]
name = "{project_name(publication)}"
version = "{release.number}.0.0"
description = {json.dumps(f"Workflow API: {publication.name}", ensure_ascii=False)}
requires-python = ">={sys.version_info.major}.{sys.version_info.minor}"
dependencies = [
{deps}
]

[project.optional-dependencies]
dev = [
{dev}
]

[tool.uv]
package = false
{sources}
[tool.pytest.ini_options]
testpaths = ["tests"]
"""


def _vendor_pyproject() -> str:
    requires = [
        str(Requirement(item)) for item in metadata.requires("orkexa") or [] if Requirement(item).marker is None
    ]
    deps = "\n".join(f'    "{item}",' for item in requires)
    return f"""[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "orkexa"
version = "{library_version}"
description = "Provider-independent agents (included in this exported project)."
license = "MIT"
requires-python = ">=3.10"
dependencies = [
{deps}
]

[project.optional-dependencies]
anthropic = ["anthropic>=2.0,<3"]
openai = ["openai>=3.0,<4"]
gemini = ["google-genai>=2.0,<3"]
all = ["anthropic>=2.0,<3", "openai>=3.0,<4", "google-genai>=2.0,<3"]
pdf = ["pypdf>=4"]

[tool.hatch.build.targets.wheel]
packages = ["src/orkexa"]
"""


def _library_license() -> str:
    try:
        for file in metadata.files("orkexa") or []:
            if file.name == "LICENSE":
                return file.read_text(encoding="utf-8")
    except (metadata.PackageNotFoundError, OSError):
        pass
    return NODESTRO_LICENSE


def _dockerfile(agent_git: bool) -> str:
    note = (
        "# orkexa comes from its Git repository: build with access to it while it is private (see README).\n"
        if agent_git
        else ""
    )
    return f"""{note}FROM python:{sys.version_info.major}.{sys.version_info.minor}-slim
WORKDIR /srv/api
COPY . .
RUN pip install --no-cache-dir -r requirements.lock
ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000
# Put HTTPS in front of this container before exposing it beyond the local machine.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""


def _project_files(
    ctx: Nodestro,
    publication: Publication,
    release: Release,
    options: ExportOptions,
    *,
    dry_run: bool = False,
) -> dict[str, bytes]:
    """Every file of the project, by its path inside the project folder."""
    bundle = release.bundle
    files: dict[str, bytes] = {}

    def add(path: str, content: str | bytes) -> None:
        files[path] = content if isinstance(content, bytes) else content.encode("utf-8")

    served = ctx.publications.served(publication, release)
    served.limits = publication.limits
    # Runtime: byte-identical copies of the installed packages.
    checksums: dict[str, str] = {}
    dirs = _package_dirs()
    for name in runtime_modules(dirs):
        package, _, module = name.partition(".")
        path = f"{package}/{module}.py"
        data = b"" if dry_run else (dirs[package] / f"{module}.py").read_bytes()
        add(path, data)
        checksums[path] = _sha(data)
    for package, directory in dirs.items():
        data = b"" if dry_run else (directory / "__init__.py").read_bytes()
        add(f"{package}/__init__.py", data)
        checksums[f"{package}/__init__.py"] = _sha(data)
    add(
        "RUNTIME.json",
        json.dumps(
            {"nodestro": runtime_version, "orkexa": library_version, "packages": runtime_sources(), "files": checksums},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    add("NODESTRO_LICENSE", NODESTRO_LICENSE)
    add("app/__init__.py", "")
    add("app/main.py", _MAIN.format(name=publication.name, number=release.number))
    add("app/config.py", _CONFIG)
    add("app/keys.py", _KEYS)
    add("app/schemas.py", schemas_source(bundle))
    # The release and what was exported.
    add("workflow/release.json", bundle.model_dump_json(indent=2) + "\n")
    knowledge_in: list[str] = []
    knowledge_out: list[str] = []
    for ref in bundle.knowledge:
        if ref.id in options.knowledge and ref.id in ctx.knowledge.store:
            knowledge_in.append(ref.name)
            if not dry_run:
                entry = ctx.knowledge.store.info(ref.id)
                add(f"workflow/knowledge/{ref.id}/entry.json", entry.model_dump_json(indent=2) + "\n")
                add(f"workflow/knowledge/{ref.id}/data", ctx.knowledge.store.get_bytes(ref.id))
                add(f"workflow/knowledge/{ref.id}/text.md", ctx.knowledge.text_of(ref.id))
            else:
                for name in ("entry.json", "data", "text.md"):
                    add(f"workflow/knowledge/{ref.id}/{name}", b"")
        else:
            knowledge_out.append(ref.name)
    memory_in: list[str] = []
    for space_ref in bundle.memory:
        if space_ref.space not in options.memory:
            continue
        memory_in.append(space_ref.space)
        content: list[dict[str, str]] = []
        if not dry_run:
            space = ctx.memory.open(space_ref.space)
            content = [{"path": item.path, "content": space.read(item.path)} for item in space.entries()]
        add(
            f"workflow/memory/{_safe(space_ref.space)}.json",
            json.dumps({"space": space_ref.space, "files": content}, indent=2) + "\n",
        )
    providers = release_providers(bundle)
    base_urls, capabilities = _provider_manifest(ctx, providers)
    manifest = {
        "format": "nodestro-export",
        "format_version": 1,
        "publication_id": publication.id,
        "slug": publication.slug,
        "name": publication.name,
        "description": publication.description,
        "release_id": release.id,
        "release_number": release.number,
        "release_digest": release.digest,
        "published_at": release.created_at.isoformat(),
        "workflow_version": release.workflow_version,
        "approvals": bundle.approvals(),
        "limits": publication.limits.model_dump(mode="json"),
        "runtime": {"nodestro": runtime_version, "orkexa": library_version},
        "packages": runtime_sources(),
        "orkexa": {
            "mode": options.orkexa,
            "requirement": "vendor/orkexa" if options.orkexa == "bundle" else _orkexa_git(),
            "private_repository_access_needed": options.orkexa == "git",
        },
        "providers": providers,
        "base_urls": base_urls,
        "capabilities": capabilities,
        "knowledge_included": [ref.id for ref in bundle.knowledge if ref.name in knowledge_in],
        "knowledge_excluded": [ref.id for ref in bundle.knowledge if ref.name in knowledge_out],
        "memory_included": memory_in,
        "secrets_included": False,
    }
    add("workflow/manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    # Dependencies.
    direct, lock, agent_line = dependencies(options, bundle)
    add("pyproject.toml", _pyproject(publication, release, direct, agent_line, options.orkexa == "bundle"))
    lock_text = (
        f"# Exact versions Nodestro {__version__} ran with "
        f"(Python {sys.version_info.major}.{sys.version_info.minor}).\n"
        + "\n".join(lock)
        + f"\n{agent_line if options.orkexa == 'git' else './vendor/orkexa[all,pdf]'}\n"
    )
    add("requirements.lock", lock_text)
    if bundle.python:
        add(
            "requirements-tools.lock",
            "# Approved third-party libraries of the workflow's tools, as locked in Nodestro\n"
            "# (already part of requirements.lock and pyproject.toml; packages of the runtime keep its versions).\n"
            + "\n".join(tool_libraries(bundle))
            + "\n",
        )
    if options.orkexa == "bundle":
        import orkexa

        source = Path(orkexa.__file__).resolve().parent
        for file in sorted(source.rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts and file.suffix in (".py", ".typed", ".json", ""):
                relative = file.relative_to(source).as_posix()
                add(f"vendor/orkexa/src/orkexa/{relative}", b"" if dry_run else file.read_bytes())
        add("vendor/orkexa/pyproject.toml", _vendor_pyproject())
        add("vendor/orkexa/LICENSE", _library_license())
    # Documentation, examples, tests.
    tools = sorted([*bundle.tools, *bundle.builtin_tools])
    add(
        "README.md",
        _readme(publication, release, options, served, tools, knowledge_in, knowledge_out, memory_in, base_urls),
    )
    sample = examples(served, "http://127.0.0.1:8000")
    add("examples/request.json", json.dumps(sample.request, indent=2, ensure_ascii=False) + "\n")
    add("examples/client.py", sample.python)
    add(
        "examples/curl.sh",
        "#!/bin/sh\n# Set NODESTRO_API_KEY first; replace RUN_ID with the run_id of the first answer.\n\n"
        + "\n\n".join(sample.curl.values())
        + "\n",
    )
    add(".env.example", _env_example(publication, providers, base_urls))
    add(".gitignore", ".env\ndata/\n.venv/\n__pycache__/\n")
    add("Dockerfile", _dockerfile(options.orkexa == "git"))
    add("tests/conftest.py", _CONFTEST)
    add("tests/test_api.py", _TEST_API)
    add("tests/test_runtime.py", _TEST_RUNTIME)
    if options.guide_id:
        guide = ctx.store.get_raw(GUIDES, options.guide_id) or {}
        add("docs/AI_GUIDE.md", str(guide.get("markdown", "")))
    return files


def _provider_manifest(ctx: Nodestro, providers: list[str]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Base URLs without credentials and declared capabilities of the release's endpoint providers."""
    base_urls: dict[str, str] = {}
    capabilities: dict[str, dict[str, str]] = {}
    for provider in providers:
        info = PROVIDERS.get(provider)
        if info is None or not info.base_url:
            continue
        stored = ctx.provider_settings(provider)
        url = str(stored.get("base_url") or "")
        if url and not has_credentials(url):
            base_urls[provider] = url
        if stored.get("capabilities"):
            capabilities[provider] = dict(stored["capabilities"])
    return base_urls, capabilities


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def build_zip(files: dict[str, bytes], root: str) -> bytes:
    """A ZIP with fixed timestamps and order: the same files give the same bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(f"{root}/{path}", date_time=_FIXED_TIME)
            info.external_attr = (0o755 if path.endswith(".sh") else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[path])
    return buffer.getvalue()


def export(ctx: Nodestro, publication: Publication, release: Release, options: ExportOptions) -> ExportRecord:
    """Generate the project, store the ZIP under ``<data>/exports`` and record the export."""
    found = check(ctx, publication, release, options)
    if not found.ok:
        first = next(item for item in found.problems if item.level == "error")
        raise NodestroError(first.code, first.message, **first.params)
    files = _project_files(ctx, publication, release, options)
    data = build_zip(files, project_name(publication))
    record = ExportRecord(
        publication_id=publication.id,
        release_id=release.id,
        release_number=release.number,
        options=options,
        file_name=f"{project_name(publication)}-v{release.number}.zip",
        size=len(data),
        sha256=_sha(data),
        files=len(files),
        warnings=[item.code for item in found.problems if item.level == "warning"],
        runtime=found.runtime,
    )
    target = ctx.config.data_dir / "exports"
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{record.id}.zip").write_bytes(data)
    ctx.store.put(EXPORTS, record.id, record)
    publication.history.append(
        PublicationEvent(kind="exported", details={"release": release.number, "export": record.id})
    )
    ctx.publications._put(publication)
    return record


def records(ctx: Nodestro, publication_id: str) -> list[ExportRecord]:
    found = [item for item in ctx.store.list(EXPORTS, ExportRecord) if item.publication_id == publication_id]
    return sorted(found, key=lambda item: item.created_at, reverse=True)


def normalize_records(store: Store) -> None:
    """Bring stored export records to the current format (idempotent).

    Options keep only the fields of :class:`ExportOptions`; an unknown field holding the library
    source (``git``/``bundle``) becomes ``orkexa``. The runtime keeps the current components.
    """
    fields = set(ExportOptions.model_fields)
    for key, _record in store.items(EXPORTS, ExportRecord):
        raw: dict[str, Any] = store.get_raw(EXPORTS, key) or {}
        options = dict(raw.get("options") or {})
        runtime = dict(raw.get("runtime") or {})
        unknown = {name: value for name, value in options.items() if name not in fields}
        if not unknown and current_runtime(runtime) == runtime:
            continue
        for value in unknown.values():
            if value in ("git", "bundle"):
                options.setdefault("orkexa", value)
        raw["options"] = {name: value for name, value in options.items() if name in fields}
        raw["runtime"] = current_runtime(runtime)
        store.put_raw(EXPORTS, key, raw)


def content(ctx: Nodestro, export_id: str) -> tuple[ExportRecord, bytes]:
    record = ctx.store.get(EXPORTS, export_id, ExportRecord)
    if record is None:
        raise KeyError(export_id)
    path = ctx.config.data_dir / "exports" / f"{record.id}.zip"
    if not path.is_file():
        raise NodestroError("export.file_missing", "The ZIP file of this export is gone; export again")
    data = path.read_bytes()
    if _sha(data) != record.sha256:
        raise NodestroError("export.file_changed", "The ZIP file of this export was changed; export again")
    return record, data


__all__ = [
    "ApiLimits",
    "ExportCheck",
    "ExportOptions",
    "ExportRecord",
    "build_zip",
    "check",
    "content",
    "export",
    "records",
    "runtime_modules",
]
