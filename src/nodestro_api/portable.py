"""The launcher of a portable application: start, stop and configure an exported workflow without installing anything.

A portable export (:mod:`nodestro_api.portable_build`) is a folder with its own Python (``python/``), every library
in that Python's ``site-packages``, the exported project (``app/``, ``workflow/``, ``nodestro_runtime/``,
``nodestro_api/``), optionally a local model server (``ollama/``) with its models (``models/``), and small start
scripts that run ``python -I -m nodestro_api.portable <command>``:

``start`` (default)
    Check the settings, create the folders, prepare the local models (verify every file by SHA-256 on the first
    start, make them visible to the model server in place, start the bundled Ollama on a free loopback port without
    network access in offline mode), start the API and the web interface, wait until the API answers, print the
    address and open the browser with a one-time sign-in link. Ctrl+C (or ``stop``) ends everything.
``stop``
    Ask a running instance to stop (through a secret only the launcher of this folder knows).
``status``
    Whether an instance runs, its address and the local model server.
``open``
    Open the browser of a running instance with a new sign-in link.
``configure``
    Enter the AI providers' keys in a terminal (stored on this computer only, see :mod:`nodestro_api.portable_secrets`).
``keys``
    Create, list or revoke the API keys of clients (``keys new --name CRM --scopes run,read``).
``check``
    A self-test without starting: Python, libraries, folders, model files, the model server binary.

Settings live in ``config/settings.toml`` (sections ``server``, ``data``, ``web``, ``models``, ``tools``, ``limits``);
environment variables of :class:`nodestro_api.standalone.StandaloneSettings` override them. The defaults work right
after unpacking: this computer only (``127.0.0.1``), data in ``data/``, result files in ``results/``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from contextlib import suppress
import getpass
import hashlib
import http.client
import json
import logging
import logging.handlers
import os
from pathlib import Path
import platform
import secrets
import socket
import stat
import sys
import threading
import time
import tomllib
from typing import TYPE_CHECKING, Any
import webbrowser

if TYPE_CHECKING:
    from fastapi import FastAPI

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "PORTABLE.json"
CONFIG = ROOT / "config" / "settings.toml"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
#: Ports tried after the configured one when it is taken.
PORT_ATTEMPTS = 20
_STATE = Path("run") / "instance.json"
_VERIFIED = Path("run") / "models-verified.json"

logger = logging.getLogger("nodestro.portable")


# ---- messages -------------------------------------------------------------------------------------------------


def _messages(language: str) -> dict[str, str]:
    """The launcher's messages in ``language`` (from the web interface's translations; English as fallback)."""
    folder = Path(__file__).resolve().parent / "webui" / "i18n"
    found: dict[str, str] = {}
    for name in ("en", language):
        file = folder / f"{name}.json"
        with suppress(OSError, ValueError):
            data = json.loads(file.read_text(encoding="utf-8"))
            found.update({key[9:]: value for key, value in data.items() if key.startswith("launcher.")})
    return found


class Console:
    """Prints the launcher's messages, translated, also to the log file."""

    def __init__(self, language: str) -> None:
        self.texts = _messages(language)

    def say(self, key: str, *, log: bool = True, **values: Any) -> None:
        """Print a message; ``log=False`` keeps it out of the log file (the one-time sign-in link, API3-04)."""
        text = self.texts.get(key, key)
        with suppress(KeyError, IndexError, ValueError):
            text = text.format(**values)
        print(text, flush=True)
        if log:
            logger.info("%s", text)


# ---- settings -------------------------------------------------------------------------------------------------


DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8800,
        "open_browser": True,
        "tls_cert": "",
        "tls_key": "",
        "behind_proxy": False,
        "allowed_hosts": [],
    },
    "data": {"data_dir": "data", "output_dir": "results", "run_retention_days": 0, "log_dir": "logs"},
    "web": {"enabled": True, "language": "en", "api_docs": "local"},
    "models": {"offline": False, "context_length": 32768, "start_server": True, "keep_alive": "10m"},
    "tools": {"sandbox": "subprocess", "timeout_seconds": 60},
    "limits": {},
}


class Settings:
    """``config/settings.toml`` over :data:`DEFAULTS`; environment variables win where they exist."""

    def __init__(self, root: Path = ROOT, values: dict[str, dict[str, Any]] | None = None) -> None:
        self.root = root
        merged = {section: dict(items) for section, items in DEFAULTS.items()}
        for section, items in (values if values is not None else self._read(root / "config" / "settings.toml")).items():
            if isinstance(items, dict):
                merged.setdefault(section, {}).update(items)
        self.values = merged

    @staticmethod
    def _read(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise SystemExit(f"config/settings.toml cannot be read: {exc}") from None

    def get(self, section: str, key: str) -> Any:
        return self.values.get(section, {}).get(key)

    def path(self, value: str) -> Path:
        candidate = Path(os.path.expandvars(os.path.expanduser(str(value))))
        return candidate if candidate.is_absolute() else self.root / candidate

    @property
    def data_dir(self) -> Path:
        return self.path(os.environ.get("DATA_DIR") or self.get("data", "data_dir"))

    @property
    def host(self) -> str:
        return str(os.environ.get("NODESTRO_HOST") or self.get("server", "host"))

    @property
    def port(self) -> int:
        return int(os.environ.get("NODESTRO_PORT") or self.get("server", "port"))

    @property
    def language(self) -> str:
        return str(os.environ.get("UI_LANGUAGE") or self.get("web", "language") or "en")

    @property
    def offline(self) -> bool:
        value = os.environ.get("NODESTRO_OFFLINE")
        return bool(self.get("models", "offline")) if value is None else value.lower() in ("1", "true", "on", "yes")

    def environment(self) -> dict[str, str]:
        """The variables :class:`~nodestro_api.standalone.StandaloneSettings` reads, from the settings file."""
        found = {
            "DATA_DIR": str(self.data_dir),
            "OUTPUT_DIR": str(self.get("data", "output_dir") or ""),
            "RUN_RETENTION_DAYS": str(int(self.get("data", "run_retention_days") or 0)),
            "API_DOCS": str(self.get("web", "api_docs") or "local"),
            "WEB_UI": "on" if self.get("web", "enabled") else "off",
            "UI_LANGUAGE": self.language,
            "TOOL_SANDBOX": str(self.get("tools", "sandbox") or "subprocess"),
            "TOOL_TIMEOUT_SECONDS": str(self.get("tools", "timeout_seconds") or 60),
        }
        for key, value in (self.values.get("limits") or {}).items():
            found[f"LIMIT_{str(key).upper()}"] = "none" if value is None else str(value).lower()
        return found


def _api_keys(data_dir: Path) -> str:
    """The client keys' entries (hashes) created with ``keys new``."""
    file = data_dir / "api-keys.txt"
    if not file.is_file():
        return ""
    return ";".join(line.strip() for line in file.read_text(encoding="utf-8").splitlines() if line.strip())


def _private_folder(folder: Path, root: Path) -> None:
    """Keeps a folder of the person's data owner-only on POSIX (``0700``, API3-04): created that way, and narrowed
    when it is the package's own (inside ``root``, e.g. ``data`` or ``logs`` from an older version). A folder named
    elsewhere that already exists is left as its owner set it. Errors are left to the folder check."""
    if os.name != "posix":
        return
    try:
        if not folder.exists():
            folder.mkdir(mode=0o700, parents=True)
            folder.chmod(0o700)
        elif (
            folder.is_dir()
            and folder.resolve().is_relative_to(root.resolve())
            and stat.S_IMODE(folder.stat().st_mode) & 0o077
        ):
            folder.chmod(0o700)
    except OSError:
        return


def _check_folder(folder: Path) -> str | None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return exc.strerror or type(exc).__name__
    return None


# ---- package facts ---------------------------------------------------------------------------------------------


def package_info(root: Path = ROOT) -> dict[str, Any]:
    if not (root / "PORTABLE.json").is_file():
        return {}
    try:
        return dict(json.loads((root / "PORTABLE.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _ollama_binary(root: Path) -> Path | None:
    for candidate in (root / "ollama" / "bin" / "ollama", root / "ollama" / "ollama", root / "ollama" / "ollama.exe"):
        if candidate.is_file():
            return candidate
    return None


def _ram_bytes() -> int | None:
    with suppress(Exception):
        from nodestro_runtime.local_models import detect_hardware

        return detect_hardware().ram_bytes
    return None


# ---- local models ----------------------------------------------------------------------------------------------


class LocalModels:
    """The bundled model server and models of a package (nothing when the package has none)."""

    def __init__(self, root: Path, settings: Settings, console: Console) -> None:
        self.root = root
        self.settings = settings
        self.console = console
        self.info = package_info(root).get("local_models") or {}
        self.server: Any = None
        self.state: dict[str, Any] = {
            "bundled": bool(self.info.get("models")),
            "running": False,
            "warnings": [],
            "offline": settings.offline,
        }

    @property
    def package(self) -> Path:
        return self.root / "models"

    def verify(self, data_dir: Path, *, full: bool = False) -> None:
        """Check the model files: by SHA-256 on the first start (or with ``full``), later by size only."""
        from orkexa.local import verify_package

        marker = data_dir / _VERIFIED
        index = self.package / "orkexa-models.json"
        fingerprint = hashlib.sha256(index.read_bytes()).hexdigest()
        known = {}
        with suppress(OSError, ValueError):
            known = json.loads(marker.read_text(encoding="utf-8"))
        hashes = full or known.get("index") != fingerprint
        if hashes:
            self.console.say("models_verifying")
        report = verify_package(self.package, hashes=hashes)
        if hashes:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"index": fingerprint, "at": time.time()}), encoding="utf-8")
        self.state["models"] = report.models

    def _models_dir(self, data_dir: Path) -> Path:
        """Where the model server reads the models: the package folder itself (made visible in place, nothing is
        copied); a copy in the data folder only when the package folder cannot be written."""
        from orkexa.local import import_package

        if _check_folder(self.package / "manifests-v2") is None:
            import_package(self.package, self.package)
            return self.package
        target = data_dir / "models"
        self.console.say("models_copying", folder=str(target))
        import_package(self.package, target)
        return target

    def hardware_warnings(self) -> list[str]:
        warnings: list[str] = []
        ram = _ram_bytes()
        needed = max((int(item.get("memory_bytes") or 0) for item in self.info.get("models", [])), default=0)
        if ram and needed and needed > ram:
            warnings.append(f"memory:{needed}:{ram}")
        return warnings

    def start(self, data_dir: Path) -> str | None:
        """Prepare and start the bundled model server; its OpenAI-compatible address, or ``None``."""
        if not self.state["bundled"]:
            return None
        from orkexa.local import ManagedOllama, OllamaError

        binary = _ollama_binary(self.root)
        if binary is None:
            raise SystemExit("The package has models but no model server (ollama/); export it again")
        if os.name == "posix":
            with suppress(OSError):
                binary.chmod(binary.stat().st_mode | 0o111)
        self.verify(data_dir)
        models_dir = self._models_dir(data_dir)
        self.state["warnings"] = self.hardware_warnings()
        for warning in self.state["warnings"]:
            _, needed, ram = warning.split(":")
            self.console.say("models_memory", needed=_gigabytes(int(needed)), ram=_gigabytes(int(ram)))
        environment = {"OLLAMA_CONTEXT_LENGTH": str(int(self.settings.get("models", "context_length") or 32768))}
        if self.settings.get("models", "keep_alive"):
            environment["OLLAMA_KEEP_ALIVE"] = str(self.settings.get("models", "keep_alive"))
        self.server = ManagedOllama(
            data_dir / "ollama",
            binary=binary,
            models_dir=models_dir,
            offline=self.settings.offline,
            environment=environment,
        )
        self.console.say("models_starting")
        try:
            base = self.server.start()
        except OllamaError as exc:
            raise SystemExit(f"The local model server did not start: {exc} (log: {self.server.log_file})") from None
        self.state.update(running=True, base_url=base, models_dir=str(models_dir))
        return f"{base.rstrip('/')}/v1"

    def stop(self) -> None:
        if self.server is not None:
            with suppress(Exception):
                self.server.stop()
            self.state["running"] = False


def _gigabytes(value: int) -> str:
    return f"{value / 1_000_000_000:.1f} GB"


# ---- instance state --------------------------------------------------------------------------------------------


def _read_state(data_dir: Path) -> dict[str, Any] | None:
    with suppress(OSError, ValueError):
        return dict(json.loads((data_dir / _STATE).read_text(encoding="utf-8")))
    return None


def _write_state(data_dir: Path, state: dict[str, Any]) -> None:
    target = data_dir / _STATE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    if os.name == "posix":
        temporary.chmod(0o600)
    os.replace(temporary, target)


def _control(state: dict[str, Any], path: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """A control request to a running instance (loopback, with its control secret)."""
    try:
        connection = http.client.HTTPConnection("127.0.0.1", int(state["port"]), timeout=timeout)
        connection.request("POST", path, headers={"X-Control-Token": str(state["control"])})
        response = connection.getresponse()
        body = response.read()
        connection.close()
    except (OSError, KeyError, ValueError):
        return None
    if response.status != 200:
        return None
    with suppress(ValueError):
        return dict(json.loads(body))
    return None


def _responding(port: int) -> bool:
    """Whether the instance answers, allowing for a busy moment (a slow computer, a run just finishing)."""
    for attempt in range(3):
        if _health(port, timeout=5.0):
            return True
        if attempt < 2:
            time.sleep(0.5)
    return False


def _health(port: int, timeout: float = 2.0) -> bool:
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        connection.request("GET", "/health")
        ok = connection.getresponse().status == 200
        connection.close()
        return ok
    except OSError:
        return False


def _free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if os.name == "posix":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _choose_port(host: str, port: int) -> int:
    for candidate in range(port, port + PORT_ATTEMPTS):
        if _free(host, candidate):
            return candidate
    raise SystemExit(f"No free port between {port} and {port + PORT_ATTEMPTS - 1}; set server.port")


def _browser_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


# ---- commands --------------------------------------------------------------------------------------------------


def _setup_logging(settings: Settings) -> Path:
    folder = settings.path(settings.get("data", "log_dir") or "logs")
    _private_folder(folder, settings.root)
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(folder / "application.log", maxBytes=5_000_000, backupCount=3)
    if os.name == "posix":
        with suppress(OSError):
            (folder / "application.log").chmod(0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return folder


def _network_problem(settings: Settings, host: str | None = None) -> str | None:
    """Why serving other computers is refused (``None``: it is allowed or not wanted). ``host``: the address that
    will be served (``start --host`` wins over the settings, so it is checked too, API3-03)."""
    host = host or settings.host
    if host in LOOPBACK:
        return None
    if settings.get("server", "behind_proxy"):
        return None
    if settings.get("server", "tls_cert") and settings.get("server", "tls_key"):
        return None
    return "network_needs_tls"


def start(args: argparse.Namespace, settings: Settings) -> int:
    console = Console(settings.language)
    data_dir = settings.data_dir
    _private_folder(data_dir, settings.root)
    problem = _check_folder(data_dir)
    if problem is not None:
        console.say("data_dir_unusable", folder=str(data_dir), problem=problem)
        return 2
    log_dir = _setup_logging(settings)
    running = _read_state(data_dir)
    if running and _health(int(running.get("port", 0))):
        console.say("already_running", url=running.get("url", ""))
        if not args.no_browser:
            _open_browser(running, console)
        return 0
    host = args.host or settings.host
    if _network_problem(settings, host):
        console.say("network_needs_tls")
        return 2
    # OUTPUT_DIR set outside the application wins over the folder chosen on the settings page.
    output_fixed = bool(os.environ.get("OUTPUT_DIR"))
    for key, value in settings.environment().items():
        os.environ.setdefault(key, value)
    if not os.environ.get("API_KEYS"):
        os.environ["API_KEYS"] = _api_keys(data_dir)
    port = _choose_port(host, args.port or settings.port)
    info = package_info(settings.root)
    from .portable_secrets import ProviderSecrets

    secrets_store = ProviderSecrets(data_dir, info.get("providers") or [])
    secrets_store.apply()
    models = LocalModels(settings.root, settings, console)
    try:
        if settings.get("models", "start_server"):
            url = models.start(data_dir)
            if url:
                os.environ["OLLAMA_BASE_URL"] = url
        return _serve(args, settings, console, host, port, secrets_store, models, log_dir, output_fixed)
    finally:
        models.stop()
        with suppress(OSError):
            (data_dir / _STATE).unlink()


def _serve(
    args: argparse.Namespace,
    settings: Settings,
    console: Console,
    host: str,
    port: int,
    secrets_store: Any,
    models: LocalModels,
    log_dir: Path,
    output_fixed: bool = False,
) -> int:
    import uvicorn

    from .output_folder import OutputFolder, file_manager
    from .standalone import StandaloneSettings, create_app
    from .webui_server import UiOptions, WebUi

    control = secrets.token_urlsafe(32)
    launch = secrets.token_urlsafe(32)
    standalone = StandaloneSettings.from_env()
    tls = bool(settings.get("server", "tls_cert") and settings.get("server", "tls_key"))
    scheme = "https" if tls else "http"
    url = f"{scheme}://{_browser_host(host)}:{port}"
    holder: dict[str, Any] = {}

    def make_ui(host_object: Any, core: Any) -> WebUi:
        options = UiOptions(
            language=settings.language,
            secure_cookies=tls or bool(settings.get("server", "behind_proxy")),
            control_token=control,
            output_dir=str(host_object.output_dir) if host_object.output_dir else None,
            output_dir_warning=host_object.output_warning,
            local_models=models.state,
            api_docs=str(settings.get("web", "api_docs") or "local") != "off",
        )
        # The settings page may show the result folder in the file manager only when the application serves this
        # computer alone (a browser elsewhere would open nothing it can see).
        local_only = host in LOOPBACK and not settings.get("server", "behind_proxy")
        output = OutputFolder(
            host_object,
            data_dir=settings.data_dir,
            project_dir=settings.root,
            fixed=output_fixed,
            protected=[log_dir],
            opener=file_manager() if local_only else None,
        )
        ui = WebUi(
            host_object,
            options,
            on_stop=lambda: holder["server"].__setattr__("should_exit", True),
            provider_status=secrets_store.status,
            set_provider=secrets_store.set,
            remove_provider=secrets_store.remove,
            auth_failed=core.auth_failed,
            output=output,
        )
        ui.sessions.add_launch_token(launch)
        return ui

    app = create_app(settings.root, settings=standalone, web_ui=make_ui)
    allowed = {*LOOPBACK, *(str(item).lower() for item in settings.get("server", "allowed_hosts") or [])}
    if host not in LOOPBACK:
        allowed.add(socket.gethostname().lower())
    _guard_hosts(app, allowed, open_network=host not in LOOPBACK)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        ssl_certfile=str(settings.path(settings.get("server", "tls_cert"))) if tls else None,
        ssl_keyfile=str(settings.path(settings.get("server", "tls_key"))) if tls else None,
        proxy_headers=bool(settings.get("server", "behind_proxy")),
        forwarded_allow_ips="127.0.0.1" if settings.get("server", "behind_proxy") else None,
        timeout_graceful_shutdown=20,
    )
    server = uvicorn.Server(config)
    holder["server"] = server
    state = {"pid": os.getpid(), "port": port, "url": url, "control": control, "started": time.time()}

    def announce() -> None:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not server.should_exit:
            if server.started and _health(port):
                console.say("ready", url=url, logs=str(log_dir))
                if host not in LOOPBACK:
                    console.say("network_on", host=host)
                if settings.get("server", "open_browser") and not args.no_browser:
                    _open(f"{url}/ui/launch?token={launch}")
                else:
                    console.say("launch_link", log=False, url=f"{url}/ui/launch?token={launch}")
                console.say("how_to_stop")
                return
            time.sleep(0.2)

    # The instance file is written before the server listens, so that "stop", "status" and "open" find every instance
    # that already answers. Written only once it answered (as before), a "stop" right after the first answer could find
    # no file, report "not running" and leave the application running. Until the server answers, these commands still
    # report "not running" (they check /health); start() removes the file when the server ends.
    _write_state(settings.data_dir, state)
    threading.Thread(target=announce, name="nodestro-announce", daemon=True).start()
    console.say("starting", name=package_info(settings.root).get("name", ""), port=port)
    server.run()
    console.say("stopped")
    return 0


def _guard_hosts(app: FastAPI, allowed: set[str], *, open_network: bool) -> None:
    """Answer only requests for this computer's own names (no DNS rebinding); serving the network allows any host
    name an operator put into ``server.allowed_hosts`` and the machine's IP addresses."""
    import ipaddress

    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def check_host(request: Any, call_next: Callable[[Any], Any]) -> Any:
        raw = (request.headers.get("host") or "").lower()
        name = raw.rsplit(":", 1)[0].strip("[]") if raw.count(":") <= 1 or raw.startswith("[") else raw
        ok = name in allowed
        if not ok and open_network:
            with suppress(ValueError):
                ipaddress.ip_address(name)
                ok = True
        if not ok:
            return JSONResponse(
                {"error": {"status": 421, "code": "host.not_allowed", "message": "Unknown host name"}}, 421
            )
        return await call_next(request)


def _open(url: str) -> None:
    with suppress(Exception):
        if webbrowser.open(url):
            return
    print(url, flush=True)


def _open_browser(state: dict[str, Any], console: Console) -> None:
    answer = _control(state, "/ui/control/launch")
    if answer and answer.get("token"):
        _open(f"{state['url']}/ui/launch?token={answer['token']}")


def stop(settings: Settings) -> int:
    console = Console(settings.language)
    state = _read_state(settings.data_dir)
    if not state or not _responding(int(state.get("port", 0))):
        console.say("not_running")
        return 0
    if _control(state, "/ui/control/stop") is None:
        console.say("stop_failed")
        return 1
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and _health(int(state["port"]), timeout=1):
        time.sleep(0.5)
    console.say("stopped")
    return 0


def status(settings: Settings) -> int:
    console = Console(settings.language)
    state = _read_state(settings.data_dir)
    if state and _responding(int(state.get("port", 0))):
        console.say("already_running", url=state.get("url", ""))
        return 0
    console.say("not_running")
    return 3


def open_command(settings: Settings) -> int:
    console = Console(settings.language)
    state = _read_state(settings.data_dir)
    if not state or not _responding(int(state.get("port", 0))):
        console.say("not_running")
        return 3
    _open_browser(state, console)
    return 0


def configure(settings: Settings) -> int:
    """Ask for each provider's key in the terminal (input hidden) and store it on this computer."""
    from nodestro_runtime.providers import PROVIDERS

    from .portable_secrets import ProviderSecrets

    console = Console(settings.language)
    info = package_info(settings.root)
    store = ProviderSecrets(settings.data_dir, info.get("providers") or [], environ=dict(os.environ))
    store.apply()
    # The local model server needs nothing; cloud models of Ollama and every other provider do.
    providers = [item for item in store.status() if item["id"] != "ollama" or item["required"]]
    if not providers:
        console.say("configure_nothing")
    for item in providers:
        provider = PROVIDERS[item["id"]]
        state = "configure_set" if item["configured"] else "configure_missing"
        console.say(state, provider=provider.label)
        if not item["editable"]:
            console.say("configure_environment", variables=", ".join(item["variables"]))
            continue
        values: dict[str, str] = {}
        if provider.auth == "aws":
            region = input(f"AWS region ({provider.label}): ").strip()
            if region:
                values["region"] = region
        else:
            if provider.base_url:
                url = input(f"Base URL ({provider.label}, Enter: {item.get('base_url') or '-'}): ").strip()
                if url:
                    values["base_url"] = url
            key = getpass.getpass(f"API key ({provider.label}, Enter: keep): ").strip()
            if key:
                values["api_key"] = key
        if values:
            try:
                store.set(item["id"], values)
            except Exception as exc:  # an ApiError with a readable message
                print(f"  {getattr(exc, 'message', exc)}", flush=True)
                continue
            console.say("configure_saved", provider=provider.label)
    configure_connections(settings, console)
    return 0


def configure_connections(
    settings: Settings,
    console: Console,
    *,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
) -> None:
    """Ask for the service connections the release's built-in tools are bound to (ADR-0001) and store them in
    ``<DATA_DIR>/secrets/connections.json``. A secret set in the environment is left there."""
    from nodestro_runtime.connections import connection_type
    from nodestro_runtime.problems import NodestroError
    from nodestro_runtime.release import ReleaseBundle

    from . import portable_connections

    release = settings.root / "workflow" / "release.json"
    if not release.is_file():
        return
    required = ReleaseBundle.model_validate_json(release.read_text("utf-8")).required_connections
    if not required:
        return
    entries = portable_connections.load(settings.data_dir)
    current = portable_connections.static(settings.data_dir)
    for connection_id, wanted in required.items():
        kind_name, _, provider = wanted.partition(":")
        try:
            kind = connection_type(kind_name)
        except NodestroError:
            console.say("configure_connection_unknown", connection=connection_id, type=wanted)
            continue
        missing = bool(current.missing({connection_id: wanted}))
        console.say(
            "configure_connection_missing" if missing else "configure_connection_set",
            connection=connection_id,
            type=wanted,
        )
        entry = dict(entries.get(connection_id) or {})
        values = dict(entry.get("settings") or {})
        for field, info in kind.settings.model_fields.items():
            shown = values.get(field, info.default if info.default is not None else "")
            answer = ask(f"  {field} (Enter: {shown if shown != '' else '-'}): ").strip()
            if answer:
                values[field] = answer
        variable = portable_connections.secret_variable(connection_id)
        secret = entry.get("secret")
        if os.environ.get(variable):
            console.say("configure_environment", variables=variable)
        else:
            label = kind.secret_label or "Secret"
            typed = ask_secret(f"  {label} (Enter: keep): ").strip()
            secret = typed or secret
        candidate = {"type": kind.name, "provider": provider or "generic", "settings": values}
        if secret:
            candidate["secret"] = secret
        check = portable_connections.StaticConnections({connection_id: candidate}, environ=os.environ, mode="server")
        if connection_id in check.problems:
            console.say("configure_connection_invalid", connection=connection_id, code=check.problems[connection_id])
            continue
        entries[connection_id] = candidate
        portable_connections.save(settings.data_dir, entries)
        console.say("configure_connection_saved", connection=connection_id)


def keys(args: argparse.Namespace, settings: Settings) -> int:
    from .serving import SCOPES, hash_secret, new_key

    file = settings.data_dir / "api-keys.txt"
    _private_folder(file.parent, settings.root)
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = file.read_text(encoding="utf-8").splitlines() if file.is_file() else []
    if args.action == "list":
        for line in lines:
            parts = line.split(":")
            if len(parts) >= 3:
                print(f"{parts[0]}  {parts[2]}  {':'.join(parts[3:])}")
        return 0
    if args.action == "revoke":
        kept = [line for line in lines if not line.startswith(f"{args.id}:")]
        file.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        print("revoked" if len(kept) < len(lines) else "not found")
        return 0 if len(kept) < len(lines) else 1
    scopes = [scope.strip() for scope in args.scopes.split(",") if scope.strip() in SCOPES]
    if not scopes:
        print(f"--scopes: choose from {', '.join(SCOPES)}")
        return 2
    key_id, secret, token = new_key()
    name = args.name.replace(";", ",").replace("\n", " ")
    lines.append(f"{key_id}:{hash_secret(secret)}:{'+'.join(scopes)}{':' + name if name else ''}")
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name == "posix":
        file.chmod(0o600)
    print("Key (shown once, give it to the client):")
    print(f"  {token}")
    print("It is valid after the next start (stop and start the application).")
    return 0


def check(settings: Settings, *, full: bool = False) -> int:
    """A self-test: the bundled Python and libraries load, folders can be written, model files and server exist."""
    console = Console(settings.language)
    results: list[tuple[str, bool, str]] = []
    results.append(("python", True, f"{platform.python_implementation()} {platform.python_version()} {sys.executable}"))
    for module in ("fastapi", "uvicorn", "pydantic", "orkexa", "nodestro_runtime.engine", "nodestro_api.standalone"):
        try:
            __import__(module)
            results.append((f"import {module}", True, ""))
        except Exception as exc:  # report every failure
            results.append((f"import {module}", False, f"{type(exc).__name__}: {exc}"))
    _private_folder(settings.data_dir, settings.root)
    data_problem = _check_folder(settings.data_dir)
    results.append(("data folder", data_problem is None, data_problem or str(settings.data_dir)))
    info = package_info(settings.root)
    if (info.get("local_models") or {}).get("models"):
        binary = _ollama_binary(settings.root)
        results.append(("model server", binary is not None, str(binary or "ollama/ missing")))
        try:
            from orkexa.local import verify_package

            report = verify_package(settings.root / "models", hashes=full)
            results.append(("models", True, ", ".join(report.models)))
        except Exception as exc:
            results.append(("models", False, str(exc)))
    try:
        from nodestro_runtime.release import ReleaseBundle

        bundle = ReleaseBundle.model_validate_json((settings.root / "workflow" / "release.json").read_text("utf-8"))
        manifest = json.loads((settings.root / "workflow" / "manifest.json").read_text("utf-8"))
        results.append(("release", bundle.digest() == manifest["release_digest"], manifest.get("name", "")))
    except Exception as exc:
        results.append(("release", False, str(exc)))
    failed = 0
    for name, ok, detail in results:
        failed += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}", flush=True)
    console.say("check_ok" if not failed else "check_failed")
    return 0 if not failed else 1


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(prog="start", description="The portable application of an exported workflow")
    commands = found.add_subparsers(dest="command")
    begin = commands.add_parser("start", help="start the application (default)")
    begin.add_argument("--no-browser", action="store_true", help="do not open the browser")
    begin.add_argument("--port", type=int, default=None)
    begin.add_argument("--host", default=None)
    commands.add_parser("stop", help="stop a running instance")
    commands.add_parser("status", help="whether an instance runs")
    commands.add_parser("open", help="open the browser of a running instance")
    commands.add_parser("configure", help="enter the AI providers' keys")
    key = commands.add_parser("keys", help="API keys of clients")
    key_commands = key.add_subparsers(dest="action", required=True)
    create = key_commands.add_parser("new")
    create.add_argument("--name", default="")
    create.add_argument("--scopes", default="run,read")
    key_commands.add_parser("list")
    revoke = key_commands.add_parser("revoke")
    revoke.add_argument("id")
    test = commands.add_parser("check", help="self-test without starting")
    test.add_argument("--full", action="store_true", help="also check every model file by SHA-256")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings()
    command = args.command or "start"
    if command == "start":
        if not hasattr(args, "no_browser"):
            args = parser().parse_args(["start", *(argv or [])])
        return start(args, settings)
    if command == "stop":
        return stop(settings)
    if command == "status":
        return status(settings)
    if command == "open":
        return open_command(settings)
    if command == "configure":
        return configure(settings)
    if command == "keys":
        return keys(args, settings)
    return check(settings, full=args.full)


if __name__ == "__main__":
    raise SystemExit(main())
