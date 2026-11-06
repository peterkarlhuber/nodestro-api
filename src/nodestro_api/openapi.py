"""The OpenAPI document of the Nodestro API: the contract of the studio REST API and the published APIs.

``python -m nodestro_api.openapi [FILE]`` writes it (default: standard output). The repository keeps
the current document as ``openapi/nodestro-api.json``; a test fails while it is out of date, so
every change of a route, a request or a response shows up in review. The SDKs are generated from
this file (``nodestro-sdk``).

``python -m nodestro_api.openapi --server [FILE]`` writes the document of the server profile
(:func:`~nodestro_api.server_app.create_tenant_app`: the login and tenant routes, without the published APIs it
does not serve yet), kept as ``openapi/nodestro-api-server.json`` (API-012). It is built without a database.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, cast


def document() -> dict[str, Any]:
    """The OpenAPI document as the running API serves it at ``/api/openapi.json``."""
    from .app import create_app
    from .context import AppConfig

    with tempfile.TemporaryDirectory(prefix="nodestro-openapi-") as directory:
        app = create_app(AppConfig(data_dir=Path(directory)), integrations=[], scheduler=False)
        found = app.openapi()
        app.state.nodestro.close()
        return found


def server_document() -> dict[str, Any]:
    """The OpenAPI document of the server profile; routes are only declared, so nothing connects to a database."""
    from .server_app import TenantComposition, create_tenant_app

    # Building the routes reads only the two lifetimes of the identity store.
    identity = SimpleNamespace(session_lifetime=timedelta(hours=12), login_lifetime=timedelta(minutes=10))
    with tempfile.TemporaryDirectory(prefix="nodestro-openapi-") as directory:
        app = create_tenant_app(
            TenantComposition(
                database=cast(Any, None),
                identity=cast(Any, identity),
                oidc=cast(Any, None),
                origin="https://nodestro.example.com",
                data_root=Path(directory),
            )
        )
        return app.openapi()


def text(*, server: bool = False) -> str:
    found = server_document() if server else document()
    return json.dumps(found, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    server = "--server" in args
    args = [arg for arg in args if arg != "--server"]
    if args:
        Path(args[0]).write_text(text(server=server), encoding="utf-8")
    else:
        sys.stdout.write(text(server=server))


if __name__ == "__main__":  # pragma: no cover
    main()
