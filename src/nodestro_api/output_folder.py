"""The result folder of a portable application, chosen by the operator on the web interface's settings page.

An exported application copies every result file to ``<folder>/<date>_<time>_<run>/`` (see
:func:`nodestro_api.standalone.copy_result_files`). Where that folder is comes from, in this order:

1. ``OUTPUT_DIR`` set in the environment outside the application (servers, scripts): it always wins and the settings
   page shows it without a way to change it (like provider keys set in the environment);
2. the operator's choice on the settings page, kept in ``<DATA_DIR>/settings/output-folder.json``;
3. ``[data] output_dir`` of ``config/settings.toml`` (the default; ``results`` next to the package after unpacking).

The choice is kept in the data folder rather than written into ``config/settings.toml``: that file is the operator's
hand-edited, commented configuration (rewriting it would lose comments and formatting, and it may sit on a read-only
medium), it states the *default* the "Restore default" button returns to, and the data folder already holds the other
state this computer adds to the package (provider keys, API keys). The choice applies to the next finished run without
a restart; files already written stay where they are.

A browser cannot hand a folder of this computer to the server (``showDirectoryPicker`` yields a handle, not a path,
and the server writes the files), so the page offers a folder browser served by this module: the names of folders,
never files or contents, for the operator's session only. Every chosen folder must be absolute; it is normalized and
its symbolic links resolved before any check, and refused when it is a system folder, the file system's root, or a
folder of the package's own code, Python, models, data or logs. A missing folder is created only when the operator
confirmed it, and the folder must be writable.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path, PurePath
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from typing import TYPE_CHECKING, Any

from .serving import ApiError

if TYPE_CHECKING:
    from .standalone import StandaloneHost

logger = logging.getLogger("nodestro.output_folder")

FILE = Path("settings") / "output-folder.json"
#: Longest path accepted (Windows' long paths are possible, but nobody types more).
MAX_PATH = 1024
#: At most this many folders are listed in one answer of the browser.
MAX_ENTRIES = 500
#: Folders of an exported package that hold its code, its Python, models or state (never a result folder).
PACKAGE_FOLDERS = ("app", "workflow", "nodestro_runtime", "nodestro_api", "python", "ollama", "models", "config", "run")

_POSIX_SYSTEM = (
    "/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32", "/opt", "/proc", "/run", "/sbin",
    "/snap", "/sys", "/usr", "/var", "/System", "/Library", "/Applications", "/private", "/cores",
)  # fmt: skip
#: Places below a system folder where people keep their own files (Fedora Atomic's /var/home, removable media, macOS'
#: temporary folders).
_POSIX_ALLOWED = ("/var/home", "/var/mnt", "/var/tmp", "/run/media", "/private/tmp", "/private/var/folders")


def _inside(path: PurePath, folder: PurePath) -> bool:
    """``path`` is ``folder`` or below it (case-insensitive on Windows)."""
    if os.name == "nt":
        path, folder = PurePath(str(path).lower()), PurePath(str(folder).lower())
    return path == folder or folder in path.parents


def _resolved(path: Path) -> Path:
    with suppress(OSError, RuntimeError):
        return path.resolve()
    return Path(os.path.normpath(path))


def _home() -> Path | None:
    """The user's home folder (``None`` when the system cannot tell, e.g. a service account without one)."""
    try:
        return _resolved(Path.home())
    except (RuntimeError, KeyError, OSError):
        return None


def system_folders() -> list[Path]:
    """Folders of the operating system and of installed programs."""
    if os.name == "nt":
        names = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
        return [_resolved(Path(value)) for value in (os.environ.get(name) for name in names) if value]
    return [Path(name) for name in _POSIX_SYSTEM]


class OutputFolder:
    """Where result files go now, the operator's choice and the folder browser, for one :class:`StandaloneHost`.

    ``fixed``: ``OUTPUT_DIR`` was set outside the application (no choice). ``protected``: further folders that must
    never receive result files (the launcher passes the package's data and log folders). ``opener``: the command that
    shows a folder in the file manager of this computer, ``None`` when that is not possible here."""

    def __init__(
        self,
        host: StandaloneHost,
        *,
        data_dir: Path,
        project_dir: Path,
        fixed: bool = False,
        protected: Iterable[Path] = (),
        opener: Callable[[Path], None] | None = None,
    ) -> None:
        self.host = host
        self.data_dir = data_dir
        self.project_dir = _resolved(project_dir)
        self.fixed = fixed
        self.opener = opener
        self._lock = threading.Lock()
        # The host resolved the configured folder (``[data] output_dir``) when it started: that is the default.
        self.default: Path | None = host.output_dir
        self.default_warning: str | None = host.output_warning
        protected_folders = [_resolved(Path(folder)) for folder in (data_dir, *protected)]
        for module in ("nodestro_api", "nodestro_runtime"):
            loaded = sys.modules.get(module)
            if loaded is not None and loaded.__file__:
                protected_folders.append(_resolved(Path(loaded.__file__).parent))
        protected_folders += [_resolved(Path(prefix)) for prefix in {sys.prefix, sys.base_prefix, sys.exec_prefix}]
        self.protected = protected_folders
        self.chosen: Path | None = None
        #: Why the stored choice is not used (it was removed, or became unusable).
        self.choice_warning: str | None = None
        if not fixed:
            self._load()
        self._apply()

    # ---- the stored choice ------------------------------------------------------------------------------------

    @property
    def file(self) -> Path:
        return self.data_dir / FILE

    def _load(self) -> None:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("%s cannot be read (%s); the default result folder is used", self.file, exc)
            return
        stored = data.get("path") if isinstance(data, dict) else None
        if not isinstance(stored, str) or not stored:
            return
        try:
            self.chosen = self.check(stored, create=False)
        except ApiError as exc:
            self.choice_warning = f"{stored}: {exc.message}"
            logger.warning("The chosen result folder %s cannot be used (%s); the default is used", stored, exc.message)

    def _save(self, folder: Path | None) -> None:
        if folder is None:
            self.file.unlink(missing_ok=True)
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.file.parent, prefix=".output-folder-", suffix=".json")
        try:
            if os.name == "posix":
                os.fchmod(handle, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(handle, "w", encoding="utf-8") as writer:
                json.dump({"path": str(folder), "chosen_at": datetime.now(UTC).isoformat()}, writer, indent=2)
            os.replace(temporary, self.file)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _apply(self) -> None:
        """Point the host at the folder in effect (the next finished run writes there)."""
        if self.chosen is not None:
            self.host.output_dir, self.host.output_warning = self.chosen, None
            self.host.output_check = self.problem
        else:
            self.host.output_dir, self.host.output_warning = self.default, self.default_warning
            self.host.output_check = None

    # ---- checks -----------------------------------------------------------------------------------------------

    def problem(self, folder: Path) -> ApiError | None:
        """Why ``folder`` (absolute) must not receive result files, after resolving its links; ``None``: it may."""
        target = _resolved(folder)
        if target.parent == target:
            return ApiError(
                422, "output.protected", "The root of a drive cannot be the result folder", path=str(target)
            )
        home = _home()
        in_home = home is not None and home.parent != home and _inside(target, home)
        if not in_home:
            for system in system_folders():
                if _inside(target, system) and not any(_inside(target, Path(ok)) for ok in _POSIX_ALLOWED):
                    return ApiError(422, "output.protected", "This is a folder of the system", path=str(target))
        if target == self.project_dir or any(_inside(target, self.project_dir / name) for name in PACKAGE_FOLDERS):
            return ApiError(422, "output.protected", "This folder belongs to the application", path=str(target))
        for folder_ in self.protected:
            if _inside(target, folder_):
                return ApiError(422, "output.protected", "This folder belongs to the application", path=str(target))
        return None

    def normalize(self, given: str) -> Path:
        """An absolute, normalized path with its links resolved (no check of what it points to yet)."""
        text = given.strip()
        if not text or len(text) > MAX_PATH or any(ord(char) < 32 for char in text):
            raise ApiError(422, "output.invalid", "Enter the full path of a folder")
        path = Path(text)
        if not path.is_absolute() or (os.name == "nt" and not path.drive):
            raise ApiError(422, "output.not_absolute", "Enter the full path of a folder (absolute)", path=text)
        return _resolved(Path(os.path.normpath(path)))

    def check(self, given: str, *, create: bool) -> Path:
        """The usable folder for ``given``, created when ``create`` (the operator's consent) and missing."""
        target = self.normalize(given)
        refused = self.problem(target)
        if refused is not None:
            raise refused
        if target.exists() and not target.is_dir():
            raise ApiError(422, "output.not_a_folder", "This is a file, not a folder", path=str(target))
        if not target.exists():
            if not create:
                raise ApiError(409, "output.missing", "The folder does not exist yet", path=str(target))
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ApiError(
                    422, "output.not_writable", "The folder cannot be created", path=str(target), problem=_why(exc)
                ) from None
            # Created through a link that appeared meanwhile: check where it really is.
            again = self.problem(target)
            if again is not None or _resolved(target) != target:
                raise again or ApiError(422, "output.protected", "The folder moved", path=str(target))
        try:
            probe = target / f".nodestro-write-test-{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise ApiError(
                422, "output.not_writable", "The folder cannot be written", path=str(target), problem=_why(exc)
            ) from None
        return target

    # ---- actions of the settings page -------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """What the settings page shows (for the operator only)."""
        current = self.host.output_dir
        source = "environment" if self.fixed else "chosen" if self.chosen is not None else "default"
        return {
            "enabled": current is not None,
            "path": str(current) if current is not None else None,
            "warning": self.host.output_warning or self.choice_warning,
            "source": source,
            "default_path": str(self.default) if self.default is not None else None,
            "can_change": not self.fixed,
            "can_open": self.opener is not None and current is not None,
        }

    def choose(self, given: str, *, create: bool) -> dict[str, Any]:
        if self.fixed:
            raise ApiError(409, "output.fixed", "OUTPUT_DIR is set in the environment; change it there")
        with self._lock:
            folder = self.check(given, create=create)
            self._save(folder)
            self.chosen, self.choice_warning = folder, None
            self._apply()
        logger.info("Result files of the next runs go to %s (chosen on the settings page)", folder)
        return self.state()

    def reset(self) -> dict[str, Any]:
        if self.fixed:
            raise ApiError(409, "output.fixed", "OUTPUT_DIR is set in the environment; change it there")
        with self._lock:
            self._save(None)
            self.chosen, self.choice_warning = None, None
            self._apply()
        logger.info("Result files of the next runs go to the default folder %s", self.default)
        return self.state()

    def open(self) -> None:
        """Show the folder in effect in this computer's file manager."""
        folder = self.host.output_dir
        if self.opener is None or folder is None:
            raise ApiError(404, "output.open_unavailable", "The folder cannot be opened from here")
        folder.mkdir(parents=True, exist_ok=True)
        self.opener(folder)

    # ---- the folder browser -----------------------------------------------------------------------------------

    def places(self) -> list[dict[str, str]]:
        """Starting points of the browser: home, the folder in effect, the default and the drives or root."""
        found: list[dict[str, str]] = []

        def add(kind: str, path: Path | None) -> None:
            if path is not None and path.is_dir() and all(item["path"] != str(path) for item in found):
                found.append({"kind": kind, "path": str(path)})

        add("home", _home())
        add("current", self.host.output_dir)
        add("default", self.default)
        if os.name == "nt":
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                with suppress(OSError):
                    add("drive", Path(f"{letter}:\\"))
        else:
            add("root", Path("/"))
            for media in ("/Volumes", "/media", "/mnt", f"/run/media/{os.environ.get('USER', '')}"):
                with suppress(OSError):
                    add("media", Path(media))
        return found

    def browse(self, given: str | None) -> dict[str, Any]:
        """The sub-folders (names only) of a folder; files and hidden folders are never listed."""
        start = given if given else str(self.host.output_dir or self.default or _home() or Path(os.sep).resolve())
        folder = self.normalize(start)
        if not folder.is_dir():
            raise ApiError(404, "output.not_found", "This folder does not exist", path=str(folder))
        folders: list[dict[str, Any]] = []
        truncated = False
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or entry.name.startswith("$"):
                        continue
                    with suppress(OSError):
                        if not entry.is_dir():
                            continue
                        if _hidden(entry):
                            continue
                        if len(folders) >= MAX_ENTRIES:
                            truncated = True
                            break
                        path = _resolved(Path(entry.path))
                        folders.append({"name": entry.name, "path": str(path), "usable": self.problem(path) is None})
        except PermissionError:
            raise ApiError(403, "output.no_access", "This folder cannot be read", path=str(folder)) from None
        except OSError as exc:
            raise ApiError(
                422, "output.no_access", "This folder cannot be read", path=str(folder), problem=_why(exc)
            ) from None
        folders.sort(key=lambda item: item["name"].casefold())
        parent = folder.parent if folder.parent != folder else None
        return {
            "path": str(folder),
            "parent": str(parent) if parent is not None else None,
            "usable": self.problem(folder) is None,
            "writable": os.access(folder, os.W_OK),
            "folders": folders,
            "truncated": truncated,
            "places": self.places(),
            "separator": os.sep,
        }

    def make_folder(self, parent: str, name: str) -> dict[str, Any]:
        """Create ``name`` in ``parent`` (a plain folder name) and answer the browser's view of the new folder."""
        clean = name.strip()
        if (
            not clean
            or len(clean) > 120
            or clean in (".", "..")
            or clean.startswith(".")
            or any(char in clean for char in '/\\<>:"|?*')
            or any(ord(char) < 32 for char in clean)
        ):
            raise ApiError(422, "output.bad_name", "This name cannot be used for a folder")
        base = self.normalize(parent)
        target = base / clean
        refused = self.problem(target)
        if refused is not None:
            raise refused
        try:
            target.mkdir()
        except FileExistsError:
            raise ApiError(409, "output.exists", "A folder of this name exists already", path=str(target)) from None
        except OSError as exc:
            raise ApiError(
                422, "output.not_writable", "The folder cannot be created", path=str(target), problem=_why(exc)
            ) from None
        return self.browse(str(target))


def _why(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def _hidden(entry: os.DirEntry[str]) -> bool:
    """Windows' hidden and system folders."""
    if os.name != "nt":
        return False
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


def file_manager(environ: dict[str, str] | None = None) -> Callable[[Path], None] | None:
    """How to show a folder in the file manager of this computer, or ``None`` (no desktop, e.g. a server)."""
    env = dict(os.environ if environ is None else environ)
    if sys.platform == "win32":
        start_file = getattr(os, "startfile", None)
        return (lambda folder: start_file(str(folder))) if start_file is not None else None
    if sys.platform == "darwin":
        command = shutil.which("open")
    else:
        if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
            return None
        command = shutil.which("xdg-open")
    if command is None:
        return None

    def show(folder: Path) -> None:
        # A list of arguments, no shell; the path is absolute (never read as an option).
        subprocess.Popen(
            [command, str(folder)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    return show


__all__ = ["FILE", "OutputFolder", "file_manager", "system_folders"]
