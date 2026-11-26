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
- ``POST /api/folders/browse`` (``admin``, N-11): the sub-folders of a folder of this computer, for choosing the
  folder to grant (:mod:`nodestro_api.folder_browser` on ``orkexa.fs``): folder names and paths, never files. System
  folders, other users' home folders, credential folders and the data folder are neither listed nor opened
  (``422 folder.not_browsable`` with ``reason``). Only for the authenticated local session: a request that came
  through the launch secret's or access key's session (:data:`nodestro_api.launch.SESSION_SCOPE_KEY`); without one
  (no launch secret, e.g. the ``nodestro`` command in a terminal) it answers ``403 folder.session_required``. It is
  a ``POST`` with a JSON body, so it gets the CSRF protection of every change (exact ``Origin``, and with
  ``NODESTRO_LAUNCH_CSRF`` the session's CSRF value) and the path stays out of access logs; a request that the
  browser marks as from another site (``Sec-Fetch-Site``) is refused (``403 request.cross_site``).

While the switch is off, and always on a server, every route but ``state`` answers ``404 folder.disabled`` and
nothing is read or written. The routes use the workspace's CSRF and session protection like every other route; none
is reachable with an API key. The grant itself is an access grant of the access model v2 (resource ``folder``,
action ``read``), so it also shows under ``/api/access/grants`` and ``/api/access/who-may``, and the audit is the
access model's (``access_granted``, ``access_grant_revoked``). The HTTP status of each ``folder.*`` problem code is
in :data:`nodestro_api.api.NODESTRO_ERROR_STATUS`.
"""

from __future__ import annotations

import os
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from nodestro_runtime.folder_grants import FolderAccessEntry, FolderGrants, FolderGrantView
from nodestro_runtime.folders import FolderLimits, folders_enabled
from nodestro_runtime.tool_approvals import Person
from nodestro_runtime.tool_policy import AccessMode
from pydantic import AwareDatetime, BaseModel, Field

from . import folder_browser
from .api import Ctx
from .launch import SESSION_SCOPE_KEY
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
    #: Leave out the common secret files (``.env*``, keys, SSH keys, registry credentials, password databases;
    #: ``nodestro_runtime.folders.DEFAULT_EXCLUDES``). Only an administrator turns it off, for this grant.
    default_excludes: bool = True
    #: ``all`` agents, or only the ``agents`` named (``<workflow id>/<node id>``).
    scope: Literal["all", "agents"] = "all"
    agents: list[str] = Field(default_factory=list, max_length=200)
    mode: AccessMode = "supervised"
    #: Until when the grant holds; ``None``: until revoked.
    expires_at: AwareDatetime | None = None


class FolderBrowseIn(BaseModel):
    """The folder to browse: an absolute path; ``None``: the home folder (or the root of the file system)."""

    path: str | None = Field(default=None, max_length=folder_browser.MAX_PATH)


class BrowsedFolder(BaseModel):
    #: The name as stored.
    name: str
    #: The canonical absolute path to browse or grant.
    path: str
    #: A symbolic link or junction; ``path`` is its target.
    link: bool = False


class FolderPlace(BaseModel):
    kind: Literal["home", "root", "drive", "media"]
    path: str


class FolderBrowse(BaseModel):
    """One folder of the browser: its sub-folders (never files) and the starting points."""

    #: The canonical folder that was listed.
    path: str
    #: Its parent folder; ``None`` at the root of a file system or drive.
    parent: str | None
    folders: list[BrowsedFolder]
    #: More folders than answered (at most ``nodestro_api.folder_browser.MAX_FOLDERS``) or the listing took too long.
    truncated: bool
    places: list[FolderPlace]
    #: The path separator of this computer.
    separator: str


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
        default_excludes=body.default_excludes,
        scope=body.scope,
        agents=body.agents,
        mode=body.mode,
        expires_at=body.expires_at,
    )


@router.post("/browse", dependencies=ADMIN)
def browse_folders(body: FolderBrowseIn, ctx: Ctx, request: Request) -> FolderBrowse:
    """The sub-folders of a folder of this computer (names only), for choosing a folder to grant."""
    _grants(ctx)  # 404 folder.disabled while folder grants are off, and always on a server
    if request.scope.get(SESSION_SCOPE_KEY) not in ("cookie", "bearer"):
        raise ApiError(
            403, "folder.session_required", "Browsing folders needs the session of the window Nodestro opened"
        )
    folder_browser.check_fetch_site(request, "request.cross_site")
    protected = [ctx.data_dir.resolve()]
    given = body.path or str(folder_browser.home() or folder_browser.file_system_root())
    try:
        listing = folder_browser.browse(given, protected=protected)
    except folder_browser.BrowseRefused as refused:
        raise ApiError(422, "folder.not_browsable", refused.message, reason=refused.reason) from None
    return FolderBrowse(
        path=listing.path,
        parent=listing.parent,
        folders=[BrowsedFolder(name=item.name, path=item.path, link=item.link) for item in listing.folders],
        truncated=listing.truncated,
        places=[FolderPlace.model_validate(place) for place in folder_browser.places(protected=protected)],
        separator=os.sep,
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


__all__ = [
    "BrowsedFolder",
    "FolderBrowse",
    "FolderBrowseIn",
    "FolderGrantIn",
    "FolderLimitBounds",
    "FolderPlace",
    "FolderState",
    "router",
]
