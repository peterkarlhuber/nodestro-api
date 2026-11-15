"""The embedded web interface is an unchanged copy of one nodestro-app-ui commit (NAL-C2, scripts/vendor_app_ui.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import shutil
import sys
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "vendor_app_ui.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("vendor_app_ui", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["vendor_app_ui"] = module
    spec.loader.exec_module(module)
    return module


vendor = _module()


def test_the_embedded_interface_matches_its_manifest() -> None:
    record = vendor.check()
    assert len(record["Commit"]) == 40
    manifest = vendor.check_tree(vendor.WEBUI)
    paths = {item["path"] for item in manifest["files"]}
    assert {"index.html", "app.js", "connection.js", "markdown.js", "app.css", "i18n/en.json"} <= paths
    assert manifest["languages"] == ["de", "en", "es", "fr", "it", "pt-BR"]


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    target = tmp_path / "webui"
    shutil.copytree(vendor.WEBUI, target, ignore=shutil.ignore_patterns("__pycache__"))
    vendor.check(target)
    return target


def test_an_edited_file_is_refused(copy: Path) -> None:
    (copy / "app.js").write_text((copy / "app.js").read_text("utf-8") + "\n// local edit\n", "utf-8")
    with pytest.raises(vendor.VendorError, match=re.escape("changed: app.js")):
        vendor.check(copy)


def test_an_extra_or_missing_file_is_refused(copy: Path) -> None:
    (copy / "extra.js").write_text("1", "utf-8")
    with pytest.raises(vendor.VendorError, match=re.escape("not in the manifest: extra.js")):
        vendor.check(copy)
    (copy / "extra.js").unlink()
    (copy / "markdown.js").unlink()
    with pytest.raises(vendor.VendorError, match=re.escape("missing: markdown.js")):
        vendor.check(copy)


def test_a_manifest_other_than_the_recorded_one_is_refused(copy: Path) -> None:
    manifest = copy / "manifest.json"
    manifest.write_text(manifest.read_text("utf-8").replace('"version": "', '"version": "9', 1), "utf-8")
    with pytest.raises(vendor.VendorError, match=re.escape("is not the one VENDORED.md names")):
        vendor.check(copy)


def test_unsafe_manifest_paths_and_foreign_contracts_are_refused(tmp_path: Path) -> None:
    for path in ("../app.js", "/etc/passwd", ".hidden", "a/../../b"):
        with pytest.raises(vendor.VendorError, match="unsafe path"):
            vendor._safe(path)
    (tmp_path / "manifest.json").write_text(
        '{"name": "nodestro-app-ui", "version": "9.0.0", "contract": {"min": 2, "max": 3}, "files": []}', "utf-8"
    )
    with pytest.raises(vendor.VendorError, match="speaks contract"):
        vendor.check_tree(tmp_path)


def test_vendoring_needs_a_full_commit_and_a_clean_checkout(tmp_path: Path) -> None:
    with pytest.raises(vendor.VendorError, match="full 40-character SHA"):
        vendor.vendor("abc123", source=tmp_path, folder=tmp_path / "webui")
