"""A process that the crash tests kill (``tests/test_crash_recovery.py``, API-022): it is started as its own Python
process and ends with SIGKILL, as a crashed or killed API process would.

``python crash_child.py delete-desktop DATA_DIR WORKFLOW_ID``
    Opens the desktop context on ``DATA_DIR`` and deletes the workflow; in the middle of the delete's transaction (after
    the workflow, its versions and its schedules were deleted) the process kills itself.
``python crash_child.py delete-server SCHEMA DATA_ROOT TENANT WORKFLOW_ID``
    The same on a server tenant's PostgreSQL stores (``NODESTRO_TEST_POSTGRES``).
``python crash_child.py test-series SCHEMA DATA_ROOT TENANT SUITE_ID``
    Starts the test series on the server tenant with leases of one second; every model request blocks. It prints
    ``started <benchmark id>`` and ``model called`` and waits to be killed.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import ScriptedClient


def _kill(*_args: Any, **_kwargs: Any) -> None:
    print("crashing", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)


def _server_context(schema: str, data_root: str, tenant: str, client: Any = None) -> Any:
    from nodestro_runtime.storage_postgres import PostgresDatabase

    from nodestro_api.server_app import TenantFiles

    database = PostgresDatabase(os.environ["NODESTRO_TEST_POSTGRES"], schema=schema)
    files = TenantFiles(
        database=database,
        data_root=Path(data_root),
        integrations=[],
        client_factory=(lambda _provider: client) if client is not None else None,
    )
    return files.open(tenant)


def _delete(ctx: Any, workflow_id: str) -> None:
    from nodestro_api import api

    # After the workflow, its versions and its schedules, inside the one store transaction.
    ctx.benchmarks.suites = _kill
    api.delete_workflow(workflow_id, ctx)
    print("not killed", flush=True)


def main(argv: list[str]) -> None:
    mode = argv[0]
    if mode == "delete-desktop":
        from nodestro_api.context import AppConfig, Nodestro

        _delete(Nodestro(AppConfig(data_dir=Path(argv[1])), integrations=[]), argv[2])
    elif mode == "delete-server":
        _delete(_server_context(argv[1], argv[2], argv[3]), argv[4])
    elif mode == "test-series":

        def blocked(_request: dict[str, Any]) -> Any:
            print("model called", flush=True)
            time.sleep(3600)

        ctx = _server_context(argv[1], argv[2], argv[3], ScriptedClient({"ALPHA": [blocked] * 20}))
        ctx.benchmarks.lease_s = 1.0
        ctx.batches.lease_s = 1.0

        async def run() -> None:
            from nodestro_api.benchmarks import BenchmarkStart

            bench = await ctx.benchmarks.start(argv[4], BenchmarkStart(judge=False, concurrency=1))
            print(f"started {bench.id}", flush=True)
            await asyncio.sleep(3600)

        asyncio.run(run())
    elif mode == "tick":
        _tick(argv[1], argv[2], Path(argv[3]))
    else:
        raise SystemExit(f"unknown mode {mode}")


def _tick(schema: str, data_root: str, gate: Path) -> None:
    import json

    from nodestro_runtime.identity_store import IdentityStore
    from nodestro_runtime.storage_postgres import PostgresDatabase

    from nodestro_api import schedules
    from nodestro_api.server_app import TenantFiles
    from nodestro_api.worker import ServerWorker

    database = PostgresDatabase(os.environ["NODESTRO_TEST_POSTGRES"], schema=schema)
    # The runs are only queued here; the client is never asked (the provider key check needs one).
    files = TenantFiles(
        database=database,
        data_root=Path(data_root),
        integrations=[],
        client_factory=lambda _provider: ScriptedClient({}),
    )
    worker = ServerWorker(files, IdentityStore(database))
    original = schedules.launch

    async def gated(*args: Any, **kwargs: Any) -> Any:
        print("launching", flush=True)
        while not gate.exists():
            await asyncio.sleep(0.02)
        return await original(*args, **kwargs)

    schedules.launch = gated  # type: ignore[assignment]
    fired = asyncio.run(worker.fire_schedules())
    print(f"fired {json.dumps(fired)} leader={worker.leader.held}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
