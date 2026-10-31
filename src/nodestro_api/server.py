"""``nodestro-server``: the processes of the server profile (plan step PR-12, ADR-0001 to ADR-0006).

A server is started from its environment (:class:`~nodestro_runtime.server_settings.ServerSettings`, plus
``NODESTRO_PROFILE=server`` and ``NODESTRO_DATA_DIR``). Every command checks the settings first and stops with one
sentence listing every problem (``profile.invalid``); nothing has a default that would weaken it.

``nodestro-server check``
    Settings, database (reachable, schema current) and the container sandbox (Docker reachable); exit code 0 or 1.
``nodestro-server migrate [--backup-dir DIR | --backup-taken]``
    Apply the database migrations. A database that already has tables needs a backup first: ``--backup-dir`` runs
    ``pg_dump`` into that directory, ``--backup-taken`` states that the operator made one.
``nodestro-server api [--host HOST] [--port PORT]``
    The studio API with logins (OpenID Connect), one context per tenant and the built studio interface
    (``NODESTRO_STATIC_DIR``). Runs are queued for workers. Behind a TLS-terminating reverse proxy in production.
``nodestro-server worker``
    Executes queued runs, reaps runs of dead workers and fires schedules (:class:`nodestro_api.worker.ServerWorker`).
``nodestro-server create-tenant ID NAME`` / ``nodestro-server setup-link TENANT``
    Create a tenant, and print a one-time setup link that makes the person who opens it the tenant's first owner.

API and worker processes start only when the database schema is current and Docker is reachable: tool code runs
only in containers on a server (ADR-0006), so a server without a container runtime does not start. Docker may be
local (socket) or remote (``DOCKER_HOST``); containers need no bind mounts
(:class:`nodestro_runtime.container_images.StreamedSandbox`).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from typing import Any

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import RuntimeProfile
from nodestro_runtime.server_settings import ServerSettings

logger = logging.getLogger(__name__)

#: The port the API listens on in its container (a reverse proxy in front of it terminates TLS).
DEFAULT_PORT = 8080


@dataclass(frozen=True)
class ServerEnvironment:
    """The validated profile and settings of a server process."""

    profile: RuntimeProfile
    settings: ServerSettings
    static_dir: Path | None
    docs_dir: Path | None
    docker: str
    #: Model endpoints (origins) tenants may use: ``NODESTRO_MODEL_ENDPOINTS``, comma-separated.
    endpoints: tuple[str, ...] = ()
    #: Worker timing: ``NODESTRO_WORKER_LEASE_SECONDS``, ``…_POLL_SECONDS``, ``…_REAP_SECONDS``.
    worker: dict[str, float] = field(default_factory=dict)


def load(environ: Mapping[str, str] | None = None) -> ServerEnvironment:
    """The server's environment, validated; ``profile.invalid`` names every problem at once."""
    environ = os.environ if environ is None else environ
    problems: list[dict[str, str]] = []
    profile: RuntimeProfile | None = None
    try:
        profile = RuntimeProfile.from_env(environ)
    except NodestroError as error:
        problems += error.params.get("problems", [])
    if profile is not None and profile.kind != "server":
        problems.append(
            {
                "field": "NODESTRO_PROFILE",
                "value": environ.get("NODESTRO_PROFILE", ""),
                "reason": "NODESTRO_PROFILE must be server for nodestro-server",
            }
        )
    settings: ServerSettings | None = None
    try:
        settings = ServerSettings.from_env(environ)
    except NodestroError as error:
        problems += error.params.get("problems", [])
    if problems or profile is None or settings is None:
        raise NodestroError(
            "profile.invalid",
            "The server cannot start: " + "; ".join(item["reason"] for item in problems),
            problems=problems,
        )
    static = environ.get("NODESTRO_STATIC_DIR", "").strip()
    docs = environ.get("NODESTRO_DOCS_DIR", "").strip()
    endpoints = tuple(item.strip() for item in environ.get("NODESTRO_MODEL_ENDPOINTS", "").split(",") if item.strip())
    for item in endpoints:
        if not item.startswith(("https://", "http://")):
            problems.append(
                {
                    "field": "NODESTRO_MODEL_ENDPOINTS",
                    "value": item,
                    "reason": f"NODESTRO_MODEL_ENDPOINTS: {item!r} is not an http(s) address",
                }
            )
    worker: dict[str, float] = {}
    for name, key in (("LEASE", "lease"), ("POLL", "poll"), ("REAP", "reap_every")):
        text = environ.get(f"NODESTRO_WORKER_{name}_SECONDS", "").strip()
        if not text:
            continue
        try:
            worker[key] = float(text)
            if worker[key] <= 0:
                raise ValueError
        except ValueError:
            problems.append(
                {
                    "field": f"NODESTRO_WORKER_{name}_SECONDS",
                    "value": text,
                    "reason": f"NODESTRO_WORKER_{name}_SECONDS must be a positive number of seconds",
                }
            )
    if problems:
        raise NodestroError(
            "profile.invalid",
            "The server cannot start: " + "; ".join(item["reason"] for item in problems),
            problems=problems,
        )
    return ServerEnvironment(
        profile=profile.validate(settings),
        settings=settings,
        static_dir=Path(static) if static else None,
        docs_dir=Path(docs) if docs else None,
        docker=environ.get("NODESTRO_DOCKER", "").strip() or "docker",
        endpoints=endpoints,
        worker=worker,
    )


def _database(server: ServerEnvironment) -> Any:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    schema = os.environ.get("NODESTRO_DATABASE_SCHEMA", "").strip() or None
    try:
        return PostgresDatabase(server.settings.database_url, schema=schema)
    except NodestroError:
        raise
    except Exception as exc:  # psycopg errors name the host but never the password
        raise NodestroError("storage.unavailable", f"The database is not reachable: {type(exc).__name__}") from None


def _require_containers(server: ServerEnvironment) -> None:
    from orkexa.dynamic import DockerSandbox

    if not DockerSandbox.available(server.docker):
        raise NodestroError(
            "sandbox.unavailable",
            "Docker is not reachable (local socket or DOCKER_HOST); a server runs tool code only in containers",
        )


def _ready(server: ServerEnvironment) -> Any:
    """The database, after checking that its schema is current and Docker is reachable."""
    database = _database(server)
    try:
        database.require_current_schema()
        _require_containers(server)
    except BaseException:
        database.close()
        raise
    return database


# ---- the API process --------------------------------------------------------------------------


def create_server_app() -> Any:
    """The studio API of a server, built from the environment (``uvicorn --factory``)."""
    import httpx
    from nodestro_runtime.identity_store import IdentityStore
    from nodestro_runtime.jobs import JobQueue

    from .identity import OidcClient, OidcSettings
    from .server_app import TenantComposition, create_tenant_app

    server = load()
    database = _ready(server)
    settings = server.settings
    oidc = OidcClient(
        OidcSettings(
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.redirect_uri,
        ),
        http=httpx.Client(timeout=15.0),
    )
    return create_tenant_app(
        TenantComposition(
            database=database,
            identity=IdentityStore(database),
            oidc=oidc,
            origin=settings.public_origin,
            data_root=server.profile.data_dir,
            docs_dir=server.docs_dir,
            queue=JobQueue(database),
            secrets=settings.secret_box(),
            static_dir=server.static_dir,
            endpoints=server.endpoints,
        )
    )


# ---- the worker process -----------------------------------------------------------------------


async def run_worker(server: ServerEnvironment) -> None:
    """Serve as a worker until SIGTERM or SIGINT; a running run is finished first."""
    from nodestro_runtime.identity_store import IdentityStore

    from .server_app import TenantFiles
    from .worker import ServerWorker, WorkerSettings

    database = _ready(server)
    files = TenantFiles(
        database=database,
        data_root=server.profile.data_dir,
        docs_dir=server.docs_dir,
        secrets=server.settings.secret_box(),
        endpoints=server.endpoints,
    )
    timing = dict(server.worker)
    settings = WorkerSettings(
        lease=timedelta(seconds=timing.pop("lease")) if "lease" in timing else WorkerSettings.lease,
        **timing,  # type: ignore[arg-type]
    )
    worker = ServerWorker(files, IdentityStore(database), settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    logger.info("Worker %s started", worker.name)
    try:
        await worker.serve(stop)
    finally:
        database.close()
        logger.info("Worker %s stopped", worker.name)


# ---- operator commands ------------------------------------------------------------------------


def check(server: ServerEnvironment) -> list[str]:
    """What keeps this server from starting (empty: ready)."""
    problems: list[str] = []
    try:
        database = _database(server)
    except NodestroError as error:
        return [f"{error.code}: {error}"]
    try:
        database.require_current_schema()
    except NodestroError as error:
        problems.append(f"{error.code}: {error}")
    finally:
        database.close()
    try:
        _require_containers(server)
    except NodestroError as error:
        problems.append(f"{error.code}: {error}")
    return problems


def migrate(server: ServerEnvironment, *, backup_dir: Path | None, backup_taken: bool) -> int:
    """Apply the migrations; returns the version the database had."""
    database = _database(server)

    def before(current: int, target: int) -> None:
        if backup_taken:
            return
        if backup_dir is None:
            raise NodestroError(
                "storage.backup_required",
                f"The database has schema version {current}; take a backup before migrating to {target} "
                "(--backup-dir DIR runs pg_dump, --backup-taken states that you made one)",
            )
        dump(server, backup_dir / f"pre-migration-{current}-to-{target}-{_stamp()}.dump")

    try:
        return int(database.migrate(before_migration=before))
    finally:
        database.close()


def dump(server: ServerEnvironment, target: Path) -> Path:
    """Write a ``pg_dump`` (custom format) of the server's database to ``target``."""
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise NodestroError("storage.backup_failed", "pg_dump is not installed")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    target.parent.mkdir(parents=True, exist_ok=True)
    # The password goes to pg_dump in its environment, never on the command line (visible to other users).
    parts = conninfo_to_dict(server.settings.database_url)
    password = parts.pop("password", None)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NODESTRO_")}
    if password is not None:
        environment["PGPASSWORD"] = str(password)
    completed = subprocess.run(
        [pg_dump, "--format=custom", "--no-owner", f"--file={target}", f"--dbname={make_conninfo('', **parts)}"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        target.unlink(missing_ok=True)
        raise NodestroError("storage.backup_failed", f"pg_dump failed: {completed.stderr.strip()[-500:]}")
    return target


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def setup_link(server: ServerEnvironment, tenant_id: str) -> str:
    """A one-time link (24 hours) that makes the person who logs in with it the first owner of ``tenant_id``."""
    from urllib.parse import urlencode

    from nodestro_runtime.identity_store import IdentityStore

    database = _database(server)
    try:
        database.require_current_schema()
        token = IdentityStore(database).create_bootstrap_token(tenant_id)
    finally:
        database.close()
    return f"{server.settings.public_origin}/api/auth/login?{urlencode({'setup': token})}"


def create_tenant(server: ServerEnvironment, tenant_id: str, name: str) -> None:
    from nodestro_runtime.identity_store import IdentityStore

    database = _database(server)
    try:
        database.require_current_schema()
        IdentityStore(database).create_tenant(tenant_id, name)
    finally:
        database.close()


# ---- command line -----------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(prog="nodestro-server", description="Nodestro server processes")
    commands = found.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Check settings, database and container runtime")
    migrating = commands.add_parser("migrate", help="Apply the database migrations")
    backup = migrating.add_mutually_exclusive_group()
    backup.add_argument("--backup-dir", type=Path, help="Run pg_dump into this directory first")
    backup.add_argument("--backup-taken", action="store_true", help="State that a current backup exists")
    api = commands.add_parser("api", help="Serve the studio API")
    api.add_argument("--host", default=os.environ.get("NODESTRO_HOST", "0.0.0.0"))
    api.add_argument("--port", type=int, default=int(os.environ.get("NODESTRO_PORT", str(DEFAULT_PORT))))
    commands.add_parser("worker", help="Execute queued runs, reap and fire schedules")
    backing_up = commands.add_parser("backup", help="Write a pg_dump of the database into a directory")
    backing_up.add_argument("directory", type=Path)
    tenant = commands.add_parser("create-tenant", help="Create a tenant")
    tenant.add_argument("tenant_id")
    tenant.add_argument("name")
    link = commands.add_parser("setup-link", help="Print a one-time link that binds the first owner of a tenant")
    link.add_argument("tenant_id")
    return found


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        server = load()
        if args.command == "check":
            problems = check(server)
            for problem in problems:
                print(f"not ready: {problem}", file=sys.stderr)
            if problems:
                raise SystemExit(1)
            print("ready")
        elif args.command == "migrate":
            before = migrate(server, backup_dir=args.backup_dir, backup_taken=args.backup_taken)
            print(f"migrated (schema version before: {before})")
        elif args.command == "api":
            import uvicorn

            _ready(server).close()  # stop with one sentence before uvicorn starts
            uvicorn.run(
                "nodestro_api.server:create_server_app",
                factory=True,
                host=args.host,
                port=args.port,
                log_level="info",
                proxy_headers=True,
                server_header=False,
            )
        elif args.command == "worker":
            asyncio.run(run_worker(server))
        elif args.command == "backup":
            print(dump(server, args.directory / f"nodestro-{_stamp()}.dump"))
        elif args.command == "create-tenant":
            create_tenant(server, args.tenant_id, args.name)
            print(f"created tenant {args.tenant_id}")
        elif args.command == "setup-link":
            print(setup_link(server, args.tenant_id))
    except NodestroError as error:
        raise SystemExit(f"{error.code}: {error}") from None


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "DEFAULT_PORT",
    "ServerEnvironment",
    "check",
    "create_server_app",
    "create_tenant",
    "dump",
    "load",
    "main",
    "migrate",
    "run_worker",
    "setup_link",
]
