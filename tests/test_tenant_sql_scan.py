"""The CI scan of the API with the runtime's tenant SQL check (``scripts/tenant_sql_scan.py``, job ``tenant-sql``):
it fails when it cannot run or proves nothing, never skips."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "tenant_sql_scan.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tenant_sql_scan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# MUP-ONA T-CHK-5
def test_the_api_package_is_scanned_with_its_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    assert _script().main([]) == 0
    out = capsys.readouterr().out
    assert "planted leak reported" in out
    assert "scanned 0 files" not in out


# MUP-ONA T-CHK-3
def test_a_leak_in_the_scanned_package_fails_the_scan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    package = tmp_path / "nodestro_api"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "leak.py").write_text('Q = "SELECT data FROM nodestro_documents WHERE collection = %s"\n', "utf-8")
    assert _script().main(["--package", str(package)]) == 1
    assert "FINDING nodestro_api/leak.py:1: [leak]" in capsys.readouterr().out


# MUP-ONA T-CHK-5
def test_a_wrong_path_or_an_empty_package_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _script()
    assert script.main(["--package", str(tmp_path / "missing")]) == 1
    (tmp_path / "empty").mkdir()
    assert script.main(["--package", str(tmp_path / "empty")]) == 1
    assert "0 Python files" in capsys.readouterr().err


# MUP-ONA T-CHK-5
def test_a_missing_pglast_or_checker_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    script = _script()
    monkeypatch.setitem(sys.modules, "pglast", None)
    assert script.main([]) == 1
    assert "pglast is not installed" in capsys.readouterr().err
    monkeypatch.delitem(sys.modules, "pglast")
    monkeypatch.setitem(sys.modules, "nodestro_runtime.tenant_sql_check", None)
    assert script.main([]) == 1
    assert "not importable" in capsys.readouterr().err


# MUP-ONA T-CHK-6
def test_a_runtime_from_another_commit_fails(capsys: pytest.CaptureFixture[str]) -> None:
    script = _script()
    installed = script.installed_runtime_commit()
    assert installed, "the dev environment installs the runtime from its git pin"
    assert script.main(["--expect-runtime-commit", installed]) == 0
    assert script.main(["--expect-runtime-commit", "0" * 40]) == 1
    assert f"installed runtime is at {installed}" in capsys.readouterr().err
