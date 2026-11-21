"""The per-launch secret of the desktop app: only the window the app opened can use the studio API.

The desktop app (``nodestro-desktop``) starts the backend on a free loopback port, hands it a random secret through
a pipe (:mod:`nodestro_api.handoff`, never the environment or the command line) and opens its window on
``/api/launch?token=<secret>``. That request is
answered once: the backend sets a session cookie (``HttpOnly``, ``SameSite=Strict``) and a small page
moves on to the studio. From then on every request needs that cookie. Other programs and users of this computer, and
web pages in the user's browser, reach the port too, but have neither the secret nor the cookie:

- a second ``/api/launch`` is refused (``access.launch_used``), so a secret seen later (a process list, a
  crash report) is worthless;
- every other request without the session cookie is refused with 401 ``access.launch_required``;
- ``SameSite=Strict`` keeps the browser from sending the cookie with requests of other sites.

Open without the cookie: the health check (``/api/health``, the app waits for it), the published
workflow APIs (``/api/published``, protected by their own keys) and the app contract's discovery (``/api/app``).

An operator can set the secret in ``NODESTRO_LAUNCH_SECRET`` instead. Without either, nothing changes (the
``nodestro`` command in a terminal). This is the desktop's protection against other local processes; it is not a user
login and does not make the studio API fit for the network (:func:`nodestro_api.local_access.require_loopback_only`
still applies).

Nodestro Local Docker (the ``local-container`` profile, :mod:`nodestro_api.local_container`) uses the same
middleware with a persistent *access key* instead of a one-time secret (``reusable``): the link
``/api/launch?token=<key>`` that ``nodestro-local link`` prints can open any number of browser sessions (each gets
its own random session cookie, under its own cookie name), and scripts send the key as ``Authorization: Bearer``.
A request without a session in a browser gets a short page that says how to open Nodestro.

The session store, the constant-time comparisons, the cookie headers and the log redaction come from the shared
library ``nodestro_session`` (``SessionStore``, ``secret_equals``, ``set_cookie``, ``cookie_values``,
``SecretRedactor``). Only digests of the session cookies are kept. The paths, codes, texts and cookie names here are
Nodestro's and unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import logging
import os
import time
from urllib.parse import parse_qs

from nodestro_runtime.problems import NodestroError
from nodestro_session.cookies import clear_cookie, cookie_values, set_cookie
from nodestro_session.redact import SecretRedactor
from nodestro_session.sessions import SessionStore
from nodestro_session.tokens import WeakSecretError, check_secret_length, secret_equals
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import handoff
from .local_access import split_host

ENV_VAR = "NODESTRO_LAUNCH_SECRET"
COOKIE = "nodestro_session"
LAUNCH_PATH = "/api/launch"
#: Signing out of a browser session (reusable access key only).
LOGOUT_PATH = "/api/logout"
#: Shorter secrets are refused: the desktop app generates 256 bits.
MIN_LENGTH = 32
#: Cookie of the local container's browser sessions (another name than the desktop's: cookies are shared across
#: the ports of ``localhost``).
LOCAL_COOKIE = "nodestro_local_session"
#: Browser sessions kept per process with a reusable key (the oldest is dropped beyond that).
MAX_SESSIONS = 64
#: The health check, and the app contract's discovery (public; a published API's key adds more, NAL-C1).
_OPEN_PATHS = frozenset({"/api/health", "/api/app"})
_OPEN_PREFIX = "/api/published"

#: The launch answer: continue to the studio from the backend's own origin (no script needed).
_CONTINUE = b'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/"><title>Nodestro</title>'

_UNSET = object()
_taken: object | str | None = _UNSET


def launch_secret_from_env() -> str | None:
    """The launch secret: the desktop app's handoff (:mod:`nodestro_api.handoff`, never the environment), otherwise
    ``NODESTRO_LAUNCH_SECRET`` (an operator's setting), removed from the environment on the first call so that no
    process the backend starts (uv, OCR, tools) inherits it; later calls return the same value."""
    global _taken
    if _taken is _UNSET:
        _taken = os.environ.pop(ENV_VAR, None) or None
    received = handoff.received()
    if received is not None:
        os.environ.pop(ENV_VAR, None)
        return received.launch_secret
    return _taken  # type: ignore[return-value]


def check_secret(secret: str | None) -> None:
    """Refuse a secret that is too short to protect anything (``access.launch_secret_weak``)."""
    try:
        check_secret_length(secret, MIN_LENGTH)
    except WeakSecretError:
        assert secret is not None
        raise NodestroError(
            "access.launch_secret_weak",
            f"{ENV_VAR} must have at least {MIN_LENGTH} characters",
            length=len(secret),
            minimum=MIN_LENGTH,
        ) from None


class RedactLaunchToken(SecretRedactor):
    """Replace the launch token and every ``?token=`` value in log records (uvicorn's access log prints the full
    request line); formatters such as uvicorn's AccessFormatter unpack the arguments, so they stay arguments."""

    def __init__(self, secret: str) -> None:
        super().__init__([secret])
        self.secret = secret


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

#: The sign-out page (``GET /api/logout``): a form, no script; its post carries this origin.
_SIGN_OUT_FORM = (
    'Sign out of Nodestro in this browser.</p><form method="post" action="/api/logout">'
    '<button type="submit">Sign out</button></form><p>'
)


class LaunchSecretMiddleware:
    """Exchange the launch secret for a session cookie and require that cookie afterwards.

    ``reusable``: the secret is a persistent access key (Nodestro Local Docker): every valid launch opens a new
    session, and ``bearer`` additionally accepts ``Authorization: Bearer <key>``. ``cookie`` names the session
    cookie and ``refusal`` is the sentence a refused request gets.

    With a reusable key there is more (Nodestro Local Docker, ``docs/SESSIONS.md`` there):

    - ``session_host``: the launch and session cookies are accepted only for requests to this host name (the
      installation's random ``nodestro-<random>.localhost``), so the cookie is never set for, or sent to, a name that
      other local programs share. The bearer key works under every name the outer checks allow.
    - ``idle`` and ``max_age`` (seconds): a session ends after that long without a request, and at the latest that
      long after it was opened.
    - ``/api/logout``: ``GET`` shows a page with a sign-out button, ``POST`` ends this browser's session and deletes
      its cookie; ``POST /api/logout?all=1`` with the bearer key ends every session.
    - Every response carries ``Referrer-Policy: no-referrer``, so the host name does not leave with a link.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: str,
        reusable: bool = False,
        bearer: bool = False,
        cookie: str = COOKIE,
        refusal: str = "This Nodestro belongs to the desktop app that started it; open Nodestro from the app",
        session_host: str | None = None,
        idle: float | None = None,
        max_age: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        check_secret(secret)
        redact_access_logs(secret)
        self.app = app
        self._secret = secret
        #: Desktop: whether the one launch happened. Its one session lives in :attr:`_sessions` too.
        self._launched = False
        #: The browser sessions (digests only); the idle time and max age apply to a reusable key's sessions.
        self._sessions = SessionStore(
            MAX_SESSIONS if reusable else 1,
            idle=idle if reusable else None,
            max_age=max_age if reusable else None,
            clock=clock,
        )
        self.reusable = reusable
        self.bearer = bearer
        self.cookie = cookie
        self.refusal = refusal
        self.session_host = session_host.strip("[]").lower() if session_host else None
        self.idle = idle
        self.max_age = max_age
        self.clock = clock

    def _open(self, path: str) -> bool:
        return path in _OPEN_PATHS or path == _OPEN_PREFIX or path.startswith(_OPEN_PREFIX + "/")

    def _on_session_host(self, scope: Scope) -> bool:
        if self.session_host is None:
            return True
        return split_host(_header(scope, b"host")) == self.session_host

    def _has_bearer(self, scope: Scope) -> bool:
        if not self.bearer:
            return False
        for key, value in scope["headers"]:
            if key == b"authorization":
                scheme, _, token = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer" and secret_equals(token.strip(), self._secret):
                    return True
        return False

    def _cookie_session(self, scope: Scope) -> str | None:
        """The session of this request's cookie, when it is a valid one (and then refreshed for the idle time). Every
        value of the cookie counts: another local program may have set a second cookie of the same name."""
        if not self._on_session_host(scope):
            return None
        for value in cookie_values(scope["headers"], self.cookie):
            if self._sessions.known(value):
                return value
        return None

    def _has_session(self, scope: Scope) -> bool:
        return self._has_bearer(scope) or self._cookie_session(scope) is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if self.reusable:
            send = _with_referrer_policy(send)
        path = scope.get("path", "")
        if path == LAUNCH_PATH and scope["type"] == "http":
            await self._launch(scope, send)
            return
        if path == LOGOUT_PATH and self.reusable and scope["type"] == "http":
            await self._logout(scope, send)
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
        token = (query.get("token") or [""])[0]
        if scope.get("method") != "GET" or not secret_equals(token, self._secret):
            await _answer(scope, send, 403, "access.launch_invalid", "The launch link is not valid")
            return
        if not self._on_session_host(scope):
            # A session cookie for any other name would also reach the other programs on that name.
            await _answer(scope, send, 403, "access.launch_host", self.refusal)
            return
        if self._launched and not self.reusable:
            await _answer(
                scope,
                send,
                403,
                "access.launch_used",
                "The launch link was already used; restart the desktop app to open a new window",
            )
            return
        # A new random session per launch; its CSRF value is not used yet (docs/TODO.md, N-02 follow-ups).
        session, _ = self._sessions.create()
        self._launched = True
        cookie = set_cookie(self.cookie, session, max_age=self.max_age if self.reusable else None)
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

    async def _logout(self, scope: Scope, send: Send) -> None:
        method = scope.get("method")
        if method == "GET":
            await _page(send, 200, _SIGN_OUT_FORM, raw=True)
            return
        if method != "POST":
            await _answer(scope, send, 405, "access.logout_method", "Sign out with POST /api/logout")
            return
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if (query.get("all") or [""])[0] in ("1", "true"):
            if not self._has_bearer(scope):
                await _answer(scope, send, 401, "access.launch_required", self.refusal)
                return
            ended = self._sessions.end_all()
            await _json(send, 200, {"ended": ended})
            return
        session = self._cookie_session(scope)
        if session is not None:
            self._sessions.end(session)
        await _page(
            send,
            200,
            "You are signed out. Open Nodestro again with the link that the launcher shows.",
            cookie=clear_cookie(self.cookie),
        )


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope["headers"]:
        if key == name:
            return str(value.decode("latin-1"))
    return ""


def _with_referrer_policy(send: Send) -> Send:
    async def sending(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = [item for item in message.get("headers", []) if item[0].lower() != b"referrer-policy"]
            message = {**message, "headers": [*headers, (b"referrer-policy", b"no-referrer")]}
        await send(message)

    return sending


def _wants_html(scope: Scope) -> bool:
    if scope["type"] != "http" or scope.get("method") != "GET":
        return False
    accept = dict(scope["headers"]).get(b"accept", b"").decode("latin-1")
    return "text/html" in accept


async def _page(send: Send, status: int, text: str, *, raw: bool = False, cookie: str | None = None) -> None:
    from html import escape

    body = _SIGN_IN.format(text=text if raw else escape(text)).encode()
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"),
    ]
    if cookie is not None:
        headers.append((b"set-cookie", cookie.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _json(send: Send, status: int, payload: dict[str, object]) -> None:
    body = json.dumps(payload).encode()
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
