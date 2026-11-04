"""Building a portable application from a published release: its own Python, every library, the models.

The result is a ZIP with one folder that runs on the chosen platform without installing anything:

``python/``
    CPython from python-build-standalone (the relocatable builds the desktop app ships too), installed by uv for the
    target platform; uv knows each build's SHA-256 and checks it. The libraries are in its ``site-packages``: wheels
    for the target platform only (``--only-binary :all:``), resolved by uv with the exporting installation's versions
    as constraints and installed from a lock with SHA-256 hashes (``lib.lock``). orkexa (not on PyPI) is copied from
    this installation, like the standard export's ``bundle`` mode.
the project
    The same files as the standard export (:func:`nodestro_api.exporter.project_files`): the release, the runtime
    modules copied byte for byte, the application shell, documentation, examples; plus the launcher
    (:mod:`nodestro_api.portable`), the web interface (optional) and ``config/settings.toml``.
``ollama/`` and ``models/`` (optional)
    The pinned Ollama runtime for the target (:mod:`orkexa.local.install`, SHA-256 checked, without the NVIDIA
    libraries) and the release's local models as an orkexa offline package (every file named and checked by its
    SHA-256). The launcher starts this server on a free loopback port.
``start.cmd`` / ``start.sh`` / ``Start.command`` ...
    Small scripts that run the launcher with the bundled Python (``python -I -m nodestro_api.portable``).
``PORTABLE.json`` and ``SHA256SUMS``
    What the package contains, for which platform, and a checksum of every file.

Building happens in a background job (:class:`ExportJob`): files are written to a work folder and streamed into the
ZIP, never held in memory; every step can be cancelled; downloads (Python, wheels, the Ollama archive) are kept in
``<data>/export-cache`` and reused. The person sees beforehand what will be downloaded (:func:`plan`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
import hashlib
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, ClassVar, Literal
import urllib.request
import zipfile

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import new_id, now
from packaging.requirements import Requirement
from pydantic import BaseModel, Field

from . import __version__

if TYPE_CHECKING:
    from .context import Nodestro
    from .exporter import ExportOptions, ExportRecord
    from .publishing import Publication, Release

logger = logging.getLogger(__name__)

JOBS = "export_jobs"


@dataclass(frozen=True)
class Platform:
    id: str
    label: str
    #: python-build-standalone's platform part of uv's download key, e.g. ``linux-x86_64-gnu``.
    python_key: str
    #: uv's ``--python-platform``.
    wheel_platform: str
    #: orkexa's Ollama artifact: (system, machine).
    ollama: tuple[str, str]
    windows: bool = False

    def site_packages(self, minor: str) -> str:
        return "Lib/site-packages" if self.windows else f"lib/python{minor}/site-packages"

    @property
    def python(self) -> str:
        return "python/python.exe" if self.windows else "python/bin/python3"


#: The platforms a portable package can be built for (whether each was tested is documented in docs/portable.md).
PLATFORMS: dict[str, Platform] = {
    item.id: item
    for item in (
        Platform("linux-x86_64", "Linux x86-64", "linux-x86_64-gnu", "x86_64-manylinux_2_28", ("linux", "x86_64")),
        Platform("linux-arm64", "Linux ARM64", "linux-aarch64-gnu", "aarch64-manylinux_2_28", ("linux", "arm64")),
        Platform(
            "macos-arm64", "macOS Apple silicon", "macos-aarch64-none", "aarch64-apple-darwin", ("darwin", "arm64")
        ),
        Platform("macos-x86_64", "macOS Intel", "macos-x86_64-none", "x86_64-apple-darwin", ("darwin", "x86_64")),
        Platform(
            "windows-x86_64",
            "Windows x64",
            "windows-x86_64-none",
            "x86_64-pc-windows-msvc",
            ("windows", "x86_64"),
            True,
        ),
        Platform(
            "windows-arm64",
            "Windows ARM64",
            "windows-aarch64-none",
            "aarch64-pc-windows-msvc",
            ("windows", "arm64"),
            True,
        ),
    )
}

#: Ollama's license file of the pinned release (MIT), checked like the archive (same pin as nodestro-desktop).
OLLAMA_LICENSE_URL = "https://raw.githubusercontent.com/ollama/ollama/v{version}/LICENSE"
OLLAMA_LICENSE_SHA256 = "5934ed2ce0d15154bcdb9c85203210abac0da4314af34081e36df4599f90b226"
#: Rough sizes for the plan before anything is downloaded (bytes, unpacked).
PYTHON_SIZE = 75_000_000
LIBRARIES_SIZE = 160_000_000
OLLAMA_SIZE = {"linux": 115_000_000, "darwin": 75_000_000, "windows": 130_000_000}
#: Files the standard export has that a portable package does not use (it has its own Python and launcher).
_NOT_PORTABLE = ("Dockerfile", ".env.example", "pyproject.toml", "requirements.lock", "requirements-tools.lock")
#: Environment variables uv may see (proxies, certificates, a package mirror, a Python download mirror).
_UV_ENV = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_PYTHON_INSTALL_MIRROR",
    "UV_NATIVE_TLS",
)


class Cancelled(Exception):
    pass


# ---- jobs -------------------------------------------------------------------------------------------------------


JobState = Literal["running", "done", "failed", "cancelled"]


class ExportJob(BaseModel):
    """A portable export being built (kept in the database; a restart marks a running one as interrupted)."""

    id: str = Field(default_factory=lambda: new_id("xjob"))
    publication_id: str
    release_number: int
    options: dict[str, Any] = Field(default_factory=dict)
    state: JobState = "running"
    #: The current step: ``check``, ``python``, ``libraries``, ``project``, ``runtime``, ``models``, ``archive``.
    step: str = "check"
    steps: list[str] = Field(default_factory=list)
    #: Progress of the whole job, 0..1 (by the steps' expected sizes).
    progress: float = 0.0
    message: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    error_code: str | None = None
    error: str | None = None
    export_id: str | None = None
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    log: list[str] = Field(default_factory=list)


class JobRunner:
    """The export jobs of one installation: at most one at a time, cancellable."""

    def __init__(self, ctx: Nodestro) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._cancel: dict[str, threading.Event] = {}
        self._thread: threading.Thread | None = None

    def recover(self) -> None:
        """Jobs left ``running`` by a stop are failed (their work folders are removed)."""
        for job in self.ctx.store.list(JOBS, ExportJob):
            if job.state == "running":
                job.state, job.error_code = "failed", "export.interrupted"
                job.error = "Nodestro was stopped while the export was built; start it again"
                job.finished_at = now()
                self.ctx.store.put(JOBS, job.id, job)
        shutil.rmtree(_work_root(self.ctx), ignore_errors=True)

    def get(self, job_id: str) -> ExportJob:
        job = self.ctx.store.get(JOBS, job_id, ExportJob)
        if job is None:
            raise KeyError(job_id)
        return job

    def for_publication(self, publication_id: str) -> list[ExportJob]:
        found = [job for job in self.ctx.store.list(JOBS, ExportJob) if job.publication_id == publication_id]
        return sorted(found, key=lambda job: job.created_at, reverse=True)[:20]

    def start(self, publication: Publication, release: Release, options: ExportOptions) -> ExportJob:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise NodestroError("export.busy", "Another export is being built; wait for it or cancel it")
            job = ExportJob(
                publication_id=publication.id,
                release_number=release.number,
                options=options.model_dump(mode="json"),
                steps=steps_of(options),
            )
            self.ctx.store.put(JOBS, job.id, job)
            cancel = threading.Event()
            self._cancel[job.id] = cancel
            self._thread = threading.Thread(
                target=self._run, args=(job, publication, release, options, cancel), name="nodestro-export", daemon=True
            )
            self._thread.start()
        return job

    def cancel(self, job_id: str) -> ExportJob:
        job = self.get(job_id)
        event = self._cancel.get(job_id)
        if job.state == "running" and event is not None:
            event.set()
        return job

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(
        self,
        job: ExportJob,
        publication: Publication,
        release: Release,
        options: ExportOptions,
        cancel: threading.Event,
    ) -> None:
        progress = Progress(self.ctx, job, cancel)
        try:
            record = build(self.ctx, publication, release, options, progress)
            job.export_id, job.state, job.progress, job.step = record.id, "done", 1.0, "done"
            job.message = record.file_name
        except Cancelled:
            job.state, job.error_code, job.error = "cancelled", "export.cancelled", "The export was cancelled"
        except NodestroError as exc:
            job.state, job.error_code, job.error = "failed", exc.code, str(exc)
        except Exception as exc:  # reported on the job, with the details in the log
            logger.exception("Building a portable export failed")
            job.state, job.error_code, job.error = "failed", "export.failed", f"{type(exc).__name__}: {exc}"[:2000]
        finally:
            job.finished_at = now()
            self.ctx.store.put(JOBS, job.id, job)
            self._cancel.pop(job.id, None)


def runner(ctx: Nodestro) -> JobRunner:
    """The installation's job runner (created on first use)."""
    found = getattr(ctx, "_portable_jobs", None)
    if found is None:
        found = JobRunner(ctx)
        found.recover()
        ctx._portable_jobs = found  # type: ignore[attr-defined]
    return found


class Progress:
    """Reports a job's progress (throttled writes) and raises :class:`Cancelled` when it was cancelled."""

    WEIGHTS: ClassVar[dict[str, int]] = {
        "check": 1,
        "python": 4,
        "libraries": 8,
        "project": 2,
        "runtime": 6,
        "models": 30,
        "archive": 30,
    }

    def __init__(self, ctx: Nodestro | None, job: ExportJob, cancel: threading.Event) -> None:
        self.ctx = ctx
        self.job = job
        self.cancel = cancel
        self._saved = 0.0

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def step(self, name: str, message: str = "") -> None:
        self.check()
        self.job.step, self.job.message, self.job.bytes_done, self.job.bytes_total = name, message, 0, 0
        self.job.log.append(f"{datetime.now().strftime('%H:%M:%S')} {name}: {message}".strip())
        self._update(force=True)

    def bytes(self, done: int, total: int) -> None:
        self.check()
        self.job.bytes_done, self.job.bytes_total = done, total
        self._update()

    def _update(self, *, force: bool = False) -> None:
        steps = self.job.steps or list(self.WEIGHTS)
        weights = [self.WEIGHTS.get(name, 1) for name in steps]
        if self.job.step in steps:
            index = steps.index(self.job.step)
            fraction = self.job.bytes_done / self.job.bytes_total if self.job.bytes_total else 0.0
            self.job.progress = round((sum(weights[:index]) + weights[index] * min(fraction, 1.0)) / sum(weights), 4)
        moment = time.monotonic()
        if self.ctx is not None and (force or moment - self._saved > 0.5):
            self._saved = moment
            self.ctx.store.put(JOBS, self.job.id, self.job)


def steps_of(options: ExportOptions) -> list[str]:
    steps = ["check", "python", "libraries", "project"]
    if options.local_models:
        steps += ["runtime", "models"]
    return [*steps, "archive"]


# ---- planning ---------------------------------------------------------------------------------------------------


def cache_root(ctx: Nodestro) -> Path:
    return ctx.config.data_dir / "export-cache"


def _work_root(ctx: Nodestro) -> Path:
    return ctx.config.data_dir / "export-work"


def python_minor() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def cached_python(ctx: Nodestro, platform: Platform) -> Path | None:
    folder = cache_root(ctx) / "python"
    if not folder.is_dir():
        return None
    for candidate in sorted(folder.glob(f"cpython-{python_minor()}.*-{platform.python_key}"), reverse=True):
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate
    return None


def cached_ollama(ctx: Nodestro, platform: Platform) -> Path | None:
    from orkexa.local.install import installed_runtime

    binary = installed_runtime(cache_root(ctx) / "ollama" / platform.id, system=platform.ollama[0])
    if binary is None:
        return None
    return binary.parent.parent if binary.parent.name == "bin" else binary.parent


class Download(BaseModel):
    what: Literal["python", "libraries", "ollama"]
    size: int
    cached: bool


def downloads(ctx: Nodestro, platform: Platform, *, local_models: bool) -> list[Download]:
    """What building would download now (nothing that is in the cache already)."""
    found = [
        Download(what="python", size=PYTHON_SIZE // 3, cached=cached_python(ctx, platform) is not None),
        # uv caches wheels itself; a repeated build for the same platform downloads nothing new.
        Download(
            what="libraries",
            size=LIBRARIES_SIZE // 3,
            cached=(cache_root(ctx) / "locks" / f"{platform.id}.lock").is_file(),
        ),
    ]
    if local_models:
        found.append(
            Download(
                what="ollama",
                size=OLLAMA_SIZE.get(platform.ollama[0], 120_000_000) * 3,
                cached=cached_ollama(ctx, platform) is not None,
            )
        )
    return found


# ---- building ---------------------------------------------------------------------------------------------------


def _uv() -> str:
    from nodestro_runtime.python_envs import find_uv

    found = find_uv()
    if found is None:
        raise NodestroError("export.uv_missing", "uv is not available in this installation")
    return found


def _uv_env(ctx: Nodestro, *, offline: bool) -> dict[str, str]:
    env = {key: os.environ[key] for key in _UV_ENV if key in os.environ}
    env["UV_CACHE_DIR"] = str(cache_root(ctx) / "uv")
    env["UV_NO_CONFIG"] = "1"
    env["UV_PYTHON_DOWNLOADS"] = "manual"
    if offline:
        env["UV_OFFLINE"] = "1"
    return env


def _run(command: list[str], env: dict[str, str], progress: Progress, *, cwd: Path | None = None) -> str:
    """Run uv; cancellable; its output is returned (and shown when it fails)."""
    process = subprocess.Popen(command, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output: list[str] = []

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    while process.poll() is None:
        if progress.cancel.is_set():
            process.kill()
            process.wait()
            raise Cancelled()
        time.sleep(0.2)
    reader.join(5)
    text = "".join(output)
    if process.returncode != 0:
        raise NodestroError("export.uv_failed", text.strip()[-3000:] or "uv failed", command=" ".join(command[1:3]))
    return text


def install_python(ctx: Nodestro, platform: Platform, progress: Progress, *, allow_downloads: bool) -> Path:
    found = cached_python(ctx, platform)
    if found is not None:
        return found
    if not allow_downloads:
        raise NodestroError(
            "export.download_needed", "Python for this platform is not in the cache; allow downloads", what="python"
        )
    folder = cache_root(ctx) / "python"
    folder.mkdir(parents=True, exist_ok=True)
    request = f"cpython-{python_minor()}-{platform.python_key}"
    _run(
        [_uv(), "python", "install", request, "--install-dir", str(folder), "--no-bin"],
        _uv_env(ctx, offline=False),
        progress,
    )
    found = cached_python(ctx, platform)
    if found is None:
        raise NodestroError("export.python_missing", f"uv did not install {request}")
    return found


def requirements(options: ExportOptions, bundle: Any) -> tuple[list[str], list[str]]:
    """The packages of a portable package and the exporting installation's versions as constraints."""
    from .exporter import _APP_PACKAGES, _closure, _version, tool_libraries

    direct = [f"{Requirement(item).name}{_extras(item)}=={_version(Requirement(item).name)}" for item in _APP_PACKAGES]
    extras = {"all", "pdf", "local"}
    for item in metadata.requires("orkexa") or []:
        requirement = Requirement(item)
        marker = requirement.marker
        if marker is None:
            direct.append(str(requirement))
            continue
        if any(marker.evaluate({"extra": extra}) for extra in extras) and not marker.evaluate({"extra": ""}):
            requirement.marker = None
            direct.append(str(requirement))
    # Semantic knowledge search on SQLite (no wheel for Windows on ARM: there the search stays full-text).
    direct.append("sqlite-vec>=0.1.6; sys_platform != 'win32' or platform_machine != 'ARM64'")
    direct.append("tzdata>=2024.1; sys_platform == 'win32'")
    direct += tool_libraries(bundle)
    closure = _closure([*_APP_PACKAGES, "orkexa[all,pdf,local]", "sqlite-vec"])
    closure.pop("orkexa", None)
    constraints = [f"{name}=={version}" for name, version in sorted(closure.items())]
    return direct, constraints


def _extras(name: str) -> str:
    extras = sorted(Requirement(name).extras)
    return f"[{','.join(extras)}]" if extras else ""


def install_libraries(
    ctx: Nodestro,
    platform: Platform,
    target: Path,
    work: Path,
    options: ExportOptions,
    bundle: Any,
    progress: Progress,
    *,
    allow_downloads: bool,
) -> str:
    """Resolve and install the libraries for ``platform`` into ``target`` (its ``site-packages``); returns the lock."""
    direct, constraints = requirements(options, bundle)
    (work / "requirements.in").write_text("\n".join(direct) + "\n", encoding="utf-8")
    (work / "constraints.txt").write_text("\n".join(constraints) + "\n", encoding="utf-8")
    lock = work / "lib.lock"
    common = [
        "--python-platform",
        platform.wheel_platform,
        "--python-version",
        python_minor(),
        "--only-binary",
        ":all:",
    ]
    env = _uv_env(ctx, offline=not allow_downloads)
    try:
        _run(
            [
                _uv(),
                "pip",
                "compile",
                "requirements.in",
                "-c",
                "constraints.txt",
                *common,
                "--generate-hashes",
                "--no-header",
                "--no-annotate",
                "-o",
                "lib.lock",
                "--quiet",
            ],
            env,
            progress,
            cwd=work,
        )
    except NodestroError as exc:
        if not allow_downloads:
            raise NodestroError(
                "export.download_needed", "The libraries for this platform are not in the cache; allow downloads"
            ) from None
        raise NodestroError(
            "export.libraries_unavailable",
            "Not every library has a ready-built package for this platform: " + str(exc)[-1500:],
            platform=platform.id,
        ) from None
    progress.check()
    _run(
        [
            _uv(),
            "pip",
            "install",
            "--target",
            str(target),
            *common,
            "--require-hashes",
            "--no-deps",
            "--no-compile",
            "-r",
            str(lock),
            "--quiet",
        ],
        env,
        progress,
        cwd=work,
    )
    shutil.rmtree(target / "bin", ignore_errors=True)  # entry scripts name the build machine's paths
    text = lock.read_text(encoding="utf-8")
    (cache_root(ctx) / "locks").mkdir(parents=True, exist_ok=True)
    (cache_root(ctx) / "locks" / f"{platform.id}.lock").write_text(text, encoding="utf-8")
    return text


def install_orkexa(target: Path) -> list[str]:
    """orkexa's source (not on PyPI) into ``target``, from this installation."""
    import orkexa

    source = Path(orkexa.__file__).resolve().parent
    copied: list[str] = []
    for file in sorted(source.rglob("*")):
        if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc":
            continue
        relative = file.relative_to(source)
        destination = target / "orkexa" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
        copied.append(relative.as_posix())
    return copied


def install_ollama(
    ctx: Nodestro, platform: Platform, target: Path, progress: Progress, *, allow_downloads: bool
) -> str:
    """The pinned Ollama runtime for ``platform`` into ``target``; returns its version."""
    from orkexa.local.install import OLLAMA_VERSION, artifact_for, install_runtime

    system, machine = platform.ollama
    home = cache_root(ctx) / "ollama" / platform.id
    if cached_ollama(ctx, platform) is None:
        if not allow_downloads:
            raise NodestroError(
                "export.download_needed", "The local model server for this platform is not in the cache", what="ollama"
            )
        install_runtime(
            home,
            artifact=artifact_for(system, machine),
            download=True,
            gpu=False,
            progress=lambda done, total: progress.bytes(done, total or 0),
            cancel=progress.cancel,
        )
    root = cached_ollama(ctx, platform)
    assert root is not None
    shutil.copytree(root, target, symlinks=True, ignore=shutil.ignore_patterns(".complete"))
    license_file = home / "LICENSE"
    if not license_file.is_file() and allow_downloads:
        url = OLLAMA_LICENSE_URL.format(version=OLLAMA_VERSION)
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(64 * 1024)
        if hashlib.sha256(data).hexdigest() != OLLAMA_LICENSE_SHA256:
            raise NodestroError("export.license_mismatch", "Ollama's license file does not match its pinned checksum")
        license_file.write_bytes(data)
    if license_file.is_file():
        shutil.copyfile(license_file, target / "LICENSE")
    return OLLAMA_VERSION


def _progress_bytes(progress: Progress) -> Callable[[int, int], None]:
    return lambda done, total: progress.bytes(done, total)


def install_models(ctx: Nodestro, names: list[str], target: Path, progress: Progress) -> dict[str, Any]:
    """The release's local models as an orkexa offline package (every file checked by SHA-256 while copying)."""
    from orkexa.local import OllamaError, export_package, verify_package

    try:
        report = export_package(
            ctx.local_models.models_dir(), names, target, progress=_progress_bytes(progress), cancel=progress.cancel
        )
        verify_package(target, hashes=False)
    except OllamaError as exc:
        if exc.code == "cancelled":
            raise Cancelled() from None
        raise NodestroError(f"export.models_{exc.code}", str(exc)) from None
    return {"models": report.models, "bytes": report.bytes, "files": report.files}


# ---- start scripts ----------------------------------------------------------------------------------------------

_START_SH = """#!/bin/sh
# Start the application (Ctrl+C stops it). Other commands: stop, status, open, configure, keys, check.
cd "$(dirname "$0")" || exit 1
exec ./python/bin/python3 -I -m nodestro_api.portable "${@:-start}"
"""
_STOP_SH = """#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec ./python/bin/python3 -I -m nodestro_api.portable stop
"""
_CONFIGURE_SH = """#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec ./python/bin/python3 -I -m nodestro_api.portable configure
"""
_START_COMMAND = """#!/bin/sh
# macOS: double-click to start in Terminal.
cd "$(dirname "$0")" || exit 1
exec ./python/bin/python3 -I -m nodestro_api.portable start
"""
_START_CMD = """@echo off
rem Start the application (close this window or press Ctrl+C to stop it).
cd /d "%~dp0"
"python\\python.exe" -I -m nodestro_api.portable %*
if errorlevel 1 pause
"""
_STOP_CMD = """@echo off
cd /d "%~dp0"
"python\\python.exe" -I -m nodestro_api.portable stop
"""
_CONFIGURE_CMD = """@echo off
cd /d "%~dp0"
"python\\python.exe" -I -m nodestro_api.portable configure
pause
"""


def start_scripts(platform: Platform) -> dict[str, tuple[str, int]]:
    """``{path: (content, mode)}`` of the start scripts for ``platform``."""
    if platform.windows:
        return {
            "start.cmd": (_START_CMD.replace("\n", "\r\n"), 0o644),
            "stop.cmd": (_STOP_CMD.replace("\n", "\r\n"), 0o644),
            "configure.cmd": (_CONFIGURE_CMD.replace("\n", "\r\n"), 0o644),
        }
    found = {
        "start.sh": (_START_SH, 0o755),
        "stop.sh": (_STOP_SH, 0o755),
        "configure.sh": (_CONFIGURE_SH, 0o755),
    }
    if platform.id.startswith("macos"):
        found["Start.command"] = (_START_COMMAND, 0o755)
    return found


def settings_toml(options: ExportOptions, *, context_length: int) -> str:
    """``config/settings.toml``: safe defaults that work right after unpacking."""
    output = options.output_dir.strip() or "results"
    return f"""# Settings of this application (see README.md, "Configuration"). Environment variables override them.
# Changes take effect at the next start. Keys of AI providers do not belong here: run "configure" instead.

[server]
# 127.0.0.1: this computer only. "0.0.0.0" serves the network and then needs HTTPS:
# tls_cert and tls_key (PEM files), or behind_proxy = true when a reverse proxy terminates HTTPS.
host = "127.0.0.1"
port = 8800
open_browser = true
tls_cert = ""
tls_key = ""
behind_proxy = false
# Further host names this application answers to when it serves the network, e.g. ["app.example.com"].
allowed_hosts = []

[data]
# Runs, uploads, result files, memory and keys (relative paths are relative to this folder).
data_dir = "data"
# A copy of every result file goes to <output_dir>/<date>_<run>/ (relative or absolute; "" turns it off).
# A folder that cannot be created or written falls back to "results" next to this file.
output_dir = {json.dumps(output)}
# Delete runs older than this many days at start (0 keeps everything).
run_retention_days = 0
log_dir = "logs"

[web]
enabled = {"true" if options.web_ui else "false"}
# de, en, es, fr, pt-BR, it (the interface has a language switch as well)
language = {json.dumps(options.language)}
# local: the API reference (/docs) only from this computer; on; off
api_docs = "local"

[models]
# true: the local model server gets no proxy and no download (models come only from this package).
offline = {"true" if options.offline else "false"}
# Context window of the local models in tokens (more needs more memory).
context_length = {context_length}
start_server = true
keep_alive = "10m"

[tools]
# subprocess: only tools a person wrote run, as a separate process. docker: every own tool runs in an
# isolated container (needs Docker; AI-generated tools run only with docker).
sandbox = "subprocess"
timeout_seconds = 60

[limits]
# Override the published limits, e.g.:
# daily_budget_usd = 10
# max_concurrent_runs = 2
# run_budget_usd = 0.5
"""


# ---- archive ----------------------------------------------------------------------------------------------------


def _walk(folder: Path) -> Iterator[Path]:
    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or path.is_file():
            yield path


def checksums(folder: Path, progress: Progress | None = None) -> str:
    lines = []
    for path in _walk(folder):
        if path.is_symlink() or path.name == "SHA256SUMS":
            continue
        if progress is not None:
            progress.check()
        digest = hashlib.sha256()
        with path.open("rb") as reader:
            while block := reader.read(4 * 1024 * 1024):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {path.relative_to(folder).as_posix()}")
    return "\n".join(lines) + "\n"


def _stored(path: Path) -> bool:
    """Files that do not compress (model weights, archives, images): stored as they are."""
    return "blobs" in path.parts or path.suffix in (".zip", ".gz", ".png", ".jpg", ".woff2", ".zst")


def write_zip(folder: Path, root: str, target: Path, progress: Progress, *, posix: bool) -> int:
    """Stream ``folder`` into a ZIP at ``target`` (ZIP64, file modes and, for Linux/macOS, symbolic links kept)."""
    entries = list(_walk(folder))
    total = sum(path.lstat().st_size for path in entries)
    done = 0
    partial = target.with_suffix(".partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6) as archive:
        for path in entries:
            progress.check()
            relative = path.relative_to(folder).as_posix()
            info = zipfile.ZipInfo(f"{root}/{relative}", date_time=time.localtime(time.time())[:6])
            info.create_system = 3  # Unix: the external attributes carry the mode
            if path.is_symlink():
                link = os.readlink(path)
                if not posix:
                    continue
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, link)
                continue
            mode = path.stat().st_mode
            info.external_attr = (stat.S_IFREG | (0o755 if mode & 0o111 else 0o644)) << 16
            info.compress_type = zipfile.ZIP_STORED if _stored(path) else zipfile.ZIP_DEFLATED
            info.file_size = path.stat().st_size
            with path.open("rb") as reader, archive.open(info, "w", force_zip64=info.file_size > 2**31) as writer:
                while block := reader.read(4 * 1024 * 1024):
                    writer.write(block)
                    done += len(block)
                    progress.bytes(done, total)
    os.replace(partial, target)
    return target.stat().st_size


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while block := reader.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _free_bytes(folder: Path) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(folder).free


def build(
    ctx: Nodestro, publication: Publication, release: Release, options: ExportOptions, progress: Progress
) -> ExportRecord:
    """Build a portable package; returns the stored export (its ZIP is under ``<data>/exports``)."""
    from . import exporter

    progress.step("check")
    found = exporter.check(ctx, publication, release, options)
    if not found.ok:
        first = next(item for item in found.problems if item.level == "error")
        raise NodestroError(first.code, first.message, **first.params)
    platform = PLATFORMS[options.platform or host_platform()]
    portable = found.portable
    assert portable is not None
    needed = portable.estimated_bytes * 2 + 200_000_000  # work folder + ZIP
    free = _free_bytes(ctx.config.data_dir)
    if needed > free:
        raise NodestroError(
            "export.disk_full",
            f"Building needs about {needed // 1_000_000} MB free disk space, {free // 1_000_000} MB are free",
            needed=needed,
            free=free,
        )
    work = _work_root(ctx) / progress.job.id
    stage = work / "package"
    shutil.rmtree(work, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        return _assemble(ctx, publication, release, options, progress, platform, found, work, stage)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _assemble(
    ctx: Nodestro,
    publication: Publication,
    release: Release,
    options: ExportOptions,
    progress: Progress,
    platform: Platform,
    found: Any,
    work: Path,
    stage: Path,
) -> ExportRecord:
    from . import exporter

    allow = options.allow_downloads
    # 1. Python for the target.
    progress.step("python", platform.label)
    python_dir = install_python(ctx, platform, progress, allow_downloads=allow)
    shutil.copytree(python_dir, stage / "python", symlinks=True)
    for marker in (stage / "python").glob("lib/python3.*/EXTERNALLY-MANAGED"):
        marker.unlink()
    python_version = re.sub(r"^cpython-([0-9.]+)-.*$", r"\1", python_dir.name)
    site = stage / "python" / platform.site_packages(python_minor())
    site.mkdir(parents=True, exist_ok=True)
    # 2. Libraries for the target, orkexa from here.
    progress.step("libraries", platform.wheel_platform)
    lock = install_libraries(ctx, platform, site, work, options, release.bundle, progress, allow_downloads=allow)
    install_orkexa(site)
    # The project folder is on the path of the bundled Python (the launcher runs with -I: nothing else is).
    depth = len(Path("python", platform.site_packages(python_minor())).parts)
    (site / "nodestro-app.pth").write_text("/".join([".."] * depth) + "\n", encoding="utf-8")
    # 3. The project.
    progress.step("project")
    files = exporter.project_files(ctx, publication, release, options)
    for name, data in files.items():
        if name in _NOT_PORTABLE or name.startswith(("tests/", "vendor/")):
            continue
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (stage / "lib.lock").write_text(
        f"# The libraries of this package for {platform.label} (Python {python_version}), with SHA-256 hashes.\n"
        + lock,
        encoding="utf-8",
    )
    for name, (content, mode) in start_scripts(platform).items():
        (stage / name).write_text(content, encoding="utf-8", newline="")
        (stage / name).chmod(mode)
    context_length = _context_length(ctx)
    (stage / "config").mkdir(exist_ok=True)
    (stage / "config" / "settings.toml").write_text(settings_toml(options, context_length=context_length), "utf-8")
    licenses = stage / "licenses"
    licenses.mkdir(exist_ok=True)
    (licenses / "nodestro.txt").write_text(exporter.NODESTRO_LICENSE, encoding="utf-8")
    (licenses / "orkexa.txt").write_text(exporter._library_license(), encoding="utf-8")
    # 4. The local model server and the models.
    local_info: dict[str, Any] = {}
    models = [item for item in found.portable.local_models if item.name in found.portable.bundled_models]
    if options.local_models and models:
        progress.step("runtime", "Ollama")
        version = install_ollama(ctx, platform, stage / "ollama", progress, allow_downloads=allow)
        if (stage / "ollama" / "LICENSE").is_file():
            shutil.copyfile(stage / "ollama" / "LICENSE", licenses / "ollama.txt")
        progress.step("models", ", ".join(item.name for item in models))
        report = install_models(ctx, [item.name for item in models], stage / "models", progress)
        local_info = {
            "server": "ollama",
            "version": version,
            "models": [item.model_dump(mode="json") for item in models],
            "bytes": report["bytes"],
        }
        (licenses / "models.md").write_text(_model_licenses(models), encoding="utf-8")
    # 5. What the package is.
    readme = stage / "README.md"
    if readme.is_file():
        readme.write_text(
            fill_readme(
                readme.read_text(encoding="utf-8"),
                options,
                found.portable,
                platform,
                local_info,
                python_version=python_version,
                context_length=context_length,
            ),
            encoding="utf-8",
        )
    manifest = {
        "format": "nodestro-portable",
        "format_version": 1,
        "name": publication.name,
        "slug": publication.slug,
        "release_number": release.number,
        "release_digest": release.digest,
        "platform": platform.id,
        "platform_label": platform.label,
        "python": python_version,
        "nodestro": found.runtime,
        "exported_with": __version__,
        "created_at": now().isoformat(),
        "language": options.language,
        "web_ui": options.web_ui,
        "offline": options.offline,
        "providers": found.portable.providers,
        "network_needs": [item.model_dump(mode="json") for item in found.portable.network_needs],
        "local_models": local_info,
        "secrets_included": False,
    }
    (stage / "PORTABLE.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (stage / "SHA256SUMS").write_text(checksums(stage, progress), encoding="utf-8")
    # 6. The archive.
    progress.step("archive")
    record_id = new_id("exp")
    target_dir = ctx.config.data_dir / "exports"
    target_dir.mkdir(parents=True, exist_ok=True)
    root = f"{exporter.project_name(publication)}-v{release.number}-{platform.id}"
    target = target_dir / f"{record_id}.zip"
    size = write_zip(stage, root, target, progress, posix=not platform.windows)
    record = exporter.ExportRecord(
        id=record_id,
        publication_id=publication.id,
        release_id=release.id,
        release_number=release.number,
        options=options,
        file_name=f"{root}.zip",
        size=size,
        sha256=_sha_file(target),
        files=sum(1 for _ in _walk(stage)),
        warnings=[item.code for item in found.problems if item.level == "warning"],
        runtime=found.runtime,
    )
    exporter.record_export(ctx, publication, release, record)
    return record


def _context_length(ctx: Nodestro) -> int:
    """The context window the local models get (the studio's setting, else the managed server's default)."""
    settings_of = getattr(getattr(ctx, "local_models", None), "settings", None)
    stored: Any = settings_of() if callable(settings_of) else {}
    value = (stored or {}).get("context_length") if isinstance(stored, dict) else None
    try:
        return int(value or os.environ.get("NODESTRO_OLLAMA_CONTEXT_LENGTH") or 32768)
    except ValueError:
        return 32768


def _size(value: int | None) -> str:
    if not value:
        return "-"
    return f"{value / 1_000_000_000:.1f} GB" if value >= 100_000_000 else f"{value / 1_000_000:.0f} MB"


def fill_readme(
    text: str,
    options: ExportOptions,
    check: Any,
    platform: Platform,
    local_info: dict[str, Any],
    *,
    python_version: str,
    context_length: int,
) -> str:
    """The package-specific parts of the portable README (``readme/portable.<lang>.md``) in its language."""
    from nodestro_runtime.providers import PROVIDERS

    from .exporter import readme_strings

    strings = readme_strings(options.language)

    def say(key: str, **values: Any) -> str:
        found = strings[key]
        for name, value in values.items():
            found = found.replace("{" + name + "}", str(value))
        return found

    bundled = [item for item in check.local_models if item.name in check.bundled_models] if local_info else []
    external = [item for item in check.local_models if item not in bundled]
    # Providers that need something on the target computer (the bundled model server needs nothing).
    providers = [name for name in check.providers if name in PROVIDERS and not (name == "ollama" and not external)]
    if providers:
        from .portable_secrets import variables

        rows = [say("portable.providers_head")]
        rows += [
            f"| {PROVIDERS[name].label} | {', '.join(f'`{item}`' for item in variables(name))} |" for name in providers
        ]
        provider_section = say("portable.providers_intro") + "\n\n" + "\n".join(rows)
    else:
        provider_section = say("portable.providers_none")
    parts: list[str] = []
    if bundled:
        rows = [say("portable.models_head")]
        for item in bundled:
            roles = ", ".join(say(f"portable.role.{role}") for role in item.roles)
            rows.append(
                f"| `{item.name}` | {roles} | {_size(item.size)} | {_size(item.memory_bytes)} | "
                f"{item.license or say('portable.unknown')} |"
            )
        parts.append(say("portable.models_intro", version=local_info.get("version", "")) + "\n\n" + "\n".join(rows))
        parts.append(
            say("portable.models_total", total=_size(int(local_info.get("bytes") or 0)), context=context_length)
        )
    if external:
        parts.append(say("portable.models_external", models=", ".join(f"`{item.name}`" for item in external)))
    models_section = "\n\n".join(parts) or say("portable.models_none")
    needs = list(check.network_needs)
    if needs:
        network = (
            say("portable.network_intro")
            + "\n\n"
            + "\n".join(f"- {say('portable.need.' + item.kind, name=item.name, detail=item.detail)}" for item in needs)
        )
    elif options.offline:
        network = say("portable.network_offline")
    else:
        network = say("portable.network_none")
    # The export always sets a folder (default ``results``); the operator can turn copies off later.
    output = say("portable.output_on", folder=options.output_dir.strip() or "results")
    replacements = {
        "{platform}": platform.label,
        "{local_models_line}": say("portable.local_models_line", models=", ".join(item.name for item in bundled))
        if bundled
        else "",
        "{models_check}": say("portable.models_check") if bundled else "",
        "{ui_line}": say("portable.ui_on" if options.web_ui else "portable.ui_off"),
        "{providers_section}": provider_section,
        "{models_section}": models_section,
        "{network_section}": network,
        "{output_section}": output,
        "{python_version}": python_version,
        "{models_files}": say("portable.models_files") if bundled else "",
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _model_licenses(models: list[Any]) -> str:
    lines = ["# Licenses of the bundled models", ""]
    for item in models:
        lines.append(f"- `{item.name}`: {item.license or 'unknown (the exporter confirmed the right to pass it on)'}")
    lines += [
        "",
        "The license texts that come with a model are part of its files in `models/` (Ollama license layers).",
        "",
    ]
    return "\n".join(lines)


def host_platform() -> str:
    import platform as host

    system = host.system().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
        host.machine().lower(), host.machine().lower()
    )
    name = {"darwin": "macos"}.get(system, system)
    candidate = f"{name}-{machine}"
    return candidate if candidate in PLATFORMS else "linux-x86_64"


__all__ = ["PLATFORMS", "ExportJob", "JobRunner", "Platform", "build", "downloads", "host_platform", "runner"]
