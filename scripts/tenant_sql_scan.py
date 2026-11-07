"""Scan the API package with the installed runtime's tenant SQL check (MUP-ONA A11, CI job ``tenant-sql``).

Usage::

    python scripts/tenant_sql_scan.py [--package src/nodestro_api] [--expect-runtime-commit <sha>]

The check is :mod:`nodestro_runtime.tenant_sql_check` of whatever ``nodestro-runtime`` the running Python has
installed: the pinned one (the dev environment) or the current runtime ``master`` (a separate environment with only
the runtime and ``pglast``; CI step "tenant-sql (runtime master)"). The API package is read from its source
directory, never imported, so the separate environment needs nothing of the API. The allowlist and the skipped
modules come from the package's own ``tenant_sql_allowlist.py``.

The script fails (exit code 1) when the check cannot run or proves nothing: the runtime's checker or ``pglast`` is
not importable, the migrations list no tenant table, the package holds no Python file, a planted leak in a copy of
the package is not reported (self-test), or the scan reports a finding. With ``--expect-runtime-commit`` it also
fails when the installed runtime was not installed from that commit (``direct_url.json`` of its distribution).
``NODESTRO_REQUIRE_TENANT_SCAN=1`` is what CI sets; the script never skips, the variable only appears in the output
so the log shows the scan was required.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType
from typing import Any

DEFAULT_PACKAGE = Path(__file__).resolve().parent.parent / "src" / "nodestro_api"
#: The planted leak of the self-test: a tenant table read without a tenant filter.
PLANTED_LEAK = 'MUP_ONA_PLANTED = "SELECT * FROM nodestro_chunks WHERE namespace = %s"\n'


class ScanError(Exception):
    """The scan could not run or proves nothing."""


def _load_checker() -> tuple[ModuleType, Path]:
    try:
        import nodestro_runtime

        tenant_sql_check = importlib.import_module("nodestro_runtime.tenant_sql_check")
    except ImportError as error:
        raise ScanError(f"the runtime's tenant SQL check is not importable: {error}") from error
    try:
        importlib.import_module("pglast")
    except ImportError as error:
        raise ScanError(f"pglast is not installed, the check cannot parse SQL: {error}") from error
    return tenant_sql_check, Path(nodestro_runtime.__file__).parent


def installed_runtime_commit() -> str | None:
    """The commit ``nodestro-runtime`` was installed from (``direct_url.json``), or None for a non-VCS install."""
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        text = distribution("nodestro-runtime").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not text:
        return None
    info: dict[str, Any] = json.loads(text)
    commit = info.get("vcs_info", {}).get("commit_id")
    return str(commit) if commit else None


def _allowlist(package: Path) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    path = package / "tenant_sql_allowlist.py"
    if not path.is_file():
        return {}, {}
    spec = importlib.util.spec_from_file_location("_mup_ona_tenant_sql_allowlist", path)
    if spec is None or spec.loader is None:
        raise ScanError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(getattr(module, "ALLOWLIST", {})), dict(getattr(module, "SKIP_MODULES", {}))


def scan(package: Path, expect_commit: str | None = None) -> list[str]:
    """Run the scan; return the report lines. Raises :class:`ScanError` when the scan cannot run or proves nothing,
    and returns findings as lines starting with ``FINDING``."""
    checker, runtime_dir = _load_checker()
    commit = installed_runtime_commit()
    lines = [
        f"runtime: {runtime_dir} (commit {commit or 'unknown'})",
        f"NODESTRO_REQUIRE_TENANT_SCAN={os.environ.get('NODESTRO_REQUIRE_TENANT_SCAN', '')}",
    ]
    if expect_commit is not None and commit != expect_commit:
        raise ScanError(f"the installed runtime is at {commit or 'unknown'}, expected {expect_commit}")
    tables = checker.tenant_tables(runtime_dir / "migrations" / "postgres")
    if not tables:
        raise ScanError("the runtime's migrations list no tenant table")
    if not package.is_dir():
        raise ScanError(f"{package} is not a directory")
    files = sorted(package.rglob("*.py"))
    if not files:
        raise ScanError(f"0 Python files under {package}")
    allowlist, skip_modules = _allowlist(package)

    # Self-test: the same check on a copy of the package with a planted leak must report it.
    with tempfile.TemporaryDirectory() as temp:
        copy = Path(temp) / package.name
        shutil.copytree(package, copy, ignore=shutil.ignore_patterns("__pycache__"))
        (copy / "mup_ona_planted_leak.py").write_text(PLANTED_LEAK, encoding="utf-8")
        planted = checker.scan_package(copy, tables, allowlist, skip_modules)
        caught = [f for f in planted.findings if f.module.endswith("/mup_ona_planted_leak.py") and f.kind == "leak"]
        if not caught:
            raise ScanError("self-test failed: a planted leak in a copy of the package was not reported")

    report = checker.scan_package(package, tables, allowlist, skip_modules)
    lines.append(
        f"scanned {len(files)} files under {package}: {len(tables)} tenant tables, {len(report.checked)} statements"
        f" checked, {len(report.allowed)} allowed, {len(report.findings)} findings; planted leak reported"
    )
    lines.extend(f"FINDING {finding}" for finding in report.findings)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE, help="the package directory to scan")
    parser.add_argument("--expect-runtime-commit", default=None, help="fail unless the runtime is at this commit")
    args = parser.parse_args(argv)
    try:
        lines = scan(args.package, args.expect_runtime_commit)
    except ScanError as error:
        print(f"tenant SQL scan failed: {error}", file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 1 if any(line.startswith("FINDING") for line in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
