"""The standard web interface as a checked set of files (NAL-C2, NAL-C3).

The interface comes from nodestro-app-ui as a folder (or a ``.tgz`` artifact of that folder) with a ``manifest.json``
that lists every file with its size and SHA-256 and the range of app contract versions it speaks. Two places use it:
``scripts/vendor_app_ui.py`` embeds a commit into ``nodestro_api/webui/``, and the launcher of a portable package
installs a newer interface without a new export (``ui install``).

What the checks prove: the folder holds exactly the files its manifest lists, unchanged, and the interface speaks a
contract version this backend serves. They do not authenticate the source (the manifest travels with the files): the
SHA-256 of the artifact that ``ui install`` requires is what ties it to a known build.

Standard library only: the launcher runs it before anything else is loaded.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
from typing import Any

MANIFEST = "manifest.json"
NAME = "nodestro-app-ui"
#: The largest interface accepted, packed and unpacked (it is about 200 KB today).
LIMIT = 20 * 1024 * 1024


class UiPackageError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(path: str) -> PurePosixPath:
    """A relative path without ``..``, empty parts, hidden parts or backslashes, or :class:`UiPackageError`."""
    pure = PurePosixPath(path)
    if (
        "\\" in path
        or pure.is_absolute()
        or not pure.parts
        or any(part in ("", ".", "..") or part.startswith(".") or ":" in part for part in pure.parts)
    ):
        raise UiPackageError(f"unsafe path: {path!r}")
    return pure


def check_tree(folder: Path, served: Iterable[int], *, extra: Iterable[str] = ()) -> dict[str, Any]:
    """``folder`` holds exactly the files its ``manifest.json`` lists (plus the names in ``extra``), each with its
    size and hash, no symbolic link, and the interface speaks one of the ``served`` contract versions. Returns the
    manifest."""
    try:
        manifest = json.loads((folder / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise UiPackageError(f"no readable {MANIFEST} in {folder}: {exc}") from None
    if not isinstance(manifest, dict) or manifest.get("name") != NAME or not isinstance(manifest.get("files"), list):
        raise UiPackageError(f"{MANIFEST} is not the manifest of {NAME}")
    contract = manifest.get("contract")
    if not (
        isinstance(contract, dict)
        and isinstance(contract.get("min"), int)
        and isinstance(contract.get("max"), int)
        and isinstance(manifest.get("version"), str)
    ):
        raise UiPackageError(f"{MANIFEST} lacks a version or a contract range")
    served = sorted(served)
    if not any(contract["min"] <= version <= contract["max"] for version in served):
        raise UiPackageError(f"the interface speaks contract {contract}, this backend serves {served}")
    listed = set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise UiPackageError(f"{MANIFEST}: a file entry is not an object")
        path = safe_path(str(item.get("path", "")))
        target = folder.joinpath(*path.parts)
        if target.is_symlink() or not target.is_file():
            raise UiPackageError(f"missing: {path}")
        data = target.read_bytes()
        if sha256(data) != item.get("sha256") or len(data) != item.get("size"):
            raise UiPackageError(f"changed: {path}")
        listed.add(path.as_posix())
    if not any(path in listed for path in ("index.html", "app.js")):
        raise UiPackageError("the interface has no index.html or app.js")
    if any(file.is_symlink() for file in folder.rglob("*")):
        raise UiPackageError("symbolic links are not allowed in the interface")
    present = {file.relative_to(folder).as_posix() for file in folder.rglob("*") if file.is_file()}
    unexpected = sorted(present - listed - {MANIFEST, *extra})
    if unexpected:
        raise UiPackageError(f"not in the manifest: {', '.join(unexpected)}")
    return manifest


def unpack_artifact(data: bytes, into: Path) -> None:
    """Unpack an interface artifact (``nodestro-app-ui-<version>.tgz``: its files at the top level) into the empty
    folder ``into``: regular files only, safe paths, at most :data:`LIMIT` bytes packed and unpacked."""
    if len(data) > LIMIT:
        raise UiPackageError("the artifact is larger than 20 MB")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            _unpack(archive, into)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise UiPackageError(f"the artifact is not a readable .tgz archive: {exc}") from None


def _unpack(archive: tarfile.TarFile, into: Path) -> None:
    """Unpack the checked members of ``archive`` (see :func:`unpack_artifact`)."""
    members = archive.getmembers()
    if sum(member.size for member in members if member.isfile()) > LIMIT:
        raise UiPackageError("the interface is larger than 20 MB unpacked")
    for member in members:
        if member.isdir():
            continue
        if not member.isfile():
            raise UiPackageError(f"not a regular file in the artifact: {member.name!r}")
        relative = safe_path(member.name.removeprefix("./"))
        target = into.joinpath(*relative.parts)
        if target.exists():
            raise UiPackageError(f"twice in the artifact: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise UiPackageError(f"cannot read {relative} from the artifact")
        target.write_bytes(source.read())
