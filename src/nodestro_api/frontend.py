"""Serving the built single-page studio next to the API: gzip for its text files, cache headers for the browser.

:func:`mount_frontend` serves real files as they are and every other non-API path as ``index.html``. Only these
responses are compressed and get Cache-Control headers: the API routes (registered before) and their event streams
(``text/event-stream``) never pass through this code, so their headers and streaming stay as they are.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route, request_response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Scope

#: The files under ``/assets`` carry a content hash in their names (Vite): a new build gives them new names.
CACHE_HASHED = "public, max-age=31536000, immutable"
#: ``index.html`` and the other files of the interface keep their names: the browser revalidates them (ETag).
CACHE_REVALIDATE = "no-cache"
#: Smaller responses are sent as they are; gzip would save a few bytes at the price of a compression round.
GZIP_MINIMUM_SIZE = 1024
#: Close to level 9 in size for the JavaScript bundle at a fraction of the CPU time (it is compressed per request).
GZIP_LEVEL = 6


def mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the built single-page studio: real files as they are, every other non-API path as ``index.html``.

    Call it after every API route: the catch-all route is appended last.
    """
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", _compressed(_HashedAssets(directory=assets)), name="assets")
    index = static_dir / "index.html"
    root = static_dir.resolve()

    async def frontend(request: Request) -> Response:
        path: str = request.path_params["path"]
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:
            return FileResponse(candidate, headers={"Cache-Control": CACHE_REVALIDATE})
        return FileResponse(index, headers={"Cache-Control": CACHE_REVALIDATE})

    app.router.routes.append(
        Route(
            "/{path:path}",
            _compressed(request_response(frontend)),
            methods=["GET"],
            name="frontend",
            include_in_schema=False,
        )
    )


def _compressed(asgi: ASGIApp) -> ASGIApp:
    """``asgi`` with gzip for clients that accept it (text responses of at least :data:`GZIP_MINIMUM_SIZE`)."""
    return GZipMiddleware(asgi, minimum_size=GZIP_MINIMUM_SIZE, compresslevel=GZIP_LEVEL)


class _HashedAssets(StaticFiles):
    """The built ``assets`` directory, cached by browsers for a year (the file names change with the content).

    Also on a ``304``; a ``404`` (a chunk of an older build) gets no Cache-Control.
    """

    def file_response(
        self, full_path: str | os.PathLike[str], stat_result: os.stat_result, scope: Scope, status_code: int = 200
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers.setdefault("Cache-Control", CACHE_HASHED)
        return response


__all__ = ["CACHE_HASHED", "CACHE_REVALIDATE", "mount_frontend"]
