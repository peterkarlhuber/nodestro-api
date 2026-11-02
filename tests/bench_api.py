"""API latency of authenticated endpoints before and after Axentra (requirement 15, items 2 and 7). Not a test.

    NODESTRO_TEST_POSTGRES=postgresql://postgres:pg@127.0.0.1:5432/postgres \\
        python tests/bench_api.py [--iterations 300 --seconds 8 --threads 8 32]

The real API process (``nodestro_api.server.create_server_app`` under uvicorn, a child process, pool 8) of each
configuration (see ``perf_support``: ``before``, ``mirror``, ``axentra``) on one schema with 50 tenants, 2000 users and
10000 sessions (the Axentra side from ``axentra backfill-identity``). A client in this process (httpx, HTTP/1.1 keep-
alive, the session cookie) measures:

- sequentially, ``--iterations`` calls each: ``GET /api/auth/me``, ``POST /api/auth/switch`` (a new session, the old
  one ended), ``GET /api/workflows`` (a list of 20), ``POST /api/inputs`` (a 10 KB text file, multipart),
  ``GET /api/inputs/{id}/content``;
- concurrently, ``--threads`` clients for ``--seconds`` each, a mix of 40 % me, 40 % list, 15 % download, 5 % upload
  over 200 sessions of members who may edit.

The API process reports its pool samples and its memory (start, after imports, after building the app, at the end).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import random
import sys
import threading
import time
from typing import Any

import httpx
import perf_support as ps

FLOW = {
    "name": "Flow",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "ALPHA"}},
        {"id": "out", "type": "output"},
    ],
    "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
}
EDITORS = ("owner", "admin", "editor")
#: The session token each switching user holds now (a switch ends the old session).
CURRENT: dict[str, str] = {}
PAYLOAD = ("Lorem ipsum dolor sit amet. " * 400)[:10240].encode()


class Session:
    """One browser: a session token (replaced by a tenant switch)."""

    def __init__(self, base: str, token: str) -> None:
        self.token = token
        self.client = httpx.Client(base_url=base, timeout=60)

    def call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {**ps.cookie(self.token), **kwargs.pop("headers", {})}
        answer = self.client.request(method, path, headers=headers, **kwargs)
        if answer.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {answer.status_code} {answer.text[:300]}")
        for value in answer.headers.get_list("set-cookie"):
            if value.startswith("__Host-nodestro-session=") and "Max-Age=0" not in value:
                self.token = value.split("=", 1)[1].split(";", 1)[0]
        return answer


def _upload(session: Session) -> str:
    answer = session.call(
        "POST", "/api/inputs", files={"files": ("notes.txt", PAYLOAD, "text/plain")}, data={"origin": "studio"}
    )
    found = answer.json()
    assert found and found[0].get("file"), found
    return str(found[0]["file"]["id"])


def _timed(fn: Any, iterations: int) -> dict[str, Any]:
    for _ in range(min(20, iterations)):
        fn()
    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn()
        timings.append(time.perf_counter() - started)
    return ps.percentiles(timings)


def measure(
    config: str, found: ps.Setup, population: ps.Population, args: argparse.Namespace, workdir: Path
) -> dict[str, Any]:
    ps.use(ps.environment(found, config, NODESTRO_DATABASE_POOL_SIZE=str(args.pool)))
    port = ps.free_port()
    child = ps.start_api(port, workdir, f"api-{config}")
    base = f"http://127.0.0.1:{port}"
    result: dict[str, Any] = {"config": config}
    try:
        rng = random.Random(3)
        # A member with an editing role in two tenants (for the switch) and editor sessions (for the mix).
        user = next(
            user
            for user, held in population.memberships.items()
            if len(held) >= 2 and all(role in EDITORS for role in held.values())
        )
        tenants = list(population.memberships[user])
        token = CURRENT.get(user) or next(
            token for token, owner, tenant in population.sessions if owner == user and tenant == tenants[0]
        )
        main = Session(base, token)
        if config == "before":
            for _ in range(20):
                main.call("POST", "/api/workflows", json=FLOW)
        file_id = _upload(main)
        target = [tenants[1]]

        def switch() -> None:
            main.call("POST", "/api/auth/switch", json={"tenant_id": target[0]})
            target[0] = tenants[0] if target[0] == tenants[1] else tenants[1]

        sequential = {
            "GET /api/auth/me": _timed(lambda: main.call("GET", "/api/auth/me"), args.iterations),
            "GET /api/workflows": _timed(lambda: main.call("GET", "/api/workflows"), args.iterations),
            "POST /api/inputs (10 KB)": _timed(lambda: _upload(main), args.iterations),
            "GET /api/inputs/{id}/content": _timed(
                lambda: main.call("GET", f"/api/inputs/{file_id}/content"), args.iterations
            ),
        }
        # Switching back and forth ends with the browser in its first tenant again (the file belongs to it).
        sequential["POST /api/auth/switch"] = _timed(switch, args.iterations - args.iterations % 2)
        if target[0] == tenants[0]:
            switch()
        CURRENT[user] = main.token  # the switches ended the seeded session: the next configuration goes on with this
        result["sequential_ms"] = sequential
        editors = [
            (token, tenant)
            for token, owner, tenant in population.sessions
            if population.memberships[owner][tenant] in EDITORS and owner != user
        ]
        chosen = rng.sample(editors, 200)
        files: dict[str, str] = {}
        for token, tenant in chosen:
            if tenant not in files:
                files[tenant] = _upload(Session(base, token))
        result["concurrent"] = {}
        for threads in args.threads:
            child.signal(__import__("signal").SIGUSR1)  # pool samples of this run only
            result["concurrent"][str(threads)] = _mix(base, chosen, files, threads, args.seconds)
    finally:
        stats = child.stop()
    result["api_process"] = stats
    return result


def _mix(base: str, chosen: list[tuple[str, str]], files: dict[str, str], threads: int, seconds: float) -> Any:
    stop = threading.Event()
    lock = threading.Lock()
    timings: dict[str, list[float]] = {"me": [], "list": [], "download": [], "upload": []}
    errors: list[str] = []

    def loop(index: int) -> None:
        rng = random.Random(index)
        sessions = [(Session(base, token), tenant) for token, tenant in chosen[index::threads]]
        mine: dict[str, list[float]] = {key: [] for key in timings}
        while not stop.is_set():
            session, tenant = rng.choice(sessions)
            pick = rng.random()
            kind = "me" if pick < 0.4 else "list" if pick < 0.8 else "download" if pick < 0.95 else "upload"
            started = time.perf_counter()
            try:
                if kind == "me":
                    session.call("GET", "/api/auth/me")
                elif kind == "list":
                    session.call("GET", "/api/workflows")
                elif kind == "download":
                    session.call("GET", f"/api/inputs/{files[tenant]}/content")
                else:
                    _upload(session)
            except Exception as exc:
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}"[:200])
                continue
            mine[kind].append(time.perf_counter() - started)
        with lock:
            for key, values in mine.items():
                timings[key].extend(values)

    workers = [threading.Thread(target=loop, args=(index,)) for index in range(threads)]
    began = time.perf_counter()
    for worker in workers:
        worker.start()
    time.sleep(seconds)
    stop.set()
    for worker in workers:
        worker.join(120)
    elapsed = time.perf_counter() - began
    total = sum(len(values) for values in timings.values())
    return {
        "threads": threads,
        "requests_per_second": round(total / elapsed, 1),
        "latency_ms": {key: ps.percentiles(values) for key, values in timings.items()},
        "all_ms": ps.percentiles([value for values in timings.values() for value in values]),
        "errors": len(errors),
        "first_errors": sorted(set(errors))[:3],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenants", type=int, default=50)
    parser.add_argument("--users", type=int, default=2000)
    parser.add_argument("--sessions", type=int, default=10000)
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--threads", type=int, nargs="+", default=[8, 32])
    parser.add_argument("--pool", type=int, default=8)
    parser.add_argument("--configs", nargs="+", default=list(ps.CONFIGS))
    parser.add_argument("--name", default="api_latency")
    args = parser.parse_args()
    ps.require_dsn()
    found = ps.create_setup()
    result: dict[str, Any] = {"parameters": {k: v for k, v in vars(args).items() if k != "name"}, "configs": {}}
    workdir = found.root / "work"
    workdir.mkdir()
    try:
        ps.migrate(found, "before")
        population = ps.seed_identity(found, tenants=args.tenants, users=args.users, sessions=args.sessions)
        migrated = False
        for config in args.configs:
            if config != "before" and not migrated:
                from nodestro_api import server as command

                command.backfill_identity(ps.migrate(found, "mirror"), dry_run=False)
                ps.analyze(found)
                migrated = True
            result["configs"][config] = measure(config, found, population, args, workdir)
            print(config, result["configs"][config].get("sequential_ms"), file=sys.stderr)
    finally:
        ps.teardown(found)
    ps.write_result(args.name, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
