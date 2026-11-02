"""``nodestro-server``: the processes of the server profile (plan step PR-12, ADR-0001 to ADR-0006).

A server is started from its environment (:class:`~nodestro_runtime.server_settings.ServerSettings`, plus
``NODESTRO_PROFILE=server`` and ``NODESTRO_DATA_DIR``). Every command checks the settings first and stops with one
sentence listing every problem (``profile.invalid``); nothing has a default that would weaken it.

``nodestro-server check``
    Settings, database (reachable, schema current) and the container sandbox (Docker reachable); exit code 0 or 1.
``nodestro-server migrate [--backup-dir DIR | --backup-taken]``
    Apply the database migrations. A database that already has tables needs a backup first: ``--backup-dir`` runs
    ``pg_dump`` into that directory, ``--backup-taken`` states that the operator made one. Runs as the owner role
    (``NODESTRO_DATABASE_MIGRATION_URL``, default ``NODESTRO_DATABASE_URL``), as do ``backup`` and ``db ...``.
``nodestro-server db grant-runtime-role [ROLE]`` / ``nodestro-server db roles``
    Give the runtime role (default: the role of ``NODESTRO_DATABASE_URL``) data access to every Nodestro and Axentra
    table, now and for later migrations (idempotent; refuses a superuser, ``BYPASSRLS`` or owning role), and show how
    row-level security applies to both roles. See :func:`nodestro_runtime.storage_postgres.grant_runtime_role`.
``nodestro-server api [--host HOST] [--port PORT]``
    The studio API with logins (OpenID Connect), one context per tenant and the built studio interface
    (``NODESTRO_STATIC_DIR``). Runs are queued for workers. Behind a TLS-terminating reverse proxy in production.
``nodestro-server worker``
    Executes queued runs, reaps runs of dead workers and fires schedules (:class:`nodestro_api.worker.ServerWorker`).
``nodestro-server create-tenant ID NAME`` / ``nodestro-server setup-link TENANT``
    Create a tenant, and print a one-time setup link that makes the person who opens it the tenant's first owner.
``nodestro-server secrets reencrypt [--dry-run] [--tenant TENANT]``
    Move every stored tenant secret (provider keys, MCP tokens, billing admin keys) to the active key of Axentra's
    keyring (``NODESTRO_AXENTRA=on``, ``NODESTRO_SECRETS_FORMAT=axs1``), or back to v1 with
    ``NODESTRO_SECRETS_FORMAT=v1`` (the rollback, before Axentra is switched off). Resumable and idempotent; a value
    that cannot be decrypted is left unchanged and reported (exit code 2).
``nodestro-server axentra status``
    The embedding state: switches, package version, schema state of both runners, keyring key ids (never keys),
    stored secrets by format, identity backend and its readiness, and the reconciliation backlog (counts only).
``nodestro-server files migrate|verify|remove-source|export-volume [--dry-run] [--tenant TENANT]``
    Input file bytes in Axentra's storage (``NODESTRO_FILES_BACKEND=axentra``): copy volume files into it (resumable,
    idempotent, SHA-256 checked, the volume file is kept), verify every object, remove migrated volume files after
    the fallback period (only with ``NODESTRO_FILES_SOURCE_REMOVAL=1``), or copy them back for a rollback. Exit code 2
    when a file failed. See :mod:`nodestro_runtime.axentra_storage`.
``nodestro-server axentra backfill-identity [--dry-run]`` / ``nodestro-server axentra reconcile-identity``
    Copy users, tenants, memberships and live sessions into Axentra (idempotent, resumable; run it in a maintenance
    window before ``NODESTRO_IDENTITY_BACKEND=mirror``), and compare both sides (exit code 1 on any difference; ids
    and counts only). See :mod:`nodestro_runtime.axentra_identity`.
``nodestro-server members TENANT`` / ``set-role TENANT USER ROLE`` / ``remove-member TENANT USER`` /
``revoke-sessions USER``
    Membership of a tenant (A-08): list it, add a member or change a role, remove a member (their sessions in the
    tenant end with it), and end every session of a user at once. ``USER`` is a user id (``u_…``), a verified
    e-mail address or the subject at the configured identity provider. A person is known once they logged in at
    least once (a login alone grants nothing); a tenant always keeps at least one owner.
``nodestro-server suspend-tenant TENANT`` / ``resume-tenant TENANT``
    Close a tenant until it is resumed (``403 identity.tenant_suspended`` for its sessions and logins; workers neither
    start nor continue its runs) and open it again. Nothing is deleted.

With ``NODESTRO_AXENTRA=on`` (off by default; see :mod:`nodestro_runtime.axentra_platform`) ``migrate`` also applies
Axentra's migrations to the same database (after the one backup of the run), and ``check``, ``api`` and ``worker``
require Axentra's schema to be current too. ``NODESTRO_IDENTITY_BACKEND`` (``nodestro`` by default, ``mirror``,
``axentra``) chooses the identity store of ``api``, ``worker`` and the member commands (:func:`_platform`);
``axentra`` refuses to start before the backfill.

API and worker processes start only when the database schema is current, every stored tenant secret is readable with
the configured keys (``profile.invalid`` otherwise, e.g. ``NODESTRO_AXENTRA=off`` while ``axs1`` values exist; see
:func:`nodestro_runtime.axentra_platform.require_readable_secrets`) and Docker is reachable: tool code runs
only in containers on a server (ADR-0006), so a server without a container runtime does not start. Docker may be
local (socket) or remote (``DOCKER_HOST``); containers need no bind mounts
(:class:`nodestro_runtime.container_images.StreamedSandbox`).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    #: ``NODESTRO_METRICS_TOKEN`` (``GET /api/metrics``; unset: no endpoint).
    metrics: Any = None


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
    from nodestro_runtime.axentra_metrics import MetricsConfig

    metrics = MetricsConfig.from_env(environ, problems)
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
        metrics=metrics,
    )


def _schema() -> str | None:
    return os.environ.get("NODESTRO_DATABASE_SCHEMA", "").strip() or None


def _database(server: ServerEnvironment, *, owner: bool = False) -> Any:
    """The runtime role's database; ``owner``: the owner role's (``NODESTRO_DATABASE_MIGRATION_URL``), for migrations,
    backups and grants."""
    from nodestro_runtime.storage_postgres import PostgresDatabase

    url = server.settings.migration_url if owner else server.settings.database_url
    try:
        return PostgresDatabase(url, schema=_schema(), pool_size=2 if owner else None)
    except NodestroError:
        raise
    except Exception as exc:  # psycopg errors name the host but never the password
        raise NodestroError("storage.unavailable", f"The database is not reachable: {type(exc).__name__}") from None


def _require_containers(server: ServerEnvironment) -> None:
    """A server runs tool code only in containers: the daemon must answer and pass Orkexa's preflight (Linux
    containers, enforced memory/CPU/process limits, a seccomp profile, cgroup v2 when rootless).

    The image build and the in-container self-test are not part of the start: a fresh daemon has no image yet. The
    runtime's Docker monitor runs them in the background, and every container run is refused until they passed
    (fail closed); before orkexa #31 the start only checked that the daemon answered."""
    from orkexa.dynamic.docker import DockerProbeError, inspect_host, preflight

    try:
        probe = preflight(inspect_host(server.docker))
    except DockerProbeError as exc:
        raise NodestroError(
            "sandbox.unavailable",
            "Docker is not reachable (local socket or DOCKER_HOST); a server runs tool code only in containers",
            reason=f"docker-{exc.reason}",
        ) from None
    if not probe.available:
        raise NodestroError(
            "sandbox.unavailable",
            f"The Docker daemon cannot isolate tool code ({probe.reason}): {probe.detail}",
            reason=f"docker-{probe.reason}",
        )


def _axentra(server: ServerEnvironment, database: Any = None) -> Any:
    """Axentra's services for this server (not started), ``None`` when ``NODESTRO_AXENTRA`` is off.

    ``database``: the process's runtime database; Axentra then borrows its pool (one connection budget per process,
    the runtime role). Without it (``migrate``), only Axentra's migration runner is used, as the owner role."""
    config = server.settings.axentra
    if not config.enabled:
        return None
    from nodestro_runtime.axentra_platform import AxentraServices

    return AxentraServices(
        config,
        database_url=server.settings.database_url,
        public_origin=server.settings.public_origin,
        schema=_schema(),
        migration_url=server.settings.migration_url,
        database=database,
    )


def _require_axentra_schema(server: ServerEnvironment, database: Any) -> None:
    services = _axentra(server, database)
    if services is not None:
        try:
            services.require_current_schema()
        finally:
            services.close()


def _require_readable_secrets(server: ServerEnvironment, database: Any) -> None:
    """Refuse a configuration that cannot read every stored tenant secret (a rollback switched too early)."""
    from nodestro_runtime.axentra_platform import require_readable_secrets

    require_readable_secrets(database, server.settings.axentra)


def _require_runtime_role(server: ServerEnvironment, database: Any) -> None:
    """Refuse a runtime role that is not fit to serve: with ``NODESTRO_AXENTRA=on`` (unless
    ``NODESTRO_AXENTRA_ALLOW_SUPERUSER=1``) or a separate migration role, row-level security must apply to it: no
    superuser, no ``BYPASSRLS``, owns no table (``storage.role_unsafe``)."""
    settings = server.settings
    if not ((settings.axentra.enabled and not settings.axentra.allow_superuser) or settings.separate_migration_role):
        return
    info = database.role_info()
    reasons = [
        reason
        for reason, flag in (
            ("is a superuser", info["superuser"]),
            ("has BYPASSRLS", info["bypass_rls"]),
            ("owns tables of the schema", info["table_owner"]),
        )
        if flag
    ]
    if reasons:
        raise NodestroError(
            "storage.role_unsafe",
            f"The runtime database role {info['role']!r} {' and '.join(reasons)}, so row-level security does not "
            "protect it; run api and worker as a runtime role (nodestro-server db grant-runtime-role) and migrate as "
            "the owner (NODESTRO_DATABASE_MIGRATION_URL)",
            role=info["role"],
        )


def _attach_mirrors(server: ServerEnvironment, database: Any) -> Any:
    """Start Axentra and attach the audit/usage mirrors to ``database`` when ``NODESTRO_AXENTRA_AUDIT`` or
    ``NODESTRO_AXENTRA_USAGE`` is on (see :mod:`nodestro_runtime.platform_events`); the started services or ``None``.
    """
    config = server.settings.axentra
    if not (config.enabled and (config.audit or config.usage)):
        return None
    from nodestro_runtime.axentra_platform import AxentraLink, attach_platform_events

    services = _axentra(server, database)
    try:
        services.start()
        attach_platform_events(database, AxentraLink(services, database))
    except BaseException:
        services.close()
        raise
    return services


def _platform(server: ServerEnvironment, database: Any) -> tuple[Any, Any]:
    """The started Axentra services of this process (``None`` when nothing needs them) and its identity store.

    One set of services serves the audit/usage mirrors and the identity store. ``NODESTRO_IDENTITY_BACKEND=nodestro``
    (the default) is Nodestro's :class:`~nodestro_runtime.identity_store.IdentityStore`; ``mirror``/``axentra`` use
    :class:`~nodestro_runtime.axentra_identity.AxentraIdentityStore` (``axentra`` refuses to start before the
    backfill, ``identity.backfill_required``)."""
    from nodestro_runtime.identity_store import IdentityStore

    config = server.settings.axentra
    services = _attach_mirrors(server, database)
    if config.identity_backend == "nodestro":
        if services is None and config.files_backend == "axentra":
            services = _axentra(server, database)
            try:
                services.start()
            except BaseException:
                services.close()
                raise
        return services, IdentityStore(database)
    try:
        if services is None:
            services = _axentra(server, database)
            services.start()
        from nodestro_runtime.axentra_identity import open_identity_store

        return services, open_identity_store(database, config, services)
    except BaseException:
        if services is not None:
            services.close()
        raise


def _observed(server: ServerEnvironment, database: Any, services: Any, identity: Any) -> tuple[Any, Any, Any]:
    """The process's notifications (``NODESTRO_AXENTRA_NOTIFY``) and metrics (``NODESTRO_METRICS_TOKEN``), each
    ``None`` when off; Axentra's services are started here when only the notifications need them.

    Returns ``(services, notifications, metrics)``; the identity store is instrumented (counted) when metrics are on.
    """
    notifications = None
    if server.settings.axentra.notify:
        from nodestro_runtime.axentra_notify import AxentraNotifications
        from nodestro_runtime.axentra_platform import AxentraLink

        if services is None:
            services = _axentra(server, database)
            services.start()
        notifications = AxentraNotifications(AxentraLink(services, database))
        notifications.ensure_templates()
    metrics = None
    if server.metrics is not None and server.metrics.enabled:
        from nodestro_runtime.axentra_metrics import ServerMetrics, instrument_identity

        instrument_identity(identity)
        metrics = ServerMetrics(database, services)
    return services, notifications, metrics


def _input_files(server: ServerEnvironment, database: Any, services: Any) -> Any:
    """Axentra's storage for the input files (``NODESTRO_FILES_BACKEND=axentra``; ``services`` started), else
    ``None`` (the data volume)."""
    if server.settings.axentra.files_backend != "axentra":
        return None
    from nodestro_runtime.axentra_storage import open_files

    return open_files(services, database)


@contextmanager
def _identity(server: ServerEnvironment, database: Any) -> Iterator[Any]:
    """The identity store for one operator command (Axentra services closed afterwards)."""
    services, identity = _platform(server, database)
    try:
        yield identity
    finally:
        database.platform_events = None
        if services is not None:
            services.close()


def _ready(server: ServerEnvironment) -> Any:
    """The database, after checking that its schema is current and Docker is reachable."""
    database = _database(server)
    try:
        database.require_current_schema()
        _require_axentra_schema(server, database)
        _require_readable_secrets(server, database)
        _require_runtime_role(server, database)
        _require_containers(server)
    except BaseException:
        database.close()
        raise
    return database


# ---- the API process --------------------------------------------------------------------------


def create_server_app() -> Any:
    """The studio API of a server, built from the environment (``uvicorn --factory``)."""
    import httpx
    from nodestro_runtime.jobs import JobQueue

    from .identity import OidcClient, OidcSettings, axentra_login_flow
    from .server_app import TenantComposition, create_tenant_app

    server = load()
    database = _ready(server)
    services, identity = _platform(server, database)  # open for the life of the process, as the database
    services, notifications, metrics = _observed(server, database, services, identity)
    settings = server.settings
    oidc_settings = OidcSettings(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=settings.redirect_uri,
    )
    # One identity service answers: in axentra mode Axentra also runs the login (its OidcService behind Nodestro's
    # routes, cookies and error codes); otherwise Nodestro's own client does.
    oidc: Any = (
        axentra_login_flow(identity, oidc_settings)
        if getattr(identity, "mode", None) == "axentra"
        else OidcClient(oidc_settings, http=httpx.Client(timeout=15.0))
    )
    return create_tenant_app(
        TenantComposition(
            database=database,
            identity=identity,
            oidc=oidc,
            origin=settings.public_origin,
            data_root=server.profile.data_dir,
            docs_dir=server.docs_dir,
            queue=JobQueue(database),
            secrets=settings.secret_box(),
            static_dir=server.static_dir,
            endpoints=server.endpoints,
            bedrock=server.bedrock,
            metrics=metrics,
            metrics_authorized=server.metrics.authorized if metrics is not None else None,
            notifications=notifications,
            files=_input_files(server, database, services),
        )
    )


# ---- the worker process -----------------------------------------------------------------------


async def run_worker(server: ServerEnvironment) -> None:
    """Serve as a worker until SIGTERM or SIGINT; a running run is finished first."""
    from .server_app import TenantFiles
    from .worker import ServerWorker, WorkerSettings

    database = _ready(server)
    mirrors, identity = _platform(server, database)
    mirrors, notifications, _metrics = _observed(server, database, mirrors, identity)
    files = TenantFiles(
        database=database,
        data_root=server.profile.data_dir,
        docs_dir=server.docs_dir,
        secrets=server.settings.secret_box(),
        endpoints=server.endpoints,
        bedrock=server.bedrock,
        files=_input_files(server, database, mirrors),
    )
    timing = dict(server.worker)
    settings = WorkerSettings(
        lease=timedelta(seconds=timing.pop("lease")) if "lease" in timing else WorkerSettings.lease,
        price_sync=server.price_sync,
        **server.capacity,  # type: ignore[arg-type]
        **timing,  # type: ignore[arg-type]
    )
    worker = ServerWorker(files, identity, settings)
    # Axentra duties (each off unless switched on): run usage settlement and inbox notifications.
    events = database.platform_events
    if events is not None and getattr(events, "run_usage_enabled", False):
        worker.settlement = events
    worker.notifications = notifications
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    logger.info("Worker %s started", worker.name)
    try:
        await worker.serve(stop)
    finally:
        database.close()
        if mirrors is not None:
            mirrors.close()
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
        steps = (
            database.require_current_schema,
            lambda: _require_axentra_schema(server, database),
            lambda: _require_runtime_role(server, database),
        )
        for step in steps:
            try:
                step()
            except NodestroError as error:
                problems.append(f"{error.code}: {error}")
        if not problems:  # the stored secrets can only be read from a current schema
            try:
                _require_readable_secrets(server, database)
            except NodestroError as error:
                problems.append(f"{error.code}: {error}")
    finally:
        database.close()
    if not problems and server.settings.axentra.identity_backend != "nodestro":
        # The identity backend starts as api and worker would (axentra: refused before the backfill).
        database = _database(server)
        try:
            with _identity(server, database):
                pass
        except NodestroError as error:
            problems.append(f"{error.code}: {error}")
        finally:
            database.close()
    if not problems and server.settings.axentra.files_backend == "axentra":
        try:
            with _files_tools(server):
                pass
        except NodestroError as error:
            problems.append(f"{error.code}: {error}")
    try:
        _require_containers(server)
    except NodestroError as error:
        problems.append(f"{error.code}: {error}")
    return problems


def migrate(server: ServerEnvironment, *, backup_dir: Path | None, backup_taken: bool) -> int:
    """Apply the migrations (Nodestro's, then Axentra's when switched on); returns Nodestro's version before.

    One backup covers the run: it is written before the first change, whichever runner needs it first. Everything runs
    as the owner role (``NODESTRO_DATABASE_MIGRATION_URL``, default ``NODESTRO_DATABASE_URL``). With a separate owner
    role, the runtime role (the user of ``NODESTRO_DATABASE_URL``) is checked before anything changes (refused when
    it is a superuser, has ``BYPASSRLS`` or owns tables) and granted its privileges afterwards, so the tables of the
    new migrations are usable at once (:func:`grant_runtime_role`)."""
    runtime = None
    if server.settings.separate_migration_role:
        from nodestro_runtime.storage_postgres import inspect_role

        runtime = runtime_role(server)
        owner = _database(server, owner=True)
        try:
            found = inspect_role(owner, runtime)
        finally:
            owner.close()
        if found["superuser"] or found["bypass_rls"] or found["owned_tables"] or runtime == found["owner"]:
            raise NodestroError(
                "storage.role_unsafe",
                f"The runtime role {runtime!r} of NODESTRO_DATABASE_URL bypasses row-level security or owns tables; "
                "nothing was migrated (docs/RUNBOOK.md, database roles)",
                role=runtime,
            )
    database = _database(server, owner=True)
    taken: list[Path] = []

    def backup(label: str) -> None:
        if backup_taken or taken:
            return
        if backup_dir is None:
            raise NodestroError(
                "storage.backup_required",
                f"The database needs a migration ({label}); take a backup first "
                "(--backup-dir DIR runs pg_dump, --backup-taken states that you made one)",
            )
        taken.append(dump(server, backup_dir / f"pre-migration-{label}-{_stamp()}.dump"))

    def before(current: int, target: int) -> None:
        backup(f"{current}-to-{target}")

    try:
        version = int(database.migrate(before_migration=before))
    finally:
        database.close()
    services = _axentra(server)
    if services is not None:
        try:
            fresh = version == 0 and not taken  # a new database: nothing to back up (as for Nodestro's runner)
            applied = services.migrate(None if fresh else (lambda pending: backup("axentra")))
        finally:
            services.close()
        if applied:
            logger.info("Applied %d Axentra migrations", len(applied))
    if runtime is not None:
        grant_runtime_role(server, runtime)
    return version


def reencrypt_secrets(server: ServerEnvironment, *, dry_run: bool, tenant: str | None) -> dict[str, Any]:
    """``nodestro-server secrets reencrypt``: see :func:`nodestro_runtime.axentra_platform.reencrypt_tenant_secrets`."""
    from nodestro_runtime.axentra_platform import KeyringSecretBox, reencrypt_tenant_secrets

    box = server.settings.secret_box()
    if not isinstance(box, KeyringSecretBox):
        raise NodestroError(
            "secrets.keyring_required",
            "Re-encryption needs NODESTRO_AXENTRA=on with the keyring; NODESTRO_SECRETS_FORMAT says which format the "
            "values move to (axs1: the active key; v1: back to NODESTRO_SECRET_KEY before switching Axentra off)",
        )
    database = _database(server)
    try:
        database.require_current_schema()
        report = reencrypt_tenant_secrets(database, box, dry_run=dry_run, tenant=tenant)
    finally:
        database.close()
    logger.info(
        "Secret re-encryption%s: %d values, %d pending, %d re-encrypted, %d failed",
        " (dry run)" if dry_run else "",
        report.total,
        report.pending,
        report.reencrypted,
        len(report.failed),
    )
    return report.as_dict()


def reconcile_mirrors(server: ServerEnvironment, *, dry_run: bool, tenant: str | None) -> dict[str, Any]:
    """``nodestro-server axentra reconcile``: write the audit/usage mirrors that are missing in Axentra
    (:meth:`nodestro_runtime.axentra_platform.AxentraEvents.reconcile`; idempotent, Nodestro's records only read)."""
    config = server.settings.axentra
    if not (config.enabled and (config.audit or config.usage)):
        raise NodestroError(
            "axentra.mirrors_off",
            "Reconciling needs NODESTRO_AXENTRA=on and NODESTRO_AXENTRA_AUDIT=1 or NODESTRO_AXENTRA_USAGE=1",
        )
    database = _database(server)
    try:
        database.require_current_schema()
        _require_axentra_schema(server, database)
        services = _attach_mirrors(server, database)
        try:
            report: dict[str, Any] = database.platform_events.reconcile(dry_run=dry_run, tenant=tenant)
        finally:
            database.platform_events = None
            services.close()
    finally:
        database.close()
    logger.info("Axentra reconcile%s: %s", " (dry run)" if dry_run else "", report)
    return report


@contextmanager
def _identity_tools(server: ServerEnvironment) -> Iterator[Any]:
    """An :class:`~nodestro_runtime.axentra_identity.AxentraIdentityStore` for the backfill and the reconciliation,
    whatever ``NODESTRO_IDENTITY_BACKEND`` is (they run before the switch); needs ``NODESTRO_AXENTRA=on``."""
    if not server.settings.axentra.enabled:
        raise NodestroError("axentra.disabled", "The identity backfill needs NODESTRO_AXENTRA=on")
    database = _database(server)
    services = _axentra(server, database)
    try:
        database.require_current_schema()
        services.require_current_schema()
        services.start()
        from nodestro_runtime.axentra_identity import AxentraIdentityStore
        from nodestro_runtime.axentra_platform import AxentraLink

        store = AxentraIdentityStore(database, AxentraLink(services, database), mode="mirror")
        problems = store.configuration_problems()
        if problems:
            raise NodestroError("profile.invalid", "; ".join(problems), problems=problems)
        yield store
    finally:
        services.close()
        database.close()


def backfill_identity(server: ServerEnvironment, *, dry_run: bool) -> dict[str, Any]:
    """``axentra backfill-identity``: see :func:`nodestro_runtime.axentra_identity.backfill_identity`."""
    from nodestro_runtime.axentra_identity import backfill_identity as run

    with _identity_tools(server) as store:
        report: dict[str, Any] = run(store, dry_run=dry_run).as_dict()
    logger.info("Identity backfill%s: %s", " (dry run)" if dry_run else "", report)
    return report


def reconcile_identity(server: ServerEnvironment) -> dict[str, Any]:
    """``nodestro-server axentra reconcile-identity``: compare both sides (read-only; ids and counts only)."""
    from nodestro_runtime.axentra_identity import reconcile_identity as run

    with _identity_tools(server) as store:
        report: dict[str, Any] = run(store).as_dict()
    return report


def axentra_status(server: ServerEnvironment) -> list[str]:
    """``nodestro-server axentra status``: the embedding state, for operators (ids, versions and counts only).

    Switches, package, both schema runners, the keyring's key ids (never a key), stored secrets by format (and
    whether this configuration can read all of them), the identity backend and whether it would start, and the
    reconciliation backlog (what ``axentra reconcile`` would write). Read-only; it never applies anything."""
    from nodestro_runtime.axentra_platform import (
        available,
        axentra_version,
        reconciliation_backlog,
        secrets_rollback_problems,
        stored_secret_formats,
    )
    from nodestro_runtime.storage_postgres import POSTGRES_SCHEMA_VERSION

    config = server.settings.axentra

    def on(value: bool) -> str:
        return "on" if value else "off"

    lines = [
        f"axentra: {on(config.enabled)}",
        f"package: {axentra_version() or 'not installed'}" + ("" if available() else " (not usable here)"),
        f"secrets format: {config.secrets_format}",
        f"audit mirror: {on(config.audit)}",
        f"usage mirror: {on(config.usage)}",
        f"run usage mirror: {on(config.run_usage)}",
        f"notifications (inbox): {on(config.notify)}",
        f"identity backend: {config.identity_backend}",
        f"metrics endpoint: {on(bool(server.metrics and server.metrics.enabled))}",
        f"files backend: {config.files_backend}",
    ]
    if config.enabled and available():
        from axentra.config.secrets import Keyring

        kids = Keyring.parse(config.keyring).kids
        lines.append(f"keyring: {kids[0]} (active)" + "".join(f", {kid}" for kid in kids[1:]))
    try:
        database = _database(server)
    except NodestroError as error:
        lines.append(f"database: {error.code}")
        return lines
    try:
        version = database.schema_version
        state = "current" if version == POSTGRES_SCHEMA_VERSION else "MIGRATION NEEDED"
        lines.append(f"nodestro schema: {version} of {POSTGRES_SCHEMA_VERSION} ({state})")
        if version:  # an empty database has no stored secrets (and no table to read them from)
            formats = stored_secret_formats(database)
            lines.append("stored secrets: " + (", ".join(f"{kind} {n}" for kind, n in formats.items()) or "none"))
            for problem in secrets_rollback_problems(config, formats):
                lines.append(f"secrets: NOT READABLE: {problem['reason']}")
        services = _axentra(server, database)
        if services is not None:
            try:
                pending = services.pending_migrations()
                problems = services.schema_problems()
                lines.append(
                    "axentra schema: " + ("current" if not problems else f"NOT CURRENT ({len(pending)} pending)")
                )
                if not problems and version == POSTGRES_SCHEMA_VERSION:
                    services.start()
                    backlog = reconciliation_backlog(services)
                    lines.append("reconcile backlog: " + ", ".join(f"{kind} {n}" for kind, n in backlog.items()))
            finally:
                services.close()
            if config.identity_backend != "nodestro" and not problems:
                try:
                    with _identity(server, database):
                        pass
                    lines.append("identity backend ready: yes")
                except NodestroError as error:
                    lines.append(f"identity backend ready: no ({error.code})")
            lines.append(f"pending migrations: {', '.join(pending) or 'none'}")
    finally:
        database.close()
    return lines


@contextmanager
def _files_tools(server: ServerEnvironment) -> Iterator[Any]:
    """Axentra's storage of the input files for one operator command (closed afterwards)."""
    if server.settings.axentra.files_backend != "axentra":
        raise NodestroError(
            "files.backend_off", "The files commands need NODESTRO_AXENTRA=on and NODESTRO_FILES_BACKEND=axentra"
        )
    from nodestro_runtime.axentra_storage import open_files

    database = _database(server)
    services = _axentra(server, database)
    try:
        database.require_current_schema()
        services.require_current_schema()
        services.start()
        yield open_files(services, database)
    finally:
        services.close()
        database.close()


FILES_COMMANDS = ("migrate", "verify", "remove-source", "export-volume")


def files_command(
    server: ServerEnvironment, command: str, *, dry_run: bool = False, tenant: str | None = None
) -> dict[str, Any]:
    """``nodestro-server files migrate|verify|remove-source|export-volume`` (see
    :class:`nodestro_runtime.axentra_storage.AxentraFiles`); ids and counts only."""
    root = server.profile.data_dir
    with _files_tools(server) as files:
        if command == "migrate":
            report = files.migrate(root, tenant=tenant, dry_run=dry_run)
        elif command == "verify":
            report = files.verify(root, tenant=tenant)
        elif command == "remove-source":
            report = files.remove_sources(root, tenant=tenant, dry_run=dry_run)
        elif command == "export-volume":
            report = files.export_to_volume(root, tenant=tenant, dry_run=dry_run)
        else:  # pragma: no cover - argparse limits the choices
            raise NodestroError("files.command", f"Unknown files command {command!r}")
    result: dict[str, Any] = report.as_dict()
    return result


def archive_files(server: ServerEnvironment, directory: Path) -> Path | None:
    """With ``NODESTRO_FILES_BACKEND=axentra`` on Axentra's local backend: a ``tar.gz`` of its directory next to the
    database dump (``None`` otherwise; S3 buckets are backed up with the bucket's own versioning or replication)."""
    config = server.settings.axentra
    if config.files_backend != "axentra":
        return None
    services = _axentra(server)
    try:
        storage = services.settings.storage
        if storage.backend != "local":
            return None
        source = Path(storage.local_path)
    finally:
        services.close()
    if not source.is_dir():
        raise NodestroError("storage.backup_failed", f"Axentra's storage directory {source} does not exist")
    import tarfile

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"nodestro-files-{_stamp()}.tar.gz"
    partial = target.with_name(target.name + ".partial")
    try:
        with tarfile.open(partial, "w:gz") as archive:
            # Temporary upload files (.tmp-*) of uploads in progress are left out: their rows are still pending.
            archive.add(source, arcname=".", filter=lambda info: None if "/.tmp-" in f"/{info.name}" else info)
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


def runtime_role(server: ServerEnvironment) -> str:
    """The role ``api`` and ``worker`` connect as (the user of ``NODESTRO_DATABASE_URL``)."""
    database = _database(server)
    try:
        return str(database.role_info()["role"])
    finally:
        database.close()


def grant_runtime_role(server: ServerEnvironment, role: str | None = None) -> dict[str, Any]:
    """``nodestro-server db grant-runtime-role [ROLE]``: as the owner role, give the runtime role (default: the role of
    ``NODESTRO_DATABASE_URL``) data access to every table, now and for later migrations; idempotent. Needs a separate
    owner connection (``NODESTRO_DATABASE_MIGRATION_URL``)."""
    from nodestro_runtime.storage_postgres import grant_runtime_role as grant

    if not server.settings.separate_migration_role:
        raise NodestroError(
            "storage.role_unsafe",
            "NODESTRO_DATABASE_MIGRATION_URL is not set (or equals NODESTRO_DATABASE_URL): the runtime role would be "
            "the owner of the tables; create a separate owner role first (docs/RUNBOOK.md, database roles)",
        )
    target = role or runtime_role(server)
    owner = _database(server, owner=True)
    try:
        report = grant(owner, target)
    finally:
        owner.close()
    logger.info("Granted the runtime role %s data access to schema %s", target, report["schema"])
    return report


def database_roles(server: ServerEnvironment) -> dict[str, Any]:
    """``nodestro-server db roles``: how row-level security applies to the runtime and the owner role (read-only)."""
    from nodestro_runtime.storage_postgres import inspect_role

    runtime = _database(server)
    try:
        runtime_info = runtime.role_info()
    finally:
        runtime.close()
    owner = _database(server, owner=True)
    try:
        owner_info = owner.role_info()
        report = inspect_role(owner, runtime_info["role"])
    finally:
        owner.close()
    return {
        "runtime": {**runtime_info, **{key: report[key] for key in ("connect", "can_create", "restricted")}},
        "owner": owner_info,
        "separate": server.settings.separate_migration_role,
        # pg_dump reads every row: the backup role must bypass the forced row-level security of Axentra's tables.
        "owner_can_back_up": bool(owner_info["superuser"] or owner_info["bypass_rls"]),
    }


def dump(server: ServerEnvironment, target: Path) -> Path:
    """Write a ``pg_dump`` (custom format) of the server's database to ``target``."""
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        raise NodestroError("storage.backup_failed", "pg_dump is not installed")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    target.parent.mkdir(parents=True, exist_ok=True)
    # As the owner role: it can read every table (with BYPASSRLS, Axentra's forced row-level security too). The
    # password goes to pg_dump in its environment, never on the command line (visible to other users).
    parts = conninfo_to_dict(server.settings.migration_url)
    password = parts.pop("password", None)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NODESTRO_")}
    if password is not None:
        environment["PGPASSWORD"] = str(password)
    # With NODESTRO_DATABASE_SCHEMA the server's tables (Nodestro's and Axentra's) are that schema: dump only it.
    schema = [f"--schema={_schema()}"] if _schema() else []
    completed = subprocess.run(
        [
            pg_dump,
            "--format=custom",
            "--no-owner",
            *schema,
            f"--file={target}",
            f"--dbname={make_conninfo('', **parts)}",
        ],
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

    database = _database(server)
    try:
        database.require_current_schema()
        with _identity(server, database) as identity:
            token = identity.create_bootstrap_token(tenant_id)
    finally:
        database.close()
    return f"{server.settings.public_origin}/api/auth/login?{urlencode({'setup': token})}"


def create_tenant(server: ServerEnvironment, tenant_id: str, name: str) -> None:
    if tenant_id == PRICE_REFERENCE_TENANT:  # the server's reference prices live under this id
        raise NodestroError("identity.tenant_id", f"The tenant id {tenant_id!r} is reserved", tenant=tenant_id)
    database = _database(server)
    try:
        database.require_current_schema()
        with _identity(server, database) as identity:
            identity.create_tenant(tenant_id, name)
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
    database = _database(server)
    try:
        database.require_current_schema()
        with _identity(server, database) as identity:
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
    from nodestro_runtime.identity_store import ROLES

    if role not in ROLES:
        raise NodestroError("identity.role", f"Unknown role {role!r} (one of {', '.join(ROLES)})", role=role)
    database = _database(server)
    try:
        database.require_current_schema()
        with _identity(server, database) as identity:
            _require_tenant(identity, tenant_id)
            user = find_user(server, database, who)
            before = identity.role(tenant_id, user.id)
            identity.set_role(tenant_id, user.id, role)
    finally:
        database.close()
    logger.info("Operator set the role of %s in tenant %s: %s -> %s", user.id, tenant_id, before or "-", role)
    return f"{user.id} is {role} in {tenant_id}" + (f" (was {before})" if before and before != role else "")


def set_tenant_status(server: ServerEnvironment, tenant_id: str, status: str) -> str:
    """Suspend or resume a tenant (both sides with Axentra identity). A suspended tenant is closed at once: its
    sessions are refused, nobody logs in to it, its setup links wait, and workers neither start nor continue its
    runs. Nothing is deleted."""
    database = _database(server)
    try:
        with _identity(server, database) as identity:
            _require_tenant(identity, tenant_id)
            identity.set_tenant_status(tenant_id, status)
    finally:
        database.close()
    logger.info("Operator set the status of tenant %s: %s", tenant_id, status)
    return f"tenant {tenant_id} is {status}"


def remove_member(server: ServerEnvironment, tenant_id: str, who: str) -> str:
    """Remove a user from a tenant; their sessions in the tenant end at once (the last owner stays)."""
    database = _database(server)
    try:
        database.require_current_schema()
        with _identity(server, database) as identity:
            _require_tenant(identity, tenant_id)
            user = find_user(server, database, who)
            if identity.role(tenant_id, user.id) is None:
                raise NodestroError(
                    "identity.not_a_member", f"{user.id} is not a member of {tenant_id}", tenant=tenant_id
                )
            identity.remove_member(tenant_id, user.id)
    finally:
        database.close()
    logger.info("Operator removed %s from tenant %s", user.id, tenant_id)
    return f"removed {user.id} from {tenant_id}"


def revoke_sessions(server: ServerEnvironment, who: str) -> str:
    """End every session of a user in every tenant (memberships stay; a new login starts a new session)."""
    database = _database(server)
    try:
        database.require_current_schema()
        user = find_user(server, database, who)
        with _identity(server, database) as identity:
            ended = identity.end_sessions(user.id)
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
    for name, text in (
        ("suspend-tenant", "Suspend a tenant: close it until it is resumed"),
        ("resume-tenant", "Resume a suspended tenant"),
    ):
        status_command = commands.add_parser(name, help=text)
        status_command.add_argument("tenant_id")
    removing = commands.add_parser("remove-member", help="Remove a member from a tenant and end their sessions there")
    removing.add_argument("tenant_id")
    removing.add_argument("user", help=user_help)
    revoking = commands.add_parser("revoke-sessions", help="End every session of a user (log them out everywhere)")
    revoking.add_argument("user", help=user_help)
    secrets = commands.add_parser("secrets", help="Tenant secrets: re-encrypt them with Axentra's active key")
    secrets_commands = secrets.add_subparsers(dest="secrets_command", required=True)
    reencrypting = secrets_commands.add_parser("reencrypt", help="Re-encrypt stored tenant secrets (resumable)")
    reencrypting.add_argument("--dry-run", action="store_true", help="Only count what would be re-encrypted")
    reencrypting.add_argument("--tenant", help="Only this tenant")
    platform = commands.add_parser("axentra", help="Axentra platform services")
    platform_commands = platform.add_subparsers(dest="axentra_command", required=True)
    platform_commands.add_parser(
        "status", help="Embedding state: switches, schemas, keyring ids, secrets, identity, reconcile backlog"
    )
    reconciling = platform_commands.add_parser(
        "reconcile", help="Write audit/usage mirrors missing in Axentra (idempotent)"
    )
    reconciling.add_argument("--dry-run", action="store_true", help="Only count what is missing")
    reconciling.add_argument("--tenant", help="Only this tenant")
    backfilling = platform_commands.add_parser(
        "backfill-identity", help="Copy users, tenants, memberships and sessions into Axentra (idempotent)"
    )
    backfilling.add_argument("--dry-run", action="store_true", help="Only report what would be copied")
    platform_commands.add_parser(
        "reconcile-identity", help="Compare identity records of Nodestro and Axentra (exit code 1 on differences)"
    )
    files = commands.add_parser("files", help="Input file bytes in Axentra's storage (NODESTRO_FILES_BACKEND=axentra)")
    files_commands = files.add_subparsers(dest="files_command", required=True)
    for name, text in (
        ("migrate", "Copy volume files into Axentra's storage (resumable, SHA-256 checked, volume kept)"),
        ("verify", "Read every stored file back and compare its SHA-256 (exit code 2 on a failure)"),
        ("remove-source", "Delete migrated, verified volume files after the fallback period (needs the switch)"),
        ("export-volume", "Copy files stored only in Axentra back to the volume (before switching back)"),
    ):
        command = files_commands.add_parser(name, help=text)
        if name != "verify":
            command.add_argument("--dry-run", action="store_true", help="Only count what would change")
        command.add_argument("--tenant", help="Only this tenant")
    db = commands.add_parser("db", help="Database roles (as the owner role, NODESTRO_DATABASE_MIGRATION_URL)")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    granting = db_commands.add_parser(
        "grant-runtime-role", help="Give the runtime role data access to every table, now and later (idempotent)"
    )
    granting.add_argument("role", nargs="?", help="Default: the role of NODESTRO_DATABASE_URL")
    db_commands.add_parser("roles", help="Show how row-level security applies to the runtime and the owner role")
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
            archived = archive_files(server, args.directory)
            if archived is not None:
                print(archived)
        elif args.command == "files":
            import json

            report = files_command(
                server, args.files_command, dry_run=getattr(args, "dry_run", False), tenant=args.tenant
            )
            print(json.dumps(report, indent=2))
            if report["failed"]:
                raise SystemExit(2)
        elif args.command in ("suspend-tenant", "resume-tenant"):
            status = "suspended" if args.command == "suspend-tenant" else "active"
            print(set_tenant_status(server, args.tenant_id, status))
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
        elif args.command == "secrets":
            import json

            report = reencrypt_secrets(server, dry_run=args.dry_run, tenant=args.tenant)
            print(json.dumps(report, indent=2))
            if report["failed"]:
                raise SystemExit(2)
        elif args.command == "axentra" and args.axentra_command == "reconcile":
            import json

            report = reconcile_mirrors(server, dry_run=args.dry_run, tenant=args.tenant)
            print(json.dumps(report, indent=2))
            if report["failed"]:
                raise SystemExit(2)
        elif args.command == "axentra" and args.axentra_command == "backfill-identity":
            import json

            print(json.dumps(backfill_identity(server, dry_run=args.dry_run), indent=2))
        elif args.command == "axentra" and args.axentra_command == "reconcile-identity":
            import json

            compared = reconcile_identity(server)
            print(json.dumps(compared, indent=2))
            if not compared["ok"]:
                raise SystemExit(1)
        elif args.command == "axentra":
            print("\n".join(axentra_status(server)))
        elif args.command == "db" and args.db_command == "grant-runtime-role":
            import json

            print(json.dumps(grant_runtime_role(server, args.role), indent=2))
        elif args.command == "db":
            import json

            roles = database_roles(server)
            print(json.dumps(roles, indent=2))
            if not roles["runtime"]["restricted"]:
                raise SystemExit(3)
        elif args.command == "pricing":
            print(pricing(server, args.pricing_command, getattr(args, "version", None), getattr(args, "note", None)))
    except NodestroError as error:
        raise SystemExit(f"{error.code}: {error}") from None


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "DEFAULT_PORT",
    "ServerEnvironment",
    "archive_files",
    "axentra_status",
    "backfill_identity",
    "check",
    "create_server_app",
    "create_tenant",
    "database_roles",
    "dump",
    "files_command",
    "find_user",
    "grant_runtime_role",
    "load",
    "main",
    "members",
    "migrate",
    "pricing",
    "reconcile_identity",
    "reconcile_mirrors",
    "reencrypt_secrets",
    "remove_member",
    "revoke_sessions",
    "run_worker",
    "runtime_role",
    "set_role",
    "setup_link",
]
