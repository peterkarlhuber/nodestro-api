"""The OpenAPI document of the Nodestro API: the contract of the studio REST API and the published APIs.

``python -m nodestro_api.openapi [FILE]`` writes it (default: standard output). The repository keeps
the current document as ``openapi/nodestro-api.json``; a test fails while it is out of date, so
every change of a route, a request or a response shows up in review. The SDKs are generated from
this file (``nodestro-sdk``).
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from typing import Any


def document() -> dict[str, Any]:
    """The OpenAPI document as the running API serves it at ``/api/openapi.json``."""
    from .app import create_app
    from .context import AppConfig

    with tempfile.TemporaryDirectory(prefix="nodestro-openapi-") as directory:
        app = create_app(AppConfig(data_dir=Path(directory)), integrations=[], scheduler=False)
        found = app.openapi()
        app.state.nodestro.close()
        return found


def text() -> str:
    return json.dumps(document(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args:
        Path(args[0]).write_text(text(), encoding="utf-8")
    else:
        sys.stdout.write(text())


if __name__ == "__main__":  # pragma: no cover
    main()
