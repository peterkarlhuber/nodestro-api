"""The API of granted folders (N-10; runtime ``docs/folders.md``): a person grants a folder of this computer for
reading, and agents read it with ``list_folder``, ``read_file`` and ``search_files``. Off by default, like the
runtime: only while ``NODESTRO_FOLDER_GRANTS=1``, and never in the server profile (a server has no person's folders).

- ``GET /api/folders/state`` (``read``): whether folder grants are on here, and the default limits and their bounds.
  Always answers, so the studio knows whether to show the folders page.
- ``GET /api/folders`` (``admin``): every folder grant of the workspace, newest first, ended ones included.
- ``GET /api/folders/{folder_id}`` (``admin``): one folder grant.
- ``GET /api/folders/{folder_id}/accesses`` (``admin``): its access log, newest first (activity log, N-13): every call
  of the folder tools, with agent, run, outcome and code, never a path.
- ``POST /api/folders`` (``admin``): grant a folder. The writer is always the request's signed-in person
  (:class:`~nodestro_runtime.tool_approvals.Person`), never a field of the body; a principal that is not a person (an
  API key, a machine) is refused (``403 access.person_required``). System folders, the home folder, credential
  folders and Nodestro's data folder are refused (``422 folder.not_grantable`` with ``reason``).
- ``DELETE /api/folders/{folder_id}`` (``admin``): revoke a folder grant; the next tool call is refused. Revoking
  twice changes nothing.

While the switch is off, and always on a server, every route but ``state`` answers ``404 folder.disabled`` and
nothing is read or written. The routes use the workspace's CSRF and session protection like every other route; none
is reachable with an API key. The grant itself is an access grant of the access model v2 (resource ``folder``,
action ``read``), so it also shows under ``/api/access/grants`` and ``/api/access/who-may``, and the audit is the
access model's (``access_granted``, ``access_grant_revoked``). The HTTP status of each ``folder.*`` problem code is
in :data:`nodestro_api.api.NODESTRO_ERROR_STATUS`.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from nodestro_runtime.folder_grants import FolderAccessEntry, FolderGrants, FolderGrantView
from nodestro_runtime.folders import FolderLimits, folders_enabled
from nodestro_runtime.tool_approvals import Person
from nodestro_runtime.tool_policy import AccessMode
from pydantic import AwareDatetime, BaseModel, Field

from .api import Ctx
from .permissions import ADMIN, READ, Principal
from .serving import ApiError

router = APIRouter(prefix="/api/folders")


class FolderLimitBounds(BaseModel):
    lowest: float
    highest: float


class FolderState(BaseModel):
    #: Whether folder grants are on here (``NODESTRO_FOLDER_GRANTS=1``, not in the server profile).
    enabled: bool
    #: The limits a grant gets unless the person sets others.
    limits: FolderLimits
    #: The bounds of each limit.
    bounds: dict[str, FolderLimitBounds]


class FolderGrantIn(BaseModel):
    """A folder grant as a person gives it. The writer and the time come from the server."""

    name: str = Field(min_length=1, max_length=80)
    #: The folder's absolute path on this computer.
    path: str = Field(min_length=1, max_length=4096)
    include: list[str] = Field(default_factory=list, max_length=100)
    exclude: list[str] = Field(default_factory=list, max_length=100)
    limits: FolderLimits | None = None
    #: Stored only: indexing a granted folder is a later step.
    index: bool = False
    #: ``all`` agents, or only the ``agents`` named (``<workflow id>/<node id>``).
    scope: Literal["all", "agents"] = "all"
    agents: list[str] = Field(default_factory=list, max_length=200)
    mode: AccessMode = "supervised"
    #: Until when the grant holds; ``None``: until revoked.
    expires_at: AwareDatetime | None = None


def _bounds() -> dict[str, FolderLimitBounds]:
    """The bounds of each limit, read from :class:`FolderLimits` (``ge`` or ``gt``, and ``le``)."""
    found: dict[str, FolderLimitBounds] = {}
    for name, field in FolderLimits.model_fields.items():
        known = {key: getattr(item, key) for item in field.metadata for key in ("ge", "gt", "le") if hasattr(item, key)}
        low, high = known.get("ge", known.get("gt")), known.get("le")
        if low is not None and high is not None:
            found[name] = FolderLimitBounds(lowest=low, highest=high)
    return found


def _enabled(ctx: Ctx) -> bool:
    return folders_enabled() and ctx.profile.kind != "server"


def _grants(ctx: Ctx) -> FolderGrants:
    if not _enabled(ctx):
        raise ApiError(404, "folder.disabled", "Folder grants are not switched on for this application")
    return FolderGrants(ctx.store, str(ctx.data_dir.resolve()))


def _person(request: Request) -> Person:
    """The signed-in person of the request; anything else is refused (``access.person_required``)."""
    principal: Principal = request.state.principal
    return Person(principal.id)


@router.get("/state", dependencies=READ)
def folder_state(ctx: Ctx) -> FolderState:
    return FolderState(enabled=_enabled(ctx), limits=FolderLimits(), bounds=_bounds())


@router.get("", dependencies=ADMIN)
def list_folders(ctx: Ctx) -> list[FolderGrantView]:
    return _grants(ctx).list()


@router.post("", status_code=201, dependencies=ADMIN)
def grant_folder(body: FolderGrantIn, ctx: Ctx, request: Request) -> FolderGrantView:
    """Grant a folder for reading. Agents see its name, never its path."""
    grants = _grants(ctx)
    return grants.grant(
        name=body.name,
        path=body.path,
        by=_person(request),
        include=body.include,
        exclude=body.exclude,
        limits=body.limits,
        index=body.index,
        scope=body.scope,
        agents=body.agents,
        mode=body.mode,
        expires_at=body.expires_at,
    )


@router.get("/{folder_id}", dependencies=ADMIN)
def get_folder(folder_id: str, ctx: Ctx) -> FolderGrantView:
    return _grants(ctx).get(folder_id)


@router.get("/{folder_id}/accesses", dependencies=ADMIN)
def folder_accesses(
    folder_id: str, ctx: Ctx, limit: Annotated[int, Query(ge=1, le=1000)] = 200
) -> list[FolderAccessEntry]:
    return _grants(ctx).accesses(folder_id, limit=limit)


@router.delete("/{folder_id}", dependencies=ADMIN)
def revoke_folder(folder_id: str, ctx: Ctx, request: Request) -> FolderGrantView:
    """Revoke a folder grant: every later call of the folder tools on it is refused."""
    return _grants(ctx).revoke(folder_id, by=_person(request))


__all__ = ["FolderGrantIn", "FolderLimitBounds", "FolderState", "router"]
