"""Statements of the API on tenant tables that are not bound to one tenant, by design, each with its reason
(security review SR-01, checked by :mod:`nodestro_runtime.tenant_sql_check` in ``tests/test_tenant_filters.py``).

Keys are the module and the **whole** statement text as the check rebuilds it (whitespace collapsed); matching is
exact, and an entry that no longer matches a statement fails the test. The API keeps its PostgreSQL access in the
runtime's stores, so the list is empty: a statement added here needs a reason why it cannot mix tenants.
"""

from __future__ import annotations

#: Modules without PostgreSQL statements (the check fails if one of them uses PostgreSQL), each with its reason.
SKIP_MODULES: dict[str, str] = {
    "nodestro_api/backup.py": (
        "local backups of the desktop's SQLite database (one data directory is one tenant, no tenant_id column);"
        " the server profile has no local backups"
    ),
}

#: ``(module, normalized statement) -> reason``.
ALLOWLIST: dict[tuple[str, str], str] = {}
