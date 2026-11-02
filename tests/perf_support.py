"""Shared set-up of the load and performance scripts (``tests/bench_*.py``). Not a test module.

Every script measures the same two server configurations on a local PostgreSQL (``NODESTRO_TEST_POSTGRES``, a
superuser that creates two roles and a schema, all dropped afterwards):

- ``before``: Nodestro only, every switch off (``NODESTRO_AXENTRA=off``). One database role that owns the schema
  (as a server before the role split: the role ``migrate`` and ``api`` use is the same).
- ``mirror`` / ``axentra``: the Axentra configuration: ``NODESTRO_AXENTRA=on``, ``NODESTRO_IDENTITY_BACKEND`` mirror
  or axentra, ``NODESTRO_AXENTRA_AUDIT/USAGE/RUN_USAGE/NOTIFY=1``, ``NODESTRO_FILES_BACKEND=axentra`` (local
  directory), ``NODESTRO_SECRETS_FORMAT=axs1``, and the restricted runtime role (no superuser, no ``BYPASSRLS``, owns
  nothing) for ``api``/``worker`` with the owner role for ``migrate`` (``NODESTRO_DATABASE_MIGRATION_URL``).

Results go to ``NODESTRO_BENCH_OUT`` (default ``/mnt/project-files/axentra-migration/performance/``) as JSON.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
import contextlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")
OUT = Path(os.environ.get("NODESTRO_BENCH_OUT", "/mnt/project-files/axentra-migration/performance/"))
ISSUER = "https://login.example"
ORIGIN = "https://studio.test"
SECRET_KEY = base64.b64encode(b"s" * 32).decode()
CONFIGS = ("before", "mirror", "axentra")
ROLES = ("owner", "admin", "editor", "operator", "viewer")


def require_dsn() -> str:
    if not DSN:
        print("NODESTRO_TEST_POSTGRES is not set", file=sys.stderr)
        raise SystemExit(2)
    return DSN


def ring(kid: str = "k1") -> str:
    return f"{kid}:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _url(user: str, password: str) -> str:
    parts = conninfo_to_dict(DSN)
    parts.update(user=user, password=password)
    return make_conninfo("", **parts)


@dataclass
class Setup:
    """Two roles, a schema owned by the owner, a data directory, the keyring; dropped by :func:`teardown`."""

    schema: str
    owner: str
    runtime: str
    owner_url: str
    runtime_url: str
    root: Path
    keyring: str = field(default_factory=ring)

    @property
    def files_dir(self) -> Path:
        return self.root / "axentra-files"


def create_setup() -> Setup:
    require_dsn()
    suffix = secrets.token_hex(4)
    password = secrets.token_hex(16)
    found = Setup(
        schema=f"nodestro_bench_{suffix}",
        owner=f"nd_bench_owner_{suffix}",
        runtime=f"nd_bench_app_{suffix}",
        owner_url=_url(f"nd_bench_owner_{suffix}", password),
        runtime_url=_url(f"nd_bench_app_{suffix}", password),
        root=Path(tempfile.mkdtemp(prefix="nodestro-bench-")),
    )
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER BYPASSRLS PASSWORD {}").format(
                sql.Identifier(found.owner), sql.Literal(password)
            )
        )
        admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
                sql.Identifier(found.runtime), sql.Literal(password)
            )
        )
        admin.execute(
            sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                sql.Identifier(found.schema), sql.Identifier(found.owner)
            )
        )
    found.files_dir.mkdir(parents=True)
    (found.root / "axentra.toml").write_text(
        f'[storage]\nbackend = "local"\nlocal_path = "{found.files_dir}"\n', encoding="utf-8"
    )
    os.environ["NODESTRO_DATABASE_SCHEMA"] = found.schema
    return found


def teardown(found: Setup) -> None:
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(found.schema)))
        for role in (found.runtime, found.owner):
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
    shutil.rmtree(found.root, ignore_errors=True)


def environment(found: Setup, config: str, **changes: str) -> dict[str, str]:
    """The server environment of ``config`` (``before``, ``mirror``, ``axentra``) on ``found``."""
    environ = {
        "NODESTRO_PROFILE": "server",
        "NODESTRO_DATA_DIR": str(found.root / "server"),
        "NODESTRO_DATABASE_SCHEMA": found.schema,
        "NODESTRO_PUBLIC_ORIGIN": ORIGIN,
        "NODESTRO_OIDC_ISSUER": ISSUER,
        "NODESTRO_OIDC_CLIENT_ID": "nodestro-studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
        "NODESTRO_SECRET_KEY": SECRET_KEY,
    }
    if config == "before":
        environ["NODESTRO_DATABASE_URL"] = found.owner_url  # one role for everything, as before the role split
    else:
        environ.update(
            {
                "NODESTRO_DATABASE_URL": found.runtime_url,
                "NODESTRO_DATABASE_MIGRATION_URL": found.owner_url,
                "NODESTRO_AXENTRA": "on",
                "NODESTRO_AXENTRA_KEYRING": found.keyring,
                "NODESTRO_AXENTRA_CONFIG": str(found.root / "axentra.toml"),
                "NODESTRO_IDENTITY_BACKEND": config,
                "NODESTRO_AXENTRA_AUDIT": "1",
                "NODESTRO_AXENTRA_USAGE": "1",
                "NODESTRO_AXENTRA_RUN_USAGE": "1",
                "NODESTRO_AXENTRA_NOTIFY": "1",
                "NODESTRO_FILES_BACKEND": "axentra",
                "NODESTRO_SECRETS_FORMAT": "axs1",
            }
        )
    environ.update(changes)
    return {key: value for key, value in environ.items() if value != ""}


def use(environ: Mapping[str, str]) -> Any:
    """Make ``environ`` the process environment (every other ``NODESTRO_*`` removed); the loaded server."""
    for key in [key for key in os.environ if key.startswith(("NODESTRO_", "AXENTRA_"))]:
        if key != "NODESTRO_TEST_POSTGRES":
            del os.environ[key]
    os.environ.update(environ)
    from nodestro_api import server as command

    command._require_containers = lambda _server: None  # tool sandboxes are not part of these measurements
    return command.load()


def migrate(found: Setup, config: str) -> Any:
    """``nodestro-server migrate`` for ``config`` (switching Axentra on later counts as backed up)."""
    from nodestro_api import server as command

    loaded = use(environment(found, config))
    command.migrate(loaded, backup_dir=None, backup_taken=True)
    return loaded


# ---- data ----------------------------------------------------------------------------------------------------


@dataclass
class Population:
    tenants: list[str]
    users: list[str]
    #: (token, user, tenant) of every live session.
    sessions: list[tuple[str, str, str]]
    #: user -> tenants (with role) of every membership.
    memberships: dict[str, dict[str, str]]


def seed_identity(
    found: Setup, *, tenants: int, users: int, sessions: int, seed: int = 7, stale_share: float = 0.5
) -> Population:
    """Tenants, users (1 to 3 memberships each, every tenant one owner), live sessions; written with ``COPY`` as the
    owner role (Nodestro's tables only: the Axentra side comes from the backfill). ``stale_share`` of the sessions
    were last used more than a minute ago (their next use writes ``last_seen_at``), the rest within the minute."""
    rng = random.Random(seed)
    tenant_ids = [f"t{index:03d}" for index in range(tenants)]
    user_ids = [f"u_{index:024x}" for index in range(users)]
    memberships: dict[str, dict[str, str]] = {}
    for index, user in enumerate(user_ids):
        chosen = rng.sample(tenant_ids, k=rng.choice((1, 1, 2, 3)))
        if index < tenants:
            chosen = [tenant_ids[index], *[item for item in chosen if item != tenant_ids[index]]]
        memberships[user] = {
            tenant: ("owner" if index < tenants and position == 0 else rng.choice(ROLES[1:]))
            for position, tenant in enumerate(chosen)
        }
    now = datetime.now(UTC)
    rows = []
    found_sessions = []
    pairs = [(user, tenant) for user, held in memberships.items() for tenant in held]
    for index in range(sessions):
        user, tenant = pairs[index % len(pairs)] if index < len(pairs) else rng.choice(pairs)
        token = secrets.token_urlsafe(32)
        seen = now - (timedelta(minutes=rng.uniform(2, 90)) if rng.random() < stale_share else timedelta(seconds=5))
        rows.append((hashlib.sha256(token.encode()).digest(), user, tenant, seen, now + timedelta(hours=12)))
        found_sessions.append((token, user, tenant))
    with psycopg.connect(found.owner_url, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(found.schema)))
        with connection.transaction():
            with connection.cursor().copy("COPY nodestro_tenants (id, name) FROM STDIN") as copy:
                for tenant in tenant_ids:
                    copy.write_row((tenant, f"Tenant {tenant}"))
            with connection.cursor().copy(
                "COPY nodestro_users (id, issuer, subject, email, name, created_at) FROM STDIN"
            ) as copy:
                for index, user in enumerate(user_ids):
                    copy.write_row((user, ISSUER, f"sub-{index}", f"user{index}@example.com", f"User {index}", now))
            with connection.cursor().copy("COPY nodestro_memberships (tenant_id, user_id, role) FROM STDIN") as copy:
                for user, held in memberships.items():
                    for tenant, role in held.items():
                        copy.write_row((tenant, user, role))
            with connection.cursor().copy(
                "COPY nodestro_sessions (token_hash, user_id, tenant_id, last_seen_at, expires_at) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
        connection.execute("ANALYZE")
    return Population(tenant_ids, user_ids, found_sessions, memberships)


def analyze(found: Setup) -> None:
    with psycopg.connect(found.owner_url, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(found.schema)))
        connection.execute("ANALYZE")


# ---- measuring -----------------------------------------------------------------------------------------------


def percentiles(values: list[float], scale: float = 1000.0) -> dict[str, float | int | None]:
    """count, p50, p95, p99, max, mean of ``values`` (seconds), in ms by default."""
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    ordered = sorted(values)

    def at(share: float) -> float:
        return round(scale * ordered[min(len(ordered) - 1, int(len(ordered) * share))], 3)

    return {
        "n": len(ordered),
        "p50": at(0.5),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": round(scale * ordered[-1], 3),
        "mean": round(scale * statistics.mean(ordered), 3),
    }


def memory() -> dict[str, float]:
    """This process's resident set now and at its peak (MiB, from ``/proc/self/status``)."""
    found: dict[str, float] = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                key, value = line.split(":", 1)
                found[{"VmRSS": "rss_mib", "VmHWM": "rss_peak_mib"}[key]] = round(int(value.split()[0]) / 1024, 1)
    except OSError:
        pass
    return found


def reset_peak_rss() -> None:
    """Reset ``VmHWM`` to the current RSS (Linux: write 5 to ``/proc/self/clear_refs``); best effort."""
    with contextlib.suppress(OSError):
        Path("/proc/self/clear_refs").write_text("5")


@contextmanager
def measured(result: dict[str, Any], *, trace: bool = True) -> Iterator[None]:
    """Wall time, CPU time, peak RSS growth and (``trace``) the tracemalloc peak of the block into ``result``."""
    import resource
    import tracemalloc

    reset_peak_rss()
    before = memory()
    cpu = resource.getrusage(resource.RUSAGE_SELF)
    if trace:
        tracemalloc.start()
    started = time.perf_counter()
    try:
        yield
    finally:
        result["seconds"] = round(time.perf_counter() - started, 3)
        after = resource.getrusage(resource.RUSAGE_SELF)
        result["cpu_seconds"] = round(after.ru_utime + after.ru_stime - cpu.ru_utime - cpu.ru_stime, 3)
        if trace:
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            result["tracemalloc_peak_mib"] = round(peak / 2**20, 2)
        now = memory()
        result["rss_peak_growth_mib"] = round(now.get("rss_peak_mib", 0) - before.get("rss_mib", 0), 1)


def machine() -> dict[str, Any]:
    with psycopg.connect(DSN, autocommit=True) as admin:
        version = admin.execute("SHOW server_version").fetchone()
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "postgres": version[0] if version else None,
        "axentra": _git(Path(__file__).resolve().parents[2] / "axentra"),
        "nodestro_runtime": _git(Path(__file__).resolve().parents[2] / "nodestro-runtime"),
        "nodestro_api": _git(Path(__file__).resolve().parents[1]),
        # Where the imported code came from (a run against other sources says so here).
        "imported_from": {name: _origin(name) for name in ("nodestro_runtime", "nodestro_api", "axentra")},
    }


def _origin(module: str) -> str | None:
    import importlib.util

    spec = importlib.util.find_spec(module)
    return str(Path(spec.origin).parent) if spec is not None and spec.origin else None


def _git(path: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def write_result(name: str, result: dict[str, Any]) -> Path:
    """Write ``result`` to ``OUT/<name>.json`` (and read it back: the target may be a network mount)."""
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"{name}.json"
    payload = {"benchmark": name, "at": datetime.now(UTC).isoformat(), "machine": machine(), **result}
    target.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    json.loads(target.read_text(encoding="utf-8"))
    print(f"wrote {target}", file=sys.stderr)
    return target


def child_env() -> dict[str, str]:
    """The environment for a child Python process of a benchmark (same interpreter, same ``PYTHONPATH``)."""
    environ = dict(os.environ)
    tests = str(Path(__file__).resolve().parent)
    environ["PYTHONPATH"] = os.pathsep.join(item for item in (tests, environ.get("PYTHONPATH", "")) if item)
    return environ


# ---- child processes: an API server and a worker with a fake model ---------------------------------------------


class PoolSampler:
    """Samples the connection pools of a process every ``every`` seconds: connections in use (``pool_size -
    pool_available``), waiting requests; plus the cumulative counters at the end."""

    def __init__(self, pools: dict[str, Any], every: float = 0.01) -> None:
        import threading

        self.pools = pools
        self.every = every
        self.samples: dict[str, list[tuple[int, int, int]]] = {name: [] for name in pools}
        self.start_stats = {name: dict(pool.get_stats()) for name, pool in pools.items()}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            for name, pool in self.pools.items():
                stats = pool.get_stats()
                size, available = int(stats.get("pool_size", 0)), int(stats.get("pool_available", 0))
                self.samples[name].append((size - available, int(stats.get("requests_waiting", 0)), size))
            time.sleep(self.every)

    def reset(self) -> None:
        for name, pool in self.pools.items():
            self.samples[name] = []
            self.start_stats[name] = dict(pool.get_stats())

    def report(self) -> dict[str, Any]:
        found = {}
        for name, pool in self.pools.items():
            stats = dict(pool.get_stats())
            samples = self.samples[name]
            used = [item[0] for item in samples] or [0]
            waiting = [item[1] for item in samples] or [0]
            found[name] = {
                "pool_max": int(stats.get("pool_max", 0)),
                "in_use_max": max(used),
                "in_use_mean": round(statistics.mean(used), 2),
                "open_max": max((item[2] for item in samples), default=0),
                "waiting_max": max(waiting),
                "waiting_mean": round(statistics.mean(waiting), 3),
                "samples": len(samples),
                "requests": int(stats.get("requests_num", 0)) - int(self.start_stats[name].get("requests_num", 0)),
                "requests_queued": int(stats.get("requests_queued", 0))
                - int(self.start_stats[name].get("requests_queued", 0)),
                "requests_wait_ms": int(stats.get("requests_wait_ms", 0))
                - int(self.start_stats[name].get("requests_wait_ms", 0)),
                "timeouts": int(stats.get("requests_errors", 0))
                - int(self.start_stats[name].get("requests_errors", 0)),
            }
        return found

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)


def _write_stats(path: str, data: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(data, default=str), encoding="utf-8")


def serve_api(port: int, stats_path: str) -> None:
    """The API process of the current environment (``create_server_app``) on ``127.0.0.1:port``; on SIGTERM it writes
    its pool samples and memory to ``stats_path``. SIGUSR1 resets the pool samples (after the warm-up)."""
    import signal

    import uvicorn

    started = memory()
    from nodestro_api import server as command

    command._require_containers = lambda _server: None
    imported = memory()
    app = command.create_server_app()
    built = memory()
    database = app.state.health_check.database
    sampler = PoolSampler({"pool": database._pool})
    signal.signal(signal.SIGUSR1, lambda *_: sampler.reset())
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    # uvicorn re-raises a captured SIGTERM after shutting down, which would end the process before the stats are
    # written: stop it through should_exit instead.
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
    signal.signal(signal.SIGTERM, lambda *_: setattr(server, "should_exit", True))
    try:
        server.run()
    finally:
        sampler.stop()
        _write_stats(
            stats_path,
            {
                "pools": sampler.report(),
                "memory": {"start": started, "imported": imported, "app_built": built, "end": memory()},
            },
        )


class FakeModel:
    """A model client that answers every request after ``delay`` seconds, appending one line per call (the run's
    marker, the process id) to ``calls`` so that duplicate calls across processes can be counted."""

    def __init__(self, delay: float, calls: str) -> None:
        import threading
        from types import SimpleNamespace

        self.delay = delay
        self.calls = calls
        self._lock = threading.Lock()
        self.messages = self
        self.beta = SimpleNamespace(messages=self)

    def _answer(self, request: dict[str, Any]) -> Any:
        import re

        from nodestro_runtime.testing import text

        found = re.search(r"run-[0-9a-f]{12}", json.dumps(request.get("messages", ""), default=str))
        marker = found.group(0) if found else "?"
        with self._lock, open(self.calls, "a", encoding="utf-8") as handle:
            handle.write(f"{marker} {os.getpid()} {time.time():.3f}\n")
        time.sleep(self.delay)
        return text(f"done {marker}")

    def create(self, **request: Any) -> Any:
        return self._answer(request)

    parse = create

    def stream(self, **request: Any) -> Any:
        from nodestro_runtime.testing import _Stream

        return _Stream(self._answer(request))

    def count_tokens(self, **request: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(input_tokens=100)


def serve_worker(stats_path: str, delay: float, calls: str) -> None:
    """``nodestro-server worker`` of the current environment with a fake model (``FakeModel``): the same composition
    as :func:`nodestro_api.server.run_worker`. On SIGTERM it drains, then writes pool samples and memory."""
    import asyncio
    import signal

    started = memory()
    from nodestro_api import server as command
    from nodestro_api.server_app import TenantFiles
    from nodestro_api.worker import ServerWorker, WorkerSettings

    command._require_containers = lambda _server: None
    imported = memory()
    server = command.load()
    database = command._ready(server)
    mirrors, identity = command._platform(server, database)
    mirrors, notifications, _metrics = command._observed(server, database, mirrors, identity)
    model = FakeModel(delay, calls)
    files = TenantFiles(
        database=database,
        data_root=server.profile.data_dir,
        docs_dir=server.docs_dir,
        secrets=server.settings.secret_box(),
        endpoints=server.endpoints,
        client_factory=lambda _provider: model,
        integrations=[],
        files=command._input_files(server, database, mirrors),
    )
    timing = dict(server.worker)
    settings = WorkerSettings(
        lease=timedelta(seconds=timing.pop("lease")) if "lease" in timing else WorkerSettings.lease,
        **server.capacity,
        **timing,
    )
    worker = ServerWorker(files, identity, settings)
    events = database.platform_events
    if events is not None and getattr(events, "run_usage_enabled", False):
        worker.settlement = events
    worker.notifications = notifications
    built = memory()
    sampler = PoolSampler({"pool": database._pool})

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        loop.add_signal_handler(signal.SIGUSR1, sampler.reset)
        print("worker ready", flush=True)
        await worker.serve(stop)

    try:
        asyncio.run(main())
    finally:
        sampler.stop()
        report = sampler.report()
        database.close()
        if mirrors is not None:
            mirrors.close()
        _write_stats(
            stats_path,
            {
                "pools": report,
                "memory": {"start": started, "imported": imported, "worker_built": built, "end": memory()},
            },
        )


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Child:
    """A child process of this module (``serve`` or ``work``) with the current ``os.environ``."""

    def __init__(self, *argv: str, stats: Path, log: Path) -> None:
        self.stats = stats
        self.log = log
        self._log = open(log, "w", encoding="utf-8")  # noqa: SIM115 - closed in stop()
        self.process = subprocess.Popen(
            [sys.executable, __file__, *argv, str(stats)],
            env=child_env(),
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )

    def signal(self, signum: int) -> None:
        self.process.send_signal(signum)

    def stop(self, timeout: float = 60) -> dict[str, Any]:
        import signal

        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(10)
        self._log.close()
        try:
            return dict(json.loads(self.stats.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {"error": "no stats", "log": self.log.read_text(encoding="utf-8")[-2000:]}

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(10)
        self._log.close()


def start_api(port: int, workdir: Path, label: str) -> Child:
    child = Child("serve", str(port), stats=workdir / f"{label}.stats.json", log=workdir / f"{label}.log")
    import httpx

    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if child.process.poll() is not None:
            raise RuntimeError(f"the API process ended: {child.log.read_text(encoding='utf-8')[-3000:]}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/auth/me", timeout=1).status_code in (401, 403):
                return child
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    child.kill()
    raise RuntimeError("the API process did not start")


def start_worker(workdir: Path, label: str, *, delay: float, calls: Path) -> Child:
    child = Child("work", str(delay), str(calls), stats=workdir / f"{label}.stats.json", log=workdir / f"{label}.log")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if child.process.poll() is not None:
            raise RuntimeError(f"the worker ended: {child.log.read_text(encoding='utf-8')[-3000:]}")
        if "worker ready" in child.log.read_text(encoding="utf-8"):
            return child
        time.sleep(0.2)
    child.kill()
    raise RuntimeError("the worker did not start")


def cookie(token: str) -> dict[str, str]:
    from nodestro_api.identity import SESSION_COOKIE

    return {"Cookie": f"{SESSION_COOKIE}={token}", "Origin": ORIGIN}


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve_api(int(sys.argv[2]), sys.argv[3])
    elif sys.argv[1] == "work":
        serve_worker(sys.argv[4], float(sys.argv[2]), sys.argv[3])
