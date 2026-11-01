"""System diagnosis for the settings page: is everything Nodestro needs in order?

Each check reports ``ok``, ``warning`` or ``error`` with a code; the interface explains
the code and suggests a fix in its own language. The checks are local and free. Only
:func:`check_connection` talks to a provider (Anthropic, OpenAI, Gemini, Amazon Bedrock through its verification, or an
OpenAI-compatible endpoint), and only when the user asks: it lists the available models,
a request that uses no tokens and costs nothing. An API key is never part of a result,
only where it comes from.
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import sqlite3
import tempfile
import time
from typing import TYPE_CHECKING, Any, Literal

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import PROVIDERS, MissingApiKeyError, provider_of
from orkexa import AuthenticationError, ProviderConnectionError, ProviderError
from orkexa import __version__ as library_version
from orkexa.dynamic import DockerSandbox, DynamicAgentError
from orkexa.providers import ProviderConfig, create_provider
from pydantic import BaseModel, Field, SecretStr

from . import __version__

if TYPE_CHECKING:
    from .context import Nodestro

Status = Literal["ok", "warning", "error"]
_LOW_DISK_BYTES = 1024**3

_PROBE_CODE = '''\
def add_numbers(a: int, b: int) -> int:
    """Add two numbers.

    Args:
        a: The first number.
        b: The second number.

    Returns:
        The sum.
    """
    return a + b
'''
_PROBE_TESTS = """\
from tool import add_numbers


def test_add():
    assert add_numbers(2, 3) == 5
"""


class DiagnosticCheck(BaseModel):
    id: str
    status: Status
    #: ``diagnostics.<id>.<code>`` in the language files: what it means and how to fix it.
    code: str
    params: dict[str, Any] = Field(default_factory=dict)
    #: Technical detail in English (paths, versions, error texts); never a secret.
    message: str = ""


class Diagnostics(BaseModel):
    versions: dict[str, str]
    checks: list[DiagnosticCheck]
    status: Status


def _check(check_id: str, status: Status, code: str, message: str = "", **params: Any) -> DiagnosticCheck:
    return DiagnosticCheck(id=check_id, status=status, code=code, message=message, params=params)


def _writable(directory: Path) -> str | None:
    """None when a file can be created and removed in ``directory``, else the reason."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".nodestro-check-", delete=True) as handle:
            handle.write(b"ok")
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _python() -> DiagnosticCheck:
    version = platform.python_version()
    return _check("python", "ok", "ok", f"Python {version} ({platform.machine()})", version=version)


def _providers(nodestro: Nodestro) -> DiagnosticCheck:
    """The providers of the default and assistant models must be configured; lists the configured ones."""
    settings = nodestro.settings()
    configured = [item for item in settings.providers if item.configured]
    by_id = {item.id: item for item in settings.providers}
    for model in (settings.default_model, settings.assistant_model):
        provider = provider_of(model)
        status = by_id.get(provider)
        if status is None or not status.configured:
            return _check("providers", "warning", "missing", provider=provider)
    names = ", ".join(item.label for item in configured)
    return _check("providers", "ok", "ok", names, providers=names)


def _database(nodestro: Nodestro) -> DiagnosticCheck:
    path = Path(nodestro.sqlite_store.database)
    try:
        connection = sqlite3.connect(path, timeout=10)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
            journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TEMP TABLE IF NOT EXISTS nodestro_check (x)")
            connection.execute("ROLLBACK")
            has_fts = bool(
                connection.execute(
                    "SELECT 1 FROM pragma_compile_options WHERE compile_options = 'ENABLE_FTS5'"
                ).fetchone()
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return _check("database", "error", "unreadable", f"{type(exc).__name__}: {exc}", path=str(path))
    size = path.stat().st_size if path.exists() else 0
    if result != "ok":
        return _check("database", "error", "damaged", str(result), path=str(path))
    message = f"SQLite {sqlite3.sqlite_version}, {journal}, {size} bytes"
    if not has_fts:
        return _check("database", "warning", "no_fts", message, path=str(path), size=size)
    return _check("database", "ok", "ok", message, path=str(path), size=size)


def _directories(nodestro: Nodestro) -> list[DiagnosticCheck]:
    data = nodestro.config.data_dir
    found = []
    for check_id, directory in (("data_dir", data), ("tools_dir", data / "tools"), ("backup_dir", data / "backups")):
        problem = _writable(directory)
        if problem:
            found.append(_check(check_id, "error", "not_writable", problem, path=str(directory)))
        else:
            found.append(_check(check_id, "ok", "ok", str(directory), path=str(directory)))
    free = shutil.disk_usage(data).free
    found.append(
        _check(
            "disk",
            "warning" if free < _LOW_DISK_BYTES else "ok",
            "low" if free < _LOW_DISK_BYTES else "ok",
            f"{free} bytes free",
            free=free,
        )
    )
    return found


def _sandbox(nodestro: Nodestro) -> list[DiagnosticCheck]:
    found = []
    kind = nodestro.settings().sandbox
    docker = DockerSandbox.available()
    if kind == "docker" and not docker:
        found.append(_check("docker", "error", "unreachable"))
    else:
        found.append(_check("docker", "ok", "available" if docker else "not_installed", sandbox=kind))
    started = time.monotonic()
    try:
        result = nodestro.tools.registry.sandbox.validate(_PROBE_CODE, _PROBE_TESTS, "add_numbers")
    except (DynamicAgentError, OSError) as exc:
        found.append(_check("sandbox", "error", "failed", f"{type(exc).__name__}: {exc}", sandbox=kind))
        return found
    seconds = round(time.monotonic() - started, 2)
    if not result.ok:
        found.append(_check("sandbox", "error", "failed", result.error or result.output[-500:], sandbox=kind))
    else:
        found.append(
            _check("sandbox", "ok", "ok", f"{kind}: test passed in {seconds} s", sandbox=kind, seconds=seconds)
        )
    return found


def _pdf() -> DiagnosticCheck:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return _check("pdf", "warning", "missing")
    return _check("pdf", "ok", "ok")


def _frontend(nodestro: Nodestro) -> DiagnosticCheck:
    static = nodestro.config.static_dir
    if static is not None and (static / "index.html").is_file():
        return _check("frontend", "ok", "ok", str(static))
    return _check("frontend", "warning", "missing")


def run_diagnostics(nodestro: Nodestro) -> Diagnostics:
    """All local checks; free and without network access."""
    checks = [
        _python(),
        _database(nodestro),
        *_directories(nodestro),
        _providers(nodestro),
        *_sandbox(nodestro),
        _pdf(),
        _frontend(nodestro),
    ]
    statuses = {check.status for check in checks}
    status: Status = "error" if "error" in statuses else ("warning" if "warning" in statuses else "ok")
    return Diagnostics(
        versions={
            "nodestro": __version__,
            "orkexa": library_version,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        },
        checks=checks,
        status=status,
    )


#: Bedrock verification codes and the connection check's codes they correspond to.
_AWS_CODES = {
    "provider.aws_credentials_invalid": "key_rejected",
    "provider.aws_access_denied": "key_rejected",
    "provider.aws_unreachable": "unreachable",
    "provider.aws_credentials_missing": "not_configured",
    "provider.not_configured": "not_configured",
    "provider.bedrock_not_available": "not_configured",
    "provider.auth_not_allowed": "not_configured",
    "provider.region_missing": "not_configured",
}


def _aws_connection(nodestro: Nodestro, provider: str, label: str) -> DiagnosticCheck:
    """Amazon Bedrock: the settings' verification (STS and one control-plane read, free); no model is listed through
    an SDK client and nothing about the AWS identity is reported."""
    started = time.monotonic()
    try:
        status = nodestro.verify_provider(provider)
    except NodestroError as exc:  # provider.verify_rate_limited
        return _check("connection", "warning", "failed", str(exc), provider=provider, label=label, reason=exc.code)
    millis = round((time.monotonic() - started) * 1000)
    verification = status.verification
    if verification is not None and verification.state == "verified":
        return _check(
            "connection",
            "ok",
            "ok",
            f"AWS settings verified in {millis} ms",
            millis=millis,
            provider=provider,
            label=label,
        )
    reason = (verification.code if verification is not None else None) or "provider.aws_verification_failed"
    code = _AWS_CODES.get(reason, "failed")
    level: Status = "warning" if code == "not_configured" else "error"
    return _check("connection", level, code, reason, provider=provider, label=label, reason=reason)


def check_connection(nodestro: Nodestro, provider: str | None = None) -> DiagnosticCheck:
    """Reach a provider's API by listing its models: no tokens, no cost.

    ``provider`` defaults to the provider of the default model. The request goes through the
    SDK client Orkexa uses for the provider, with Orkexa's error mapping (keys are redacted).
    """
    provider = provider or provider_of(nodestro.settings().default_model)
    label = PROVIDERS[provider].label if provider in PROVIDERS else provider
    status = next((item for item in nodestro.settings().providers if item.id == provider), None)
    if status is not None and not status.configured:
        code = "no_key" if PROVIDERS[provider].needs_key else "not_configured"
        return _check("connection", "warning", code, provider=provider, label=label)
    try:
        connection = nodestro.connection(provider)
    except MissingApiKeyError as exc:
        code = "no_key" if exc.code == "api_key_missing" else "not_configured"
        return _check("connection", "warning", code, provider=provider, label=label)
    except NodestroError as exc:
        return _check("connection", "error", "failed", str(exc), provider=provider, label=label)
    if PROVIDERS[provider].auth == "aws":
        return _aws_connection(nodestro, provider, label)
    started = time.monotonic()
    adapter = create_provider(
        provider,
        ProviderConfig(
            api_key=SecretStr(connection.api_key) if connection.api_key else None,
            base_url=connection.base_url,
            timeout=15,
            max_retries=0,
            client=connection.client,
        ),
    )
    try:
        client = adapter.client
        if connection.client is not None and hasattr(client, "with_options"):
            client = client.with_options(max_retries=0, timeout=15)
        page = client.models.list(limit=1) if provider == "anthropic" else client.models.list()
        count = len(getattr(page, "data", None) or getattr(page, "page", None) or [])
    except Exception as exc:  # anything is reported, never raised
        mapped = adapter.map_error(exc) or exc
        text = f"{type(mapped).__name__}: {mapped}"
        if isinstance(mapped, AuthenticationError):
            return _check("connection", "error", "key_rejected", text, provider=provider, label=label)
        if isinstance(mapped, ProviderConnectionError):
            return _check("connection", "error", "unreachable", text, provider=provider, label=label)
        if isinstance(mapped, ProviderError):
            return _check(
                "connection",
                "error",
                "api_error",
                text,
                http_status=mapped.status_code or "",
                provider=provider,
                label=label,
            )
        return _check("connection", "error", "failed", text, provider=provider, label=label)
    millis = round((time.monotonic() - started) * 1000)
    proxy = bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    return _check(
        "connection",
        "ok",
        "ok",
        f"{count} model(s) listed in {millis} ms",
        millis=millis,
        proxy=proxy,
        provider=provider,
        label=label,
    )
