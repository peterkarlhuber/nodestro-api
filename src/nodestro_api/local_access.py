"""Protection of the studio API against requests that a web page sends through the user's browser.

The studio API has no keys of its own: it trusts whoever reaches it on ``127.0.0.1``. A web
page the user visits can reach that address too, through the browser:

- with *DNS rebinding*, a page of ``evil.example`` makes its own name point to ``127.0.0.1`` and
  then reads and changes everything as a same-origin page. Its requests carry
  ``Host: evil.example``; only known host names are answered.
- with a *cross-site request*, a page sends a form or a ``fetch`` without preflight to the API.
  The browser marks it with the page's ``Origin``; changing requests are only accepted from the
  studio's own origin or from this computer.

``NODESTRO_ALLOWED_HOSTS`` (comma-separated) adds host names, e.g. of a reverse proxy;
``*`` accepts every host name. Listening on every interface (``--host 0.0.0.0``) accepts every
host name as well, since the addresses of this computer are not known in advance; the origin
check stays. Published workflow APIs are protected by their keys and are checked the same way.
"""

from __future__ import annotations

from collections.abc import Iterable
import ipaddress
import json
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

#: Methods that change nothing; a cross-site page cannot read their answers (no CORS headers).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "*"})


def is_loopback(host: str) -> bool:
    """``localhost``, ``*.localhost`` and loopback addresses (``127.0.0.0/8``, ``::1``)."""
    host = host.strip("[]").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def split_host(value: str) -> str:
    """The host name of a ``Host`` header value (``name:port``, ``[::1]:port``), lower case."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value[1:]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def allowed_hosts(bind_host: str | None, extra: Iterable[str] = ()) -> frozenset[str] | None:
    """The host names to answer besides loopback; ``None``: every host name."""
    names = {split_host(item) for item in extra if item.strip()}
    if "*" in {item.strip() for item in extra} or (bind_host is not None and bind_host.strip() in _WILDCARD_HOSTS):
        return None
    if bind_host and not is_loopback(bind_host):
        names.add(split_host(bind_host))
    return frozenset(names)


class LocalAccessMiddleware:
    """Refuse requests with a foreign ``Host`` (DNS rebinding) and changing requests with a foreign ``Origin``."""

    def __init__(self, app: ASGIApp, *, hosts: frozenset[str] | None) -> None:
        self.app = app
        self.hosts = hosts

    def _host_allowed(self, host: str) -> bool:
        return self.hosts is None or is_loopback(host) or host in self.hosts

    def _origin_allowed(self, origin: str, host_header: str) -> bool:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False  # "null" (sandboxed frames, files) and other schemes
        if parts.netloc.lower() == host_header.strip().lower():
            return True  # the studio's own pages
        return is_loopback(parts.hostname) or (self.hosts is not None and parts.hostname in self.hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in scope["headers"]}
        host_header = headers.get("host", "")
        if not self._host_allowed(split_host(host_header)):
            await _refuse(
                scope,
                send,
                "request.host_not_allowed",
                f"Requests for the host name {split_host(host_header)!r} are not answered "
                "(add it to NODESTRO_ALLOWED_HOSTS when Nodestro runs behind a proxy)",
                host=split_host(host_header),
            )
            return
        origin = headers.get("origin")
        changing = scope.get("method", "GET") not in SAFE_METHODS
        if origin is not None and changing and not self._origin_allowed(origin, host_header):
            await _refuse(
                scope,
                send,
                "request.origin_not_allowed",
                f"Changes from the web page {origin!r} are not accepted; use Nodestro itself or an API client",
                origin=origin,
            )
            return
        await self.app(scope, receive, send)


async def _refuse(scope: Scope, send: Send, code: str, detail: str, **params: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": detail, "code": code, "params": params}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
