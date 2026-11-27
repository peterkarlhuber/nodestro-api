"""The data-only extension store's admin routes (API-081, runtime RT-098): only a signed-in person with ``admin``
writes, every upload is checked by the runtime before anything is written, nothing is installed into or imported in
this process, and every change is audited."""

from __future__ import annotations

import base64
from collections.abc import Callable
import hashlib
import io
from pathlib import Path
import sys
from typing import Any
import zipfile

from fastapi.testclient import TestClient
from nodestro_runtime import activity
import pytest

from nodestro_api.permissions import ALL_PERMISSIONS, Permission, Principal

EXTENSIONS = "/api/extensions"
MODULE = "acme_store_tools"
SOURCE = b"""
from nodestro_runtime.extensions import Integration

open(__file__ + ".imported-in-host", "w").close()  # import-time code: must never run in the API process

integration = Integration(name="acme")
"""


def wheel(version: str = "1.0", *, extra: dict[str, bytes] | None = None, name: str = "acme-store") -> bytes:
    """A pure-Python wheel with a correct ``RECORD`` (``extra``: further members, listed too)."""
    info = f"{name.replace('-', '_')}-{version}.dist-info"
    members = {
        f"{MODULE}/__init__.py": SOURCE,
        **(extra or {}),
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/entry_points.txt": f"[nodestro.integrations]\nacme = {MODULE}:integration\n".encode(),
    }
    rows = [
        f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')},{len(data)}"
        for path, data in members.items()
    ]
    members[f"{info}/RECORD"] = ("\n".join([*rows, f"{info}/RECORD,,"]) + "\n").encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, data in members.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def upload(client: TestClient, data: bytes, **fields: str) -> Any:
    form = {"sha256": sha(data), "origin": "https://example.org/acme", **fields}
    return client.post(EXTENSIONS, files={"file": ("acme.whl", data, "application/octet-stream")}, data=form)


def _as(client: TestClient, principal: Principal) -> None:
    client.app.state.principal_resolver = lambda _connection: principal  # type: ignore[attr-defined]


def _workspace(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


def _never_in_this_process(client: TestClient) -> None:
    assert MODULE not in sys.modules
    root = _workspace(client).extension_store.root
    assert not [entry for entry in sys.path if str(root) in entry]
    assert not list(root.rglob("*.imported-in-host"))


def test_an_administrator_uploads_lists_updates_removes_and_revokes(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    empty = client.get(EXTENSIONS).json()
    assert empty["enabled"] is True and empty["tools_switch"] is False and empty["packages"] == []
    first = wheel()
    created = upload(client, first)
    assert created.status_code == 201, created.text
    assert created.json()["sha256"] == sha(first) and created.json()["installed_by"] == "local"
    listed = client.get(EXTENSIONS).json()["packages"]
    # Installed after the workspace opened: listed for its tools at the next start, locked until then.
    assert [(item["sha256"], item["state"], item["reason"]) for item in listed] == [(sha(first), "locked", "pending")]
    assert (_workspace(client).extension_store.root / sha(first) / MODULE / "__init__.py").is_file()
    second = wheel("1.1")
    assert upload(client, second).status_code == 422  # one version per distribution: update it instead
    updated = upload(client, second, replaces=sha(first))
    assert updated.status_code == 201 and updated.json()["replaces"] == sha(first)
    removed = client.delete(f"{EXTENSIONS}/{sha(second)}")
    assert removed.status_code == 200 and removed.json()["packages"] == []
    assert client.delete(f"{EXTENSIONS}/{sha(second)}").json()["code"] == "extension_store.not_found"
    revoked = client.post(f"{EXTENSIONS}/{sha(first)}/revoke")
    assert [item["sha256"] for item in revoked.json()["revoked"]] == [sha(first)]
    again = upload(client, first)
    assert (again.status_code, again.json()["code"], again.json()["params"]) == (
        422,
        "extension_store.refused",
        {"reason": "revoked"},
    )
    actions = sorted(
        (event.action, event.outcome)
        for event in activity.query(_workspace(client).store)
        if event.source == "extension"
    )
    assert actions == [
        ("extension_package.installed", "success"),
        ("extension_package.refused", "denied"),
        ("extension_package.refused", "denied"),
        ("extension_package.removed", "success"),
        ("extension_package.revoked", "success"),
        ("extension_package.updated", "success"),
    ]
    _never_in_this_process(client)


@pytest.mark.parametrize(
    ("make", "fields", "status", "reason"),
    [
        (lambda: wheel(), {"sha256": "0" * 64}, 422, "hash_mismatch"),
        (lambda: wheel(extra={"evil.pth": b"import os\n"}), {}, 422, "unsafe_member"),
        (lambda: wheel(extra={"acme_store-1.0.data/scripts/x": b"#!/bin/sh\n"}), {}, 422, "data_part"),
        (lambda: b"not a wheel", {}, 422, "archive"),
    ],
)
def test_an_unsafe_upload_is_refused_and_nothing_is_written(
    make_app: Callable[..., TestClient], make: Callable[[], bytes], fields: dict[str, str], status: int, reason: str
) -> None:
    client = make_app()
    response = upload(client, make(), **fields)
    assert (response.status_code, response.json()["code"], response.json()["params"]) == (
        status,
        "extension_store.refused",
        {"reason": reason},
    )
    assert client.get(EXTENSIONS).json()["packages"] == []
    _never_in_this_process(client)


def test_malformed_fields_are_refused_before_the_store(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    data = wheel()
    assert upload(client, data, sha256="ABC").status_code == 422
    assert upload(client, data, origin="").status_code == 422
    assert upload(client, data, replaces="../../etc").status_code == 422
    assert client.get(EXTENSIONS).json()["packages"] == []


def test_only_a_person_with_admin_changes_the_store(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    data = wheel()
    runner = Principal(id="runner", permissions=frozenset({Permission.READ, Permission.RUN, Permission.EDIT}))
    _as(client, runner)
    for response in (
        client.get(EXTENSIONS),
        upload(client, data),
        client.delete(f"{EXTENSIONS}/{sha(data)}"),
        client.post(f"{EXTENSIONS}/{sha(data)}/revoke"),
    ):
        assert (response.status_code, response.json()["params"]) == (403, {"permission": "admin"})
    for machine in ("api:key_1", "system", "mcp:client"):
        _as(client, Principal(id=machine, permissions=ALL_PERMISSIONS))
        refused = upload(client, data)
        assert (refused.status_code, refused.json()["code"]) == (403, "access.person_required"), machine
        assert client.post(f"{EXTENSIONS}/{sha(data)}/revoke").json()["code"] == "access.person_required"
    assert _workspace(client).extension_store.listing() == {"packages": [], "revoked": []}


def test_the_store_belongs_to_one_data_directory(make_app: Callable[..., TestClient]) -> None:
    """Two installations never see each other's packages."""
    one, other = make_app(), make_app()
    assert upload(one, wheel()).status_code == 201
    assert other.get(EXTENSIONS).json()["packages"] == []
    assert other.delete(f"{EXTENSIONS}/{sha(wheel())}").status_code == 404
    assert _workspace(one).extension_store.root != _workspace(other).extension_store.root
    assert Path(_workspace(one).extension_store.root).is_relative_to(_workspace(one).data_dir)
