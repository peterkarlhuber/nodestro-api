"""The service connections of an exported application (NAL-A1, nodestro-runtime ADR-0001).

An export never contains a connection: the release only lists the ones its built-in tools are bound to
(``ReleaseBundle.required_connections``, ``{connection_id: "type:provider"}``). The operator enters them on the
computer the application runs on, like the provider keys (:mod:`nodestro_api.portable_secrets`):

1. ``NODESTRO_CONNECTION_<ID>_SECRET`` (the id in upper case) replaces a connection's secret; the environment
   always wins;
2. ``<DATA_DIR>/secrets/connections.json``: what ``configure`` stored, with owner-only permissions, keyed by
   connection id (``type``, ``provider``, ``name``, ``settings``, ``secret``). It is never part of the package, a
   download, a log or an answer.

Settings are checked with the studio's rules (address rules of the server profile). A release whose required
connections are missing or invalid refuses to start runs (``connection.missing``).
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
from typing import Any

from nodestro_runtime.connections import StaticConnections, secret_variable

from .portable_secrets import write_private

FILE = Path("secrets") / "connections.json"


def path_of(data_dir: Path) -> Path:
    return data_dir / FILE


def load(data_dir: Path) -> dict[str, dict[str, Any]]:
    target = path_of(data_dir)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


def save(data_dir: Path, entries: Mapping[str, Mapping[str, Any]]) -> None:
    write_private(path_of(data_dir), {key: dict(value) for key, value in entries.items()})


def static(data_dir: Path, environ: Mapping[str, str] | None = None) -> StaticConnections:
    """The connections as configured now (file plus environment)."""
    return StaticConnections(load(data_dir), environ=os.environ if environ is None else environ, mode="server")


__all__ = ["FILE", "StaticConnections", "load", "path_of", "save", "secret_variable", "static"]
