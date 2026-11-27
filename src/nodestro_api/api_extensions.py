"""The data-only extension store (N-22, runtime RT-098, owner decision 2026-11-27; API-081): an administrator takes a
foreign extension package into ``<data>/extensions/<sha256>/``, lists, updates, removes and revokes it. The package is
never installed into the backend's Python environment and never imported here; its tools run only in the Docker
sandbox, only while ``NODESTRO_EXTENSION_TOOLS=1`` (off by default), and no tool class is released.

- ``GET /api/extensions`` (``admin``): the store's packages (with their state in this process: ``locked`` with the
  reason, or ``sandboxed`` with their tools), the revoked hashes and the packages refused in the host environment.
- ``POST /api/extensions`` (``admin``, multipart): ``file`` (a pure-Python wheel), ``sha256`` (the hash the
  administrator expects), ``origin`` (where it comes from), optionally ``replaces`` (the SHA-256 of the installed
  version it updates). The runtime checks it before anything is written (``422 extension_store.refused`` with
  ``reason``) and takes it in atomically.
- ``DELETE /api/extensions/{sha256}`` (``admin``): remove a package (it may be uploaded again, checked again).
- ``POST /api/extensions/{sha256}/revoke`` (``admin``): revoke a hash: removed when installed, never installed again.

Only a signed-in person writes (``403 access.person_required`` for an API key or a machine principal). A change takes
effect for the listing of tools when the workspace opens next; a removal or revocation applies to calls at once. Every
change and every refused upload is an activity event (source ``extension``). On a server there is no store: every
route answers ``404 extension_store.disabled``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from nodestro_runtime.extension_store import MAX_WHEEL_BYTES, ExtensionStore
from nodestro_runtime.extensions import extension_tools_enabled
from nodestro_runtime.tool_approvals import Person
from pydantic import BaseModel, Field

from .api import Ctx
from .permissions import ADMIN, Principal
from .serving import ApiError

router = APIRouter(prefix="/api/extensions")

_SHA256 = r"^[0-9a-f]{64}$"


class StoredExtension(BaseModel):
    """One package of the store and how it stands in this process."""

    sha256: str
    distribution: str
    version: str
    #: ``[entry point name, module:attribute]``.
    entries: list[list[str]]
    origin: str
    installed_at: str
    installed_by: str
    replaces: str | None = None
    files: int
    bytes: int
    #: ``sandboxed`` (its tools run in the Docker sandbox) or ``locked``.
    state: Literal["sandboxed", "locked"] = "locked"
    #: Why it is locked: ``off`` (the switch), ``not_secured`` (no proven sandbox), ``invalid`` (it failed a check),
    #: ``pending`` (installed or changed after the workspace opened: listed at the next start).
    reason: str | None = "pending"
    detail: str | None = None
    tools: list[str] = Field(default_factory=list)


class RevokedExtension(BaseModel):
    sha256: str
    revoked_at: str
    revoked_by: str


class RefusedExtension(BaseModel):
    """A package found in the backend's Python environment: never imported, never offered (``invalid``)."""

    name: str
    distribution: str
    version: str
    detail: str | None = None


class ExtensionStoreView(BaseModel):
    #: Whether this installation has a store (desktop and Nodestro Local Docker; never a server).
    enabled: bool
    #: Whether the development switch ``NODESTRO_EXTENSION_TOOLS`` is on (off by default).
    tools_switch: bool
    #: The largest wheel the store accepts, in bytes.
    max_bytes: int
    packages: list[StoredExtension]
    revoked: list[RevokedExtension]
    host_refused: list[RefusedExtension]


def _store(ctx: Ctx) -> ExtensionStore:
    store = ctx.extension_store
    if store is None or ctx.profile.kind == "server":
        raise ApiError(404, "extension_store.disabled", "This installation has no extension store")
    return store


def _person(request: Request) -> str:
    """The signed-in person of the request; an API key or a machine is refused (``access.person_required``)."""
    principal: Principal = request.state.principal
    return Person(principal.id).id


def _view(ctx: Ctx, store: ExtensionStore) -> ExtensionStoreView:
    listing = store.listing()
    status: dict[str, list[dict[str, Any]]] = {}
    refused: list[RefusedExtension] = []
    for item in ctx.extensions.packages_status():
        if item.get("sha256"):
            status.setdefault(str(item["sha256"]), []).append(item)
        else:
            refused.append(
                RefusedExtension(
                    name=str(item["name"]),
                    distribution=str(item["distribution"]),
                    version=str(item["version"]),
                    detail=item.get("detail"),  # type: ignore[arg-type]
                )
            )
    packages: list[StoredExtension] = []
    for entry in listing["packages"]:
        rows = status.get(entry["sha256"], [])
        locked = [row for row in rows if row["state"] == "locked"]
        state: dict[str, Any] = {}
        if rows:
            first = locked[0] if locked else rows[0]
            state = {
                "state": "locked" if locked else "sandboxed",
                "reason": first["reason"],
                "detail": first["detail"],
                "tools": [tool for row in rows for tool in row["tools"]],
            }
        packages.append(StoredExtension.model_validate({**entry, **state}))
    return ExtensionStoreView(
        enabled=True,
        tools_switch=extension_tools_enabled(),
        max_bytes=MAX_WHEEL_BYTES,
        packages=packages,
        revoked=[RevokedExtension.model_validate(item) for item in listing["revoked"]],
        host_refused=refused,
    )


@router.get("", dependencies=ADMIN)
def list_extensions(ctx: Ctx) -> ExtensionStoreView:
    """The store's packages, the revoked hashes and the packages refused in the host environment."""
    return _view(ctx, _store(ctx))


@router.post("", status_code=201, dependencies=ADMIN)
async def upload_extension(
    ctx: Ctx,
    request: Request,
    file: Annotated[UploadFile, File()],
    sha256: Annotated[str, Form(pattern=_SHA256)],
    origin: Annotated[str, Form(min_length=1, max_length=500)],
    replaces: Annotated[str | None, Form(pattern=_SHA256)] = None,
) -> StoredExtension:
    """Take a pure-Python wheel into the store after every check of the runtime (nothing is installed or imported)."""
    store = _store(ctx)
    actor = _person(request)
    data = await file.read(MAX_WHEEL_BYTES + 1)
    entry = await asyncio.to_thread(store.install, data, sha256=sha256, origin=origin, actor=actor, replaces=replaces)
    return StoredExtension.model_validate(entry)


@router.delete("/{sha256}", dependencies=ADMIN)
def remove_extension(sha256: str, ctx: Ctx, request: Request) -> ExtensionStoreView:
    """Remove a package: later calls of its tools are refused at once."""
    store = _store(ctx)
    store.remove(sha256, actor=_person(request))
    return _view(ctx, store)


@router.post("/{sha256}/revoke", dependencies=ADMIN)
def revoke_extension(sha256: str, ctx: Ctx, request: Request) -> ExtensionStoreView:
    """Revoke a hash: removed when installed, refused at every later upload."""
    store = _store(ctx)
    store.revoke(sha256, actor=_person(request))
    return _view(ctx, store)


__all__ = [
    "ExtensionStoreView",
    "RefusedExtension",
    "RevokedExtension",
    "StoredExtension",
    "router",
]
