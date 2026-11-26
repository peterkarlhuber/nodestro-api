"""The folder browser of this computer: folder names only, on the path guard's :func:`orkexa.fs.browse_folder` (N-11).

A browser cannot hand a folder of this computer to the server (``showDirectoryPicker`` yields a handle, not a path),
so a page that asks for a folder (a folder grant, N-10; the result folder of an exported application,
:mod:`nodestro_api.output_folder`) browses folders through the backend. Both use this module, so there is one code
path:

- :func:`browse` answers the sub-folders of an absolute folder: names and canonical paths, never a file, a file name
  or a size. System folders, other users' home folders, credential folders (``.ssh`` ...) and the ``protected``
  folders (the data folder) are neither listed nor opened; names starting with ``.`` are left out; a link is shown
  only when its target passes the same checks (``link``), and its ``path`` is the target. Every refusal is a
  :class:`BrowseRefused` with a ``reason`` (:data:`REASONS`), which each caller turns into its own problem code.
- :func:`places` names the starting points: the home folder, the root of the file system or the drives, removable
  media.
- :func:`check_fetch_site` refuses a request that a browser marks as sent by another site (fetch metadata, as
  nodestro-session's guard does). A listing changes nothing, but it is personal data of the session's owner: it is
  answered only to the application's own pages, also for ``GET``.

Who may browse is the caller's decision: the studio API's route (``POST /api/folders/browse``,
:mod:`nodestro_api.api_folders`) answers only the authenticated local session; the exported interface's route
answers only its operator.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from orkexa.fs import FolderListing, FsError, browse_folder, browse_problem
from starlette.requests import Request

from .serving import ApiError

#: The longest path accepted (Windows' long paths are possible, but nobody types more).
MAX_PATH = 1024
#: At most this many folders are answered for one folder (the answer says ``truncated`` beyond).
MAX_FOLDERS = 500

Reason = Literal["hidden", "not_found", "not_a_folder", "no_access", "changed", "invalid_path", "network_path"]

#: Why a folder cannot be browsed, by the path guard's code.
REASONS: dict[str, Reason] = {
    "fs.not_browsable": "hidden",
    "fs.not_found": "not_found",
    "fs.not_a_directory": "not_a_folder",
    "fs.permission": "no_access",
    "fs.unreadable": "no_access",
    "fs.root_changed": "changed",
    "fs.invalid_path": "invalid_path",
    "fs.network_path": "network_path",
}


class BrowseRefused(Exception):
    """A folder that cannot be browsed: ``reason`` is one of :data:`REASONS`' values."""

    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason: Reason = reason
        self.message = message


def home() -> Path | None:
    """The user's home folder (``None`` when the system cannot tell, e.g. a service account without one)."""
    try:
        return Path.home().resolve()
    except (RuntimeError, KeyError, OSError):
        return None


def file_system_root() -> str:
    """The root of the file system (POSIX) or of the system drive (Windows)."""
    if os.name == "nt":
        return os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    return "/"


def checked_text(given: str) -> str:
    """``given`` as an absolute path to browse, without resolving it (the path guard resolves and checks it)."""
    text = given.strip()
    if not text or len(text) > MAX_PATH or any(ord(char) < 32 for char in text):
        raise BrowseRefused("invalid_path", "Enter the full path of a folder")
    path = Path(text)
    if not path.is_absolute() or (os.name == "nt" and not path.drive):
        raise BrowseRefused("invalid_path", "Enter the full path of a folder (absolute)")
    return text


def browse(given: str, *, protected: Iterable[str | os.PathLike[str]] = ()) -> FolderListing:
    """The sub-folders of the absolute folder ``given`` (:func:`orkexa.fs.browse_folder`), or :class:`BrowseRefused`."""
    try:
        return browse_folder(checked_text(given), protected=list(protected), max_folders=MAX_FOLDERS)
    except FsError as exc:
        raise BrowseRefused(REASONS.get(exc.code, "no_access"), str(exc)) from None


def places(
    first: Iterable[tuple[str, Path | None]] = (), *, protected: Iterable[str | os.PathLike[str]] = ()
) -> list[dict[str, str]]:
    """Starting points of the browser that it can open: the home folder, then ``first`` (``(kind, folder)``), then the
    drives (Windows) or the root and the removable media (POSIX)."""
    shielded = list(protected)
    found: list[dict[str, str]] = []

    def add(kind: str, path: Path | None) -> None:
        if path is None or any(item["path"] == str(path) for item in found):
            return
        with suppress(OSError):
            if path.is_dir() and browse_problem(os.path.realpath(path), protected=shielded) is None:
                found.append({"kind": kind, "path": str(path)})

    add("home", home())
    for kind, path in first:
        add(kind, path)
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            add("drive", Path(f"{letter}:\\"))
    else:
        add("root", Path("/"))
        for media in ("/Volumes", "/media", "/mnt", f"/run/media/{os.environ.get('USER', '')}"):
            add("media", Path(media))
    return found


def check_fetch_site(request: Request, code: str) -> None:
    """Refuse a request that the browser marks as coming from another site: ``Sec-Fetch-Site`` other than
    ``same-origin`` or ``none`` (typed by the user), or an ``Origin`` of another host (``403 <code>``). Programs
    outside a browser send neither header."""
    sites = {value.strip().lower() for value in request.headers.getlist("sec-fetch-site")}
    if sites - {"same-origin", "none"}:
        raise ApiError(403, code, "This request must come from the application's own page")
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower():
        raise ApiError(403, code, "This request must come from the application's own page")


__all__ = [
    "MAX_FOLDERS",
    "REASONS",
    "BrowseRefused",
    "browse",
    "check_fetch_site",
    "checked_text",
    "file_system_root",
    "home",
    "places",
]
