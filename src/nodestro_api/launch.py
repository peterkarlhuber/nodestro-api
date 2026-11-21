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
``nodestro`` command in a terminal), unless ``NODESTRO_TERMINAL_LAUNCH`` is on. This is the desktop's protection
against other local processes; it is not a user login and does not make the studio API fit for the network
(:func:`nodestro_api.local_access.require_loopback_only` still applies).

The hardening steps of nodestro-session ``docs/design.md`` 5.2 (NS-002) each sit behind their own switch, all off by
default (:class:`LaunchHardening`): a TTL for the secret, CSRF value + JSON only + required ``Origin`` for changes, the
sign-out on the desktop, a secret for the terminal start, the secret in the URL fragment, and the loopback binding of
the command line. With every switch off, every answer stays the same byte for byte.

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

import atexit
import base64
from collections.abc import Callable, Mapping
import contextlib
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs

from nodestro_runtime.problems import NodestroError
from nodestro_session.cookies import clear_cookie, cookie_values, set_cookie
from nodestro_session.private import UnsafePathError, ensure_private_dir, remove_file, write_private_file
from nodestro_session.redact import SecretRedactor
from nodestro_session.sessions import SessionStore
from nodestro_session.tokens import WeakSecretError, check_secret_length, redirect_page, secret_equals
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

#: The header with the session's CSRF value (``NODESTRO_LAUNCH_CSRF``).
CSRF_HEADER = "X-Nodestro-CSRF"
#: Where the launch page leaves the session's values for the studio (``sessionStorage``, scoped to the origin).
SESSION_STORAGE_KEY = "nodestro.session"
#: The largest launch exchange body (``POST /api/launch`` with ``NODESTRO_LAUNCH_FRAGMENT``).
MAX_LAUNCH_BODY = 4096
#: The longest launch-secret TTL accepted (``NODESTRO_LAUNCH_TTL``).
MAX_TTL_SECONDS = 3600.0
#: Methods that change nothing.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: The request bodies the studio sends: JSON, and forms for file uploads (``NODESTRO_LAUNCH_CSRF``).
_ACCEPTED_BODIES = frozenset({"application/json", "multipart/form-data"})

#: The switches of the hardening steps of nodestro-session ``docs/design.md`` 5.2 (NS-002), each off by default.
TTL_ENV = "NODESTRO_LAUNCH_TTL"
CSRF_ENV = "NODESTRO_LAUNCH_CSRF"
LOGOUT_ENV = "NODESTRO_DESKTOP_LOGOUT"
TERMINAL_ENV = "NODESTRO_TERMINAL_LAUNCH"
FRAGMENT_ENV = "NODESTRO_LAUNCH_FRAGMENT"
BIND_ENV = "NODESTRO_BIND_LOOPBACK"


@dataclass(frozen=True)
class LaunchHardening:
    """The hardening of the desktop launch (nodestro-session ``docs/design.md`` 5.2, NS-002). Every switch is off by
    default, and with all of them off nothing changes. Each one waits for its counterpart (the desktop shell or the
    studio frontend) before it may be turned on. They apply to the desktop profile's launch secret only, never to
    Nodestro Local Docker's access key (``reusable``).

    - ``ttl`` (``NODESTRO_LAUNCH_TTL=<seconds>``): the launch secret works only this long after the backend's first
      answer (``access.launch_expired``).
    - ``csrf`` (``NODESTRO_LAUNCH_CSRF=1``): every change (``POST``, ``PUT``, ``PATCH``, ``DELETE``) of a session needs
      an ``Origin`` (``request.origin_required``), a JSON or form body (``request.json_only``) and the session's CSRF
      value in ``X-Nodestro-CSRF`` (``access.csrf_invalid``); the launch page hands the value to the studio.
    - ``logout`` (``NODESTRO_DESKTOP_LOGOUT=1``): ``POST /api/logout`` ends the window's session.
    - ``terminal`` (``NODESTRO_TERMINAL_LAUNCH=1``): the ``nodestro`` command without a secret issues one and opens
      the studio through a launch link (:func:`issue_terminal_secret`), so no other local program can use it.
    - ``fragment`` (``NODESTRO_LAUNCH_FRAGMENT=1``): the launch link carries the secret in its fragment
      (``/api/launch#token=<secret>``), which never reaches a server log; ``?token=`` is refused.
    - ``bind_loopback`` (``NODESTRO_BIND_LOOPBACK=1``): the command line listens on a random ``127.a.b.c`` and holds
      ``[::1]`` on the same port (``nodestro_session.bind_loopback``, :func:`nodestro_api.cli.prepare`).
    """

    ttl: float | None = None
    csrf: bool = False
    logout: bool = False
    terminal: bool = False
    fragment: bool = False
    bind_loopback: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LaunchHardening:
        """The switches from the environment: ``1`` turns one on, anything else leaves it off; the TTL is a number of
        seconds (``access.launch_ttl_invalid`` otherwise)."""
        env = os.environ if env is None else env

        def on(name: str) -> bool:
            return env.get(name, "").strip() == "1"

        return cls(
            ttl=_ttl(env.get(TTL_ENV, "")),
            csrf=on(CSRF_ENV),
            logout=on(LOGOUT_ENV),
            terminal=on(TERMINAL_ENV),
            fragment=on(FRAGMENT_ENV),
            bind_loopback=on(BIND_ENV),
        )

    @property
    def page(self) -> bool:
        """Whether the launch answers with the launch page (a script) instead of the plain redirect page."""
        return self.csrf or self.logout or self.fragment


def _ttl(value: str) -> float | None:
    text = value.strip()
    if not text or text == "0":
        return None
    try:
        seconds = float(text)
    except ValueError:
        seconds = -1.0
    if not 0 < seconds <= MAX_TTL_SECONDS:  # also NaN
        raise NodestroError(
            "access.launch_ttl_invalid",
            f"{TTL_ENV} must be a number of seconds from 1 to {int(MAX_TTL_SECONDS)} (0 or empty: no limit)",
            value=text[:40],
        )
    return seconds


_UNSET = object()
_taken: object | str | None = _UNSET
#: The secret the command line issued itself (``NODESTRO_TERMINAL_LAUNCH``), and its redirect file.
_terminal: str | None = None
_terminal_redirect: Path | None = None


def launch_secret_from_env() -> str | None:
    """The launch secret: the desktop app's handoff (:mod:`nodestro_api.handoff`, never the environment), otherwise
    ``NODESTRO_LAUNCH_SECRET`` (an operator's setting), removed from the environment on the first call so that no
    process the backend starts (uv, OCR, tools) inherits it; later calls return the same value. Without either, the
    secret the command line issued (:func:`issue_terminal_secret`), if any."""
    global _taken
    if _taken is _UNSET:
        _taken = os.environ.pop(ENV_VAR, None) or None
    received = handoff.received()
    if received is not None:
        os.environ.pop(ENV_VAR, None)
        return received.launch_secret
    if _taken is None:
        return _terminal
    return _taken  # type: ignore[return-value]


def issue_terminal_secret() -> str:
    """Issue this process's launch secret (``NODESTRO_TERMINAL_LAUNCH``): 256 random bits, kept in memory only (never
    in ``os.environ``), returned by :func:`launch_secret_from_env` from now on and removed from the access log."""
    global _terminal
    if _terminal is None:
        _terminal = secrets.token_hex(32)
    return _terminal


def launch_link(base_url: str, secret: str, *, fragment: bool) -> str:
    """The launch link for ``secret``: in the fragment with ``fragment``, otherwise in the query."""
    from urllib.parse import quote

    separator = "#token=" if fragment else "?token="
    return f"{base_url.rstrip('/')}{LAUNCH_PATH}{separator}{quote(secret, safe='')}"


def write_launch_file(link: str, data_dir: Path) -> Path:
    """A redirect file that opens ``link`` (0600 in the private directory ``<data_dir>/launch``), so that the browser is
    started with a ``file://`` path, never with the secret on its command line (which every local user can read). The
    file is removed when the secret is used and when the process ends. Raises ``UnsafePathError`` when the directory is
    not private, ``OSError`` when it cannot be written."""
    global _terminal_redirect
    folder = ensure_private_dir(data_dir / "launch")
    path = folder / f"nodestro-launch-{secrets.token_hex(8)}.html"
    write_private_file(path, redirect_page(link, "Nodestro"))
    _forget_terminal_redirect()
    _terminal_redirect = path
    return path


def _forget_terminal_redirect() -> None:
    global _terminal_redirect
    path, _terminal_redirect = _terminal_redirect, None
    if path is not None:
        with contextlib.suppress(OSError, UnsafePathError):
            remove_file(path)


atexit.register(_forget_terminal_redirect)


def reset_terminal_secret() -> None:
    """Forget the command line's own secret and its redirect file (tests)."""
    global _terminal
    _forget_terminal_redirect()
    _terminal = None


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
        hardening: LaunchHardening | None = None,
    ) -> None:
        check_secret(secret)
        redact_access_logs(secret)
        self.app = app
        self._secret = secret
        #: The NS-002 switches; never for a reusable access key (Nodestro Local Docker).
        self.hardening = LaunchHardening() if reusable or hardening is None else hardening
        #: The TTL counts from here: the backend's first answer (the desktop app waits for its health check).
        self._started = clock()
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
            if self.hardening.fragment:
                await self._launch_exchange(scope, receive, send)
            else:
                await self._launch(scope, send)
            return
        if path == LOGOUT_PATH and self.reusable and scope["type"] == "http":
            await self._logout(scope, send)
            return
        if path == LOGOUT_PATH and self.hardening.logout and scope["type"] == "http":
            await self._desktop_logout(scope, send)
            return
        if self._open(path):
            await self.app(scope, receive, send)
            return
        if self._has_session(scope):
            if self.hardening.csrf:
                problem = self._change_problem(scope, self._cookie_session(scope))
                if problem is not None:
                    await _answer(scope, send, *problem)
                    return
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
        if self._expired():
            await _answer(scope, send, *_EXPIRED)
            return
        # A new random session per launch; its CSRF value goes to the studio only with NODESTRO_LAUNCH_CSRF.
        session, csrf = self._sessions.create()
        self._launched = True
        _forget_terminal_redirect()
        cookie = set_cookie(self.cookie, session, max_age=self.max_age if self.reusable else None)
        if self.hardening.page:
            # The launch page leaves the session's values for the studio, then moves on to it (as below).
            await _launch_page(send, self._handed(csrf), cookie=cookie)
            return
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

    def _expired(self) -> bool:
        """Whether the launch secret's TTL (``NODESTRO_LAUNCH_TTL``) is over; never for a reusable key."""
        ttl = self.hardening.ttl
        return ttl is not None and self.clock() - self._started >= ttl

    def _handed(self, csrf: str) -> dict[str, object]:
        """What the launch page leaves for the studio: the CSRF value (with ``NODESTRO_LAUNCH_CSRF``) and whether the
        studio offers a sign-out (``NODESTRO_DESKTOP_LOGOUT``)."""
        return {"csrf": csrf if self.hardening.csrf else None, "logout": self.hardening.logout}

    def _change_problem(self, scope: Scope, session: str | None) -> tuple[int, str, str] | None:
        """Why a change of the window's session is refused (``NODESTRO_LAUNCH_CSRF``), or ``None``: it needs an
        ``Origin`` (the outer check already refuses a foreign one), a JSON or form body, and the session's CSRF value.
        Reading requests pass.

        A WebSocket needs an ``Origin``: the outer check (:class:`nodestro_api.local_access.LocalAccessMiddleware`,
        ``strict_origin``) refuses only a foreign one, never a missing one. A browser cannot send the CSRF header on a
        WebSocket, but it always sends its ``Origin``, so this keeps every web page out. A program outside the browser
        that got the cookie can set any ``Origin``; the CSRF value does not cover WebSockets (the API has no
        WebSocket route today)."""
        if scope["type"] == "websocket":
            return None if _header(scope, b"origin") else _ORIGIN_REQUIRED
        if scope["type"] != "http" or scope.get("method", "GET") in _SAFE_METHODS:
            return None
        if not _header(scope, b"origin"):
            return _ORIGIN_REQUIRED
        if not _body_accepted(scope):
            return _JSON_ONLY
        values = [value.decode("latin-1") for key, value in scope["headers"] if key == CSRF_HEADER.lower().encode()]
        if session is None or len(values) != 1 or not self._sessions.check(session, values[0]):
            return _CSRF_INVALID
        return None

    async def _launch_exchange(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The launch with the secret in the URL fragment (``NODESTRO_LAUNCH_FRAGMENT``): ``GET`` answers the launch
        page, which takes the secret from the fragment and sends it with ``POST`` as ``{"token": ...}``; that answer
        opens the session (cookie) and carries what the page leaves for the studio. ``?token=`` is refused."""
        method = scope.get("method")
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        if method == "GET" and "token" not in query:
            await _launch_page(send, None)
            return
        if method != "POST" or query:
            await _answer(scope, send, 403, "access.launch_invalid", "The launch link is not valid")
            return
        if not _header(scope, b"origin"):
            await _answer(scope, send, *_ORIGIN_REQUIRED)
            return
        if _media_type(scope) != "application/json":
            await _answer(scope, send, *_JSON_ONLY)
            return
        token = _token_of(await _read_body(receive, MAX_LAUNCH_BODY))
        if token is None or not secret_equals(token, self._secret):
            await _answer(scope, send, 403, "access.launch_invalid", "The launch link is not valid")
            return
        if self._launched:
            await _answer(
                scope,
                send,
                403,
                "access.launch_used",
                "The launch link was already used; restart the desktop app to open a new window",
            )
            return
        if self._expired():
            await _answer(scope, send, *_EXPIRED)
            return
        session, csrf = self._sessions.create()
        self._launched = True
        _forget_terminal_redirect()
        await _json(send, 200, self._handed(csrf), cookie=set_cookie(self.cookie, session))

    async def _desktop_logout(self, scope: Scope, send: Send) -> None:
        """``POST /api/logout`` on the desktop (``NODESTRO_DESKTOP_LOGOUT``): ends the window's session and deletes its
        cookie. It needs the session, an ``Origin`` and, with ``NODESTRO_LAUNCH_CSRF``, the CSRF value. The launch
        secret stays used: the desktop app opens a new window only after a restart."""
        if scope.get("method") != "POST":
            await _answer(scope, send, 405, "access.logout_method", "Sign out with POST /api/logout")
            return
        if not _header(scope, b"origin"):
            await _answer(scope, send, *_ORIGIN_REQUIRED)
            return
        session = self._cookie_session(scope)
        if session is None:
            await _answer(scope, send, 401, "access.launch_required", self.refusal)
            return
        if self.hardening.csrf:
            problem = self._change_problem(scope, session)
            if problem is not None:
                await _answer(scope, send, *problem)
                return
        self._sessions.end(session)
        await _json(send, 200, {"ended": 1}, cookie=clear_cookie(self.cookie))

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


_EXPIRED = (403, "access.launch_expired", "The launch link has expired; restart the desktop app to open a new window")
_ORIGIN_REQUIRED = (
    403,
    "request.origin_required",
    "Changes need the Origin of Nodestro's own page; use Nodestro itself or an API client",
)
_JSON_ONLY = (415, "request.json_only", "Changes must send JSON (or a form with files)")
_CSRF_INVALID = (403, "access.csrf_invalid", "The request lacks the session's CSRF value; open Nodestro from the app")

#: The launch page's script: it leaves the session's values (from the page, or from the launch exchange with the
#: secret of the fragment) in ``sessionStorage`` and moves on to the studio. Fixed text, allowed by its hash.
_LAUNCH_SCRIPT = (
    "(function(){"
    'var status=document.getElementById("launch-status");'
    "function say(text){status.textContent=text}"
    "function go(data){try{sessionStorage.setItem(" + json.dumps(SESSION_STORAGE_KEY) + ",JSON.stringify(data))}"
    'catch(error){}location.replace("/")}'
    'var given=document.getElementById("launch-session");'
    "if(given){go(JSON.parse(given.textContent));return}"
    "var match=/^#token=([^&]+)$/.exec(location.hash);"
    'history.replaceState(null,"",location.pathname);'
    'if(!match){say("The launch link is not valid. Start Nodestro from the desktop app.");return}'
    'var token;try{token=decodeURIComponent(match[1])}catch(error){token=""}'
    'fetch("/api/launch",{method:"POST",headers:{"Content-Type":"application/json"},'
    'body:JSON.stringify({token:token}),credentials:"same-origin",cache:"no-store"})'
    ".then(function(answer){return answer.json().then(function(data){"
    'if(answer.ok){go(data)}else{say(data.detail||"The launch link is not valid.")}})})'
    '.catch(function(){say("Nodestro did not answer. Start it again from the desktop app.")})'
    "})();"
)
_LAUNCH_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_LAUNCH_SCRIPT.encode()).digest()).decode()
_LAUNCH_PAGE_POLICY = (
    f"default-src 'none'; script-src 'sha256-{_LAUNCH_SCRIPT_HASH}'; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


async def _launch_page(send: Send, handed: dict[str, object] | None, *, cookie: str | None = None) -> None:
    """The launch page: with ``handed`` (the query launch), the session's values to leave for the studio; without
    (``NODESTRO_LAUNCH_FRAGMENT``), the exchange of the fragment's secret. No secret is ever in it."""
    data = ""
    if handed is not None:
        # In a data block (never run); "<" escaped so that no value can end the element.
        text = json.dumps(handed).replace("<", "\\u003c")
        data = f'<script type="application/json" id="launch-session">{text}</script>'
    body = (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
        f'<title>Nodestro</title><p id="launch-status">Opening Nodestro\u2026</p>{data}'
        f"<script>{_LAUNCH_SCRIPT}</script></html>"
    ).encode()
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"no-referrer"),
        (b"content-security-policy", _LAUNCH_PAGE_POLICY.encode()),
        (b"content-length", str(len(body)).encode()),
    ]
    if cookie is not None:
        headers.insert(1, (b"set-cookie", cookie.encode()))
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _media_type(scope: Scope) -> str:
    values: list[str] = [value.decode("latin-1") for key, value in scope["headers"] if key == b"content-type"]
    if len(values) != 1:
        return "" if not values else "(several)"
    return values[0].split(";", 1)[0].strip().lower()


def _body_accepted(scope: Scope) -> bool:
    """A change sends JSON, a form with files, or no body at all; never a body that a web page's form or a ``fetch``
    without preflight can send (``text/plain``, ``application/x-www-form-urlencoded``) or one without a type."""
    media = _media_type(scope)
    if media:
        return media in _ACCEPTED_BODIES
    length = _header(scope, b"content-length").strip()
    chunked = any(key == b"transfer-encoding" for key, _ in scope["headers"])
    return not chunked and length in ("", "0")


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    """The request body, or ``None`` when it is longer than ``limit``."""
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        body += bytes(message.get("body", b""))
        if len(body) > limit:
            return None
        if not message.get("more_body", False):
            return body


def _token_of(body: bytes | None) -> str | None:
    if body is None:
        return None
    try:
        found = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    token = found.get("token") if isinstance(found, dict) else None
    return token if isinstance(token, str) and token else None


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


async def _json(send: Send, status: int, payload: dict[str, object], *, cookie: str | None = None) -> None:
    body = json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    if cookie is not None:
        headers.append((b"set-cookie", cookie.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
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
