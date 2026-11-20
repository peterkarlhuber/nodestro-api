"""The product host: nodestro-api as a library that another product embeds (``docs/embedding.md``).

:func:`create_product_app` builds an app from named router groups (:data:`GROUPS`, the registry of
:mod:`nodestro_api.router_groups`) and the product's own routers. It is the studio app with fewer routes, never with
fewer protections: the same checks of the configuration, the same guards (host and origin checks, launch secret or
access key, body limits, security headers), the same error answers, the same background services and the same
fail-closed access check of every route, the product's included.

The interface is versioned (:data:`HOST_API_VERSION`): the signature of :func:`create_product_app`, the fields of
:class:`Branding` and :class:`HostSwitches` and the group names change only with a new version
(``tests/test_product_host.py`` freezes them).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from nodestro_runtime.extensions import Integration
from nodestro_runtime.profile import ProfileKind

from . import __version__
from .app import compose_app, install_guards, service_lifespan
from .context import AppConfig
from .router_groups import GROUPS, REQUIRED_GROUPS

#: The version of the host interface; a change of it (a signature, a field or a group name) raises it.
#: 1: N-01. 2: the group ``activity`` (N-13, the activity log); nothing else changed. 3: the group ``conversations``
#: (N-04, conversations with own agents); nothing else changed.
HOST_API_VERSION = 3


@dataclass(frozen=True)
class Branding:
    """How the product names its API: the OpenAPI document's title, version and description, and the title of the
    API reference page (``/api/docs``)."""

    title: str = "Nodestro"
    version: str = __version__
    description: str = ""


@dataclass(frozen=True)
class HostSwitches:
    """Switches of the host's behavior. None of them weakens a guard: protection is not configurable here.

    ``scheduler``: run due schedules in the background (``False``: only explicit ticks, as in tests)."""

    scheduler: bool = True


def create_product_app(
    profile: ProfileKind,
    data_dir: Path | str,
    routers: Sequence[str | APIRouter],
    branding: Branding | None = None,
    switches: HostSwitches | None = None,
    *,
    config: AppConfig | None = None,
    integrations: Iterable[Integration] | None = None,
    client_factory: Callable[[str], Any] | None = None,
) -> FastAPI:
    """A product's app on one workspace in ``data_dir``.

    ``profile`` is the operating profile, ``desktop`` or ``local-container`` (a server is refused with
    ``profile.invalid``, as by :func:`nodestro_api.app.create_app`). ``routers`` names the router groups to serve
    (:data:`GROUPS`; an unknown name raises ``ValueError``) and may add the product's own ``APIRouter`` objects,
    which are included after the groups; every route of them must declare its access requirement
    (:mod:`nodestro_api.permissions`), or the app is not built. The ``core`` group (health, meta, error codes) is
    always served. Routes of groups not named answer ``404`` and are not in the OpenAPI document.

    The remaining settings (host, launch secret, access key, allowed hosts, ...) come from ``config``, by default
    from the environment as for the studio (:meth:`AppConfig.from_env`); ``profile`` and ``data_dir`` replace
    the ones there. ``integrations`` and ``client_factory`` are passed to the workspace as by ``create_app``.
    A database that only the user may repair gives the recovery app, as for the studio."""
    names = [item for item in routers if isinstance(item, str)]
    own = [item for item in routers if not isinstance(item, str)]
    for item in own:
        if not isinstance(item, APIRouter):
            raise TypeError(f"A product router must be a group name or an APIRouter, not {type(item).__name__}")
    branding = branding or Branding()
    switches = switches or HostSwitches()
    base = config if config is not None else AppConfig.from_env()
    return compose_app(
        replace(base, profile_kind=profile, data_dir=Path(data_dir)),
        names,
        routers=own,
        title=branding.title,
        version=branding.version,
        description=branding.description,
        client_factory=client_factory,
        integrations=integrations,
        scheduler=switches.scheduler,
    )


__all__ = [
    "GROUPS",
    "HOST_API_VERSION",
    "REQUIRED_GROUPS",
    "Branding",
    "HostSwitches",
    "create_product_app",
    "install_guards",
    "service_lifespan",
]
