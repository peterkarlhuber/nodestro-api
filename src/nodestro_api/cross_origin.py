"""The standard interface from another origin: allowed origins, the token profile and CORS (NAL-C5).

The protocol and its threat model are in ``docs/cross-origin.md``. In short: an interface served from an origin the
operator allowed pairs with a C4 code (:mod:`nodestro_api.pairing`) and gets a device right bound to that origin, a
short-lived access token (memory only) and a rotating refresh cookie (``SameSite=None; Secure; Partitioned``) on the
token paths. CORS answers only allowed origins, only on the app contract's paths. All of it is off unless
``NODESTRO_CROSS_ORIGIN=on`` and pairing is on (until the independent review D1).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from .pairing import Device, Pairing

#: Seconds an access token is valid.
ACCESS_SECONDS = 15 * 60
#: Access tokens kept per device (the oldest goes first) and in all.
TOKENS_PER_DEVICE = 5
MAX_TOKENS = 4096
TOKEN_PREFIX = "nat_"
REFRESH_COOKIE = "nodestro_refresh"
TOKEN_PATH = "/api/app/token"
REFRESH_PATH = "/api/app/token/refresh"
#: The header every token request must carry: no cross-site form can send it, and it forces a preflight.
APP_HEADER = "x-nodestro-app"
ORIGINS_FOLDER = "settings"
ORIGINS_FILE = "allowed-origins.json"
MAX_ORIGINS = 32
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")
ALLOW_HEADERS = "Authorization, Content-Type, Accept, Idempotency-Key, Last-Event-ID, X-Nodestro-App"
EXPOSE_HEADERS = "Nodestro-App-Contract, Retry-After, Content-Disposition"
MAX_AGE = "600"


def enabled(environ: dict[str, str] | None = None) -> bool:
    """Cross-origin access is off unless ``NODESTRO_CROSS_ORIGIN`` is ``on`` (or ``1``, ``true``, ``yes``)."""
    value = (environ if environ is not None else os.environ).get("NODESTRO_CROSS_ORIGIN", "")
    return value.strip().lower() in ("1", "on", "true", "yes")


def normalize_origin(text: str) -> str:
    """An origin in canonical form (``https://host[:port]``, lower case); raises ``ValueError`` for anything else.

    ``http://`` is accepted only for loopback (a local development server); no path, query, fragment, user info,
    wildcard or ``null``."""
    raw = text.strip()
    if not raw or raw.lower() == "null" or "*" in raw or any(char.isspace() for char in raw):
        raise ValueError("An origin is https://HOST[:PORT]")
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("An origin is https://HOST[:PORT]")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("An origin has no path, query, fragment or user")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("The port of the origin is invalid") from None
    try:
        # Browsers send an internationalized host name in its ASCII form (punycode).
        host = parts.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("The host name of the origin is invalid") from None
    shown = f"[{host}]" if ":" in host else host
    if parts.scheme == "http" and shown not in _LOOPBACK_HOSTS:
        raise ValueError("Only https origins are allowed (http only for localhost)")
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{shown}" + (f":{port}" if port is not None and port != default else "")


class AllowedOrigins:
    """The origins the operator allowed (``data/settings/allowed-origins.json``), read again when the file changes."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / ORIGINS_FOLDER / ORIGINS_FILE
        self._cache: tuple[tuple[int, int], frozenset[str]] | None = None
        self._lock = threading.Lock()

    def _read(self) -> frozenset[str]:
        try:
            stat = self.path.stat()
        except OSError:
            return frozenset()
        mark = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache[0] == mark:
            return self._cache[1]
        found: set[str] = set()
        try:
            data = json.loads(self.path.read_text("utf-8"))
            for item in data.get("origins", []) if isinstance(data, dict) else []:
                try:
                    found.add(normalize_origin(str(item)))
                except ValueError:
                    continue
        except (OSError, ValueError):
            # A damaged file allows nothing (never a crash, never "everything").
            return frozenset()
        self._cache = (mark, frozenset(found))
        return self._cache[1]

    def list(self) -> list[str]:
        with self._lock:
            return sorted(self._read())

    def allowed(self, origin: str | None) -> bool:
        if not origin:
            return False
        with self._lock:
            return origin in self._read()

    def _write(self, origins: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".partial")
        partial.write_text(json.dumps({"format": 1, "origins": sorted(origins)}, indent=2) + "\n", encoding="utf-8")
        os.replace(partial, self.path)
        self._cache = None

    def add(self, origin: str) -> str:
        chosen = normalize_origin(origin)
        with self._lock:
            current = set(self._read())
            if chosen not in current:
                if len(current) >= MAX_ORIGINS:
                    raise ValueError(f"At most {MAX_ORIGINS} origins")
                self._write(current | {chosen})
        return chosen

    def remove(self, origin: str) -> str | None:
        """Remove ``origin``; returns its canonical form, or ``None`` when it was not allowed."""
        try:
            chosen = normalize_origin(origin)
        except ValueError:
            chosen = origin.strip()
        with self._lock:
            current = set(self._read())
            if chosen not in current:
                return None
            self._write(current - {chosen})
        return chosen


@dataclass
class _Token:
    device_id: str
    origin: str
    expires: float


class AccessTokens:
    """Short-lived access tokens, only as hashes, only in the memory of the running process."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tokens: OrderedDict[str, _Token] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, device_id: str, origin: str) -> str:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            own = [key for key, item in self._tokens.items() if item.device_id == device_id]
            for key in own[: max(0, len(own) - TOKENS_PER_DEVICE + 1)]:
                del self._tokens[key]
            while len(self._tokens) >= MAX_TOKENS:
                self._tokens.popitem(last=False)
            self._tokens[self._key(token)] = _Token(device_id, origin, self._clock() + ACCESS_SECONDS)
        return token

    def get(self, token: str) -> _Token | None:
        with self._lock:
            found = self._tokens.get(self._key(token))
            if found is None:
                return None
            if found.expires <= self._clock():
                del self._tokens[self._key(token)]
                return None
            return found

    def drop(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(self._key(token), None)

    def drop_device(self, device_id: str | None) -> None:
        """Drop the tokens of one device (``None``: of every device)."""
        with self._lock:
            for key in [key for key, item in self._tokens.items() if device_id is None or item.device_id == device_id]:
                del self._tokens[key]

    def _prune(self) -> None:
        moment = self._clock()
        for key in [key for key, item in self._tokens.items() if item.expires <= moment]:
            del self._tokens[key]


def cookie_name(instance_id: str) -> str:
    """The refresh cookie's name for one backend: cookies do not tell ports apart, so two backends on one host (the
    interface's point is to use several) would overwrite each other's cookie under one name."""
    suffix = "".join(char for char in instance_id if char.isalnum())[-16:]
    return f"{REFRESH_COOKIE}_{suffix}" if suffix else REFRESH_COOKIE


def refresh_cookie(value: str, *, max_age: int, name: str = REFRESH_COOKIE) -> str:
    """The ``Set-Cookie`` value of the refresh cookie (written by hand: ``Partitioned`` is not in every Starlette)."""
    return f"{name}={value}; Max-Age={max_age}; Path={TOKEN_PATH}; HttpOnly; Secure; SameSite=None; Partitioned"


def request_origin(headers: Headers | Any) -> str | None:
    """The request's ``Origin`` in canonical form, or ``None`` (absent, ``null`` or malformed)."""
    raw = headers.get("origin")
    if not raw:
        return None
    try:
        return normalize_origin(raw)
    except ValueError:
        return None


def same_origin(headers: Headers | Any) -> bool:
    """The ``Origin`` header names the backend itself (the host the request was sent to)."""
    raw = headers.get("origin") or ""
    return bool(raw) and urlsplit(raw).netloc.lower() == (headers.get("host") or "").lower()


class CrossOrigin:
    """Allowed origins and access tokens of one running application, over its pairing (:class:`Pairing`)."""

    def __init__(self, data_dir: Path, pairing: Pairing) -> None:
        self.origins = AllowedOrigins(data_dir)
        self.tokens = AccessTokens()
        self.pairing = pairing

    def device_of(self, token: str, origin: str | None) -> Device | None:
        """The device an access token stands for, if the token, its device and its origin are all still valid and
        the request comes from the token's origin."""
        found = self.tokens.get(token)
        if found is None or origin != found.origin or not self.origins.allowed(found.origin):
            return None
        device = self.pairing.device(found.device_id)
        if device is None or device.origin != found.origin:
            self.tokens.drop(token)
            return None
        return device

    def revoke_origin(self, origin: str) -> int:
        """Revoke every device paired from ``origin`` and drop their tokens; returns how many were revoked."""
        revoked = self.pairing.revoke_origin(origin)
        for device in self.pairing.devices():
            if device.origin == origin:
                self.tokens.drop_device(device.id)
        return revoked


def _problem(status: int, code: str, message: str) -> tuple[int, bytes]:
    return status, json.dumps({"error": {"status": status, "code": code, "message": message}}).encode("utf-8")


def _vary(send: Send) -> Send:
    async def tagged(message: Message) -> None:
        if message["type"] == "http.response.start":
            MutableHeaders(scope=message).add_vary_header("Origin")
        await send(message)

    return tagged


class CorsMiddleware:
    """CORS for allowed origins on the app contract's paths only (``docs/cross-origin.md``, "CORS").

    ``app_prefix`` is ``/api/published/<slug>`` of the package's app. Any other path, a request without ``Origin``
    and a request from the backend's own origin pass unchanged."""

    def __init__(self, app: ASGIApp, *, cross_origin: CrossOrigin, app_prefix: str) -> None:
        self.app = app
        self.cross_origin = cross_origin
        self.app_prefix = app_prefix.rstrip("/")

    def _kind(self, path: str) -> str | None:
        if path in (TOKEN_PATH, REFRESH_PATH):
            return "token"
        if path == "/api/app":
            return "discovery"
        if path == self.app_prefix or path.startswith(self.app_prefix + "/"):
            return "app"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        kind = self._kind(scope.get("path", ""))
        if kind is None:
            await self.app(scope, receive, send)
            return
        if "origin" not in headers or same_origin(headers):
            # The answer of a CORS path depends on Origin: caches must keep them apart.
            await self.app(scope, receive, _vary(send))
            return
        origin = request_origin(headers)
        allowed = origin is not None and self.cross_origin.origins.allowed(origin)
        method = scope.get("method", "GET")
        if not allowed:
            refused = _problem(403, "cors.origin_not_allowed", "This origin may not use this backend")
            await self._answer(send, *refused, {"vary": "Origin"})
            return
        assert origin is not None
        cors = {"access-control-allow-origin": origin, "vary": "Origin"}
        if kind == "token":
            cors["access-control-allow-credentials"] = "true"
        if method == "OPTIONS" and "access-control-request-method" in headers:
            methods = {"token": "POST, DELETE", "discovery": "GET", "app": "GET, POST"}[kind]
            await self._answer(
                send,
                204,
                b"",
                {
                    **cors,
                    "access-control-allow-methods": methods,
                    "access-control-allow-headers": ALLOW_HEADERS,
                    "access-control-max-age": MAX_AGE,
                },
            )
            return
        cors["access-control-expose-headers"] = EXPOSE_HEADERS
        if kind != "token":
            # Cookies never authenticate a cross-origin request here, and a key never lives in another origin's page.
            authorization = headers.get("authorization", "")
            token = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
            if headers.get("x-api-key") or (authorization and not token.startswith(TOKEN_PREFIX)):
                await self._answer(
                    send,
                    *_problem(401, "auth.token_required", "From another origin, sign in with an access token"),
                    cors,
                )
                return
            if kind == "app" and not token:
                await self._answer(send, *_problem(401, "auth.token_required", "Sign in first"), cors)
                return
            scope = {**scope, "headers": [(key, value) for key, value in scope["headers"] if key != b"cookie"]}

        async def tagged(message: Message) -> None:
            if message["type"] == "http.response.start":
                answer = MutableHeaders(scope=message)
                for key, value in cors.items():
                    answer[key] = value
            await send(message)

        await self.app(scope, receive, tagged)

    @staticmethod
    async def _answer(send: Send, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        values = [(b"cache-control", b"no-store"), (b"content-length", str(len(body)).encode())]
        if body:
            values.append((b"content-type", b"application/json"))
        values += [(key.encode(), value.encode()) for key, value in (headers or {}).items()]
        await send({"type": "http.response.start", "status": status, "headers": values})
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "ACCESS_SECONDS",
    "REFRESH_COOKIE",
    "REFRESH_PATH",
    "TOKEN_PATH",
    "AccessTokens",
    "AllowedOrigins",
    "CorsMiddleware",
    "CrossOrigin",
    "enabled",
    "normalize_origin",
    "refresh_cookie",
]
