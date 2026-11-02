"""Large-dataset migration and reconciliation commands, and their resumability (requirement 15, item 5). Not a test.

    NODESTRO_TEST_POSTGRES=postgresql://postgres:pg@127.0.0.1:5432/postgres \\
        python tests/bench_migrations.py [--tenants 50 --users 2000 --sessions 10000 --secrets 5000 --files 2000]

A server database written with Axentra off (``before``): ``--tenants`` tenants, ``--users`` users, ``--sessions``
live sessions, ``--secrets`` MCP server documents with a v1-encrypted token, ``--files`` input files on the data
volume (each upload stores two blobs: the file and its extraction), ``--records`` approval audit events, AI usage
entries and finished runs. Then Axentra is switched on (``migrate``, the Axentra configuration of ``perf_support``)
and the operator commands run as ``nodestro-server`` runs them:

``axentra backfill-identity`` (also ``--dry-run``), ``axentra reconcile-identity``, ``secrets reencrypt`` (v1 ->
axs1), ``files migrate`` and ``files verify``, ``axentra reconcile`` (audit, usage, run usage).

Two passes on two fresh databases: ``clean`` (each command once, then again: the rerun must find nothing to do) and
``killed`` (each command started as a ``nodestro-server`` process and killed with SIGKILL once about 40 % of its rows
are written, then run again to the end; afterwards the counts are checked: nothing missing, nothing twice). Per run:
wall and CPU seconds, rows per second, the peak RSS growth of the process.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets as random_secrets
import signal
import subprocess
import sys
import time
from typing import Any

import perf_support as ps
import psycopg
from psycopg import sql

KILL_SHARE = 0.4


def _owner(found: ps.Setup) -> psycopg.Connection[Any]:
    connection = psycopg.connect(found.owner_url, autocommit=True)
    connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(found.schema)))
    return connection


def _count(found: ps.Setup, query: str, params: tuple[Any, ...] = ()) -> int:
    with _owner(found) as connection:
        row = connection.execute(query, params).fetchone()
    return int(row[0]) if row else 0


# ---- the dataset ----------------------------------------------------------------------------------------------


def seed(found: ps.Setup, args: argparse.Namespace) -> dict[str, Any]:
    from nodestro_runtime.server_settings import SecretBox

    ps.migrate(found, "before")
    population = ps.seed_identity(found, tenants=args.tenants, users=args.users, sessions=args.sessions)
    tenants = population.tenants
    box = SecretBox(base64.b64decode(ps.SECRET_KEY))
    with (
        _owner(found) as connection,
        connection.cursor().copy("COPY nodestro_documents (tenant_id, collection, id, data) FROM STDIN") as copy,
    ):
        for index in range(args.secrets):
            tenant = tenants[index % len(tenants)]
            server_id = f"mcp_{index:06d}"
            data = {
                "id": server_id,
                "name": f"Server {index}",
                "url": "https://mcp.example/sse",
                "token_sealed": box.encrypt(tenant, f"mcp:{server_id}", f"token-{index}"),
            }
            copy.write_row((tenant, "mcp_servers", server_id, json.dumps(data)))
    _seed_records(found, tenants, args.records)
    _seed_files(found, tenants, args.files)
    ps.analyze(found)
    return {
        "tenants": len(tenants),
        "users": len(population.users),
        "memberships": sum(len(held) for held in population.memberships.values()),
        "sessions": len(population.sessions),
        "secret_documents": args.secrets,
        "input_files": args.files,
        "volume_blobs": 2 * args.files,
        "audit_events": args.records,
        "usage_entries": args.records,
        "finished_runs": args.records,
    }


def _seed_records(found: ps.Setup, tenants: list[str], count: int) -> None:
    """Approval audit events and AI usage entries written by Nodestro (no mirror yet), and finished runs."""
    from datetime import datetime, timedelta, timezone

    from nodestro_runtime.schemas import Usage
    from nodestro_runtime.tool_approvals import AUDIT, ToolApprovalEvent
    from nodestro_runtime.usage_log import UsageEntry

    at = datetime.now(timezone.utc) - timedelta(days=2)
    with (
        _owner(found) as connection,
        connection.cursor().copy(
            "COPY nodestro_documents (tenant_id, collection, id, data, updated_at) FROM STDIN"
        ) as copy,
    ):
        for index in range(count):
            tenant = tenants[index % len(tenants)]
            event = ToolApprovalEvent(
                kind="decided", actor="u_bench", tool="search", run_id=f"run_{index}", outcome="approved", at=at
            )
            copy.write_row((tenant, AUDIT, f"{event.at.isoformat()}_{event.id}", event.model_dump_json(), at))
            entry = UsageEntry(
                source="assistant",
                model="anthropic:claude-sonnet-5-5",
                at=at,
                usage=Usage(
                    requests=1, input_tokens=120, output_tokens=30, estimated_cost_usd=0.002, cost_complete=True
                ),
            )
            copy.write_row((tenant, "usage_log", entry.id, entry.model_dump_json(), at))
            run = {
                "id": f"run_{index:08d}",
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
            copy.write_row((tenant, "runs", run["id"], json.dumps(run), at))


def _seed_files(found: ps.Setup, tenants: list[str], count: int) -> None:
    """Input files through Nodestro's own ``InputFiles.add`` on the data volume (Axentra off)."""
    from nodestro_api import server as command
    from nodestro_api.server_app import TenantFiles

    loaded = ps.use(ps.environment(found, "before"))
    database = command._database(loaded)
    try:
        files = TenantFiles(database=database, data_root=loaded.profile.data_dir, integrations=[])
        contexts = {tenant: files.open(tenant) for tenant in tenants}
        for index in range(count):
            tenant = tenants[index % len(tenants)]
            body = f"File {index}\n".encode() + random_secrets.token_bytes(2048).hex().encode()
            contexts[tenant].inputs.add(body, name=f"notes-{index}.txt", media_type="text/plain")
        for context in contexts.values():
            context.close()
    finally:
        database.close()


# ---- the commands --------------------------------------------------------------------------------------------


def _commands(found: ps.Setup) -> dict[str, dict[str, Any]]:
    """Per operator command: its argv, the rows it writes (progress) and the total it reaches."""
    return {
        "backfill-identity": {
            "argv": ["axentra", "backfill-identity"],
            "progress": "SELECT count(*) FROM nodestro_axentra_ids WHERE kind = 'user'",
        },
        "secrets-reencrypt": {
            "argv": ["secrets", "reencrypt"],
            "progress": "SELECT count(*) FROM nodestro_documents WHERE collection = 'mcp_servers'"
            " AND data ->> 'token_sealed' LIKE 'axs1.%%'",
        },
        "files-migrate": {
            "argv": ["files", "migrate"],
            "progress": "SELECT count(*) FROM nodestro_documents WHERE collection = 'file_objects'",
        },
        "axentra-reconcile": {
            "argv": ["axentra", "reconcile"],
            "progress": "SELECT count(*) FROM axentra_usage_events",
        },
    }


def _run_in_process(argv: list[str]) -> dict[str, Any]:
    """``nodestro-server ARGV`` in this process; its JSON report, wall/CPU seconds and peak RSS growth."""
    import contextlib
    import io

    from nodestro_api import server as command

    out = io.StringIO()
    result: dict[str, Any] = {}
    with ps.measured(result, trace=False), contextlib.redirect_stdout(out):
        try:
            command.main(argv)
        except SystemExit as stopped:
            result["exit_code"] = stopped.code
    text = out.getvalue().strip()
    try:
        result["report"] = json.loads(text)
    except ValueError:
        result["output"] = text[-2000:]
    return result


def _killed(found: ps.Setup, name: str, spec: dict[str, Any], total: int) -> dict[str, Any]:
    """Start the command as its own process, SIGKILL it once ``KILL_SHARE`` of ``total`` rows are written."""
    code = (
        "import sys; from nodestro_api import server as s; s._require_containers = lambda _s: None; "
        f"s.main({spec['argv']!r})"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code], env=ps.child_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    began = time.perf_counter()
    killed_at = None
    done = 0
    while process.poll() is None:
        done = _count(found, spec["progress"])
        if done >= KILL_SHARE * total:
            process.send_signal(signal.SIGKILL)
            killed_at = time.perf_counter() - began
            break
        time.sleep(0.05)
    process.wait(30)
    stderr = process.stderr.read().decode(errors="replace")[-1500:] if process.stderr else ""
    return {
        "killed_after_s": round(killed_at, 2) if killed_at is not None else None,
        "rows_when_killed": done,
        "rows_after_kill": _count(found, spec["progress"]),
        "total": total,
        "exit_code": process.returncode,
        "finished_before_kill": killed_at is None,
        "stderr_tail": stderr if killed_at is None else "",
    }


def _checks(found: ps.Setup, data: dict[str, Any]) -> dict[str, Any]:
    """Counts after the commands: everything once on Axentra's side, nothing twice."""
    users = data["users"]

    def one(query: str) -> int:
        return _count(found, query)

    return {
        "linked_users": one("SELECT count(*) FROM nodestro_axentra_ids WHERE kind = 'user'"),
        "axentra_users": one("SELECT count(*) FROM axentra_users"),
        "users_expected": users,
        "duplicate_identities": one(
            "SELECT count(*) FROM (SELECT provider, subject FROM axentra_user_identities"
            " GROUP BY provider, subject HAVING count(*) > 1) d"
        ),
        "axentra_sessions_live": one("SELECT count(*) FROM axentra_sessions WHERE revoked_at IS NULL"),
        "sessions_expected": data["sessions"],
        "axentra_memberships": one("SELECT count(*) FROM axentra_memberships"),
        "memberships_expected": data["memberships"],
        "secrets_axs1": one(
            "SELECT count(*) FROM nodestro_documents WHERE collection = 'mcp_servers'"
            " AND data ->> 'token_sealed' LIKE 'axs1.%%'"
        ),
        "secrets_expected": data["secret_documents"],
        "file_mappings": one("SELECT count(*) FROM nodestro_documents WHERE collection = 'file_objects'"),
        "axentra_file_rows": one("SELECT count(*) FROM axentra_files"),
        "file_mappings_expected": data["volume_blobs"],
        "usage_events": one("SELECT count(*) FROM axentra_usage_events"),
        # One usage entry: requests, input, output, cost; one run: the same four meters.
        "usage_events_expected": 4 * data["usage_entries"] + 4 * data["finished_runs"],
        "duplicate_usage_keys": one(
            "SELECT count(*) FROM (SELECT idempotency_key FROM axentra_usage_events"
            " GROUP BY tenant_id, idempotency_key HAVING count(*) > 1) d"
        ),
        "mirrored_audit_events": one(
            "SELECT count(DISTINCT details ->> 'nodestro_event') FROM axentra_audit_events"
            " WHERE action LIKE 'nodestro.tool_approval.%%'"
        ),
        "audit_rows": one("SELECT count(*) FROM axentra_audit_events WHERE action LIKE 'nodestro.tool_approval.%%'"),
        "audit_expected": data["audit_events"],
    }


def run_pass(kind: str, args: argparse.Namespace) -> dict[str, Any]:
    found = ps.create_setup()
    result: dict[str, Any] = {"pass": kind}
    try:
        data = seed(found, args)
        result["data"] = data
        ps.migrate(found, "mirror")  # NODESTRO_AXENTRA=on, both schemas; the identity backend stays mirror
        ps.use(ps.environment(found, "mirror"))
        totals = {
            "backfill-identity": data["users"],
            "secrets-reencrypt": data["secret_documents"],
            "files-migrate": data["volume_blobs"],
            "axentra-reconcile": 4 * data["usage_entries"] + 4 * data["finished_runs"],
        }
        rows = {
            "backfill-identity": data["tenants"] + data["users"] + data["memberships"] + data["sessions"],
            "secrets-reencrypt": data["secret_documents"],
            "files-migrate": data["volume_blobs"],
            "axentra-reconcile": data["audit_events"] + data["usage_entries"] + data["finished_runs"],
        }
        commands = _commands(found)
        if kind == "clean":
            result["backfill-identity --dry-run"] = _run_in_process(["axentra", "backfill-identity", "--dry-run"])
        for name, spec in commands.items():
            entry: dict[str, Any] = {}
            if kind == "killed":
                entry["killed"] = _killed(found, name, spec, totals[name])
                entry["resumed"] = _run_in_process(spec["argv"])
            else:
                entry["run"] = _run_in_process(spec["argv"])
                entry["run"]["rows_per_second"] = round(rows[name] / max(entry["run"]["seconds"], 1e-9), 1)
                entry["rows"] = rows[name]
            entry["rerun"] = _run_in_process(spec["argv"])
            result[name] = entry
            print(
                name,
                {
                    k: {kk: vv for kk, vv in v.items() if kk != "report"}
                    for k, v in entry.items()
                    if isinstance(v, dict)
                },
                file=sys.stderr,
            )
            ps.analyze(found)
        result["reconcile-identity"] = _run_in_process(["axentra", "reconcile-identity"])
        result["files-verify"] = _run_in_process(["files", "verify"])
        result["secrets-reencrypt --dry-run"] = _run_in_process(["secrets", "reencrypt", "--dry-run"])
        result["axentra-reconcile --dry-run"] = _run_in_process(["axentra", "reconcile", "--dry-run"])
        result["checks"] = _checks(found, data)
        print(kind, result["checks"], file=sys.stderr)
    finally:
        ps.teardown(found)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenants", type=int, default=50)
    parser.add_argument("--users", type=int, default=2000)
    parser.add_argument("--sessions", type=int, default=10000)
    parser.add_argument("--secrets", type=int, default=5000)
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--records", type=int, default=2000)
    parser.add_argument("--passes", nargs="+", default=["clean", "killed"])
    parser.add_argument("--name", default="migrations")
    args = parser.parse_args()
    ps.require_dsn()
    result: dict[str, Any] = {"parameters": {k: v for k, v in vars(args).items() if k != "name"}}
    for kind in args.passes:
        result[kind] = run_pass(kind, args)
    ps.write_result(args.name, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
