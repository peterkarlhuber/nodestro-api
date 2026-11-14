"""Request ids and structured logs of the server profile (API-011).

- Every request of the server's API gets a request id: the caller's ``X-Request-ID`` when it is a short token
  (letters, digits, ``.``, ``_``, ``-``; at most 64 characters; a proxy's id is kept), otherwise a new random one. The
  answer carries it in ``X-Request-ID``, and every log record written while the request is served carries it as
  ``request_id`` (``-`` outside a request).
- ``NODESTRO_LOG_FORMAT=json`` writes one JSON object per log line (``time``, ``level``, ``logger``, ``message``,
  ``request_id``, ``exception``) for log collectors; the default stays the plain text format. The access log keeps its
  redaction of sign-in secrets (:class:`~nodestro_api.identity.RedactAuthSecrets`), which runs before formatting.

Nothing here needs a dependency; OpenTelemetry is not built in (the request id is the correlation id to search for).
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime
import json
import logging
import os
import re
import secrets
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "X-Request-ID"
_VALID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_current: ContextVar[str] = ContextVar("nodestro_request_id", default="-")


def request_id() -> str:
    """The id of the request being served (``-`` outside a request)."""
    return _current.get()


class RequestIdMiddleware:
    """Gives every HTTP request an id (see the module documentation) and sends it back in ``X-Request-ID``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        given = dict(scope.get("headers") or []).get(HEADER.lower().encode(), b"").decode("latin-1")
        found = given if _VALID.fullmatch(given) else secrets.token_hex(8)
        token = _current.set(found)

        async def tagged(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [item for item in message.get("headers", []) if item[0].lower() != HEADER.lower().encode()]
                headers.append((HEADER.encode(), found.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, tagged)
        finally:
            _current.reset(token)


class RequestIdFilter(logging.Filter):
    """Adds ``request_id`` to every record (for the text format's ``%(request_id)s`` and for the JSON format)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        found: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id()),
        }
        if record.exc_info:
            found["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            # A traceback that is already text: Tabrion's DbErrorRedactionFilter keeps the redacted one there and
            # clears exc_info (TABRION MIG-NOD-3, nodestro_api.db_log_redaction).
            found["exception"] = record.exc_text
        return json.dumps(found, ensure_ascii=False, default=str)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"


def log_format() -> str:
    """``json`` or ``text`` (``NODESTRO_LOG_FORMAT``; anything else is ``text``)."""
    return "json" if os.environ.get("NODESTRO_LOG_FORMAT", "").strip().lower() == "json" else "text"


def configure_logging(fmt: str | None = None, *, level: int = logging.INFO) -> logging.Handler:
    """The root handler of a server process (``nodestro-server api`` and ``worker``): request ids, text or JSON."""
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter() if (fmt or log_format()) == "json" else logging.Formatter(TEXT_FORMAT))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return handler
