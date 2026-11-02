"""Every PostgreSQL statement of the API on a tenant table is bound to one tenant (security review SR-01).

The check is the runtime's :mod:`nodestro_runtime.tenant_sql_check` (its rules and negative tests are in the runtime's
``tests/test_tenant_filters.py``); the tenant tables come from the runtime's migrations. Statements of the API that are
cross-tenant by design are listed in :mod:`nodestro_api.tenant_sql_allowlist` with their reasons.
"""

from __future__ import annotations

from pathlib import Path

import nodestro_runtime
from nodestro_runtime.tenant_sql_check import scan_package, tenant_tables

import nodestro_api
from nodestro_api import tenant_sql_allowlist

TABLES = tenant_tables(Path(nodestro_runtime.__file__).parent / "migrations" / "postgres")


def test_every_api_statement_on_tenant_data_is_bound_to_one_tenant() -> None:
    report = scan_package(
        Path(nodestro_api.__file__).parent, TABLES, tenant_sql_allowlist.ALLOWLIST, tenant_sql_allowlist.SKIP_MODULES
    )
    assert [str(f) for f in report.findings] == []


def test_a_leak_in_the_api_package_is_caught(tmp_path: Path) -> None:
    root = tmp_path / "nodestro_api"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "leak.py").write_text(
        'Q = "SELECT data FROM nodestro_documents WHERE collection = %s"\n'
        'J = "UPDATE nodestro_jobs SET cancel_requested = true WHERE ref = %s"\n',
        encoding="utf-8",
    )
    report = scan_package(root, TABLES, {}, {})
    assert sorted((f.module, f.line, f.kind) for f in report.findings) == [
        ("nodestro_api/leak.py", 1, "leak"),
        ("nodestro_api/leak.py", 2, "leak"),
    ]
