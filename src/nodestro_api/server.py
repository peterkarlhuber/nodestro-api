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
``nodestro-server members TENANT`` / ``set-role TENANT USER ROLE`` / ``remove-member TENANT USER`` /
``revoke-sessions USER``
    Membership of a tenant (A-08): list it, add a member or change a role, remove a member (their sessions in the
    tenant end with it), and end every session of a user at once. ``USER`` is a user id (``u_…``), a verified
    e-mail address or the subject at the configured identity provider. A person is known once they logged in at
    least once (a login alone grants nothing); a tenant always keeps at least one owner.

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
import re
import shutil
import signal
import subprocess
import sys
from typing import Any

from nodestro_runtime.bedrock import BedrockOperatorConfig
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import RuntimeProfile
from nodestro_runtime.server_settings import ServerSettings

from .pricing_setup import PRICE_REFERENCE_TENANT, PriceSyncConfig

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
    #: Worker timing: ``NODESTRO_WORKER_LEASE_SECONDS``, ``…_POLL_SECONDS``, ``…_REAP_SECONDS``,
    #: ``…_CLEANUP_SECONDS``.
    worker: dict[str, float] = field(default_factory=dict)
    #: The reference price sync of the workers (``NODESTRO_PRICE_*``, see :mod:`nodestro_api.pricing_setup`).
    price_sync: PriceSyncConfig = field(default_factory=PriceSyncConfig)
    #: Runs one worker holds and how many of them may wait for a tool approval (``NODESTRO_WORKER_MAX_RUNS``,
    #: ``NODESTRO_WORKER_MAX_WAITING``; defaults 2 and 1, see :func:`nodestro_runtime.jobs.check_capacity`).
    capacity: dict[str, int] = field(default_factory=dict)
    #: The operator's Amazon Bedrock settings (``NODESTRO_BEDROCK_TENANT_ROLES[_FILE]``,
    #: ``NODESTRO_BEDROCK_DATA_RESIDENCY``, ``NODESTRO_BEDROCK_ALLOWED_REGIONS``): which tenant may assume which IAM
    #: role. ``None`` or no role for a tenant: Bedrock is not available to it (the server's own AWS chain is never
    #: used for a tenant). Each tenant's workspace receives only its own entry.
    bedrock: BedrockOperatorConfig | None = None


def _capacity(environ: Mapping[str, str], problems: list[dict[str, str]]) -> dict[str, int]:
    from nodestro_runtime.jobs import DEFAULT_MAX_RUNS, DEFAULT_MAX_WAITING, check_capacity

    found: dict[str, int] = {}
    valid = True
    for name, key in (("MAX_RUNS", "max_runs"), ("MAX_WAITING", "max_waiting")):
        text = environ.get(f"NODESTRO_WORKER_{name}", "").strip()
        if not text:
            continue
        try:
            found[key] = int(text)
        except ValueError:
            valid = False
            problems.append(
                {
                    "field": f"NODESTRO_WORKER_{name}",
                    "value": text,
                    "reason": f"NODESTRO_WORKER_{name} must be a whole number",
                }
            )
    if valid:
        try:
            check_capacity(found.get("max_runs", DEFAULT_MAX_RUNS), found.get("max_waiting", DEFAULT_MAX_WAITING))
        except ValueError as error:
            problems.append(
                {
                    "field": "NODESTRO_WORKER_MAX_RUNS",
                    "value": environ.get("NODESTRO_WORKER_MAX_RUNS", ""),
                    "reason": f"NODESTRO_WORKER_MAX_RUNS / NODESTRO_WORKER_MAX_WAITING: {error}",
                }
            )
    return found


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
    for name, key in (("LEASE", "lease"), ("POLL", "poll"), ("REAP", "reap_every"), ("CLEANUP", "cleanup_every")):
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
    capacity = _capacity(environ, problems)
    price_sync = PriceSyncConfig()
    try:
        price_sync = PriceSyncConfig.from_env(environ)
    except NodestroError as error:
        problems.append(
            {"field": error.params.get("field", ""), "value": error.params.get("value", ""), "reason": str(error)}
        )
    bedrock: BedrockOperatorConfig | None = None
    try:
        bedrock = BedrockOperatorConfig.from_env(environ)
    except NodestroError as error:
        # The problem names the variable, never a role ARN or an external id.
        problems.append(
            {"field": error.params.get("field", ""), "value": error.params.get("value", ""), "reason": str(error)}
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
        price_sync=price_sync,
        capacity=capacity,
        bedrock=bedrock,
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
            bedrock=server.bedrock,
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
        bedrock=server.bedrock,
    )
    timing = dict(server.worker)
    settings = WorkerSettings(
        lease=timedelta(seconds=timing.pop("lease")) if "lease" in timing else WorkerSettings.lease,
        price_sync=server.price_sync,
        **server.capacity,  # type: ignore[arg-type]
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

    if tenant_id == PRICE_REFERENCE_TENANT:  # the server's reference prices live under this id
        raise NodestroError("identity.tenant_id", f"The tenant id {tenant_id!r} is reserved", tenant=tenant_id)
    database = _database(server)
    try:
        database.require_current_schema()
        IdentityStore(database).create_tenant(tenant_id, name)
    finally:
        database.close()


# ---- members ----------------------------------------------------------------------------------

_USER_ID = re.compile(r"u_[0-9a-f]{24}")


def find_user(server: ServerEnvironment, database: Any, who: str) -> Any:
    """The user named by an id (``u_…``), a verified e-mail address or a subject at the configured provider."""
    from nodestro_runtime.identity_store import IdentityStore, User

    who = who.strip()
    identity = IdentityStore(database)
    if _USER_ID.fullmatch(who):
        found = identity.user(who)
        if found is not None:
            return found
        rows: list[Any] = []
    elif "@" in who:
        rows = database._query(
            "SELECT id, issuer, subject, email, name FROM nodestro_users WHERE lower(email) = lower(%s) ORDER BY id",
            (who,),
        )
    else:
        rows = database._query(
            "SELECT id, issuer, subject, email, name FROM nodestro_users WHERE issuer = %s AND subject = %s",
            (server.settings.oidc_issuer, who),
        )
    if not rows:
        raise NodestroError(
            "identity.user_unknown",
            f"No user {who!r} has logged in yet; ask them to log in once (they are told they have no access yet), "
            "then run this command again",
            user=who,
        )
    if len(rows) > 1:
        raise NodestroError(
            "identity.user_ambiguous",
            f"{who!r} names {len(rows)} users; use the user id ({', '.join(row[0] for row in rows)})",
            user=who,
        )
    row = rows[0]
    return User(id=row[0], issuer=row[1], subject=row[2], email=row[3], name=row[4])


def _require_tenant(identity: Any, tenant_id: str) -> None:
    if identity.tenant(tenant_id) is None:
        raise NodestroError("identity.tenant_unknown", f"No tenant {tenant_id}", tenant=tenant_id)


def members(server: ServerEnvironment, tenant_id: str) -> list[str]:
    """One line per member of a tenant: user id, role, e-mail address, name and subject."""
    from nodestro_runtime.identity_store import IdentityStore

    database = _database(server)
    try:
        database.require_current_schema()
        identity = IdentityStore(database)
        _require_tenant(identity, tenant_id)
        lines = []
        for member in identity.members(tenant_id):
            user = identity.user(member.user_id)
            email = (user.email if user else None) or "-"
            name = (user.name if user else None) or "-"
            subject = user.subject if user else "-"
            lines.append(f"{member.user_id}\t{member.role}\t{email}\t{name}\t{subject}")
        return lines
    finally:
        database.close()


def set_role(server: ServerEnvironment, tenant_id: str, who: str, role: str) -> str:
    """Give a user a role in a tenant (adds the member); takes effect with their next request."""
    from nodestro_runtime.identity_store import ROLES, IdentityStore

    if role not in ROLES:
        raise NodestroError("identity.role", f"Unknown role {role!r} (one of {', '.join(ROLES)})", role=role)
    database = _database(server)
    try:
        database.require_current_schema()
        identity = IdentityStore(database)
        _require_tenant(identity, tenant_id)
        user = find_user(server, database, who)
        before = identity.role(tenant_id, user.id)
        identity.set_role(tenant_id, user.id, role)
    finally:
        database.close()
    logger.info("Operator set the role of %s in tenant %s: %s -> %s", user.id, tenant_id, before or "-", role)
    return f"{user.id} is {role} in {tenant_id}" + (f" (was {before})" if before and before != role else "")


def remove_member(server: ServerEnvironment, tenant_id: str, who: str) -> str:
    """Remove a user from a tenant; their sessions in the tenant end at once (the last owner stays)."""
    from nodestro_runtime.identity_store import IdentityStore

    database = _database(server)
    try:
        database.require_current_schema()
        identity = IdentityStore(database)
        _require_tenant(identity, tenant_id)
        user = find_user(server, database, who)
        if identity.role(tenant_id, user.id) is None:
            raise NodestroError("identity.not_a_member", f"{user.id} is not a member of {tenant_id}", tenant=tenant_id)
        identity.remove_member(tenant_id, user.id)
    finally:
        database.close()
    logger.info("Operator removed %s from tenant %s", user.id, tenant_id)
    return f"removed {user.id} from {tenant_id}"


def revoke_sessions(server: ServerEnvironment, who: str) -> str:
    """End every session of a user in every tenant (memberships stay; a new login starts a new session)."""
    from nodestro_runtime.identity_store import IdentityStore

    database = _database(server)
    try:
        database.require_current_schema()
        user = find_user(server, database, who)
        ended = IdentityStore(database).end_sessions(user.id)
    finally:
        database.close()
    logger.info("Operator ended %d sessions of %s", ended, user.id)
    return f"ended {ended} sessions of {user.id}"


# ---- reference prices ------------------------------------------------------------------------


def pricing(server: ServerEnvironment, command: str, version: str | None = None, note: str | None = None) -> str:
    """``nodestro-server pricing status|sync|approve|reject``: the operator's side of the reference prices."""
    from .pricing_setup import server_sync

    database = _ready(server)
    try:
        sync = server_sync(database, server.price_sync)
        service = sync.pricing
        if command == "sync":
            report = sync.sync(force=True)
            if not report.ran:
                return f"not run: {report.skipped_reason}"
            lines = [f"active version: {report.active_version}"]
            lines += [f"{item.source}: {item.outcome or ''} {item.error or ''}".rstrip() for item in report.sources]
            return "\n".join(lines)
        if command == "approve" and version:
            return f"active version: {service.approve(version, note=note).active_version}"
        if command == "reject" and version:
            service.reject(version, note=note)
            return f"rejected {version}"
        status = service.status()
        lines = [
            f"active version: {status.active_version}",
            f"last success: {status.last_success_at or 'never'}",
            f"last error: {status.last_error or '-'}",
            f"staleness: {status.staleness}",
            f"open reviews: {', '.join(status.pending_reviews) or '-'}",
        ]
        return "\n".join(lines)
    finally:
        database.close()


# ---- command line -----------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    from nodestro_runtime.identity_store import ROLES

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
    listing = commands.add_parser("members", help="List the members of a tenant with their roles")
    listing.add_argument("tenant_id")
    user_help = "user id (u_...), verified e-mail address or subject at the identity provider"
    role = commands.add_parser("set-role", help="Add a member to a tenant or change their role")
    role.add_argument("tenant_id")
    role.add_argument("user", help=user_help)
    role.add_argument("role", choices=ROLES)
    removing = commands.add_parser("remove-member", help="Remove a member from a tenant and end their sessions there")
    removing.add_argument("tenant_id")
    removing.add_argument("user", help=user_help)
    revoking = commands.add_parser("revoke-sessions", help="End every session of a user (log them out everywhere)")
    revoking.add_argument("user", help=user_help)
    pricing = commands.add_parser("pricing", help="Reference prices: status, sync, open reviews and their decisions")
    pricing_commands = pricing.add_subparsers(dest="pricing_command", required=True)
    pricing_commands.add_parser("status", help="Print the state of the reference prices and the open reviews")
    pricing_commands.add_parser("sync", help="Check the reference prices against their sources now")
    for action in ("approve", "reject"):
        decision = pricing_commands.add_parser(action, help=f"{action.capitalize()} an open price review")
        decision.add_argument("version")
        decision.add_argument("--note")
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
        elif args.command == "members":
            print("\n".join(members(server, args.tenant_id)) or f"tenant {args.tenant_id} has no members")
        elif args.command == "set-role":
            print(set_role(server, args.tenant_id, args.user, args.role))
        elif args.command == "remove-member":
            print(remove_member(server, args.tenant_id, args.user))
        elif args.command == "revoke-sessions":
            print(revoke_sessions(server, args.user))
        elif args.command == "pricing":
            print(pricing(server, args.pricing_command, getattr(args, "version", None), getattr(args, "note", None)))
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
    "find_user",
    "load",
    "main",
    "members",
    "migrate",
    "pricing",
    "remove_member",
    "revoke_sessions",
    "run_worker",
    "set_role",
    "setup_link",
]
