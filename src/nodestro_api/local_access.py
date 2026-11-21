"""Protection of the studio API against requests that a web page sends through the user's browser.

The studio API has no keys of its own: it trusts whoever reaches it on ``127.0.0.1``. A web
page the user visits can reach that address too, through the browser:

- with *DNS rebinding*, a page of ``evil.example`` makes its own name point to ``127.0.0.1`` and
  then reads and changes everything as a same-origin page. Its requests carry
  ``Host: evil.example``; only known host names are answered.
- with a *cross-site request*, a page sends a form or a ``fetch`` without preflight to the API.
  The browser marks it with the page's ``Origin``; :func:`nodestro_api.app.create_app` accepts changing requests
  and WebSockets only from exactly the origin the request was sent to (``strict_origin``, API2-10), not from other
  pages on this computer (another program's page on ``localhost:<other port>``). The frontend's development server
  (``localhost:5173``) is added only with ``NODESTRO_DEV_CORS=1`` (``extra_origins``).

The loopback rules (:func:`is_loopback`, :func:`split_host`, :func:`split_port`, :func:`is_ip_address`,
:func:`listens_everywhere` and the bind check of :func:`require_loopback_only`) come from the shared library
``nodestro_session`` (``nodestro_session.hosts``, ``nodestro_session.bind``); this module keeps the studio's own
rules, error codes and texts.

These checks protect a studio API on loopback against the browser; they are **not authentication**.
As long as the studio API has no login, it is never offered to the network
(:func:`require_loopback_only`, checked by :func:`nodestro_api.app.create_app` and the command line):

- it listens on a loopback address only (``127.0.0.1``, ``::1``, ``localhost``); ``0.0.0.0``, ``::``
  and every other address are refused with ``access.network_needs_login``;
- ``NODESTRO_ALLOWED_HOSTS`` may only add loopback names (``localhost``, ``*.localhost``, loopback
  addresses); other names and ``*`` would let a reverse proxy pass the network through, and are refused;
- a request that a proxy forwarded (``Forwarded``, ``X-Forwarded-For``, ``X-Forwarded-Host``,
  ``X-Real-IP``) is refused with ``request.forwarded_not_allowed``. This catches common proxy setups;
  a proxy that strips these headers is not detectable, which is why the configuration is refused too.

Published workflow APIs (``/api/published``) are protected by their keys: forwarded requests to them (and to the app
contract's discovery ``/api/app``) are accepted, and the network listener (:mod:`nodestro_api.network`) serves them to
other devices.

Nodestro Local Docker (:mod:`nodestro_api.local_container`) listens on every interface *of its container* and is
reached through a port the host publishes on its own loopback. There the checks are strict
(``strict_origin``): only loopback host names are answered (not the container's addresses or name), and changing
requests and WebSockets are accepted only from exactly the origin the request was sent to, so a page served by another
program on ``localhost`` (same site, other port) cannot use the studio either. Only the published port and the
container's own port are answered in ``Host`` (``ports``). The access key (:mod:`nodestro_api.launch`) is the
authentication.
"""

from __future__ import annotations

from collections.abc import Iterable
import json
import socket
from urllib.parse import urlsplit

from nodestro_runtime.problems import NodestroError
from nodestro_session.bind import loopback_refusals
from nodestro_session.hosts import is_ip_address, is_loopback, listens_everywhere, split_host, split_port
from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "FORWARDING_HEADERS",
    "SAFE_METHODS",
    "LocalAccessMiddleware",
    "allowed_hosts",
    "computer_names",
    "is_ip_address",
    "is_loopback",
    "listens_everywhere",
    "require_loopback_only",
    "split_host",
    "split_port",
]

#: Methods that change nothing; a cross-site page cannot read their answers (no CORS headers).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Headers a reverse proxy adds to the requests it forwards.
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
#: Key-protected routes that may be reached through a proxy (published workflow APIs).
_KEY_PROTECTED_PREFIX = "/api/published"
#: The app contract's discovery (public, more with a published API's key) may be reached through a proxy too.
_DISCOVERY_PATH = "/api/app"


def computer_names() -> set[str]:
    """This computer's own name, as other devices of the network reach it (``name``, ``name.local``)."""
    name = socket.gethostname().strip().lower().rstrip(".")
    if not name:
        return set()
    short = name.split(".", 1)[0]
    return {name, short, f"{short}.local"}


def allowed_hosts(bind_host: str | None, extra: Iterable[str] = ()) -> frozenset[str] | None:
    """The host names to answer besides loopback (and, when listening everywhere, IP addresses);
    ``None``: every host name."""
    if "*" in {item.strip() for item in extra}:
        return None
    names = {split_host(item) for item in extra if item.strip()}
    if listens_everywhere(bind_host):
        names |= computer_names()
    elif bind_host and not is_loopback(bind_host):
        names.add(split_host(bind_host))
    return frozenset(names)


def require_loopback_only(bind_host: str | None, extra_hosts: Iterable[str] = ()) -> None:
    """Refuse a configuration that offers the login-less studio API to the network (``access.network_needs_login``).

    Raised before anything is opened; the host and the host names that were refused are in the parameters."""
    refused: list[str] = []
    host = (bind_host or "").strip()
    bad_host, others = loopback_refusals(host, extra_hosts)
    if bad_host:
        refused.append(f"listening on {host or '(every interface)'!r}")
    if others:
        refused.append(f"NODESTRO_ALLOWED_HOSTS={','.join(others)}")
    if refused:
        raise NodestroError(
            "access.network_needs_login",
            "The studio API has no login yet, so it is only offered on this computer (127.0.0.1, ::1, localhost); "
            f"refused: {'; '.join(refused)}. Use the network listener for published workflow APIs",
            host=host,
            hosts=others,
        )


class LocalAccessMiddleware:
    """Refuse requests with a foreign ``Host`` (DNS rebinding) and changing requests with a foreign ``Origin``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        hosts: frozenset[str] | None,
        ip_addresses: bool = False,
        strict_origin: bool = False,
        ports: frozenset[int] | None = None,
        extra_origins: frozenset[str] = frozenset(),
    ) -> None:
        """``ip_addresses``: answer every IP address (listening on every interface); a page cannot
        rebind an address, only a name. ``strict_origin``: accept changes and WebSockets only from the exact origin
        of the request (Nodestro Local Docker). ``ports``: answer only these ports in ``Host`` (a ``Host`` without a
        port is port 80); ``None``: every port. ``extra_origins``: further origins (``scheme://host:port``) whose
        changes are accepted (the frontend's development server with ``NODESTRO_DEV_CORS=1``)."""
        self.app = app
        self.hosts = hosts
        self.ip_addresses = ip_addresses
        self.strict_origin = strict_origin
        self.ports = ports
        self.extra_origins = frozenset(origin.rstrip("/").lower() for origin in extra_origins)

    def _host_allowed(self, host: str) -> bool:
        if self.hosts is None or is_loopback(host) or host in self.hosts:
            return True
        return self.ip_addresses and is_ip_address(host)

    def _origin_allowed(self, origin: str, host_header: str) -> bool:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False  # "null" (sandboxed frames, files) and other schemes
        if parts.netloc.lower() == host_header.strip().lower():
            return True  # the studio's own pages
        if origin.rstrip("/").lower() in self.extra_origins:
            return True
        if self.strict_origin:
            return False
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
                "(add it to NODESTRO_ALLOWED_HOSTS when Nodestro is reached under this name)",
                host=split_host(host_header),
            )
            return
        port = split_port(host_header)
        if self.ports is not None and (80 if port is None else port) not in self.ports:
            await _refuse(
                scope,
                send,
                "request.port_not_allowed",
                f"Requests for the port {port if port is not None else 80} are not answered; "
                "use the port that Nodestro is published on",
                port=str(port if port is not None else 80),
            )
            return
        path = scope.get("path", "")
        forwarded = [name for name in FORWARDING_HEADERS if name in headers]
        keyed = path in (_KEY_PROTECTED_PREFIX, _DISCOVERY_PATH) or path.startswith(_KEY_PROTECTED_PREFIX + "/")
        if forwarded and not keyed:
            await _refuse(
                scope,
                send,
                "request.forwarded_not_allowed",
                "Requests forwarded by a proxy are not answered: the studio API has no login yet "
                f"(headers: {', '.join(forwarded)})",
                headers=", ".join(forwarded),
            )
            return
        origin = headers.get("origin")
        changing = scope.get("method", "GET") not in SAFE_METHODS or (
            self.strict_origin and scope["type"] == "websocket"
        )
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
