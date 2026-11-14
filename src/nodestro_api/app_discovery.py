"""``GET /api/app``: the discovery of the app API contract ``nodestro-app/1`` (NAL-C1, ``docs/app-api/v1.md``).

A client asks every backend the same question before anything else: which contract versions do you serve, who are
you, how do I sign in? The answer comes from the desktop studio (:mod:`nodestro_api.app`), its network listener
(:mod:`nodestro_api.network`) and every exported project and portable package (:mod:`nodestro_api.standalone`), with
the same structure.

- **Without credentials** it names only the contract, the backend's id and the name its operator gave it (may be
  empty), the sign-in methods and whether TLS is used: no app names, no list of publications, no versions.
- **With an API key or a session** of the embedded interface (the same mechanisms as the published API, so an invalid
  key answers ``401``) it adds the backend's versions, the capabilities it serves and the apps that credential may
  use (the publications the key is bound to; on the network listener only those reachable there).

Requests are limited like the published API's: per key, and per client address without one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from nodestro_runtime import __version__ as runtime_version

from . import __version__
from .app_contract import (
    SERVED_CAPABILITIES,
    AppDiscovery,
    AppLinks,
    AppSummary,
    AuthInfo,
    BackendInfo,
    Instance,
    TlsInfo,
    new_instance_id,
)
from .permissions import PUBLIC
from .serving import (
    _LOOPBACK,
    PREFIX,
    ApiError,
    ApiLimits,
    KeyAuthenticatedRoute,
    PublishedError,
    ServedRelease,
    ServingCore,
)

PATH = "/api/app"
#: Discovery requests per minute and key (the published API's default request limit).
KEY_REQUESTS_PER_MINUTE = ApiLimits().requests_per_minute
#: Discovery requests per minute and client address without credentials.
ANONYMOUS_REQUESTS_PER_MINUTE = 60


def build_app_router(core: ServingCore, *, channel: Literal["local", "network"] = "local") -> APIRouter:
    """The discovery route. ``channel`` as for :func:`nodestro_api.serving.build_router`: ``local`` answers only
    this computer, ``network`` lists only publications that are reachable in the network.

    The host (:class:`~nodestro_api.serving.ServingHost`) may provide ``instance()`` (the backend's identity),
    ``auth_methods()`` and ``visible_releases(identity, channel)``; without them the backend has a new id per process,
    offers API keys only and lists no apps."""
    router = APIRouter(route_class=KeyAuthenticatedRoute, tags=["app contract"])
    fallback: dict[str, Instance] = {}

    def instance() -> Instance:
        provide: Callable[[], Instance] | None = getattr(core.host, "instance", None)
        if provide is not None:
            return provide()
        return fallback.setdefault("instance", Instance(id=new_instance_id()))

    async def discover(request: Request) -> JSONResponse:
        client = request.client.host if request.client else "unknown"
        if channel == "local" and client not in _LOOPBACK:
            raise ApiError(
                403, "lan.not_enabled", "Published APIs answer only this computer; enable network access in Nodestro"
            )
        identity = core.identify(request)
        if identity is None:
            core.throttle(f"app:{client}", ANONYMOUS_REQUESTS_PER_MINUTE)
        else:
            core.throttle(f"app-key:{identity.id}", KEY_REQUESTS_PER_MINUTE)
        me = instance()
        methods: Callable[[], list[str]] | None = getattr(core.host, "auth_methods", None)
        answer = AppDiscovery(
            backend=BackendInfo(id=me.id, name=me.name),
            auth=AuthInfo.model_validate({"methods": methods() if methods is not None else ["api_key"]}),
            tls=TlsInfo(enabled=request.url.scheme == "https"),
        )
        if identity is not None:
            visible: Callable[..., list[ServedRelease]] | None = getattr(core.host, "visible_releases", None)
            # Naming an app needs the scope that reads its info (GET /api/published/{slug}); a run-only key gets none.
            readable = "read" in identity.scopes
            releases = visible(identity, channel=channel) if visible is not None and readable else []
            answer.backend.version = __version__
            answer.backend.runtime_version = runtime_version
            answer.capabilities = list(SERVED_CAPABILITIES)
            answer.apps = [_summary(release) for release in sorted(releases, key=lambda item: item.slug)]
        return JSONResponse(answer.model_dump(mode="json", exclude_none=True), headers={"Cache-Control": "no-store"})

    router.add_api_route(
        PATH,
        discover,
        methods=["GET"],
        summary="Discover the app contract, the backend and (signed in) its apps",
        description="Without credentials: contract, backend id and name, sign-in methods, TLS. With an API key or a "
        "session: also versions, capabilities and the apps this credential may use (docs/app-api/v1.md).",
        dependencies=PUBLIC,
        responses={
            200: {"model": AppDiscovery, "description": "The discovery"},
            401: {"model": PublishedError, "description": "An unknown, revoked or expired API key (auth.invalid)"},
            403: {"model": PublishedError, "description": "This port answers only this computer (lan.not_enabled)"},
            429: {"model": PublishedError, "description": "Too many requests or failed authentications (Retry-After)"},
        },
    )
    return router


def _summary(release: ServedRelease) -> AppSummary:
    base = f"{PREFIX}/{release.slug}"
    return AppSummary(
        slug=release.slug,
        name=release.name,
        description=release.description,
        release=release.number,
        links=AppLinks(info=base, openapi=f"{base}/openapi.json"),
    )


__all__ = ["ANONYMOUS_REQUESTS_PER_MINUTE", "KEY_REQUESTS_PER_MINUTE", "PATH", "build_app_router"]
