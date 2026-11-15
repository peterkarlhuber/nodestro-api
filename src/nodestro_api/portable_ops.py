"""Operating a portable package like installed software (NAL-C3): its folder model, the integrity check, backup and
restore, update and rollback, taking over an older package's data, and a newer web interface without a new export.

The folder model (``layout_version`` 2 in ``PORTABLE.json``)::

    <package>/
        start.sh, stop.sh, configure.sh (start.cmd ... on Windows), README.md
        program/            what the export wrote, never changed: Python, libraries, runtime, workflow, interface,
                            PORTABLE.json, SHA256SUMS (a checksum of every file in program/)
        program.previous/   the program before the last update (``rollback`` swaps the two)
        program.next/       only for a moment: the program an update or rollback puts in place
        config/             settings.toml (no secrets)
        data/               runs, keys, approvals; data/secrets/ holds the provider and connection secrets
        results/            result files
        logs/
        ui/                 a newer web interface installed with ``ui install`` (optional)
        backups/            backups made before an update or a restore

Everything but ``program/`` belongs to the person who runs the package; an update replaces ``program/`` only. A
package with ``layout_version`` 1 (or none) has the program files directly in its folder; ``adopt`` copies its
settings and data into a new package.

What the integrity check proves: no file of ``program/`` was changed, removed or added since the export (by mistake,
a broken copy, a stray edit). It is not a defence against someone who may write to the folder: they could write a new
``SHA256SUMS`` as well. Only a signature can prove where a package comes from (API-046).

Standard library only (and :mod:`nodestro_api.ui_package`): the commands run before the application is loaded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import time
from typing import Any
import zipfile

from . import ui_package

PROGRAM = "program"
PREVIOUS = "program.previous"
#: A program waiting to be put in place (by update or rollback, see :func:`apply_pending`).
NEXT = "program.next"
UI = "ui"
BACKUPS = "backups"
SUMS = "SHA256SUMS"
#: What ``ui install`` notes about the artifact it installed (in ``ui/``, beside the interface's files).
INSTALLED = ".installed.json"
INFO = "PORTABLE.json"
LAYOUT_VERSION = 2
#: Files the program writes into ``program/`` while it runs, so outside its checksums: the model server's index of the
#: bundled models, written in place on the first start (nothing is copied).
WRITTEN_AT_RUN_TIME = ("models/manifests-v2/",)
#: Python's bytecode caches: written by Python next to the modules it imports, made again when they do not match their
#: source, so neither exported (see :func:`nodestro_api.portable_build.strip_bytecode`) nor checked.
BYTECODE = "__pycache__"
#: Skipped by the check at each start (the model files are checked by size at each start and by SHA-256 on the first
#: start and with ``verify``/``check --full``, see :class:`nodestro_api.portable.LocalModels`).
SKIPPED_AT_START = ("models/",)
#: Below the data folder, never part of a backup: the instance file and lock of a running application, a copy of the
#: models and the model server's own folder (both made again from the package).
NOT_BACKED_UP = ("run", "models", "ollama")
SECRETS = "secrets"
BACKUP_FORMAT = "nodestro-portable-backup"
BACKUP_VERSION = 1
#: The folders a backup holds, at its top level.
BACKUP_PARTS = ("config", "data", "results")

logger = logging.getLogger("nodestro.portable")


class PackageRefusal(Exception):
    """A command cannot be carried out; ``key`` names the launcher's message, ``values`` fill it."""

    def __init__(self, key: str, message: str, **values: Any) -> None:
        super().__init__(message)
        self.key = key
        self.values = values


# ---- the folder model ------------------------------------------------------------------------------------------


def read_info(folder: Path) -> dict[str, Any]:
    """``PORTABLE.json`` of a program folder (``{}`` when there is none or it cannot be read)."""
    try:
        found = json.loads((folder / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(found) if isinstance(found, dict) else {}


def layout_of(info: dict[str, Any]) -> int:
    value = info.get("layout_version", 1)
    return value if isinstance(value, int) else 1


def package_root(program: Path) -> Path:
    """The package folder of the program folder the launcher runs from: its parent in layout 2, itself before."""
    if program.name == PROGRAM and layout_of(read_info(program)) >= LAYOUT_VERSION:
        return program.parent
    return program


def program_of(root: Path) -> Path:
    """The program folder of a package folder (layout 2: ``program/``; before: the package folder itself)."""
    candidate = root / PROGRAM
    if layout_of(read_info(candidate)) >= LAYOUT_VERSION:
        return candidate
    return root


# ---- integrity -------------------------------------------------------------------------------------------------


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while block := reader.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (OSError, ValueError):
        return False
    return True


def verify_program(program: Path, *, skip: Iterable[str] = ()) -> list[str]:
    """Problems of ``program/`` against its ``SHA256SUMS`` (``[]``: every file is as exported): ``missing: <path>``,
    ``changed: <path>``, ``extra: <path>`` and ``link: <path>`` (a symbolic link that leads out of the folder).
    Paths that start with one of ``skip`` are not hashed (only their presence is checked)."""
    skipped = tuple(skip)
    try:
        text = (program / SUMS).read_text(encoding="utf-8")
    except OSError:
        return [f"missing: {SUMS}"]
    problems: list[str] = []
    listed: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        # Hidden files are part of a program (site-packages has ``.lock`` files and ``.agents/`` folders).
        relative = archive_path(name) if separator and len(digest) == 64 else None
        if relative is None:
            problems.append(f"changed: {SUMS}")
            continue
        listed.add(relative.as_posix())
        if BYTECODE in relative.parts:
            continue
        target = program.joinpath(*relative.parts)
        if target.is_symlink() or not target.is_file():
            problems.append(f"missing: {relative}")
        elif not relative.as_posix().startswith(skipped) and _hash(target) != digest:
            problems.append(f"changed: {relative}")
    for path in sorted(program.rglob("*")):
        relative_name = path.relative_to(program).as_posix()
        if BYTECODE in path.relative_to(program).parts:
            continue
        if path.is_symlink():
            if not _inside(path, program):
                problems.append(f"link: {relative_name}")
            continue
        if not path.is_file() or relative_name == SUMS or relative_name in listed:
            continue
        if relative_name.startswith(WRITTEN_AT_RUN_TIME):
            continue
        problems.append(f"extra: {relative_name}")
    return problems


# ---- private folders -------------------------------------------------------------------------------------------


def _current_sid() -> str | None:
    """The security identifier of the user this process runs as (Windows), from ``whoami``."""
    try:
        answer = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fields = [field.strip().strip('"') for field in answer.strip().split(",")]
    sid = fields[-1] if fields else ""
    return sid if sid.startswith("S-1-") else None


def restrict_folder(folder: Path) -> bool:
    """Keep ``folder`` to the current user: ``0700`` on POSIX; on Windows an access list with the current user only
    (no inherited entries, ``icacls``). ``False`` when that could not be done (logged, never fatal)."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            folder.chmod(stat.S_IRWXU)
            return True
        if sys.platform != "win32":
            return False
        sid = _current_sid()
        if sid is None:
            logger.warning("Could not find the current user to protect %s", folder)
            return False
        subprocess.run(
            ["icacls", str(folder), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "/q"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not protect %s: %s", folder, exc)
        return False


# ---- archives --------------------------------------------------------------------------------------------------


def archive_path(name: str) -> PurePosixPath | None:
    """The relative path of a ZIP entry, or ``None`` when it is unsafe (the rules of the skill import: no absolute
    path, drive, ``..``, backslash, control character or empty name)."""
    if not name or "\\" in name or "\x00" in name or any(ord(char) < 32 for char in name):
        return None
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or ":" in part for part in parts):
        return None
    return PurePosixPath(*parts)


def _is_link(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)


def _free_bytes(folder: Path) -> int:
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def extract(
    archive: zipfile.ZipFile,
    into: Path,
    *,
    links: bool = False,
    allowed: Iterable[str] | None = None,
    refused: Callable[[PurePosixPath], bool] = lambda _path: False,
) -> None:
    """Unpack ``archive`` into the empty folder ``into`` after checking every entry: safe paths (also no Windows
    device names), no entry twice, top-level names from ``allowed`` (if given), nothing ``refused`` says no to, enough
    free space. Symbolic links only with ``links`` (a portable package's Python has some on Linux and macOS): made
    after every file, relative, and each must still lead into ``into`` once all of them exist (a chain of links that
    each look harmless while the next is missing is refused, internal review H1). Files are never written through a
    link, because none exists while files are written."""
    infos = archive.infolist()
    seen: set[str] = set()
    tops = set(allowed) if allowed is not None else None
    for info in infos:
        relative = archive_path(info.filename)
        if relative is None or any(_reserved(part) for part in relative.parts):
            raise PackageRefusal("archive_unsafe", f"unsafe path in the archive: {info.filename!r}", name=info.filename)
        # Exact names: a Linux Python has terminfo entries that differ only in case (``2/2621a``, ``2/2621A``). On a
        # file system that ignores case, the second one cannot be created (``xb`` below) and the archive is refused.
        key = relative.as_posix()
        if key in seen:
            raise PackageRefusal("archive_unsafe", f"twice in the archive: {relative}", name=str(relative))
        seen.add(key)
        if tops is not None and relative.parts[0] not in tops:
            raise PackageRefusal("archive_unexpected", f"not part of a backup: {relative}", name=str(relative))
        if refused(relative):
            raise PackageRefusal("archive_unexpected", f"not part of a backup: {relative}", name=str(relative))
        if _is_link(info) and not links:
            raise PackageRefusal("archive_unsafe", f"symbolic link in the archive: {relative}", name=str(relative))
    needed = sum(info.file_size for info in infos)
    if needed + 50_000_000 > _free_bytes(into):
        raise PackageRefusal("disk_full", "not enough free disk space", needed=needed // 1_000_000)
    into.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    try:
        for info in infos:
            relative = archive_path(info.filename)
            assert relative is not None
            target = into.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if _is_link(info):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 4 * 1024 * 1024)
            mode = (info.external_attr >> 16) & 0o777
            if os.name == "posix" and info.create_system == 3 and mode:
                target.chmod(0o755 if mode & 0o111 else 0o644)
        for info in infos:
            if not _is_link(info):
                continue
            relative = archive_path(info.filename)
            assert relative is not None
            target = into.joinpath(*relative.parts)
            link = archive.read(info).decode("utf-8")
            if (
                not link
                or os.path.isabs(link)
                or "\\" in link
                or ":" in link
                or any(parent.is_symlink() for parent in _parents(target, into))
            ):
                raise PackageRefusal(
                    "archive_unsafe", f"symbolic link out of the package: {relative}", name=str(relative)
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
            made.append(target)
    except (OSError, UnicodeDecodeError) as exc:
        raise PackageRefusal("archive_unsafe", f"the archive cannot be unpacked: {exc}", name=str(exc)) from None
    base = os.path.realpath(into)
    for target in made:
        resolved = os.path.realpath(target)
        if os.path.commonpath([base, resolved]) != base:
            name = target.relative_to(into).as_posix()
            raise PackageRefusal("archive_unsafe", f"symbolic link out of the package: {name}", name=name)


def _parents(target: Path, into: Path) -> list[Path]:
    """The folders between ``into`` and ``target`` (excluding both)."""
    found = []
    probe = target.parent
    while probe != into and into in probe.parents:
        found.append(probe)
        probe = probe.parent
    return found


_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
)


def _reserved(part: str) -> bool:
    """A Windows device name (``CON``, ``nul.txt`` ...) or a name Windows changes (trailing dot or space)."""
    return part.split(".")[0].lower() in _RESERVED or part.endswith((".", " "))


def _files(folder: Path, skip: Callable[[PurePosixPath], bool] = lambda _path: False) -> Iterator[Path]:
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if skip(PurePosixPath(path.relative_to(folder).as_posix())):
            continue
        yield path


def _add(archive: zipfile.ZipFile, path: Path, name: str) -> int:
    info = zipfile.ZipInfo.from_file(path, name)
    info.compress_type = zipfile.ZIP_DEFLATED
    with path.open("rb") as reader, archive.open(info, "w", force_zip64=True) as writer:
        shutil.copyfileobj(reader, writer, 4 * 1024 * 1024)
    return info.file_size


# ---- backup and restore ----------------------------------------------------------------------------------------


def snapshot_data(data_dir: Path, archive: zipfile.ZipFile, *, include_secrets: bool, exclude: Path | None) -> int:
    """Write the data folder into ``archive`` under ``data/``; returns the bytes written.

    Plain file copies: the application is stopped while it runs (the launcher refuses otherwise), so the SQLite files
    are consistent. This is the one place that reads the data folder for a backup; an online backup of the database
    (Tabrion MIG-NOD-6) replaces it here."""

    def skip(relative: PurePosixPath) -> bool:
        first = relative.parts[0]
        return first in NOT_BACKED_UP or (first == SECRETS and not include_secrets)

    written = 0
    for path in _files(data_dir, skip):
        if exclude is not None and path.resolve() == exclude.resolve():
            continue
        written += _add(archive, path, f"data/{path.relative_to(data_dir).as_posix()}")
    return written


def backup(
    root: Path,
    data_dir: Path,
    output_dir: Path | None,
    target: Path,
    info: dict[str, Any],
    *,
    include_secrets: bool = False,
    include_results: bool = False,
) -> dict[str, Any]:
    """A ZIP at ``target`` with ``BACKUP.json``, ``config/``, ``data/`` (without ``data/secrets/`` unless
    ``include_secrets``) and, with ``include_results``, the result files under ``results/``. The application must be
    stopped. Returns what ``BACKUP.json`` says."""
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_VERSION,
        "slug": info.get("slug"),
        "name": info.get("name"),
        "release_number": info.get("release_number"),
        "release_digest": info.get("release_digest"),
        "exported_with": info.get("exported_with"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "secrets": include_secrets,
        "results": bool(include_results and output_dir is not None),
    }
    partial = target.with_name(target.name + ".partial")
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr("BACKUP.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
            config = root / "config"
            if config.is_dir():
                for path in _files(config):
                    _add(archive, path, f"config/{path.relative_to(config).as_posix()}")
            if data_dir.is_dir():
                snapshot_data(data_dir, archive, include_secrets=include_secrets, exclude=partial)
            if meta["results"] and output_dir is not None and output_dir.is_dir():
                for path in _files(output_dir):
                    if path.resolve() in (partial, target):
                        continue
                    _add(archive, path, f"results/{path.relative_to(output_dir).as_posix()}")
        if os.name == "posix":
            partial.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return meta


def read_backup(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        meta = json.loads(archive.read("BACKUP.json"))
    except (KeyError, ValueError, zipfile.BadZipFile):
        raise PackageRefusal("backup_unreadable", "the file is not a backup of a portable package") from None
    if not isinstance(meta, dict) or meta.get("format") != BACKUP_FORMAT:
        raise PackageRefusal("backup_unreadable", "the file is not a backup of a portable package")
    if meta.get("format_version") != BACKUP_VERSION:
        raise PackageRefusal(
            "backup_version", "this backup was made by a newer version", version=meta.get("format_version")
        )
    return meta


def check_own_folder(root: Path, folder: Path, what: str) -> None:
    """Refuse a data or result folder that a restore or update must not replace or write into: the package folder
    itself, a folder above it, the home folder, a file system root, or one that holds the program, backups or
    staging folders (a setting like ``data_dir = "."`` or ``"~"``; internal review H2)."""
    resolved = folder.resolve()
    package = root.resolve()
    home = Path.home().resolve()
    if (
        resolved in (package, home, Path(resolved.anchor))
        or resolved in package.parents
        or any((resolved / name).exists() for name in (PROGRAM, BACKUPS, INFO))
    ):
        raise PackageRefusal(
            "folder_unsafe", f"the {what} folder {resolved} cannot be used for this", folder=str(resolved)
        )


def _replace_folder(staged: Path, target: Path, aside: Path) -> None:
    """Put ``staged`` where ``target`` is. The old ``target`` is moved to ``aside`` first (removed with the staging
    folder once everything succeeded, put back when the move of ``staged`` fails); the caller made a backup."""
    moved = False
    if target.exists() or target.is_symlink():
        shutil.move(str(target), str(aside))
        moved = True
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
    except BaseException:
        if moved and not target.exists():
            shutil.move(str(aside), str(target))
        raise


def restore(root: Path, data_dir: Path, output_dir: Path | None, source: Path, info: dict[str, Any]) -> dict[str, Any]:
    """Restore a backup of this application (the same ``slug``): first a backup of the current state into
    ``backups/`` (with secrets and, when the backup has result files, those), then ``config/`` and ``data/`` are
    replaced; secrets the backup does not hold are kept; result files are copied into the result folder (existing
    files of the same name are overwritten, no other file is removed). The application must be stopped."""
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile):
        raise PackageRefusal("backup_unreadable", "the file is not a backup of a portable package") from None
    with archive:
        meta = read_backup(archive)
        if meta.get("slug") != info.get("slug"):
            raise PackageRefusal(
                "backup_other_app",
                "the backup belongs to another application",
                backup=str(meta.get("name") or meta.get("slug")),
                package=str(info.get("name") or info.get("slug")),
            )
        check_own_folder(root, data_dir, "data")
        if output_dir is not None and meta.get("results"):
            check_own_folder(root, output_dir, "result")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        saved = backups_folder(root) / f"before-restore-{stamp}.zip"
        backup(
            root,
            data_dir,
            output_dir,
            saved,
            info,
            include_secrets=True,
            include_results=bool(meta.get("results")),
        )
        staging = root / f".restore-{stamp}"
        try:
            unpacked = staging / "unpacked"
            extract(archive, unpacked, allowed=("BACKUP.json", *BACKUP_PARTS), refused=_not_in_a_backup)
            secrets = data_dir / SECRETS
            if not meta.get("secrets") and secrets.is_dir():
                (unpacked / "data").mkdir(parents=True, exist_ok=True)
                shutil.copytree(secrets, unpacked / "data" / SECRETS, dirs_exist_ok=True)
            if (unpacked / "config").is_dir():
                _replace_folder(unpacked / "config", root / "config", staging / "old-config")
            (unpacked / "data").mkdir(exist_ok=True)
            _replace_folder(unpacked / "data", data_dir, staging / "old-data")
            if (data_dir / SECRETS).is_dir():
                restrict_folder(data_dir / SECRETS)
            if output_dir is not None and (unpacked / "results").is_dir():
                shutil.copytree(unpacked / "results", output_dir, dirs_exist_ok=True)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {**meta, "saved": str(saved)}


def _not_in_a_backup(relative: PurePosixPath) -> bool:
    """What a backup never holds, so a restore never writes it: the instance file, the record of the model check,
    a model copy and the model server's folder below ``data/`` (internal review M2)."""
    return len(relative.parts) > 1 and relative.parts[0] == "data" and relative.parts[1] in NOT_BACKED_UP


def backups_folder(root: Path) -> Path:
    folder = root / BACKUPS
    restrict_folder(folder)
    return folder


# ---- update and rollback ---------------------------------------------------------------------------------------


def _package_folder(unpacked: Path) -> Path:
    """The package folder in an unpacked ZIP: the folder itself or its one top-level folder."""
    if (unpacked / PROGRAM / INFO).is_file():
        return unpacked
    children = [child for child in unpacked.iterdir() if child.is_dir() and not child.name.startswith("__MACOSX")]
    if len(children) == 1 and (children[0] / PROGRAM / INFO).is_file():
        return children[0]
    raise PackageRefusal("update_not_a_package", "no portable package (layout 2) found there")


def _start_files(folder: Path) -> list[Path]:
    """The files next to ``program/`` that belong to the program: start scripts and the README."""
    found = []
    for name in ("start.sh", "stop.sh", "configure.sh", "Start.command", "start.cmd", "stop.cmd", "configure.cmd"):
        if (folder / name).is_file():
            found.append(folder / name)
    if (folder / "README.md").is_file():
        found.append(folder / "README.md")
    return found


def stage_package(root: Path, source: Path, stamp: str) -> tuple[Path, Path]:
    """Unpack or copy the new package next to the current one (same disk, so the swap is a rename); returns the
    staging folder and the new package folder in it."""
    staging = root / f".update-{stamp}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        if source.is_dir():
            package = _package_folder(source)
            target = staging / "package"
            target.mkdir()
            shutil.copytree(package / PROGRAM, target / PROGRAM, symlinks=True)
            for path in _start_files(package):
                shutil.copy2(path, target / path.name)
            if (package / "config" / "settings.toml").is_file():
                (target / "config").mkdir()
                shutil.copy2(package / "config" / "settings.toml", target / "config" / "settings.toml")
            return staging, target
        try:
            archive = zipfile.ZipFile(source)
        except (OSError, zipfile.BadZipFile):
            raise PackageRefusal("update_not_a_package", "no portable package (layout 2) found there") from None
        with archive:
            extract(archive, staging / "unpacked", links=os.name == "posix")
        return staging, _package_folder(staging / "unpacked")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def update(
    root: Path,
    data_dir: Path,
    output_dir: Path | None,
    source: Path,
    *,
    say: Callable[..., None] = lambda key, **values: None,
    swap_now: bool = os.name != "nt",
) -> dict[str, Any]:
    """Replace ``program/`` with the one of a newer package of the same application for the same platform.

    The new program is checked against its ``SHA256SUMS`` before anything changes, the data are backed up into
    ``backups/before-update-<time>.zip`` (without secrets: the update does not touch them), the current program is
    kept as ``program.previous/`` (``rollback`` brings it back), and the start scripts and README are replaced. The
    runtime brings the data to its version on the next start. The application must be stopped.

    The new program is put into ``program.next/`` and swapped in by :func:`apply_pending`: here on Linux and macOS,
    by ``start.cmd`` after this process ended on Windows (``swap_now`` false), where the folder of the Python that
    runs this command cannot be renamed while it runs."""
    program = root / PROGRAM
    current = read_info(program)
    if layout_of(current) < LAYOUT_VERSION:
        raise PackageRefusal("update_old_layout", "this package has the old folder layout: use adopt in the new one")
    check_own_folder(root, data_dir, "data")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    say("update_checking")
    staging, package = stage_package(root, source, stamp)
    try:
        incoming = read_info(package / PROGRAM)
        if layout_of(incoming) < LAYOUT_VERSION:
            raise PackageRefusal("update_not_a_package", "no portable package (layout 2) found there")
        if incoming.get("slug") != current.get("slug"):
            raise PackageRefusal(
                "update_other_app",
                "the new package is another application",
                new=str(incoming.get("name") or incoming.get("slug")),
                current=str(current.get("name") or current.get("slug")),
            )
        if incoming.get("platform") != current.get("platform"):
            raise PackageRefusal(
                "update_other_platform",
                "the new package is for another platform",
                new=str(incoming.get("platform_label") or incoming.get("platform")),
                current=str(current.get("platform_label") or current.get("platform")),
            )
        problems = verify_program(package / PROGRAM, skip=SKIPPED_AT_START)
        if problems:
            raise PackageRefusal("update_damaged", "the new package is damaged", count=len(problems), first=problems[0])
        saved = backups_folder(root) / f"before-update-{stamp}.zip"
        say("update_backup", file=str(saved))
        backup(root, data_dir, output_dir, saved, current, include_secrets=False, include_results=False)
        following = root / NEXT
        if following.exists():
            shutil.rmtree(following)
        os.replace(package / PROGRAM, following)
        for path in _start_files(package):
            # On Windows start.cmd is the batch file running this command: cmd reads it line by line, so the new one
            # is put in place by its last line (see portable_build._START_CMD).
            name = path.name + ".new" if path.name == "start.cmd" and swap_now is False else path.name
            shutil.copy2(path, root / name)
        if swap_now:
            apply_pending(root)
        if not (root / "config" / "settings.toml").is_file() and (package / "config" / "settings.toml").is_file():
            (root / "config").mkdir(exist_ok=True)
            shutil.copy2(package / "config" / "settings.toml", root / "config" / "settings.toml")
        record = {
            "at": stamp,
            "from": current.get("release_number"),
            "to": incoming.get("release_number"),
            "backup": saved.name,
        }
        (backups_folder(root) / "last-update.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def apply_pending(root: Path) -> bool:
    """Put ``program.next/`` in place: the current program becomes ``program.previous/`` (an older one there is
    removed). ``False`` when nothing is pending. ``start.cmd`` does the same with ``move`` on Windows."""
    program, previous, following = root / PROGRAM, root / PREVIOUS, root / NEXT
    if not (following / INFO).is_file():
        return False
    if previous.exists():
        shutil.rmtree(previous)
    os.replace(program, previous)
    try:
        os.replace(following, program)
    except OSError:
        os.replace(previous, program)
        raise
    return True


def rollback(root: Path, *, swap_now: bool = os.name != "nt") -> dict[str, Any]:
    """Swap ``program/`` and ``program.previous/`` (a second ``rollback`` goes forward again), through
    ``program.next/`` like an update. The data stay as they are; when the newer version changed them in a way the
    older one cannot read, ``restore`` the backup the update made (its name is in the answer)."""
    previous, following = root / PREVIOUS, root / NEXT
    info = read_info(previous)
    if layout_of(info) < LAYOUT_VERSION:
        raise PackageRefusal("rollback_nothing", "there is no previous version to go back to")
    if following.exists():
        shutil.rmtree(following)
    os.replace(previous, following)
    if swap_now:
        apply_pending(root)
    record: dict[str, Any] = {}
    with suppress(OSError, ValueError):
        record = dict(json.loads((root / BACKUPS / "last-update.json").read_text(encoding="utf-8")))
    return {"release_number": info.get("release_number"), "backup": record.get("backup")}


# ---- taking over an older package ------------------------------------------------------------------------------


def adopt(
    root: Path,
    old: Path,
    *,
    relative: dict[str, str],
    say: Callable[..., None] = lambda key, **values: None,
) -> list[str]:
    """Copy the settings and data of another package of the same application (any layout) into this one, which must
    not have data yet. ``relative`` maps the old package's folders (``data``, ``results``, ``logs`` as its settings
    name them) to copy: a folder the old settings name with an absolute path stays where it is (the copied settings
    keep pointing there). The old package is not changed. Returns what was copied."""
    old = old.resolve()
    old_program = program_of(old)
    old_info, info = read_info(old_program), read_info(root / PROGRAM)
    if not old_info:
        raise PackageRefusal("adopt_not_a_package", "no portable package found there", folder=str(old))
    if old_info.get("slug") != info.get("slug"):
        raise PackageRefusal(
            "adopt_other_app",
            "the old package is another application",
            old=str(old_info.get("name") or old_info.get("slug")),
            current=str(info.get("name") or info.get("slug")),
        )
    copied: list[str] = []
    targets: dict[str, Path] = {}
    for name, folder in relative.items():
        # Only plain relative folders that stay inside both packages (internal review M1).
        parts = PurePosixPath(folder.replace("\\", "/")).parts
        source, target = old / folder, root / folder
        if (
            not parts
            or ".." in parts
            or PurePosixPath(folder).is_absolute()
            or not _inside(source, old)
            or source.resolve() == old
            or not _inside(target, root)
            or target.resolve() == root.resolve()
        ):
            raise PackageRefusal("folder_unsafe", f"the folder {folder} cannot be taken over", folder=folder)
        targets[name] = target
    for target in targets.values():
        if target.is_dir() and any(target.iterdir()):
            raise PackageRefusal("adopt_has_data", "this package already has data", folder=str(target))
    settings = old / "config" / "settings.toml"
    if settings.is_file():
        mine = root / "config" / "settings.toml"
        mine.parent.mkdir(parents=True, exist_ok=True)
        if mine.is_file():
            shutil.copy2(mine, mine.with_name("settings.toml.package"))
        shutil.copy2(settings, mine)
        copied.append("config/settings.toml")
    for name, folder in relative.items():
        source = old / folder
        if not source.is_dir():
            continue
        say("adopt_copying", folder=folder)
        shutil.copytree(source, targets[name], symlinks=True, dirs_exist_ok=True, ignore=_not_adopted(source))
        copied.append(folder)
        if (targets[name] / SECRETS).is_dir():
            restrict_folder(targets[name] / SECRETS)  # internal review L1
    return copied


def _not_adopted(data: Path) -> Callable[[str, list[str]], set[str]]:
    def ignore(folder: str, names: list[str]) -> set[str]:
        if Path(folder) == data:
            return {name for name in names if name in ("run",)}
        return set()

    return ignore


# ---- the web interface -----------------------------------------------------------------------------------------


def installed_ui(root: Path, served: Iterable[int]) -> Path | None:
    """The interface ``ui install`` put into ``ui/``, checked; ``None`` when there is none."""
    folder = root / UI
    if not folder.exists():
        return None
    try:
        ui_package.check_tree(folder, served, extra=(INSTALLED,))
    except ui_package.UiPackageError as exc:
        raise PackageRefusal("ui_damaged", f"the installed interface is damaged: {exc}", problem=str(exc)) from None
    return folder


def install_ui(root: Path, artifact: Path, expected_sha256: str, served: Iterable[int]) -> dict[str, Any]:
    """Install the interface artifact (``nodestro-app-ui-<version>.tgz``) into ``ui/``: its SHA-256 must be the one
    given (from the ``.sha256`` file published with it), every file must match its manifest, and it must speak a
    contract version this backend serves. ``ui reset`` goes back to the interface of the export."""
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise PackageRefusal("ui_hash_needed", "--sha256 takes the 64 hex digits of the artifact's SHA-256")
    try:
        if artifact.stat().st_size > ui_package.LIMIT:
            raise PackageRefusal("ui_invalid", "the artifact is larger than 20 MB", problem="size")
        data = artifact.read_bytes()
    except OSError as exc:
        raise PackageRefusal("ui_invalid", f"cannot read {artifact}: {exc}", problem=str(exc)) from None
    if ui_package.sha256(data) != expected:
        raise PackageRefusal("ui_hash_mismatch", "the artifact's SHA-256 is not the one given")
    staging = root / f".ui-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        ui_package.unpack_artifact(data, staging)
        manifest = ui_package.check_tree(staging, served)
        if not (staging / "index.html").is_file():
            raise ui_package.UiPackageError("the interface has no index.html")
        (staging / INSTALLED).write_text(
            json.dumps({"artifact": artifact.name, "sha256": expected}, indent=2) + "\n", encoding="utf-8"
        )
        aside = root / f".ui-old-{time.strftime('%Y%m%d-%H%M%S')}"
        _replace_folder(staging, root / UI, aside)
        shutil.rmtree(aside, ignore_errors=True)
    except ui_package.UiPackageError as exc:
        raise PackageRefusal("ui_invalid", f"the artifact is not a usable interface: {exc}", problem=str(exc)) from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"version": manifest["version"], "contract": manifest["contract"]}


def reset_ui(root: Path) -> bool:
    folder = root / UI
    if not folder.exists():
        return False
    shutil.rmtree(folder)
    return True
