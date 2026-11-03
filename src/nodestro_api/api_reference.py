"""The API reference page (``/api/docs``) with a vendored, exactly pinned Swagger UI (API2-01).

FastAPI's own page loads ``swagger-ui-dist@5`` (a floating major version) from a CDN and its favicon from another
site, and starts it with an inline script. Here the page loads only files this package ships
(``nodestro_api/swagger_ui``, version and SHA-256 in ``VENDORED.md``), served under ``/api/docs/assets``, and starts
Swagger UI from one of them, so it works offline (desktop) and needs neither a third-party origin nor an inline
script in its Content-Security-Policy.

The page and its files need ``read`` like the rest of the reference (on a server a login, A-09); the OpenAPI document
itself is served by the caller (FastAPI's ``openapi_url`` on the desktop, a route with ``read`` on a server).
"""

from __future__ import annotations

from collections.abc import Sequence
import html
from pathlib import Path

from fastapi import FastAPI, params
from fastapi.responses import FileResponse, HTMLResponse

from .permissions import READ

#: The vendored ``swagger-ui-dist`` release (npm).
SWAGGER_UI_VERSION = "5.34.0"
ASSETS_DIR = Path(__file__).resolve().parent / "swagger_ui"
ASSETS_PATH = "/api/docs/assets"
#: The files the page uses (name -> media type); nothing else of the directory is served.
ASSETS = {
    "swagger-ui-bundle.js": "text/javascript; charset=utf-8",
    "swagger-ui.css": "text/css; charset=utf-8",
    "favicon-32x32.png": "image/png",
    "nodestro-init.js": "text/javascript; charset=utf-8",
}
#: The files taken unchanged from the package and their SHA-256 (``tests/test_api_reference.py`` checks them).
VENDORED_SHA256 = {
    "swagger-ui-bundle.js": "3f6abf70480db7e07914df4c98a77385f22ea585eaf457a3e6415ba0eae2d167",
    "swagger-ui.css": "c3cfe8248c4b0b89673600fb829924eafbac6de4bc4b2b06f5f5243483074d0a",
    "favicon-32x32.png": "3ed612f41e050ca5e7000cad6f1cbe7e7da39f65fca99c02e99e6591056e5837",
    "swagger-ui-bundle.js.LICENSE.txt": "e5a4792feaa5332dfba37f1d7adf84308b93b4f2d8da0569a9f48a3cb61c969b",
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "NOTICE": "0d20d1adef18aee3f40dd258172155521ce702ac445cb5f7b7d60ed32dad2fb2",
}
#: The vendored files carry their version in this package's release: a browser may keep them for a day.
_CACHE = "private, max-age=86400"


def reference_page(openapi_url: str, title: str) -> str:
    """The HTML of the reference page: vendored files only, no inline script or style."""
    url = html.escape(openapi_url, quote=True)
    name = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<link rel="icon" type="image/png" href="{ASSETS_PATH}/favicon-32x32.png">
<link rel="stylesheet" href="{ASSETS_PATH}/swagger-ui.css">
</head>
<body>
<div id="swagger-ui" data-openapi-url="{url}"></div>
<script src="{ASSETS_PATH}/swagger-ui-bundle.js"></script>
<script src="{ASSETS_PATH}/nodestro-init.js"></script>
</body>
</html>
"""


def install_api_reference(
    app: FastAPI, openapi_url: str, *, dependencies: Sequence[params.Depends] = READ, docs_url: str = "/api/docs"
) -> None:
    """Add the reference page at ``docs_url`` and its files at :data:`ASSETS_PATH` (both with ``dependencies``)."""

    @app.get(docs_url, dependencies=list(dependencies), include_in_schema=False)
    def api_docs() -> HTMLResponse:
        return HTMLResponse(reference_page(openapi_url, f"{app.title} - API reference"))

    @app.get(ASSETS_PATH + "/{name}", dependencies=list(dependencies), include_in_schema=False)
    def api_docs_asset(name: str) -> FileResponse:
        media_type = ASSETS.get(name)
        if media_type is None:
            raise KeyError(name)
        return FileResponse(ASSETS_DIR / name, media_type=media_type, headers={"Cache-Control": _CACHE})


__all__ = ["ASSETS", "ASSETS_PATH", "SWAGGER_UI_VERSION", "VENDORED_SHA256", "install_api_reference", "reference_page"]
