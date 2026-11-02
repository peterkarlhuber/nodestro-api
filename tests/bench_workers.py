"""Worker parallelism and fault tolerance under load, before and after Axentra (requirement 15, items 6 and 7).
Not a test.

    NODESTRO_TEST_POSTGRES=postgresql://postgres:pg@127.0.0.1:5432/postgres \\
        python tests/bench_workers.py [--workers 1 2 4 --runs 200 --model-ms 200]

Real processes: the API (``create_server_app`` under uvicorn) queues runs (``POST /api/workflows/{id}/runs``) and
``--workers`` worker processes (the composition of ``nodestro-server worker``, with a fake model that answers after
``--model-ms`` and writes one line per call) execute them; ``NODESTRO_WORKER_MAX_RUNS=4``, poll 0.2 s (a worker
executes one run at a time; its other slots hold runs that wait for a tool approval). Per
configuration (``before``, ``axentra``: see ``perf_support``) and worker count: makespan of ``--runs`` runs queued at
once over 10 tenants, runs per second, claim latency (job ``started_at - created_at``), model calls per run.

Fault tolerance: 3 workers, ``--runs`` runs with a slow model; one worker is killed (SIGKILL) once a third of the runs
finished. Expected: no run is executed twice (at most one model call per run), the killed worker's runs end as
``run.interrupted`` after the lease (5 s), every other run succeeds, the other workers keep going.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import random
import secrets
import signal
import sys
import time
from typing import Any

from bench_api import FLOW
import httpx
import perf_support as ps
import psycopg
from psycopg import sql

WORKER_ENV = {
    "NODESTRO_WORKER_MAX_RUNS": "4",
    "NODESTRO_WORKER_MAX_WAITING": "1",
    "NODESTRO_WORKER_POLL_SECONDS": "0.2",
    "NODESTRO_WORKER_REAP_SECONDS": "1",
    "NODESTRO_WORKER_LEASE_SECONDS": "5",
}


def _jobs(found: ps.Setup) -> list[tuple[Any, ...]]:
    with psycopg.connect(found.owner_url, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(found.schema)))
        return connection.execute(
            "SELECT ref, status, worker, created_at, started_at, finished_at, attempts FROM nodestro_jobs"
            " WHERE kind = 'run' ORDER BY id"
        ).fetchall()


def _runs(found: ps.Setup, refs: set[str]) -> Counter[str]:
    with psycopg.connect(found.owner_url, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(found.schema)))
        rows = connection.execute(
            "SELECT id, data ->> 'status', data ->> 'error_code' FROM nodestro_documents WHERE collection = 'runs'"
        ).fetchall()
    return Counter(f"{status}:{code}" if code else str(status) for run_id, status, code in rows if run_id in refs)


def _calls(path: Path) -> Counter[str]:
    if not path.exists():
        return Counter()
    return Counter(line.split()[0] for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


class Fixture:
    """Workflows of 10 tenants and an editor session in each."""

    def __init__(self, population: ps.Population) -> None:
        self.population = population
        self.sessions: dict[str, str] = {}
        for token, user, tenant in population.sessions:
            if tenant not in self.sessions and population.memberships[user][tenant] in ("owner", "admin"):
                self.sessions[tenant] = token
            if len(self.sessions) == 10:
                break
        self.workflows: dict[str, str] = {}

    def prepare(self, base: str) -> None:
        if self.workflows:
            return
        with httpx.Client(base_url=base, timeout=60) as client:
            for tenant, token in self.sessions.items():
                answer = client.put(
                    "/api/settings",
                    json={"providers": {"anthropic": {"api_key": "sk-bench-not-a-key"}}},
                    headers=ps.cookie(token),
                )
                assert answer.status_code == 200, answer.text
                answer = client.post("/api/workflows", json=FLOW, headers=ps.cookie(token))
                assert answer.status_code == 201, answer.text
                self.workflows[tenant] = str(answer.json()["id"])

    def enqueue(self, base: str, count: int) -> tuple[list[str], list[float]]:
        tenants = list(self.workflows)

        def one(index: int) -> tuple[str, float]:
            tenant = tenants[index % len(tenants)]
            marker = f"run-{secrets.token_hex(6)}"
            started = time.perf_counter()
            with httpx.Client(base_url=base, timeout=60) as client:
                answer = client.post(
                    f"/api/workflows/{self.workflows[tenant]}/runs",
                    json={"input": marker},
                    headers=ps.cookie(self.sessions[tenant]),
                )
            assert answer.status_code == 201, answer.text
            return str(answer.json()["id"]), time.perf_counter() - started

        with ThreadPoolExecutor(8) as pool:
            done = list(pool.map(one, range(count)))
        return [run_id for run_id, _ in done], [seconds for _, seconds in done]


def _wait_done(found: ps.Setup, refs: set[str], timeout: float) -> list[tuple[Any, ...]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = [job for job in _jobs(found) if job[0] in refs]
        if len(jobs) >= len(refs) and all(job[1] not in ("queued", "running") for job in jobs):
            return jobs
        time.sleep(0.25)
    raise RuntimeError("the runs did not finish in time")


def throughput(
    config: str, found: ps.Setup, fixture: Fixture, workers: int, args: argparse.Namespace, workdir: Path
) -> dict[str, Any]:
    ps.use(ps.environment(found, config, **WORKER_ENV))
    port = ps.free_port()
    api = ps.start_api(port, workdir, f"api-{config}-{workers}")
    base = f"http://127.0.0.1:{port}"
    calls = workdir / f"calls-{config}-{workers}.txt"
    children: list[ps.Child] = []
    try:
        fixture.prepare(base)
        children = [
            ps.start_worker(workdir, f"worker-{config}-{workers}-{index}", delay=args.model_ms / 1000, calls=calls)
            for index in range(workers)
        ]
        began = time.perf_counter()
        refs, enqueue = fixture.enqueue(base, args.runs)
        jobs = _wait_done(found, set(refs), 600)
        makespan = time.perf_counter() - began
    finally:
        stats = [child.stop() for child in children]
        api_stats = api.stop()
    claim = [(job[4] - job[3]).total_seconds() for job in jobs if job[4] is not None]
    run_seconds = [(job[5] - job[4]).total_seconds() for job in jobs if job[4] is not None and job[5] is not None]
    made = _calls(calls)
    return {
        "config": config,
        "workers": workers,
        "runs": len(refs),
        "makespan_s": round(makespan, 2),
        "runs_per_second": round(len(refs) / makespan, 2),
        # A worker executes one run at a time (its other slots only hold runs that wait for an approval).
        "model_bound_runs_per_second": round(workers / (args.model_ms / 1000), 2),
        "enqueue_ms": ps.percentiles(enqueue),
        "claim_latency_ms": ps.percentiles(claim),
        "job_seconds_ms": ps.percentiles(run_seconds),
        "job_states": dict(Counter(job[1] for job in jobs)),
        "run_states": dict(_runs(found, set(refs))),
        "model_calls": sum(made.values()),
        "runs_with_more_than_one_call": sum(1 for count in made.values() if count > 1),
        "per_worker": sorted(Counter(str(job[2]) for job in jobs).values()),
        "worker_processes": stats,
        "api_process": api_stats,
    }


def fault(config: str, found: ps.Setup, fixture: Fixture, args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    ps.use(ps.environment(found, config, **WORKER_ENV))
    port = ps.free_port()
    api = ps.start_api(port, workdir, f"api-{config}-fault")
    base = f"http://127.0.0.1:{port}"
    calls = workdir / f"calls-{config}-fault.txt"
    delay = args.fault_model_ms / 1000
    children: list[ps.Child] = []
    try:
        fixture.prepare(base)
        children = [
            ps.start_worker(workdir, f"worker-{config}-fault-{index}", delay=delay, calls=calls) for index in range(3)
        ]
        refs, _ = fixture.enqueue(base, args.runs)
        wanted = set(refs)
        victim = children[0]
        while True:
            jobs = [job for job in _jobs(found) if job[0] in wanted]
            if sum(1 for job in jobs if job[1] == "done") >= len(refs) // 3:
                break
            time.sleep(0.05)
        held = [job[0] for job in _jobs(found) if job[1] == "running" and job[0] in wanted]
        victim_name = _worker_of(found, victim, held)
        victim.process.send_signal(signal.SIGKILL)
        victim.process.wait(10)
        killed_at = time.perf_counter()
        killed_running = [job[0] for job in _jobs(found) if job[1] == "running" and job[2] == victim_name]
        done_at_kill = sum(1 for job in _jobs(found) if job[1] == "done" and job[0] in wanted)
        jobs = _wait_done(found, wanted, 600)
        recovered_s = time.perf_counter() - killed_at
    finally:
        stats = [child.stop() for child in children[1:]]
        if children:
            children[0]._log.close()
        api.stop()
    made = _calls(calls)
    states = _runs(found, wanted)
    by_ref = {job[0]: job for job in jobs}
    return {
        "config": config,
        "workers": 3,
        "runs": len(refs),
        "model_ms": args.fault_model_ms,
        "killed_worker_running_jobs": len(killed_running),
        "done_when_killed": done_at_kill,
        "seconds_until_all_settled_after_kill": round(recovered_s, 2),
        "job_states": dict(Counter(job[1] for job in jobs)),
        "run_states": dict(states),
        "killed_jobs_states": dict(Counter(by_ref[ref][1] for ref in killed_running)),
        "model_calls": sum(made.values()),
        "runs_with_more_than_one_call": sum(1 for count in made.values() if count > 1),
        "finished_by_survivors": sum(1 for job in jobs if job[1] == "done" and job[2] != victim_name),
        "survivor_processes": stats,
    }


def _worker_of(found: ps.Setup, child: ps.Child, running: list[str]) -> str | None:
    """The job table's name of ``child`` (``host:pid:random``, see ``nodestro_runtime.jobs.worker_name``)."""
    pid = f":{child.process.pid}:"
    return next((str(job[2]) for job in _jobs(found) if job[2] and pid in str(job[2])), None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--runs", type=int, default=200)
    parser.add_argument("--model-ms", type=float, default=200)
    parser.add_argument("--fault-model-ms", type=float, default=500)
    parser.add_argument("--configs", nargs="+", default=["before", "axentra"])
    parser.add_argument("--name", default="workers")
    args = parser.parse_args()
    ps.require_dsn()
    found = ps.create_setup()
    result: dict[str, Any] = {
        "parameters": {k: v for k, v in vars(args).items() if k != "name"} | {"worker_env": WORKER_ENV},
        "throughput": [],
        "fault": [],
    }
    random.seed(5)
    workdir = found.root / "work"
    workdir.mkdir()
    try:
        ps.migrate(found, "before")
        population = ps.seed_identity(found, tenants=10, users=200, sessions=1000)
        fixture = Fixture(population)
        migrated = False
        for config in args.configs:
            if config != "before" and not migrated:
                from nodestro_api import server as command

                command.backfill_identity(ps.migrate(found, "mirror"), dry_run=False)
                ps.analyze(found)
                migrated = True
            for workers in args.workers:
                found_result = throughput(config, found, fixture, workers, args, workdir)
                result["throughput"].append(found_result)
                print({k: v for k, v in found_result.items() if "process" not in k}, file=sys.stderr)
            found_fault = fault(config, found, fixture, args, workdir)
            result["fault"].append(found_fault)
            print({k: v for k, v in found_fault.items() if "process" not in k}, file=sys.stderr)
    finally:
        ps.teardown(found)
    ps.write_result(args.name, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
