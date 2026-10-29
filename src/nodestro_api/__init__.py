"""Nodestro API: the HTTP backend of Nodestro (studio REST API, run events, published workflow APIs).

The API runs every workflow with ``nodestro-runtime``; it contains no workflow engine of its
own. :func:`nodestro_api.app.create_app` builds the FastAPI application,
``nodestro-api`` (:mod:`nodestro_api.cli`) starts it without the graphical studio.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nodestro-api")
except PackageNotFoundError:  # pragma: no cover - a copy without package metadata (e.g. an export)
    __version__ = "0.0.0"

__all__ = ["__version__"]
