"""Portable export jobs on a server (API3-07): refused unless the operator enables them, then one build at a time
on the whole server and a size limit per package."""

from __future__ import annotations

import threading
import types
from typing import Any

import pytest
from test_identity import Server, database, identity, server  # noqa: F401  (fixtures)

from nodestro_api import portable_build
from nodestro_api.serving import ApiError

OPTIONS = {"kind": "portable", "confirm_distribution": True, "allow_downloads": True, "local_models": False}


@pytest.fixture
def backend() -> str:
    return "nodestro"


def _ctx(kind: str) -> Any:
    return types.SimpleNamespace(profile=types.SimpleNamespace(kind=kind))


def test_server_admission_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(portable_build.SERVER_JOBS_ENV, raising=False)
    monkeypatch.delenv(portable_build.SERVER_MAX_BYTES_ENV, raising=False)
    for kind in ("desktop", "local-container"):
        portable_build.server_admission(_ctx(kind), 10**12)  # not limited here
    with pytest.raises(ApiError) as refused:
        portable_build.server_admission(_ctx("server"), 1)
    assert (refused.value.status, refused.value.code) == (403, "export.server_unsupported")
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, value)
        with pytest.raises(ApiError):
            portable_build.server_admission(_ctx("server"), 1)
    monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, "1")
    portable_build.server_admission(_ctx("server"), portable_build.SERVER_MAX_BYTES)
    with pytest.raises(ApiError) as large:
        portable_build.server_admission(_ctx("server"), portable_build.SERVER_MAX_BYTES + 1)
    assert large.value.code == "export.server_too_large"
    default = portable_build.SERVER_MAX_BYTES
    for raw, expected in (("1000", 1000), ("x", default), ("-5", default)):
        monkeypatch.setenv(portable_build.SERVER_MAX_BYTES_ENV, raw)
        assert portable_build.server_max_bytes() == expected


def _published(owner: Any) -> str:
    flow = {
        "name": "Export flow",
        "nodes": [
            {"id": "start", "type": "start", "start": {"fields": [{"name": "topic", "label": "T", "type": "text"}]}},
            {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "Write."}},
            {"id": "out", "type": "output"},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
    }
    created = owner.post("/api/workflows", json=flow)
    assert created.status_code == 201, created.text
    published = owner.post(f"/api/workflows/{created.json()['id']}/api/publish", json={"note": "x"})
    assert published.status_code == 201, published.text
    return str(published.json()["publication"]["id"])


def test_tenants_cannot_build_portable_packages_by_default(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    studio: Server = request.getfixturevalue("server")
    monkeypatch.delenv(portable_build.SERVER_JOBS_ENV, raising=False)
    built: list[str] = []
    monkeypatch.setattr(portable_build, "build", lambda ctx, *rest: built.append("x"))
    owner = studio.member("acme", "alice", "owner")
    publication = _published(owner)
    for client in (owner, studio.member("acme", "erin", "editor")):
        answer = client.post(f"/api/publications/{publication}/export-jobs", json={"options": OPTIONS})
        assert answer.status_code == 403, answer.text
        assert answer.json()["code"] == "export.server_unsupported"
    assert built == []
    assert owner.get(f"/api/publications/{publication}/export-jobs").json() == []


def test_enabled_server_builds_one_package_at_a_time(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    studio: Server = request.getfixturevalue("server")
    monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, "1")
    monkeypatch.delenv(portable_build.SERVER_MAX_BYTES_ENV, raising=False)
    release = threading.Event()

    def build(*_args: Any) -> Any:
        release.wait(20)
        raise portable_build.Cancelled()

    monkeypatch.setattr(portable_build, "build", build)
    alice = studio.member("acme", "alice", "owner")
    bob = studio.member("globex", "bob", "owner")
    first, second = _published(alice), _published(bob)
    try:
        started = alice.post(f"/api/publications/{first}/export-jobs", json={"options": OPTIONS})
        assert started.status_code == 202, started.text
        # Another tenant waits: the limit is the whole server's, not per tenant.
        busy = bob.post(f"/api/publications/{second}/export-jobs", json={"options": OPTIONS})
        assert busy.status_code == 400 and busy.json()["code"] == "export.busy", busy.text
    finally:
        release.set()
    job = started.json()["id"]
    for _ in range(100):
        if alice.get(f"/api/export-jobs/{job}").json()["state"] != "running":
            break
        threading.Event().wait(0.05)
    assert alice.get(f"/api/export-jobs/{job}").json()["state"] == "cancelled"
    # The slot is free again.
    again = bob.post(f"/api/publications/{second}/export-jobs", json={"options": OPTIONS})
    assert again.status_code == 202, again.text
    # A package above the operator's limit is refused before it is built.
    monkeypatch.setenv(portable_build.SERVER_MAX_BYTES_ENV, "1")
    for _ in range(100):
        if bob.get(f"/api/export-jobs/{again.json()['id']}").json()["state"] != "running":
            break
        threading.Event().wait(0.05)
    large = alice.post(f"/api/publications/{first}/export-jobs", json={"options": OPTIONS})
    assert large.status_code == 403 and large.json()["code"] == "export.server_too_large", large.text


def test_the_export_check_reports_the_server_refusal_and_limit_up_front(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-053: the check names ``export.server_unsupported`` (or the size limit) before "Build package"; a failed
    job carries the parameters of its code."""
    studio: Server = request.getfixturevalue("server")
    monkeypatch.delenv(portable_build.SERVER_JOBS_ENV, raising=False)
    monkeypatch.delenv(portable_build.SERVER_MAX_BYTES_ENV, raising=False)
    owner = studio.member("acme", "alice", "owner")
    publication = _published(owner)

    def codes() -> dict[str, dict[str, Any]]:
        answer = owner.post(f"/api/publications/{publication}/export/check", json={"options": OPTIONS})
        assert answer.status_code == 200, answer.text
        return {item["code"]: item for item in answer.json()["problems"]}

    refused = codes()
    assert refused["export.server_unsupported"]["level"] == "error"
    monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, "1")
    limit = codes()["export.server_limit"]
    assert limit["level"] == "info" and limit["params"]["limit"] == portable_build.SERVER_MAX_BYTES
    monkeypatch.setenv(portable_build.SERVER_MAX_BYTES_ENV, "1")
    large = codes()["export.server_too_large"]
    assert large["level"] == "error" and large["params"]["limit"] == 1 and large["params"]["size"] > 1
    # A job that fails keeps the parameters of its code (the built package was too large).
    monkeypatch.delenv(portable_build.SERVER_MAX_BYTES_ENV)

    def build(*_args: Any) -> Any:
        raise portable_build.NodestroError("export.server_too_large", "Too large", size=5, limit=4)

    monkeypatch.setattr(portable_build, "build", build)
    started = owner.post(f"/api/publications/{publication}/export-jobs", json={"options": OPTIONS})
    assert started.status_code == 202, started.text
    for _ in range(100):
        job = owner.get(f"/api/export-jobs/{started.json()['id']}").json()
        if job["state"] != "running":
            break
        threading.Event().wait(0.05)
    assert job["state"] == "failed" and job["error_code"] == "export.server_too_large"
    assert job["error_params"] == {"size": 5, "limit": 4}
