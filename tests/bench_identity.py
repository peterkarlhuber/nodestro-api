"""Session resolution latency per identity backend (requirement 15, item 1). Not a test.

    NODESTRO_TEST_POSTGRES=postgresql://postgres:pg@127.0.0.1:5432/postgres \\
        python tests/bench_identity.py [--tenants 50 --users 2000 --sessions 10000 --calls 3000 --seconds 6]

One schema with ``--tenants`` tenants, ``--users`` users (1 to 3 memberships each) and ``--sessions`` live sessions,
half of them last used more than a minute ago (their next use writes ``last_seen_at``). It is measured as a server
process resolves sessions (``IdentityStore.session``, what ``SessionResolver`` calls on every request):

- ``before``: ``NODESTRO_AXENTRA=off``, Nodestro's ``IdentityStore`` (one role);
- ``mirror`` / ``axentra``: Axentra on, backfilled, runtime role, ``NODESTRO_IDENTITY_BACKEND`` mirror / axentra.

Per mode: ``--calls`` sequential resolutions of random tokens, then 1, 8 and 32 threads for ``--seconds`` each on
the process's one pool (``--pool``, default 8). The ``last_seen_at`` of every session is reset to the same state
before each run, so every run writes the same share of touches. Also the statements per resolution
(``pg_stat_statements`` is not needed: the count comes from a wrapped connection).
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from typing import Any

import perf_support as ps

RESET = (
    "UPDATE nodestro_sessions SET last_seen_at = CASE WHEN get_byte(token_hash, 0) < 128"
    " THEN clock_timestamp() - interval '30 minutes' ELSE clock_timestamp() - interval '5 seconds' END"
)
RESET_AXENTRA = (
    "UPDATE axentra_sessions SET last_seen_at = CASE WHEN get_byte(decode(token_hash, 'hex'), 0) < 128"
    " THEN now() - interval '30 minutes' ELSE now() - interval '5 seconds' END"
)


def _reset(found: ps.Setup, axentra: bool) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(found.owner_url, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(found.schema)))
        connection.execute(RESET)
        if axentra:
            connection.execute(RESET_AXENTRA)


class Counted:
    """Counts the statements the identity store sends (a wrapper around the pool's connections)."""

    def __init__(self, database: Any) -> None:
        self.count = 0
        self.transactions = 0
        pool = database._pool
        original = pool.connection
        outer = self

        from contextlib import contextmanager

        @contextmanager
        def connection(*args: Any, **kwargs: Any) -> Any:
            with original(*args, **kwargs) as conn:
                execute = conn.execute

                def counted(query: Any, *a: Any, **k: Any) -> Any:
                    outer.count += 1
                    return execute(query, *a, **k)

                conn.execute = counted
                try:
                    yield conn
                finally:
                    conn.execute = execute

        pool.connection = connection


def measure_mode(found: ps.Setup, mode: str, population: ps.Population, args: argparse.Namespace) -> dict[str, Any]:
    from nodestro_api import server as command

    loaded = ps.use(ps.environment(found, mode, NODESTRO_DATABASE_POOL_SIZE=str(args.pool)))
    database = command._database(loaded)
    services = None
    if mode == "before":
        from nodestro_runtime.identity_store import IdentityStore

        identity: Any = IdentityStore(database)
    else:
        services, identity = command._platform(loaded, database)
    rng = random.Random(11)
    tokens = [token for token, _user, _tenant in population.sessions]
    result: dict[str, Any] = {"mode": mode, "pool_size": database.pool_size, "store": repr(identity)}
    try:
        # Correctness first: every seeded session resolves to its user and tenant.
        for token, user, tenant in rng.sample(population.sessions, 50):
            session = identity.session(token)
            assert session is not None and (session.user_id, session.tenant_id) == (user, tenant), (mode, session)
        _reset(found, mode != "before")
        counter = Counted(database)
        for token in rng.sample(tokens, 200):  # warm-up (caches, prepared plans), not counted
            identity.session(token)
        _reset(found, mode != "before")
        counter.count = 0
        timings = []
        sample = [rng.choice(tokens) for _ in range(args.calls)]
        for token in sample:
            started = time.perf_counter()
            identity.session(token)
            timings.append(time.perf_counter() - started)
        result["sequential"] = ps.percentiles(timings)
        result["statements_per_resolution"] = round(counter.count / len(sample), 2)
        # The same token again within the minute: no touch (the common case of a browser polling).
        counter.count = 0
        hot = tokens[:200]
        for token in hot:
            identity.session(token)
        counter.count = 0
        timings = []
        for _ in range(5):
            for token in hot:
                started = time.perf_counter()
                identity.session(token)
                timings.append(time.perf_counter() - started)
        result["sequential_no_touch"] = ps.percentiles(timings)
        result["statements_per_resolution_no_touch"] = round(counter.count / len(timings), 2)
        result["concurrent"] = {}
        for threads in args.threads:
            _reset(found, mode != "before")
            result["concurrent"][str(threads)] = _concurrent(identity, database, tokens, threads, args.seconds)
    finally:
        if services is not None:
            database.platform_events = None
            services.close()
        database.close()
    return result


def _concurrent(identity: Any, database: Any, tokens: list[str], threads: int, seconds: float) -> dict[str, Any]:
    stop = threading.Event()
    lock = threading.Lock()
    timings: list[float] = []
    errors: list[str] = []

    def loop(index: int) -> None:
        rng = random.Random(index)
        mine: list[float] = []
        while not stop.is_set():
            token = rng.choice(tokens)
            started = time.perf_counter()
            try:
                identity.session(token)
            except Exception as exc:  # pragma: no cover - reported
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}"[:200])
                continue
            mine.append(time.perf_counter() - started)
        with lock:
            timings.extend(mine)

    pool_before = dict(database._pool.get_stats())
    workers = [threading.Thread(target=loop, args=(index,)) for index in range(threads)]
    began = time.perf_counter()
    for worker in workers:
        worker.start()
    time.sleep(seconds)
    stop.set()
    for worker in workers:
        worker.join(60)
    elapsed = time.perf_counter() - began
    stats = dict(database._pool.get_stats())
    return {
        "threads": threads,
        "per_second": round(len(timings) / elapsed, 1),
        "latency_ms": ps.percentiles(timings),
        "errors": len(errors),
        "first_errors": sorted(set(errors))[:3],
        "pool_waits": int(stats.get("requests_queued", 0)) - int(pool_before.get("requests_queued", 0)),
        "pool_wait_ms_total": int(stats.get("requests_wait_ms", 0)) - int(pool_before.get("requests_wait_ms", 0)),
        "pool_timeouts": int(stats.get("requests_errors", 0)) - int(pool_before.get("requests_errors", 0)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenants", type=int, default=50)
    parser.add_argument("--users", type=int, default=2000)
    parser.add_argument("--sessions", type=int, default=10000)
    parser.add_argument("--calls", type=int, default=3000)
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--threads", type=int, nargs="+", default=[1, 8, 32])
    parser.add_argument("--pool", type=int, default=8)
    parser.add_argument("--modes", nargs="+", default=list(ps.CONFIGS))
    parser.add_argument("--name", default="identity_sessions")
    args = parser.parse_args()
    ps.require_dsn()
    found = ps.create_setup()
    result: dict[str, Any] = {
        "parameters": {k: v for k, v in vars(args).items() if k != "name"},
        "modes": {},
    }
    try:
        ps.migrate(found, "before")
        population = ps.seed_identity(found, tenants=args.tenants, users=args.users, sessions=args.sessions)
        result["population"] = {
            "tenants": len(population.tenants),
            "users": len(population.users),
            "memberships": sum(len(held) for held in population.memberships.values()),
            "sessions": len(population.sessions),
        }
        if "before" in args.modes:
            result["modes"]["before"] = measure_mode(found, "before", population, args)
            print(result["modes"]["before"], file=sys.stderr)
        if set(args.modes) & {"mirror", "axentra"}:
            from nodestro_api import server as command

            loaded = ps.migrate(found, "mirror")
            backfill: dict[str, Any] = {}
            with ps.measured(backfill, trace=False):
                backfill["report"] = command.backfill_identity(loaded, dry_run=False)
            result["backfill"] = backfill
            ps.analyze(found)
        for mode in ("mirror", "axentra"):
            if mode in args.modes:
                result["modes"][mode] = measure_mode(found, mode, population, args)
                print(result["modes"][mode], file=sys.stderr)
    finally:
        ps.teardown(found)
    ps.write_result(args.name, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
