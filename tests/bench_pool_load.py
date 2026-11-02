"""Connections and pool use of the API and worker processes under combined load (requirement 15, items 3 and 7).
Not a test.

    NODESTRO_TEST_POSTGRES=postgresql://postgres:pg@127.0.0.1:5432/postgres \\
        python tests/bench_pool_load.py [--pools 4 8 16 --seconds 15 --threads 16 --workers 2]

Per configuration (``before``, ``axentra``: see ``perf_support``) and pool size
(``NODESTRO_DATABASE_POOL_SIZE`` of every process): one API process (``create_server_app`` under uvicorn) and
``--workers`` worker processes (fake model, 100 ms). Load for ``--seconds``: ``--threads`` browser threads with the
mix of ``bench_api`` (me, list, download, upload) and one thread queuing runs (``POST /api/workflows/{id}/runs``, up
to ``--runs-per-second``) that the workers execute. Each process samples its pool every 10 ms: connections in use
(max, mean), waiting requests, requests that had to wait and for how long, and pool timeouts; plus the server's
backends of both roles in ``pg_stat_activity``.
"""

from __future__ import annotations

import argparse
import random
import secrets
import signal
import statistics
import sys
import threading
import time
from typing import Any

from bench_api import EDITORS, Session, _mix, _upload
from bench_workers import WORKER_ENV, Fixture, _wait_done
import httpx
import perf_support as ps
import psycopg


def _backends(found: ps.Setup, stop: threading.Event, samples: list[int]) -> None:
    with psycopg.connect(ps.DSN, autocommit=True) as admin:
        while not stop.is_set():
            row = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = ANY(%s)", ([found.owner, found.runtime],)
            ).fetchone()
            samples.append(int(row[0]) if row else 0)
            time.sleep(0.05)


def measure(
    config: str, pool: int, found: ps.Setup, fixture: Fixture, chosen: list[tuple[str, str]], args: argparse.Namespace
) -> dict[str, Any]:
    workdir = found.root / "work"
    ps.use(ps.environment(found, config, NODESTRO_DATABASE_POOL_SIZE=str(pool), **WORKER_ENV))
    port = ps.free_port()
    label = f"{config}-{pool}"
    api = ps.start_api(port, workdir, f"api-{label}")
    base = f"http://127.0.0.1:{port}"
    workers: list[ps.Child] = []
    queued: list[str] = []
    enqueue_ms: list[float] = []
    try:
        fixture.prepare(base)
        files = {}
        for token, tenant in chosen:
            if tenant not in files:
                files[tenant] = _upload(Session(base, token))
        workers = [
            ps.start_worker(workdir, f"worker-{label}-{index}", delay=0.1, calls=workdir / f"calls-{label}.txt")
            for index in range(args.workers)
        ]
        for child in [api, *workers]:
            child.signal(signal.SIGUSR1)  # samples of the load only
        stop = threading.Event()
        backends: list[int] = []
        sampler = threading.Thread(target=_backends, args=(found, stop, backends))
        sampler.start()

        def enqueue() -> None:
            tenants = list(fixture.workflows)
            rng = random.Random(1)
            with httpx.Client(base_url=base, timeout=60) as client:
                while not stop.is_set():
                    tenant = rng.choice(tenants)
                    started = time.perf_counter()
                    answer = client.post(
                        f"/api/workflows/{fixture.workflows[tenant]}/runs",
                        json={"input": f"run-{secrets.token_hex(6)}"},
                        headers=ps.cookie(fixture.sessions[tenant]),
                    )
                    if answer.status_code == 201:
                        queued.append(str(answer.json()["id"]))
                        enqueue_ms.append(time.perf_counter() - started)
                    time.sleep(max(0.0, 1 / args.runs_per_second - (time.perf_counter() - started)))

        queuer = threading.Thread(target=enqueue)
        queuer.start()
        mix = _mix(base, chosen, files, args.threads, args.seconds)
        stop.set()
        queuer.join(60)
        sampler.join(10)
        jobs = _wait_done(found, set(queued), 300)
    finally:
        worker_stats = [child.stop() for child in workers]
        api_stats = api.stop()
    claim = [(job[4] - job[3]).total_seconds() for job in jobs if job[4] is not None]
    return {
        "config": config,
        "pool_size": pool,
        "api_mix": mix,
        "runs_queued": len(queued),
        "enqueue_ms": ps.percentiles(enqueue_ms),
        "claim_latency_ms": ps.percentiles(claim),
        "job_states": {state: sum(1 for job in jobs if job[1] == state) for state in {job[1] for job in jobs}},
        "server_backends": {"max": max(backends or [0]), "mean": round(statistics.mean(backends or [0]), 1)},
        "api_pool": api_stats.get("pools", {}).get("pool"),
        "worker_pools": [stats.get("pools", {}).get("pool") for stats in worker_stats],
        "api_memory": api_stats.get("memory"),
        "worker_memory": [stats.get("memory") for stats in worker_stats],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pools", type=int, nargs="+", default=[4, 8, 16])
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--runs-per-second", type=float, default=5.0)
    parser.add_argument("--configs", nargs="+", default=["before", "axentra"])
    parser.add_argument("--name", default="pool_load")
    args = parser.parse_args()
    ps.require_dsn()
    found = ps.create_setup()
    (found.root / "work").mkdir()
    result: dict[str, Any] = {"parameters": {k: v for k, v in vars(args).items() if k != "name"}, "runs": []}
    try:
        ps.migrate(found, "before")
        population = ps.seed_identity(found, tenants=50, users=2000, sessions=10000)
        fixture = Fixture(population)
        rng = random.Random(3)
        editors = [
            (token, tenant)
            for token, owner, tenant in population.sessions
            if population.memberships[owner][tenant] in EDITORS and tenant not in fixture.sessions
        ]
        chosen = rng.sample(editors, 200)
        migrated = False
        for config in args.configs:
            if config != "before" and not migrated:
                from nodestro_api import server as command

                command.backfill_identity(ps.migrate(found, "mirror"), dry_run=False)
                ps.analyze(found)
                migrated = True
            for pool in args.pools:
                found_result = measure(config, pool, found, fixture, chosen, args)
                result["runs"].append(found_result)
                print(
                    config,
                    pool,
                    found_result["api_mix"]["requests_per_second"],
                    found_result["api_mix"]["all_ms"],
                    found_result["api_pool"],
                    found_result["worker_pools"],
                    file=sys.stderr,
                )
    finally:
        ps.teardown(found)
    ps.write_result(args.name, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
