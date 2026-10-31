"""The per-launch secret of the desktop app: only the window the app opened can use the studio API.

The desktop app (``nodestro-desktop``) starts the backend on a free loopback port with a random secret in
``NODESTRO_LAUNCH_SECRET`` and opens its window on ``/api/launch?token=<secret>``. That request is
answered once: the backend sets a session cookie (``HttpOnly``, ``SameSite=Strict``) and a small page
moves on to the studio. From then on every request needs that cookie. Other programs and users of this computer, and
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

Nodestro Local Docker (the ``local-container`` profile, :mod:`nodestro_api.local_container`) uses the same
middleware with a persistent *access key* instead of a one-time secret (``reusable``): the link
``/api/launch?token=<key>`` that ``nodestro-local link`` prints can open any number of browser sessions (each gets
its own random session cookie, under its own cookie name), and scripts send the key as ``Authorization: Bearer``.
A request without a session in a browser gets a short page that says how to open Nodestro.
"""

from __future__ import annotations

import hmac
from http.cookies import CookieError, SimpleCookie
import json
import logging
import os
import re
import secrets
from urllib.parse import parse_qs

from nodestro_runtime.problems import NodestroError
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_VAR = "NODESTRO_LAUNCH_SECRET"
COOKIE = "nodestro_session"
LAUNCH_PATH = "/api/launch"
#: Shorter secrets are refused: the desktop app generates 256 bits.
MIN_LENGTH = 32
#: Cookie of the local container's browser sessions (another name than the desktop's: cookies are shared across
#: the ports of ``localhost``).
LOCAL_COOKIE = "nodestro_local_session"
#: Browser sessions kept per process with a reusable key (the oldest is dropped beyond that).
MAX_SESSIONS = 64
_OPEN_PATHS = frozenset({"/api/health"})
_OPEN_PREFIX = "/api/published"

_TOKEN = re.compile(r"([?&]token=)[^&\s\"]*")

#: The launch answer: continue to the studio from the backend's own origin (no script needed).
_CONTINUE = b'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/"><title>Nodestro</title>'

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


class RedactLaunchToken(logging.Filter):
    """Replace the launch token in log records (uvicorn's access log prints the full request line)."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def _redact(self, text: str) -> str:
        return _TOKEN.sub(r"\1[redacted]", text).replace(self.secret, "[redacted]")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            # Formatters such as uvicorn's AccessFormatter unpack the arguments: keep them, redacted.
            record.args = tuple(self._redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        return True


def redact_access_logs(secret: str) -> None:
    """Keep the launch token out of the access log (``uvicorn.access``)."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, RedactLaunchToken) and item.secret == secret for item in logger.filters):
        logger.addFilter(RedactLaunchToken(secret))


_SIGN_IN = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Nodestro</title><style>body{{font:16px/1.5 system-ui,sans-serif;
max-width:36rem;margin:4rem auto;padding:0 1rem;color:#222;background:#fff}}code{{background:#eee;padding:.1rem .3rem;
border-radius:.2rem}}@media (prefers-color-scheme:dark){{body{{color:#ddd;background:#111}}code{{background:#333}}}}
</style><h1>Nodestro</h1><p>{text}</p></html>"""


class LaunchSecretMiddleware:
    """Exchange the launch secret for a session cookie and require that cookie afterwards.

    ``reusable``: the secret is a persistent access key (Nodestro Local Docker): every valid launch opens a new
    session, and ``bearer`` additionally accepts ``Authorization: Bearer <key>``. ``cookie`` names the session
    cookie and ``refusal`` is the sentence a refused request gets."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: str,
        reusable: bool = False,
        bearer: bool = False,
        cookie: str = COOKIE,
        refusal: str = "This Nodestro belongs to the desktop app that started it; open Nodestro from the app",
    ) -> None:
        check_secret(secret)
        redact_access_logs(secret)
        self.app = app
        self._secret = secret.encode()
        self._session: bytes | None = None
        self._sessions: dict[bytes, None] = {}
        self.reusable = reusable
        self.bearer = bearer
        self.cookie = cookie
        self.refusal = refusal

    def _open(self, path: str) -> bool:
        return path in _OPEN_PATHS or path == _OPEN_PREFIX or path.startswith(_OPEN_PREFIX + "/")

    def _valid(self, token: bytes) -> bool:
        if self.reusable:
            return any(hmac.compare_digest(token, session) for session in self._sessions)
        return self._session is not None and hmac.compare_digest(token, self._session)

    def _has_session(self, scope: Scope) -> bool:
        for key, value in scope["headers"]:
            if key == b"authorization" and self.bearer:
                scheme, _, token = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer" and hmac.compare_digest(token.strip().encode(), self._secret):
                    return True
            if key != b"cookie":
                continue
            try:
                cookies = SimpleCookie(value.decode("latin-1"))
            except CookieError:
                continue
            morsel = cookies.get(self.cookie)
            if morsel is not None and self._valid(morsel.value.encode()):
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
        if self.reusable and _wants_html(scope):
            await _page(send, 401, self.refusal)
            return
        await _answer(scope, send, 401, "access.launch_required", self.refusal)

    async def _launch(self, scope: Scope, send: Send) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        token = (query.get("token") or [""])[0].encode()
        if scope.get("method") != "GET" or not hmac.compare_digest(token, self._secret):
            await _answer(scope, send, 403, "access.launch_invalid", "The launch link is not valid")
            return
        if self._session is not None and not self.reusable:
            await _answer(
                scope,
                send,
                403,
                "access.launch_used",
                "The launch link was already used; restart the desktop app to open a new window",
            )
            return
        session = secrets.token_urlsafe(32).encode()
        if self.reusable:
            self._sessions[session] = None
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
        else:
            self._session = session
        cookie = f"{self.cookie}={session.decode()}; HttpOnly; SameSite=Strict; Path=/"
        # Not a redirect: the window's navigation to this URL starts on the app's own page (another site), and a
        # browser does not send a SameSite=Strict cookie on a redirect that a cross-site navigation started.
        # The page below moves on from the backend's own origin, so the cookie goes with the next request.
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"set-cookie", cookie.encode()),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'none'"),
                    (b"content-length", str(len(_CONTINUE)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _CONTINUE})


def _wants_html(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") != "GET":
        return False
    accept = dict(scope["headers"]).get(b"accept", b"").decode("latin-1")
    return "text/html" in accept


async def _page(send: Send, status: int, text: str) -> None:
    from html import escape

    body = _SIGN_IN.format(text=escape(text)).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
