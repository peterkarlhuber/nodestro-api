"""Nodestro Local Docker: the studio of one person, in a container, used from the host's browser.

The ``local-container`` profile (:mod:`nodestro_runtime.profile`) runs the same studio as the desktop on SQLite,
but the process is in a container: it has to listen on the container's interfaces (``0.0.0.0``) so that Docker can
publish its port, and Docker publishes it on the host's loopback only (``127.0.0.1:<port>``, see the
``nodestro-local-docker`` repository). A container is not the person's computer, so the desktop's protections are
not switched off but replaced by stricter ones:

- **An access key** (256 bits, ``NODESTRO_ACCESS_KEY_FILE``, created on first start with mode ``0600``) is required
  for every request except the health check and the key-protected published APIs. The browser exchanges it once per
  session through the link ``nodestro-local link`` prints (``/api/launch?token=...``) for an ``HttpOnly``,
  ``SameSite=Strict`` session cookie; scripts send it as ``Authorization: Bearer``. Browser sessions exist only under
  the installation's random host name ``nodestro-<random>.localhost`` (cookies are scoped by host name, not by port,
  so a cookie for ``localhost`` would also reach every other local web server), end after 8 hours without a request
  or 24 hours at most, and end with ``/api/logout``. Other users and programs of the
  host, other containers and (on Linux) other hosts that can route to the container's address have no key.
- **Host and Origin checks without exceptions**: only loopback host names are answered (``localhost``,
  ``127.0.0.1``, ``[::1]``; not the container's addresses or name, so DNS rebinding and requests to the container's
  address are refused), forwarded requests are refused, and changing requests and WebSockets are accepted only from
  the exact origin they were sent to (a page of another program on ``localhost`` is the same *site* and would get the
  cookie, but has another origin). ``NODESTRO_ALLOWED_HOSTS`` may only add loopback names.
- **No network listener for published APIs** (its port would be inside the container), no CORS for development
  servers, and tool code that needs isolation runs only in sandbox containers through the sandbox gateway
  (:mod:`nodestro_runtime.sandbox_gateway`), never in this container.

The ``nodestro-local`` command runs inside the container (the launcher on the host calls it with
``docker compose exec``): ``serve`` starts the studio, ``link`` prints the browser link, ``rotate-key`` replaces the
access key and the browser host name, ``logout`` ends every browser session, ``backup`` and ``restore`` use the
studio's own complete backups (:mod:`nodestro_api.backup`) through the running API, ``status`` and ``check`` report
readiness, and ``gateway`` runs the sandbox gateway (the same image, another container).
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Any
import urllib.error
import urllib.request
import uuid

from nodestro_runtime.problems import NodestroError

from .launch import LAUNCH_PATH, MIN_LENGTH
from .local_access import is_loopback, split_host

#: Where the access key is read from (and created on first start by ``nodestro-local serve``).
ENV_KEY_FILE = "NODESTRO_ACCESS_KEY_FILE"
DEFAULT_KEY_FILE = "/var/lib/nodestro/secrets/access-key"
#: The browser's host name: a random ``*.localhost`` name per installation, in a file next to the access key
#: (``docs/SESSIONS.md`` in nodestro-local-docker). ``NODESTRO_BROWSER_HOST`` replaces it with another loopback
#: name, which gives up its protection.
BROWSER_HOST_FILE = "browser-host"
ENV_BROWSER_HOST = "NODESTRO_BROWSER_HOST"
_BROWSER_HOST = re.compile(r"nodestro-[a-z0-9]{10}\.localhost")
_HOST_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
#: The port Docker publishes on the host's loopback (set by compose.yaml).
ENV_HOST_PORT = "NODESTRO_HOST_PORT"
DEFAULT_HOST_PORT = 8765
#: How long a browser session lasts: hours without a request, and hours after it was opened.
ENV_SESSION_IDLE = "NODESTRO_SESSION_IDLE_HOURS"
ENV_SESSION_MAX = "NODESTRO_SESSION_MAX_HOURS"
DEFAULT_SESSION_IDLE_HOURS = 8.0
DEFAULT_SESSION_MAX_HOURS = 24.0
#: Where the studio listens inside the container.
CONTAINER_PORT = 8080
_BASE = f"http://127.0.0.1:{CONTAINER_PORT}"


# ---- the access key ---------------------------------------------------------------------------------------------


def key_file(environ: dict[str, str] | None = None) -> Path:
    environ = dict(os.environ) if environ is None else environ
    return Path(environ.get(ENV_KEY_FILE, "").strip() or DEFAULT_KEY_FILE)


def read_access_key(path: Path) -> str:
    """The key in ``path``; refuses a missing, short or group/world-readable file (``access.key_invalid``)."""
    try:
        info = path.stat()
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise NodestroError(
            "access.key_invalid", f"The access key file {path} cannot be read: {exc}", path=str(path)
        ) from None
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise NodestroError(
            "access.key_invalid",
            f"The access key file {path} must be readable by its owner only (mode 0600)",
            path=str(path),
        )
    if len(text) < MIN_LENGTH:
        raise NodestroError(
            "access.key_invalid", f"The access key in {path} is shorter than {MIN_LENGTH} characters", path=str(path)
        )
    return text


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 0600, replacing it atomically."""
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(text + "\n")
    os.replace(staging, path)


def create_access_key(path: Path, *, replace: bool = False) -> str:
    """Write a new random key (256 bits) with mode 0600; keeps an existing one unless ``replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists() and not replace:
        return read_access_key(path)
    key = secrets.token_urlsafe(32)
    _write_private(path, key)
    return key


def access_key_from_env() -> str | None:
    """The access key when ``NODESTRO_ACCESS_KEY_FILE`` names one (read when the app starts)."""
    if not os.environ.get(ENV_KEY_FILE, "").strip():
        return None
    return read_access_key(key_file())


# ---- the browser's host name ----------------------------------------------------------------------------------


def browser_host_file(environ: dict[str, str] | None = None) -> Path:
    """The file with the browser's host name: next to the access key."""
    return key_file(environ).with_name(BROWSER_HOST_FILE)


def read_browser_host(path: Path) -> str:
    """The host name in ``path``; refuses a missing, malformed or group/world-readable file
    (``access.browser_host_invalid``)."""
    try:
        info = path.stat()
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise NodestroError(
            "access.browser_host_invalid", f"The host name file {path} cannot be read: {exc}", path=str(path)
        ) from None
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise NodestroError(
            "access.browser_host_invalid",
            f"The host name file {path} must be readable by its owner only (mode 0600)",
            path=str(path),
        )
    if not _BROWSER_HOST.fullmatch(text):
        raise NodestroError(
            "access.browser_host_invalid", f"The host name in {path} is not a Nodestro host name", path=str(path)
        )
    return text


def create_browser_host(path: Path, *, replace: bool = False) -> str:
    """Write a new random ``nodestro-<10 letters and digits>.localhost`` (about 51 bits) with mode 0600; keeps an
    existing one unless ``replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        return read_browser_host(path)
    name = "nodestro-" + "".join(secrets.choice(_HOST_ALPHABET) for _ in range(10)) + ".localhost"
    _write_private(path, name)
    return name


def is_private_host(host: str) -> bool:
    """Whether ``host`` is a random Nodestro host name (and not a name other local programs share)."""
    return _BROWSER_HOST.fullmatch(host) is not None


def browser_host_from_env(environ: dict[str, str] | None = None) -> str | None:
    """The browser's host name: ``NODESTRO_BROWSER_HOST`` when set (a loopback name or address), otherwise the file
    next to ``NODESTRO_ACCESS_KEY_FILE``; ``None`` when neither exists."""
    environ = dict(os.environ) if environ is None else environ
    override = environ.get(ENV_BROWSER_HOST, "").strip().lower().strip("[]")
    if override:
        if override.count(":") == 1 or not is_loopback(override):
            raise NodestroError(
                "access.network_needs_login",
                f"{ENV_BROWSER_HOST} must be a loopback name or address without a port; refused: {override}",
                host=override,
                hosts=[override],
            )
        return override
    if not environ.get(ENV_KEY_FILE, "").strip():
        return None
    path = browser_host_file(environ)
    return read_browser_host(path) if path.exists() else None


def _number_from_env(environ: dict[str, str], name: str, default: float, *, integer: bool = False) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        value = 0
    if value <= 0 or (integer and value > 65535):
        raise NodestroError("config.invalid", f"{name} must be a positive number; got {raw!r}", name=name)
    return value


def host_port_from_env(environ: dict[str, str] | None = None) -> int:
    """The port Docker publishes on the host (``NODESTRO_HOST_PORT``, default 8765)."""
    environ = dict(os.environ) if environ is None else environ
    return int(_number_from_env(environ, ENV_HOST_PORT, DEFAULT_HOST_PORT, integer=True))


def session_hours_from_env(environ: dict[str, str] | None = None) -> tuple[float, float]:
    """The idle and maximum hours of a browser session (``NODESTRO_SESSION_IDLE_HOURS``, ``_MAX_HOURS``)."""
    environ = dict(os.environ) if environ is None else environ
    return (
        _number_from_env(environ, ENV_SESSION_IDLE, DEFAULT_SESSION_IDLE_HOURS),
        _number_from_env(environ, ENV_SESSION_MAX, DEFAULT_SESSION_MAX_HOURS),
    )


# ---- the configuration ------------------------------------------------------------------------------------------


def require_safe_configuration(access_key: str | None, extra_hosts: Iterable[str], browser_host: str | None) -> None:
    """Refuse a local container without access key or browser host name, or with non-loopback host names
    (``access.key_required``, ``access.browser_host_required``, ``access.network_needs_login``)."""
    if access_key is None:
        raise NodestroError(
            "access.key_required",
            f"Nodestro Local Docker needs an access key ({ENV_KEY_FILE}); start it with nodestro-local serve",
        )
    if browser_host is None or not is_loopback(browser_host):
        raise NodestroError(
            "access.browser_host_required",
            "Nodestro Local Docker needs its browser host name (a loopback name next to the access key, or "
            f"{ENV_BROWSER_HOST}); start it with nodestro-local serve",
        )
    others = [item.strip() for item in extra_hosts if item.strip() and not is_loopback(split_host(item.strip()))]
    if others:
        raise NodestroError(
            "access.network_needs_login",
            "Nodestro Local Docker is only offered on this computer's loopback; refused: "
            f"NODESTRO_ALLOWED_HOSTS={','.join(others)}",
            host="0.0.0.0",
            hosts=others,
        )


def loopback_hosts(extra_hosts: Iterable[str]) -> frozenset[str]:
    """The host names answered besides the loopback addresses: the loopback names in ``NODESTRO_ALLOWED_HOSTS``."""
    return frozenset(split_host(item) for item in extra_hosts if item.strip())


REFUSAL = (
    "This is Nodestro Local Docker. Open it with the link that the launcher shows "
    "(nodestro-local open, or: docker compose exec nodestro nodestro-local link)."
)


# ---- talking to the running studio ------------------------------------------------------------------------------


def _request(
    method: str,
    path: str,
    key: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 600.0,
) -> tuple[int, bytes]:
    request = urllib.request.Request(  # a fixed loopback URL
        _BASE + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {key}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        raise SystemExit(f"Nodestro is not running in this container ({error})") from None


def _detail(body: bytes) -> str:
    try:
        found = json.loads(body)
    except ValueError:
        return body[:300].decode("utf-8", "replace")
    return str(found.get("detail") or found)


def backup(key: str, output: str) -> str:
    status, body = _request("GET", "/api/backup", key)
    if status != 200:
        raise SystemExit(f"The backup failed (HTTP {status}): {_detail(body)}")
    if output == "-":
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()
        return "-"
    Path(output).write_bytes(body)
    return output


def restore(key: str, source: str, *, file_name: str = "backup.zip") -> dict[str, Any]:
    data = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    boundary = uuid.uuid4().hex
    form = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    status, body = _request(
        "POST",
        "/api/backup/inspect",
        key,
        data=form,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if status != 200:
        raise SystemExit(f"The backup was not accepted (HTTP {status}): {_detail(body)}")
    info = json.loads(body)
    status, body = _request(
        "POST",
        "/api/backup/restore",
        key,
        data=json.dumps({"id": info["id"], "confirm": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    if status != 200:
        raise SystemExit(f"The restore failed (HTTP {status}): {_detail(body)}")
    result: dict[str, Any] = json.loads(body)
    return result


def status_report(key: str) -> dict[str, Any]:
    code, body = _request("GET", "/api/health", key, timeout=10)
    report: dict[str, Any] = {"running": code == 200}
    code, body = _request("GET", "/api/settings", key, timeout=30)
    if code == 200:
        settings = json.loads(body)
        report["sandbox"] = settings.get("sandbox")
        report["sandbox_containers"] = bool(settings.get("docker_available"))
    return report


def launch_link(key: str, *, host: str | None = None, port: int | None = None) -> str:
    """The browser link; warns on standard error when the host name is one that other local programs share."""
    try:
        name = host if host is not None else browser_host_from_env({**os.environ, ENV_KEY_FILE: str(key_file())})
        port = port if port is not None else host_port_from_env()
    except NodestroError as error:
        raise SystemExit(str(error)) from None
    if name is None:
        raise SystemExit("Nodestro has not created its browser host name yet; start it with nodestro-local serve")
    name = name.strip("[]").lower()
    if not is_loopback(name):
        raise SystemExit("Nodestro Local Docker is only reachable through the host's loopback")
    if not is_private_host(name):
        print(
            f"Warning: the host name {name} is shared with other programs on this computer, which can then receive "
            "the session cookie. Leave NODESTRO_BROWSER_HOST unset unless the browser cannot open *.localhost names.",
            file=sys.stderr,
        )
    shown = f"[{name}]" if ":" in name else name
    return f"http://{shown}:{port}{LAUNCH_PATH}?token={key}"


# ---- the command ------------------------------------------------------------------------------------------------


def _serve(args: argparse.Namespace) -> None:
    from .cli import serve

    path = key_file()
    create_access_key(path)
    create_browser_host(browser_host_file())
    os.environ[ENV_KEY_FILE] = str(path)
    os.environ["NODESTRO_PROFILE"] = "local-container"
    os.environ["NODESTRO_HOST"] = "0.0.0.0"  # the container's interfaces; published on loopback
    print(
        "Nodestro Local Docker is starting. Open it with the link from: nodestro-local link --port <host port>",
        flush=True,
    )
    serve(
        "nodestro_studio.app:create_app",
        argparse.Namespace(host="0.0.0.0", port=CONTAINER_PORT, reload=False),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nodestro-local", description="Nodestro Local Docker (inside the container)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="Start the studio (the container's main process)")
    link = commands.add_parser("link", help="Print the link that opens Nodestro in the browser")
    link.add_argument("--port", type=int, default=None, help=f"The published port (default: {ENV_HOST_PORT} or 8765)")
    link.add_argument("--host", default=None, help="The host's loopback name or address (default: the browser host)")
    commands.add_parser(
        "rotate-key", help="Replace the access key and the browser host name (restart the container afterwards)"
    )
    commands.add_parser("logout", help="End every browser session of the running studio")
    make = commands.add_parser("backup", help="Write a complete backup (ZIP) of the running studio")
    make.add_argument("output", nargs="?", default="-", help="File, or - for standard output (default)")
    back = commands.add_parser("restore", help="Restore a backup (ZIP) into the running studio")
    back.add_argument("source", nargs="?", default="-", help="File, or - for standard input (default)")
    commands.add_parser("status", help="Report whether the studio and the sandbox containers are ready")
    commands.add_parser("check", help="Health check for Docker (exit code 0 when ready)")
    gate = commands.add_parser("gateway", help="Run the sandbox gateway (its own container)")
    gate.add_argument("--listen", default="/run/nodestro-sandbox/docker.sock")
    gate.add_argument("--docker", default=os.environ.get("NODESTRO_SANDBOX_DOCKER", "/var/run/docker.sock"))
    args = parser.parse_args(argv)

    if args.command == "serve":
        _serve(args)
        return 0
    if args.command == "gateway":
        from nodestro_runtime import sandbox_gateway

        return sandbox_gateway.main(["--listen", args.listen, "--docker", args.docker])
    if args.command == "rotate-key":
        create_access_key(key_file(), replace=True)
        create_browser_host(browser_host_file(), replace=True)
        print(
            "A new access key and browser host name were created. Restart Nodestro; open it with the new link "
            "afterwards."
        )
        return 0
    try:
        key = read_access_key(key_file())
    except NodestroError as error:
        raise SystemExit(str(error)) from None
    if args.command == "link":
        print(launch_link(key, host=args.host, port=args.port))
        return 0
    if args.command == "logout":
        code, body = _request("POST", "/api/logout?all=1", key, timeout=30)
        if code != 200:
            raise SystemExit(f"Signing out failed (HTTP {code}): {_detail(body)}")
        print(f"Every browser session was ended ({json.loads(body).get('ended', 0)}).")
        return 0
    if args.command == "backup":
        written = backup(key, args.output)
        if written != "-":
            print(f"Backup written to {written}")
        return 0
    if args.command == "restore":
        result = restore(key, args.source)
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 0
    if args.command == "status":
        print(json.dumps(status_report(key), indent=2))
        return 0
    # check: the studio answers and the sandbox containers can be reached
    report = status_report(key)
    return 0 if report.get("running") else 1


__all__ = [
    "CONTAINER_PORT",
    "DEFAULT_HOST_PORT",
    "ENV_BROWSER_HOST",
    "ENV_HOST_PORT",
    "ENV_KEY_FILE",
    "REFUSAL",
    "access_key_from_env",
    "browser_host_file",
    "browser_host_from_env",
    "create_access_key",
    "create_browser_host",
    "host_port_from_env",
    "is_private_host",
    "key_file",
    "launch_link",
    "loopback_hosts",
    "main",
    "read_access_key",
    "read_browser_host",
    "require_safe_configuration",
    "session_hours_from_env",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
