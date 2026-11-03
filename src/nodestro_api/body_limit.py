"""The size limit of request bodies (API2-03, API-001).

:class:`BodyLimitMiddleware` counts the bytes of a request body while it is read, so neither a missing nor a wrong
``Content-Length`` (a chunked upload) gets a larger body in, and answers ``413 request.too_large`` (``params.limit_mb``)
before the application has kept more than the limit in memory. A declared ``Content-Length`` above the limit is
refused at once, without reading.

Limits per kind of route (:data:`ROUTE_LIMITS`), configurable through the environment (whole bytes):

- ``json``, every route not listed below: ``NODESTRO_MAX_REQUEST_BYTES``, default 2 MiB;
- ``large``, workflow import, batch plan and start, creating and changing a test series:
  ``NODESTRO_MAX_LARGE_REQUEST_BYTES``, default 16 MiB;
- ``upload``, knowledge and input file uploads (one file has at most 50 MB, a request may carry several) and batch
  files: ``NODESTRO_MAX_UPLOAD_BYTES``, default 256 MiB;
- ``backup``, an uploaded backup (desktop, ``install``): ``NODESTRO_MAX_BACKUP_BYTES``, default 2 GiB;
- ``trace``, trace import: the trace limit (50 MB) plus 1 MiB, so the route's own ``trace.too_large`` still answers
  a declared size above 50 MB (fixed);
- ``published``, the studio's test of a published API: the published APIs' 80 MB (fixed).

The published workflow APIs (``/api/published``) keep their own limit and error format (:func:`serving.read_body`).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re

from nodestro_runtime.traces import MAX_TRACE_BYTES
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .serving import _MAX_BODY_BYTES as PUBLISHED_MAX_BYTES
from .serving import PREFIX as PUBLISHED_PREFIX

MIB = 1024 * 1024

#: (methods, path pattern, kind); the first match wins, every other request is ``json``.
ROUTE_LIMITS: tuple[tuple[frozenset[str], re.Pattern[str], str], ...] = tuple(
    (frozenset(methods), re.compile(pattern), kind)
    for methods, pattern, kind in (
        ({"POST"}, r"^/api/knowledge$", "upload"),
        ({"POST"}, r"^/api/inputs$", "upload"),
        ({"POST"}, r"^/api/batches/parse$", "upload"),
        ({"POST"}, r"^/api/backup/inspect$", "backup"),
        ({"POST"}, r"^/api/runs/import$", "trace"),
        ({"POST"}, r"^/api/workflows/import$", "large"),
        ({"POST"}, r"^/api/batches(/plan)?$", "large"),
        ({"POST"}, r"^/api/benchmarks$", "large"),
        ({"PUT"}, r"^/api/benchmarks/[^/]+$", "large"),
        ({"POST"}, r"^/api/publications/[^/]+/test$", "published"),
    )
)

_VARIABLES = {
    "json": ("NODESTRO_MAX_REQUEST_BYTES", 2 * MIB),
    "large": ("NODESTRO_MAX_LARGE_REQUEST_BYTES", 16 * MIB),
    "upload": ("NODESTRO_MAX_UPLOAD_BYTES", 256 * MIB),
    "backup": ("NODESTRO_MAX_BACKUP_BYTES", 2048 * MIB),
}


@dataclass(frozen=True)
class BodyLimits:
    """The limits in bytes per kind of route."""

    json: int = 2 * MIB
    large: int = 16 * MIB
    upload: int = 256 * MIB
    backup: int = 2048 * MIB
    trace: int = MAX_TRACE_BYTES + MIB
    published: int = PUBLISHED_MAX_BYTES

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> BodyLimits:
        """The limits from the environment (a positive whole number of bytes each; others raise ``ValueError``)."""
        environ = dict(os.environ) if environ is None else environ
        values: dict[str, int] = {}
        for kind, (variable, default) in _VARIABLES.items():
            raw = environ.get(variable, "").strip()
            if not raw:
                values[kind] = default
                continue
            if not raw.isdigit() or int(raw) <= 0:
                raise ValueError(f"{variable} must be a positive number of bytes, not {raw!r}")
            values[kind] = int(raw)
        return cls(**values)

    def for_request(self, method: str, path: str) -> int | None:
        """The limit of one request; ``None`` for the published APIs (they check their own)."""
        if path == PUBLISHED_PREFIX or path.startswith(PUBLISHED_PREFIX + "/"):
            return None
        kind = next(
            (kind for methods, pattern, kind in ROUTE_LIMITS if method in methods and pattern.match(path)), None
        )
        return int(getattr(self, kind or "json"))


class BodyLimitMiddleware:
    """Refuse request bodies above the route's limit with ``413 request.too_large``."""

    def __init__(self, app: ASGIApp, *, limits: BodyLimits | None = None) -> None:
        self.app = app
        self.limits = limits or BodyLimits()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limits.for_request(scope.get("method", "GET"), scope.get("path", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                declared = value.decode("latin-1").strip()
                if declared.isdigit() and int(declared) > limit:
                    await _too_large(send, limit)
                    return
        received = 0
        exceeded = False
        started = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # The application sees a client that went away: it stops reading and keeps nothing more.
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                # Whatever the application answers to the cut body, the client gets the 413.
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await _too_large(send, limit)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            await _too_large(send, limit)


async def _too_large(send: Send, limit: int) -> None:
    limit_mb = max(limit // MIB, 1)
    body = json.dumps(
        {
            "detail": f"The request is too large: at most {limit_mb} MB are accepted here",
            "code": "request.too_large",
            "params": {"limit_mb": limit_mb},
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = ["ROUTE_LIMITS", "BodyLimitMiddleware", "BodyLimits"]
