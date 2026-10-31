"""``nodestro-api``: start the HTTP API without the graphical studio.

The options are the ones of ``nodestro`` (the studio uses :func:`serve` as well), except that
no browser is opened. Data, settings and keys are shared with the studio (``~/.nodestro``).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def parser(prog: str, description: str) -> argparse.ArgumentParser:
    """The command line options shared by ``nodestro`` and ``nodestro-api``."""
    found = argparse.ArgumentParser(prog=prog, description=description)
    found.add_argument("--host", default=os.environ.get("NODESTRO_HOST", "127.0.0.1"))
    found.add_argument("--port", type=int, default=int(os.environ.get("NODESTRO_PORT", "8000")))
    found.add_argument("--data-dir", help="Where workflows, runs and knowledge are stored (default ~/.nodestro)")
    found.add_argument(
        "--profile",
        default=None,
        help="Operating profile: desktop (default) or server (not available yet); also NODESTRO_PROFILE",
    )
    found.add_argument("--reload", action="store_true", help="Restart on code changes (development)")
    return found


def prepare(args: argparse.Namespace) -> str:
    """Apply ``--data-dir``, ``--profile`` and ``.env``; returns the URL the server will listen on.

    Stops when the operating profile cannot be started (before anything is created on disk) and when
    another Nodestro process already uses the data directory.
    """
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


def serve(factory: str, args: argparse.Namespace) -> None:
    """Run ``factory`` (``module:function`` returning the FastAPI app) with uvicorn.

    Stops with one sentence instead of a traceback when the studio API would be offered to the network
    without a login (``create_app`` refuses it as well).
    """
    from nodestro_runtime.problems import NodestroError
    import uvicorn

    from .context import AppConfig
    from .local_access import require_loopback_only

    try:
        config = AppConfig.from_env()
        if config.profile.validate().kind == "desktop":
            require_loopback_only(args.host, config.allowed_hosts)
    except NodestroError as error:
        raise SystemExit(str(error)) from None

    uvicorn.run(factory, factory=True, host=args.host, port=args.port, reload=args.reload, log_level="info")


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
    print(f"Nodestro API is running at {url}/api  (reference: {url}/api/docs, stop with Ctrl+C)")
    serve("nodestro_api.app:create_app", args)


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
