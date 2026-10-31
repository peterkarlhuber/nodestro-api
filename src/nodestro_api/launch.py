"""The per-launch secret of the desktop app: only the window the app opened can use the studio API.

The desktop app (``nodestro-desktop``) starts the backend on a free loopback port with a random secret in
``NODESTRO_LAUNCH_SECRET`` and opens its window on ``/api/launch?token=<secret>``. That request is
answered once: the backend sets a session cookie (``HttpOnly``, ``SameSite=Strict``) and redirects to the
studio. From then on every request needs that cookie. Other programs and users of this computer, and
web pages in the user's browser, reach the port too, but have neither the secret nor the cookie:

- a second ``/api/launch`` is refused (``access.launch_used``), so a secret seen later (a process list, a
  crash report) is worthless;
- every other request without the session cookie is refused with 401 ``access.launch_required``;
- ``SameSite=Strict`` keeps the browser from sending the cookie with requests of other sites.

Open without the cookie: the health check (``/api/health``, the app waits for it) and the published
workflow APIs (``/api/published``, protected by their own keys).

Without ``NODESTRO_LAUNCH_SECRET`` nothing changes (the ``nodestro`` command in a terminal). This is the
desktop's protection against other local processes; it is not a user login and does not make the studio
API fit for the network (:func:`nodestro_api.local_access.require_loopback_only` still applies).
"""

from __future__ import annotations

import hmac
from http.cookies import CookieError, SimpleCookie
import json
import os
import secrets
from urllib.parse import parse_qs

from nodestro_runtime.problems import NodestroError
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_VAR = "NODESTRO_LAUNCH_SECRET"
COOKIE = "nodestro_session"
LAUNCH_PATH = "/api/launch"
#: Shorter secrets are refused: the desktop app generates 256 bits.
MIN_LENGTH = 32
_OPEN_PATHS = frozenset({"/api/health"})
_OPEN_PREFIX = "/api/published"

_UNSET = object()
_taken: object | str | None = _UNSET


def launch_secret_from_env() -> str | None:
    """``NODESTRO_LAUNCH_SECRET``, removed from the environment on the first call so that no process the
    backend starts (uv, OCR, tools) inherits it; later calls return the same value."""
    global _taken
    if _taken is _UNSET:
        _taken = os.environ.pop(ENV_VAR, None) or None
    return _taken  # type: ignore[return-value]


def check_secret(secret: str | None) -> None:
    """Refuse a secret that is too short to protect anything (``access.launch_secret_weak``)."""
    if secret is not None and len(secret) < MIN_LENGTH:
        raise NodestroError(
            "access.launch_secret_weak",
            f"{ENV_VAR} must have at least {MIN_LENGTH} characters",
            length=len(secret),
            minimum=MIN_LENGTH,
        )


class LaunchSecretMiddleware:
    """Exchange the one-time launch secret for a session cookie and require that cookie afterwards."""

    def __init__(self, app: ASGIApp, *, secret: str) -> None:
        check_secret(secret)
        self.app = app
        self._secret = secret.encode()
        self._session: bytes | None = None

    def _open(self, path: str) -> bool:
        return path in _OPEN_PATHS or path == _OPEN_PREFIX or path.startswith(_OPEN_PREFIX + "/")

    def _has_session(self, scope: Scope) -> bool:
        if self._session is None:
            return False
        for key, value in scope["headers"]:
            if key != b"cookie":
                continue
            try:
                cookies = SimpleCookie(value.decode("latin-1"))
            except CookieError:
                continue
            morsel = cookies.get(COOKIE)
            if morsel is not None and hmac.compare_digest(morsel.value.encode(), self._session):
                return True
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == LAUNCH_PATH and scope["type"] == "http":
            await self._launch(scope, send)
            return
        if self._open(path) or self._has_session(scope):
            await self.app(scope, receive, send)
            return
        await _answer(
            scope,
            send,
            401,
            "access.launch_required",
            "This Nodestro belongs to the desktop app that started it; open Nodestro from the app",
        )

    async def _launch(self, scope: Scope, send: Send) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        token = (query.get("token") or [""])[0].encode()
        if scope.get("method") != "GET" or not hmac.compare_digest(token, self._secret):
            await _answer(scope, send, 403, "access.launch_invalid", "The launch link is not valid")
            return
        if self._session is not None:
            await _answer(
                scope,
                send,
                403,
                "access.launch_used",
                "The launch link was already used; restart the desktop app to open a new window",
            )
            return
        self._session = secrets.token_urlsafe(32).encode()
        cookie = f"{COOKIE}={self._session.decode()}; HttpOnly; SameSite=Strict; Path=/"
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", b"/"),
                    (b"set-cookie", cookie.encode()),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


async def _answer(scope: Scope, send: Send, status: int, code: str, detail: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": detail, "code": code, "params": {}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
