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
    return f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"


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
    from .local_container import require_safe_configuration

    try:
        config = AppConfig.from_env()
        kind = config.profile.validate().kind
        if kind == "desktop":
            require_loopback_only(args.host, config.allowed_hosts)
        elif kind == "local-container":
            require_safe_configuration(config.access_key, config.allowed_hosts, config.browser_host)
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
    require_free_port(args)

    if args.reload or ready is None:
        if ready is not None:
            ready()  # the reloader binds in another process
        uvicorn.run(factory, factory=True, host=args.host, port=args.port, reload=args.reload, log_level="info")
        return
    server = uvicorn.Server(uvicorn.Config(factory, factory=True, host=args.host, port=args.port, log_level="info"))
    startup = server.startup

    async def started(sockets: list[socket.socket] | None = None) -> None:
        await startup(sockets=sockets)
        if server.started:
            ready()

    server.startup = started  # type: ignore[method-assign]
    # Recovery mode and the diagnosis may ask to be started again (TABRION MIG-NOD-9): the server stops as on Ctrl+C,
    # the application's shutdown runs, and the process ends with the requested code for the desktop shell.
    recovery.EXIT.bind(lambda: setattr(server, "should_exit", True))
    try:
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
        ready=lambda: print(
            f"Nodestro API is running at {url}/api  (reference: {url}/api/docs, stop with Ctrl+C)", flush=True
        ),
    )


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
