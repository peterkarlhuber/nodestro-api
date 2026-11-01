"""Who may call which route: permissions, the request principal, and the check every route declares.

Every route of the studio API declares exactly one access requirement in its decorator::

    @router.put("/workflows/{workflow_id}", dependencies=EDIT)

The requirement is a FastAPI dependency, so it is enforced on every request, not just documented:
it resolves the request's :class:`Principal` (through ``app.state.principal_resolver``) and refuses the
request with ``401 access.login_required`` when there is none, or ``403 access.forbidden`` when the
principal lacks the permission. Nothing is allowed by default: a missing resolver refuses every request,
and :func:`check_routes` refuses to build an application with a route that declares no requirement.

Permissions (the strongest one that applies is declared):

- ``read``: look at workflows, runs, files, settings and everything else, including live event streams.
- ``edit``: create, change and delete definitions and stored data (workflows, agents, knowledge, memory, files).
- ``run``: everything that executes something or costs money: runs, approvals, batches, test series,
  schedules, tool code (checks, tests, tool generation) and requests to model providers (assistant, architect).
- ``admin``: the settings of a tenant (on the desktop: of the installation): settings, provider keys, API keys
  and publishing, Python package installations.
- ``install``: the installation itself, which only its operator may change: backups and restore, diagnostics,
  the network listener. The desktop's local owner holds it; on a server no tenant role does (operators work
  through the deployment, ADR-0003).

Two kinds of routes check no principal and say so explicitly: :data:`PUBLIC` routes (the health check) and the
published workflow APIs under ``/api/published``, which authenticate their callers with Nodestro API keys
(:mod:`nodestro_api.serving`). The prefix alone exempts nothing: a route there passes only if it is a
:class:`~nodestro_api.serving.KeyAuthenticatedRoute` listed in
:data:`~nodestro_api.serving.KEY_AUTHENTICATED_ROUTES`. The API reference that FastAPI generates
(``/api/docs``, ``/api/openapi.json``) is public as well on the desktop; on a server it needs ``read``
(:func:`nodestro_api.server_app.create_tenant_app`).

In the desktop profile there is one principal, the local owner (:data:`LOCAL_OWNER`), who holds every
permission: the studio API behaves as before. On a server, principals come from logins (OpenID Connect,
:mod:`nodestro_api.identity`): one user in one tenant with one role (``nodestro-server api``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
import time
from typing import Any, cast

from fastapi import FastAPI, params
import fastapi.routing
from fastapi.routing import APIRoute, APIWebSocketRoute
from nodestro_runtime.profile import SINGLE_USER_PROFILES, RuntimeProfile
from starlette.requests import HTTPConnection
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute

from .serving import KEY_AUTHENTICATED_ROUTES, KeyAuthenticatedRoute
from .serving import PREFIX as PUBLISHED_PREFIX


class Permission(str, Enum):
    READ = "read"
    EDIT = "edit"
    RUN = "run"
    ADMIN = "admin"
    INSTALL = "install"


#: Every permission (the local owner holds all of them).
ALL_PERMISSIONS = frozenset(Permission)


@dataclass(frozen=True)
class Principal:
    """Who sends a request: an id, the permissions granted to it, and the tenant whose data it works on."""

    id: str
    permissions: frozenset[Permission]
    #: The tenant of the request (``local``: the desktop's own data).
    tenant_id: str = "local"
    #: The role in the tenant the permissions come from (``None``: not a tenant member, e.g. the local owner).
    role: str | None = None

    def allows(self, permission: Permission) -> bool:
        return permission in self.permissions


#: The person at this computer in the desktop profile: the only principal there, with every permission.
LOCAL_OWNER = Principal(id="local", permissions=ALL_PERMISSIONS)

#: Finds the principal of a request; ``None``: nobody is logged in.
PrincipalResolver = Callable[[HTTPConnection], "Principal | None"]


def local_owner(_connection: HTTPConnection) -> Principal:
    return LOCAL_OWNER


def nobody(_connection: HTTPConnection) -> None:
    return None


def resolver_for(profile: RuntimeProfile) -> PrincipalResolver:
    """The principals of a profile: the local owner on the desktop and in Nodestro Local Docker (one person each),
    nobody otherwise (a server resolves its logins itself)."""
    return local_owner if profile.kind in SINGLE_USER_PROFILES else nobody


class AccessError(Exception):
    """A request without the required login or permission (answered with ``status`` and ``code``)."""

    def __init__(self, status: int, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.params = params


class Access:
    """The access requirement of one route; called by FastAPI on every request to it."""

    def __init__(self, permission: Permission | None, *, public: bool = False) -> None:
        if (permission is None) != public:
            raise ValueError("A route needs exactly one permission, or is explicitly public")
        self.permission = permission
        self.public = public

    @property
    def label(self) -> str:
        return "public" if self.permission is None else self.permission.value

    def __call__(self, connection: HTTPConnection) -> Principal | None:
        if self.permission is None:
            return None
        resolve: PrincipalResolver | None = getattr(connection.app.state, "principal_resolver", None)
        principal = resolve(connection) if resolve is not None else None
        if principal is None:
            raise AccessError(401, "access.login_required", "Log in to use this part of Nodestro")
        if not principal.allows(self.permission):
            raise AccessError(
                403,
                "access.forbidden",
                f"This needs the permission {self.permission.value!r}",
                permission=self.permission.value,
            )
        connection.state.principal = principal
        return principal

    def __repr__(self) -> str:
        return f"Access({self.label})"


#: How often a long-lived response (an event stream) checks again that its principal still has access (seconds).
STREAM_RECHECK_SECONDS = 15.0


class StreamAccess:
    """Checks again, while an event stream is open, that the principal that opened it still may read it (A-04).

    Access is checked when a stream opens; a member who is removed, logs out or loses the permission while it is
    open would otherwise keep receiving events. :meth:`lost` resolves the request's principal again at most every
    ``app.state.stream_recheck_seconds`` (default :data:`STREAM_RECHECK_SECONDS`); the stream ends when it is gone,
    is another user or tenant, or lacks ``permission``. :attr:`wait` is how long a stream may wait for its next event
    before it should check (and send a keep-alive)."""

    def __init__(self, connection: HTTPConnection, permission: Permission = Permission.READ) -> None:
        self.connection = connection
        self.permission = permission
        self.principal: Principal | None = getattr(connection.state, "principal", None)
        self.every = float(getattr(connection.app.state, "stream_recheck_seconds", STREAM_RECHECK_SECONDS))
        self._due = time.monotonic() + self.every

    @property
    def wait(self) -> float:
        return min(STREAM_RECHECK_SECONDS, self.every)

    async def lost(self) -> bool:
        """Whether the stream must end because its principal no longer has access (checks only when due)."""
        if time.monotonic() < self._due:
            return False
        self._due = time.monotonic() + self.every
        resolve: PrincipalResolver | None = getattr(self.connection.app.state, "principal_resolver", None)
        if resolve is None or self.principal is None:
            return True
        try:
            current = await asyncio.to_thread(resolve, self.connection)
        except Exception:
            return True  # fail closed: a stream whose access cannot be confirmed ends
        return (
            current is None
            or current.id != self.principal.id
            or current.tenant_id != self.principal.tenant_id
            or not current.allows(self.permission)
        )


def _requirement(access: Access) -> Sequence[params.Depends]:
    return (params.Depends(access, use_cache=False),)


#: What a route decorator passes as ``dependencies=``.
READ = _requirement(Access(Permission.READ))
EDIT = _requirement(Access(Permission.EDIT))
RUN = _requirement(Access(Permission.RUN))
ADMIN = _requirement(Access(Permission.ADMIN))
INSTALL = _requirement(Access(Permission.INSTALL))
PUBLIC = _requirement(Access(None, public=True))

#: Routes authenticated by something other than a principal, with the reason (checked by :func:`check_routes`).
SEPARATELY_AUTHENTICATED = {PUBLISHED_PREFIX: "published workflow APIs: Nodestro API keys (nodestro_api.serving)"}


class RouteAccessError(RuntimeError):
    """Routes without an access requirement; the application is not built."""


@dataclass(frozen=True)
class RouteAccess:
    """How one method of one route is protected."""

    path: str
    method: str
    access: str  # "read", "edit", "run", "admin", "install", "public", "api_key" or "generated"
    reason: str = ""


def _declared(dependencies: Sequence[params.Depends]) -> list[Access]:
    return [item.dependency for item in dependencies if isinstance(item.dependency, Access)]


def _generated_paths(app: FastAPI) -> set[str]:
    """The routes FastAPI adds itself: the OpenAPI document and the API reference pages."""
    paths = {app.openapi_url, app.docs_url, app.redoc_url}
    if app.docs_url and app.swagger_ui_oauth2_redirect_url:
        paths.add(app.swagger_ui_oauth2_redirect_url)
    return {path for path in paths if path}


def _routes(app: FastAPI) -> Iterator[tuple[BaseRoute, str, list[str], Sequence[params.Depends]]]:
    """Every route as it is served: the route, its full path, its methods, its dependencies.

    FastAPI 0.162 and later keep included routers as groups (``iter_route_contexts`` flattens them, with the
    dependencies the include adds); earlier versions list every route in ``app.routes`` directly."""
    contexts = getattr(fastapi.routing, "iter_route_contexts", None)
    if contexts is None:
        items: Iterable[Any] = app.routes
    else:
        items = contexts(app.routes)
    for item in items:
        route = getattr(item, "original_route", item)
        path = getattr(item, "path", None) or getattr(route, "path", repr(route))
        if isinstance(route, APIWebSocketRoute | WebSocketRoute):
            methods = ["WEBSOCKET"]
        else:
            methods = sorted(getattr(item, "methods", None) or getattr(route, "methods", None) or [])
        dependencies = getattr(item, "dependencies", None)
        if dependencies is None:
            dependencies = getattr(route, "dependencies", [])
        yield cast(BaseRoute, route), str(path), methods, cast(Sequence[params.Depends], dependencies)


def classify(app: FastAPI) -> tuple[list[RouteAccess], list[str]]:
    """Every method of every route with its protection, and the problems (routes that declare none)."""
    found: list[RouteAccess] = []
    problems: list[str] = []
    generated = _generated_paths(app)
    for route, path, methods, dependencies in _routes(app):
        if isinstance(route, APIRoute | APIWebSocketRoute):
            declared = _declared(dependencies)
            separate = next(
                (reason for prefix, reason in SEPARATELY_AUTHENTICATED.items() if _under(path, prefix)), None
            )
            if separate is not None and not declared:
                unlisted = [method for method in methods if not _key_authenticated(route, path, method)]
                if unlisted:
                    problems.append(
                        f"{'/'.join(unlisted)} {path}: not a key-authenticated published endpoint "
                        "(nodestro_api.serving.KEY_AUTHENTICATED_ROUTES)"
                    )
                found += [
                    RouteAccess(path, method, "api_key", separate) for method in methods if method not in unlisted
                ]
            elif len(declared) == 1:
                found += [RouteAccess(path, method, declared[0].label) for method in methods]
            else:
                what = "no access requirement" if not declared else f"{len(declared)} access requirements"
                problems.append(f"{'/'.join(methods)} {path}: {what}")
        elif isinstance(route, Route) and path in generated:
            found += [
                RouteAccess(path, method, "generated", "API reference generated by FastAPI") for method in methods
            ]
        else:
            kind = "mount" if isinstance(route, Mount) else type(route).__name__
            problems.append(f"{kind} {path}: not an API route with an access requirement")
    return found, problems


def _key_authenticated(route: BaseRoute, path: str, method: str) -> bool:
    return isinstance(route, KeyAuthenticatedRoute) and (method, path) in KEY_AUTHENTICATED_ROUTES


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def check_routes(app: FastAPI) -> list[RouteAccess]:
    """Every route's protection; raises :class:`RouteAccessError` if a route declares none (fail closed)."""
    found, problems = classify(app)
    if problems:
        raise RouteAccessError("Routes without an access requirement: " + "; ".join(problems))
    return found


__all__ = [
    "ADMIN",
    "ALL_PERMISSIONS",
    "EDIT",
    "INSTALL",
    "LOCAL_OWNER",
    "PUBLIC",
    "READ",
    "RUN",
    "SEPARATELY_AUTHENTICATED",
    "STREAM_RECHECK_SECONDS",
    "Access",
    "AccessError",
    "Permission",
    "Principal",
    "PrincipalResolver",
    "RouteAccess",
    "RouteAccessError",
    "StreamAccess",
    "check_routes",
    "classify",
    "local_owner",
    "nobody",
    "resolver_for",
]
