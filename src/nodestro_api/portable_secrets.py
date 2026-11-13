"""The AI providers' credentials of an exported application, entered on the computer it runs on.

An export never contains a provider key (:mod:`nodestro_api.exporter`). The application reads them from

1. the environment (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``<PROVIDER>_BASE_URL`` ...): for servers, secret
   managers and anything an operator sets outside the application; the environment always wins;
2. ``<DATA_DIR>/secrets/providers.json``: what the operator entered with ``configure`` in a terminal or on the setup
   page of the web interface (local operator only). The file is created with owner-only permissions (``0600`` in a
   ``0700`` folder on Linux and macOS; on Windows the user profile's permissions of the folder apply) and is never
   part of the package, a download, a log or an answer to a browser: the interface only learns *whether* a provider
   is configured.

Changing a key needs no new export: a change on the setup page applies to the next run at once; ``configure`` in a
terminal applies at the next start. The file is not encrypted: a key stored next to it would protect nothing, so
the protection is the file's permissions (like an SSH key). Operators who need more set the variables from their
secret store instead.
"""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from nodestro_runtime.providers import PROVIDERS, base_url_variable, check_base_url, has_credentials

FILE = Path("secrets") / "providers.json"
_MAX_VALUE = 4096


def path_of(data_dir: Path) -> Path:
    return data_dir / FILE


def load(data_dir: Path) -> dict[str, dict[str, str]]:
    target = path_of(data_dir)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(name): {str(key): str(value) for key, value in entry.items() if isinstance(value, str)}
        for name, entry in data.items()
        if isinstance(entry, dict) and name in PROVIDERS
    }


def save(data_dir: Path, values: dict[str, dict[str, str]]) -> None:
    """Write the file atomically with owner-only permissions."""
    write_private(path_of(data_dir), values)


def write_private(target: Path, values: Any) -> None:
    """Write ``values`` as JSON to ``target`` atomically, owner-only (``0600`` in a ``0700`` folder on POSIX)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        target.parent.chmod(stat.S_IRWXU)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-", suffix=".json")
    try:
        if os.name == "posix":
            os.fchmod(handle, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            json.dump(values, writer, indent=2, sort_keys=True)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def variables(provider: str) -> list[str]:
    """The environment variables a provider's connection reads (key and endpoint)."""
    info = PROVIDERS[provider]
    names: list[str] = []
    if info.base_url:
        names.append(base_url_variable(provider))
    if info.auth == "aws":
        names += list(info.region_env_vars)
    names += list(info.env_vars or ([] if info.auth == "aws" else [f"{provider.upper()}_API_KEY"]))
    return names


def _key_variable(provider: str) -> str:
    info = PROVIDERS[provider]
    return info.env_vars[0] if info.env_vars else f"{provider.upper()}_API_KEY"


class ProviderSecrets:
    """The credentials of the release's providers: from the environment, else from the operator's file."""

    def __init__(
        self, data_dir: Path, providers: Iterable[str], environ: MutableMapping[str, str] | None = None
    ) -> None:
        self.data_dir = data_dir
        self.providers = [name for name in providers if name in PROVIDERS]
        self.environ: MutableMapping[str, str] = os.environ if environ is None else environ
        #: Variables this object put into the environment (the others were set outside the application).
        self._applied: set[str] = set()

    def apply(self) -> None:
        """Put the stored values into the environment, without overriding anything set outside."""
        for provider, entry in load(self.data_dir).items():
            for variable, value in self._variables_of(provider, entry).items():
                if variable not in self.environ or variable in self._applied:
                    self.environ[variable] = value
                    self._applied.add(variable)

    def _variables_of(self, provider: str, entry: dict[str, str]) -> dict[str, str]:
        found: dict[str, str] = {}
        info = PROVIDERS[provider]
        if entry.get("api_key"):
            found[_key_variable(provider)] = entry["api_key"]
        if info.base_url and entry.get("base_url"):
            found[base_url_variable(provider)] = entry["base_url"]
        if info.auth == "aws" and entry.get("region"):
            found[info.region_env_vars[0]] = entry["region"]
        return found

    def status(self) -> list[dict[str, Any]]:
        """Per provider: configured or not, and where from (never a value)."""
        found = []
        for provider in self.providers:
            info = PROVIDERS[provider]
            names = variables(provider)
            present = [name for name in names if self.environ.get(name)]
            if info.auth == "aws":
                configured = bool(present)
            elif info.needs_key:
                configured = any(name in present for name in info.env_vars)
            else:  # endpoints: the URL comes from the export or the environment; a key is optional
                configured = True
            source = "none"
            if present:
                source = "settings" if all(name in self._applied for name in present) else "environment"
            found.append(
                {
                    "id": provider,
                    "label": info.label,
                    "configured": configured,
                    "required": info.needs_key or info.auth == "aws",
                    "source": source,
                    "auth": info.auth,
                    "variables": names,
                    "base_url": self.environ.get(base_url_variable(provider)) if info.base_url else None,
                    "editable": not present or all(name in self._applied for name in present),
                }
            )
        return found

    def set(self, provider: str, values: dict[str, Any]) -> dict[str, Any]:
        """Store a provider's key (and endpoint or region) and use it from the next run on."""
        from .serving import ApiError

        if provider not in self.providers:
            raise ApiError(404, "provider.unknown", "This application does not use this provider", provider=provider)
        info = PROVIDERS[provider]
        entry: dict[str, str] = {}
        key = str(values.get("api_key") or "").strip()
        if key:
            if len(key) > _MAX_VALUE or any(ord(char) < 32 for char in key):
                raise ApiError(422, "provider.key_invalid", "This is not a valid key")
            entry["api_key"] = key
        if info.base_url and values.get("base_url"):
            url = str(values["base_url"]).strip()
            try:
                url = check_base_url(url)
            except Exception:
                raise ApiError(422, "provider.base_url_invalid", "This is not a valid address") from None
            if has_credentials(url):
                raise ApiError(422, "provider.base_url_credentials", "Put the key into the key field, not the URL")
            entry["base_url"] = url
        if info.auth == "aws" and values.get("region"):
            entry["region"] = str(values["region"]).strip()[:40]
        if not entry:
            raise ApiError(422, "provider.nothing_to_store", "Enter a key")
        blocked = [
            name for name in self._variables_of(provider, entry) if self.environ.get(name) and name not in self._applied
        ]
        if blocked:
            raise ApiError(
                409,
                "provider.set_in_environment",
                "This value is set in the environment of the application and wins over the settings",
                variables=", ".join(blocked),
            )
        stored = load(self.data_dir)
        stored[provider] = {**stored.get(provider, {}), **entry}
        save(self.data_dir, stored)
        for variable, value in self._variables_of(provider, entry).items():
            self.environ[variable] = value
            self._applied.add(variable)
        return next(item for item in self.status() if item["id"] == provider)

    def remove(self, provider: str) -> dict[str, Any]:
        from .serving import ApiError

        if provider not in self.providers:
            raise ApiError(404, "provider.unknown", "This application does not use this provider", provider=provider)
        stored = load(self.data_dir)
        entry = stored.pop(provider, None)
        save(self.data_dir, stored)
        for variable in self._variables_of(provider, entry or {}):
            if variable in self._applied:
                self.environ.pop(variable, None)
                self._applied.discard(variable)
        return next(item for item in self.status() if item["id"] == provider)


__all__ = ["FILE", "ProviderSecrets", "load", "path_of", "save", "variables"]
