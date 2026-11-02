"""Cost of the audit and usage mirrors and the run usage settlement (requirement 15, item 4). Not a test.

    NODESTRO_TEST_POSTGRES=postgresql://postgres:pg@127.0.0.1:5432/postgres \\
        python tests/bench_mirrors.py [--writes 1000 --runs 10000]

On the database of a server process (``before``: Axentra off, one role; ``after``: the Axentra configuration of
``perf_support`` with the runtime role, mirrors attached by ``nodestro_api.server._platform``):

- the latency of one approval audit event (``tool_approvals.record_audit``) and one AI usage entry
  (``UsageLog.record``, four meters), sequentially and from 8 threads, before and after; the failure path (the
  mirror raises inside its savepoint, the Nodestro record is kept);
- the run usage settlement: ``--runs`` finished runs over 50 tenants settled with ``settle_runs(full=True)`` (as
  ``axentra reconcile`` does) and the worker's periodic pass (``settle_runs()``) when nothing new finished, at two
  history sizes (it must not grow with the number of runs already settled).
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import logging
import sys
import threading
import time
from typing import Any

import perf_support as ps
import psycopg
from psycopg import sql


def _usage_record() -> Any:
    from decimal import Decimal

    from orkexa import UsageRecord

    return UsageRecord(
        agent="assistant",
        model="claude-sonnet-5-5",
        input_tokens=120,
        output_tokens=30,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        thinking_tokens=0,
        web_search_requests=0,
        web_fetch_requests=0,
        estimated_cost_usd=Decimal("0.002"),
        provider="anthropic",
    )


def _writers(database: Any, tenants: list[str]) -> dict[str, Any]:
    from nodestro_runtime.tool_approvals import ToolApprovalEvent, record_audit
    from nodestro_runtime.usage_log import UsageLog

    record = _usage_record()

    def audit(index: int) -> None:
        store = database.store(tenants[index % len(tenants)])
        record_audit(
            store,
            ToolApprovalEvent(
                kind="decided", actor="u_bench", tool="search", run_id=f"run_{index}", outcome="approved"
            ),
        )

    def usage(index: int) -> None:
        UsageLog(database.store(tenants[index % len(tenants)])).record("assistant", [record], label="bench")

    def plain(index: int) -> None:  # a Nodestro document write of the same size, for reference
        database.store(tenants[index % len(tenants)]).put_raw("bench", f"d{index}", {"n": index, "text": "x" * 300})

    return {"document_put": plain, "audit_event": audit, "usage_entry": usage}


def _sequential(fn: Any, writes: int) -> dict[str, Any]:
    for index in range(50):
        fn(10_000_000 + index)
    timings = []
    for index in range(writes):
        started = time.perf_counter()
        fn(index)
        timings.append(time.perf_counter() - started)
    return ps.percentiles(timings)


def _threaded(fn: Any, writes: int, threads: int) -> dict[str, Any]:
    timings: list[float] = []
    lock = threading.Lock()

    def loop(offset: int) -> None:
        mine = []
        for index in range(offset, writes, threads):
            started = time.perf_counter()
            fn(20_000_000 + index)
            mine.append(time.perf_counter() - started)
        with lock:
            timings.extend(mine)

    workers = [threading.Thread(target=loop, args=(offset,)) for offset in range(threads)]
    began = time.perf_counter()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    elapsed = time.perf_counter() - began
    return {"threads": threads, "writes_per_second": round(writes / elapsed, 1), "latency_ms": ps.percentiles(timings)}


def write_costs(found: ps.Setup, config: str, tenants: list[str], writes: int) -> dict[str, Any]:
    from nodestro_api import server as command

    loaded = ps.use(ps.environment(found, "before" if config == "before" else "axentra"))
    database = command._database(loaded)
    services = None
    if config != "before":
        services, _identity = command._platform(loaded, database)
        assert database.platform_events is not None
    result: dict[str, Any] = {"config": config}
    try:
        for name, fn in _writers(database, tenants).items():
            result[name] = {"sequential_ms": _sequential(fn, writes), "concurrent": _threaded(fn, writes, 8)}
        if services is not None:
            events = database.platform_events
            result["mirror_stats"] = vars(events.stats).copy()
            # The failure path: the mirror raises inside its savepoint (meters "missing"); Nodestro's entry is kept.
            logging.getLogger("nodestro_runtime.axentra_platform").setLevel(logging.CRITICAL)
            events._meters_ready = False
            usage = _writers(database, tenants)["usage_entry"]
            result["usage_entry_mirror_failing"] = {"sequential_ms": _sequential(usage, writes // 4)}
            events._meters_ready = True
            result["mirror_stats_after_failures"] = vars(events.stats).copy()
    finally:
        database.platform_events = None
        if services is not None:
            services.close()
        database.close()
    return result


def seed_runs(found: ps.Setup, tenants: list[str], runs: int, *, start: int, age: timedelta) -> None:
    """``runs`` finished top-level runs (succeeded, usage with a complete cost), ``updated_at`` ``age`` ago."""
    at = datetime.now(UTC) - age
    with psycopg.connect(found.owner_url, autocommit=True) as connection:
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(found.schema)))
        with connection.cursor().copy(
            "COPY nodestro_documents (tenant_id, collection, id, data, updated_at) FROM STDIN"
        ) as copy:
            for index in range(start, start + runs):
                run_id = f"run_{index:08d}"
                data = {
                    "id": run_id,
                    "status": "succeeded",
                    "trigger": "manual",
                    "workflow_id": "wf_bench",
                    "finished_at": at.isoformat(),
                    "usage": {
                        "requests": 2,
                        "input_tokens": 1500,
                        "output_tokens": 300,
                        "estimated_cost_usd": 0.0123,
                        "cost_complete": True,
                    },
                }
                copy.write_row((tenants[index % len(tenants)], "runs", run_id, json.dumps(data), at))
        connection.execute("ANALYZE nodestro_documents")


def settlement(found: ps.Setup, tenants: list[str], runs: int) -> dict[str, Any]:
    from nodestro_api import server as command

    result: dict[str, Any] = {}
    seed_runs(found, tenants, runs, start=0, age=timedelta(days=1))
    loaded = ps.use(ps.environment(found, "axentra"))
    database = command._database(loaded)
    services, _identity = command._platform(loaded, database)
    events = database.platform_events
    try:
        full: dict[str, Any] = {}
        with ps.measured(full):
            full["report"] = events.settle_runs(full=True)
        full["runs_per_second"] = round(runs / full["seconds"], 1)
        result["full_settlement"] = full
        again: dict[str, Any] = {}
        with ps.measured(again):
            again["report"] = events.settle_runs(full=True)
        result["full_settlement_repeated"] = again
        # The worker's periodic pass: the first one looks 7 days back (every run above is in it), the next ones
        # only at runs changed since the previous pass.
        first: dict[str, Any] = {}
        with ps.measured(first):
            first["report"] = events.settle_runs()
        result["periodic_first_pass"] = first
        idle: dict[str, Any] = {}
        with ps.measured(idle):
            idle["report"] = events.settle_runs()
        result[f"periodic_idle_pass_{runs}_settled"] = idle
        seed_runs(found, tenants, 4 * runs, start=runs, age=timedelta(days=1))
        events.settle_runs(full=True)
        idle = {}
        with ps.measured(idle):
            idle["report"] = events.settle_runs()
        result[f"periodic_idle_pass_{5 * runs}_settled"] = idle
        from nodestro_runtime.axentra_platform import reconciliation_backlog

        backlog: dict[str, Any] = {}
        with ps.measured(backlog, trace=False):
            backlog["report"] = reconciliation_backlog(services)
        result[f"reconciliation_backlog_{5 * runs}_settled"] = backlog  # axentra status, the metrics gauge
        seed_runs(found, tenants, 100, start=5 * runs, age=timedelta(seconds=0))
        new: dict[str, Any] = {}
        with ps.measured(new):
            new["report"] = events.settle_runs()
        result[f"periodic_pass_100_new_{5 * runs}_settled"] = new
    finally:
        database.platform_events = None
        services.close()
        database.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenants", type=int, default=50)
    parser.add_argument("--writes", type=int, default=1000)
    parser.add_argument("--runs", type=int, default=10000)
    parser.add_argument("--name", default="mirrors")
    args = parser.parse_args()
    ps.require_dsn()
    found = ps.create_setup()
    result: dict[str, Any] = {"parameters": {k: v for k, v in vars(args).items() if k != "name"}}
    try:
        ps.migrate(found, "before")
        population = ps.seed_identity(found, tenants=args.tenants, users=4 * args.tenants, sessions=100)
        result["before"] = write_costs(found, "before", population.tenants, args.writes)
        print(result["before"], file=sys.stderr)
        from nodestro_api import server as command

        command.backfill_identity(ps.migrate(found, "mirror"), dry_run=False)
        ps.analyze(found)
        result["after"] = write_costs(found, "after", population.tenants, args.writes)
        print(result["after"], file=sys.stderr)
        result["run_usage"] = settlement(found, population.tenants, args.runs)
        print(result["run_usage"], file=sys.stderr)
    finally:
        ps.teardown(found)
    ps.write_result(args.name, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
