"""``nodestro-api``: start the HTTP API without the graphical studio.

The options are the ones of ``nodestro`` (the studio uses :func:`serve` as well), except that
no browser is opened. Data, settings and keys are shared with the studio (``~/.nodestro``).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import os
from pathlib import Path
import socket
import sys
from typing import Any

#: The sockets of ``NODESTRO_BIND_LOOPBACK`` (:func:`prepare`): the listener and the ``[::1]`` guard, held while the
#: process runs.
_bound: Any = None


def parser(prog: str, description: str) -> argparse.ArgumentParser:
    """The command line options shared by ``nodestro`` and ``nodestro-api``."""
    found = argparse.ArgumentParser(prog=prog, description=description)
    found.add_argument("--host", default=os.environ.get("NODESTRO_HOST", "127.0.0.1"))
    found.add_argument("--port", type=int, default=int(os.environ.get("NODESTRO_PORT", "8000")))
    found.add_argument("--data-dir", help="Where workflows, runs and knowledge are stored (default ~/.nodestro)")
    found.add_argument(
        "--profile",
        default=None,
        help="Operating profile: desktop (default) or local-container (Nodestro Local Docker, use nodestro-local); "
        "a server starts with nodestro-server; also NODESTRO_PROFILE",
    )
    found.add_argument("--reload", action="store_true", help="Restart on code changes (development)")
    found.add_argument(
        "--secrets-fd",
        type=int,
        default=None,
        metavar="FD",
        help="Read the launch secret and the MCP token vault key as one JSON object from this inherited file "
        "descriptor (0: standard input) and close it; the desktop app uses it instead of environment variables",
    )
    return found


def prepare(args: argparse.Namespace) -> str:
    """Apply ``--data-dir``, ``--profile`` and ``.env``; returns the URL the server will listen on.

    Stops when the operating profile cannot be started (before anything is created on disk) and when
    another Nodestro process already uses the data directory.

    The secrets on ``--secrets-fd`` are read first, before anything else runs (:mod:`nodestro_api.handoff`).

    Two hardening switches of the desktop profile act here (NS-002, both off by default):
    ``NODESTRO_TERMINAL_LAUNCH=1`` issues a launch secret when there is none (:func:`terminal_launch`), and
    ``NODESTRO_BIND_LOOPBACK=1`` listens on a random ``127.a.b.c`` with ``[::1]`` held on the same port; the URL
    returned is then that address.
    """
    _receive_secrets(getattr(args, "secrets_fd", None))
    if args.data_dir:
        os.environ["NODESTRO_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    if getattr(args, "profile", None):
        os.environ["NODESTRO_PROFILE"] = args.profile
    # The application answers only the host names it is reachable under (nodestro_api.local_access).
    os.environ["NODESTRO_HOST"] = args.host
    load_dotenv(Path.cwd() / ".env")
    _refuse_an_unavailable_profile()
    _refuse_a_used_data_directory()
    hardening = _desktop_hardening()
    if hardening is not None and hardening.terminal:
        _issue_terminal_secret()
    if hardening is not None and hardening.bind_loopback and args.host in ("127.0.0.1", "localhost"):
        return _bind_loopback(args)
    return f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"


def _desktop_hardening() -> Any:
    """The NS-002 switches of the desktop profile (:class:`nodestro_api.launch.LaunchHardening`); ``None`` in another
    profile. Stops with one sentence when a switch has an invalid value."""
    from nodestro_runtime.problems import NodestroError

    from .context import AppConfig

    try:
        config = AppConfig.from_env()
    except NodestroError as error:
        raise SystemExit(str(error)) from None
    return config.hardening if config.profile_kind == "desktop" else None


def _issue_terminal_secret() -> None:
    """``NODESTRO_TERMINAL_LAUNCH``: without the desktop app's or an operator's secret, this process issues its own,
    so that only the browser that opens the launch link (:func:`terminal_launch`) can use the studio."""
    from . import launch

    if launch.launch_secret_from_env() is None:
        launch.issue_terminal_secret()


def terminal_launch(url: str, *, write_file: bool) -> tuple[str, Path | None] | None:
    """The launch link of the secret this process issued (``NODESTRO_TERMINAL_LAUNCH``), and with ``write_file`` a
    0600 redirect file that opens it (``None`` when the data directory's ``launch`` folder is not private); ``None``
    without such a secret. Open the browser with the file's ``file://`` path, never with the link: a browser's command
    line is readable by every local user."""
    from nodestro_runtime.problems import NodestroError
    from nodestro_session.private import UnsafePathError

    from . import launch
    from .context import AppConfig

    secret = launch._terminal
    if secret is None or launch.launch_secret_from_env() != secret:
        return None
    try:
        config = AppConfig.from_env()
    except NodestroError:
        return None
    link = launch.launch_link(url, secret, fragment=config.hardening.fragment)
    if not write_file:
        return link, None
    try:
        return link, launch.write_launch_file(link, config.data_dir)
    except (OSError, UnsafePathError) as error:
        print(f"No launch file was written ({error}); open the link below yourself.", file=sys.stderr)
        return link, None


def _bind_loopback(args: argparse.Namespace) -> str:
    """``NODESTRO_BIND_LOOPBACK``: listen on a random ``127.a.b.c:<port>`` and hold ``[::1]:<port>``
    (``nodestro_session.bind_loopback``); a fresh origin on each start, and no other program can answer for this port
    on ``localhost``. Stops with exit code 2 when the port is taken, and with one sentence together with ``--reload``
    (the reloading child process would bind on its own)."""
    from nodestro_session.bind import PortUnavailableError, bind_loopback

    global _bound
    if args.reload:
        raise SystemExit("NODESTRO_BIND_LOOPBACK=1 cannot be used with --reload")
    try:
        _bound = bind_loopback(args.port)
    except PortUnavailableError as error:
        print(f"Port {args.port} is not available on the loopback addresses ({error}).", file=sys.stderr)
        raise SystemExit(2) from None
    args.host = _bound.host
    args.port = _bound.port
    os.environ["NODESTRO_HOST"] = _bound.host
    return f"http://{_bound.host}:{_bound.port}"


def port_problem(host: str, port: int) -> str | None:
    """Why the server cannot listen on ``host:port`` (``None``: it can). Checked before anything says "running"."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if os.name == "posix":  # as uvicorn binds: a port in TIME_WAIT is free, a listening one is not
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError as error:
        if error.errno in (98, 48, 10048):  # EADDRINUSE on Linux, macOS, Windows
            return (
                f"Port {port} on {host} is already in use (another Nodestro or another program). "
                "Start with --port <number> or stop the other program."
            )
        return f"Cannot listen on {host}:{port}: {error.strerror or error}"
    return None


def require_free_port(args: argparse.Namespace) -> None:
    """Exit with code 2 and one sentence when the port is taken (DX handover B2)."""
    problem = port_problem(args.host, args.port)
    if problem is not None:
        print(problem, file=sys.stderr)
        raise SystemExit(2)


def serve(factory: str, args: argparse.Namespace, ready: Callable[[], None] | None = None) -> None:
    """Run ``factory`` (``module:function`` returning the FastAPI app) with uvicorn.

    ``ready`` is called once the server listens (it prints "running at" and opens the browser), never when the
    start fails. Stops with one sentence instead of a traceback when the studio API would be offered to the
    network without a login (``create_app`` refuses it as well).
    """
    from nodestro_runtime.mcp.vault import vault_box_from_env
    from nodestro_runtime.problems import NodestroError
    import uvicorn

    from . import recovery
    from .context import AppConfig
    from .handoff import VAULT_KEY_ENV, received
    from .launch import check_secret
    from .local_access import require_loopback_only
    from .local_container import require_local_vault_key, require_safe_configuration

    try:
        config = AppConfig.from_env()
        kind = config.profile.validate().kind
        if kind == "desktop":
            require_loopback_only(args.host, config.allowed_hosts)
        elif kind == "local-container":
            require_safe_configuration(config.access_key, config.allowed_hosts, config.browser_host)
            require_local_vault_key(received())
        check_secret(config.launch_secret)
        check_secret(config.access_key)
        if config.vault_key is not None:
            # A malformed key is refused before the server starts, as the workspace would refuse it.
            vault_box_from_env({VAULT_KEY_ENV: config.vault_key})
        if (config.launch_secret is not None or received() is not None) and args.reload:
            # The reloading child process would start without the secrets, i.e. unprotected.
            raise NodestroError(
                "access.launch_secret_reload", "--reload cannot be used with NODESTRO_LAUNCH_SECRET or --secrets-fd"
            )
    except NodestroError as error:
        raise SystemExit(str(error)) from None
    # After the refusals above (a network address without a login is refused as such, not as a busy port) and
    # before anything says "running": a taken port stops here with one sentence, exit code 2 (DX handover B2).
    # With NODESTRO_BIND_LOOPBACK the port is held already (prepare).
    if _bound is None:
        require_free_port(args)

    if (args.reload or ready is None) and _bound is None:
        if ready is not None:
            ready()  # the reloader binds in another process
        uvicorn.run(factory, factory=True, host=args.host, port=args.port, reload=args.reload, log_level="info")
        return
    server = uvicorn.Server(uvicorn.Config(factory, factory=True, host=args.host, port=args.port, log_level="info"))
    startup = server.startup

    async def started(sockets: list[socket.socket] | None = None) -> None:
        await startup(sockets=sockets)
        if server.started and ready is not None:
            ready()

    server.startup = started  # type: ignore[method-assign]
    # Recovery mode and the diagnosis may ask to be started again (TABRION MIG-NOD-9): the server stops as on Ctrl+C,
    # the application's shutdown runs, and the process ends with the requested code for the desktop shell.
    recovery.EXIT.bind(lambda: setattr(server, "should_exit", True))
    try:
        if _bound is not None:
            server.run(sockets=[_bound.listener])
        else:
            server.run()
    finally:
        code = recovery.EXIT.code
        recovery.EXIT.bind(None)
    if code is not None:
        print(f"Nodestro stopped to start again (exit code {code})", flush=True)
        raise SystemExit(code)


def _receive_secrets(fd: int | None) -> None:
    """Read the desktop app's handoff once; stop with one sentence when it is not usable."""
    from nodestro_runtime.problems import NodestroError

    from . import handoff

    try:
        handoff.receive(fd)
    except NodestroError as error:
        raise SystemExit(str(error)) from None


def _refuse_an_unavailable_profile() -> None:
    """Stop with one sentence when ``NODESTRO_PROFILE``/``NODESTRO_DATA_DIR`` name a profile that cannot start."""
    from nodestro_runtime.problems import NodestroError

    from .context import AppConfig

    try:
        AppConfig.from_env().profile.validate()
    except NodestroError as error:
        raise SystemExit(str(error)) from None


def _refuse_a_used_data_directory() -> None:
    """Stop with a clear sentence when another Nodestro process already uses the data directory.

    The workspace refuses it anyway (see ``nodestro_runtime.workspace.DataDirectoryLock``); checking
    here first replaces the server's traceback by one line.
    """
    from nodestro_runtime.problems import NodestroError
    from nodestro_runtime.workspace import DataDirectoryLock

    from .context import AppConfig

    data_dir = AppConfig.from_env().data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        DataDirectoryLock(data_dir).release()
    except NodestroError as error:
        raise SystemExit(str(error)) from None


def main(argv: list[str] | None = None) -> None:
    args = parser("nodestro-api", "Nodestro API - the HTTP backend without the studio").parse_args(argv)
    url = prepare(args)
    serve(
        "nodestro_api.app:create_app",
        args,
        # flush: a parent that reads the output through a pipe (the desktop, a script) sees it at once.
        ready=lambda: _announce(url),
    )


def _announce(url: str) -> None:
    print(f"Nodestro API is running at {url}/api  (reference: {url}/api/docs, stop with Ctrl+C)", flush=True)
    found = terminal_launch(url, write_file=False)
    if found is not None:
        # NODESTRO_TERMINAL_LAUNCH: the link works once; it is printed to this terminal only.
        print(f"Open it once with this link: {found[0]}", flush=True)


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines (e.g. ANTHROPIC_API_KEY) without overriding the environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


if __name__ == "__main__":  # pragma: no cover
    main()
