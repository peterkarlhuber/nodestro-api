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


def _ctx(kind: str, stored: tuple[int, ...] = ()) -> Any:
    """A context of ``kind`` whose tenant keeps packages of the ``stored`` sizes."""
    from nodestro_api.exporter import ExportOptions, ExportRecord

    records = [
        ExportRecord(
            publication_id="pub",
            release_id="rel",
            release_number=1,
            options=ExportOptions(),
            file_name="x.zip",
            size=size,
            sha256="0" * 64,
            files=1,
        )
        for size in stored
    ]
    store = types.SimpleNamespace(list=lambda _collection, _model: list(records))
    return types.SimpleNamespace(profile=types.SimpleNamespace(kind=kind), store=store)


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


def test_a_tenants_stored_packages_have_a_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """API-048: a build is refused when the tenant's stored packages plus the estimate exceed its quota (default
    8 GiB; an invalid or non-positive value is the default, never unlimited)."""
    monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, "1")
    monkeypatch.delenv(portable_build.SERVER_MAX_BYTES_ENV, raising=False)
    monkeypatch.delenv(portable_build.SERVER_TENANT_QUOTA_ENV, raising=False)
    quota = portable_build.SERVER_TENANT_QUOTA
    assert quota == 8 * 1024**3 and portable_build.server_tenant_quota() == quota
    for raw in ("x", "0", "-1", "1.5"):
        monkeypatch.setenv(portable_build.SERVER_TENANT_QUOTA_ENV, raw)
        assert portable_build.server_tenant_quota() == quota
    monkeypatch.setenv(portable_build.SERVER_TENANT_QUOTA_ENV, "1000")
    portable_build.server_admission(_ctx("server", (400, 300)), 300)  # exactly at the quota
    with pytest.raises(ApiError) as over:
        portable_build.server_admission(_ctx("server", (400, 300)), 301)
    assert (over.value.status, over.value.code) == (403, "export.server_quota")
    assert over.value.params == {"size": 301, "used": 700, "limit": 1000}
    for kind in ("desktop", "local-container"):  # only servers have the quota
        portable_build.server_admission(_ctx(kind, (10**12,)), 10**12)


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


def test_the_quota_counts_each_tenants_own_packages(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-048: the check and the job refuse a build once the tenant's stored exports reach the quota; another
    tenant's packages never count, and deleting an export frees its space."""
    studio: Server = request.getfixturevalue("server")
    monkeypatch.setenv(portable_build.SERVER_JOBS_ENV, "1")
    monkeypatch.delenv(portable_build.SERVER_MAX_BYTES_ENV, raising=False)
    built: list[str] = []
    monkeypatch.setattr(portable_build, "build", lambda *_args: built.append("x"))
    alice = studio.member("acme", "alice", "owner")
    bob = studio.member("globex", "bob", "owner")
    mine, theirs = _published(alice), _published(bob)
    source = alice.post(f"/api/publications/{mine}/exports", json={"options": {"kind": "standard"}})
    assert source.status_code == 201, source.text
    stored = source.json()["size"]
    assert stored > 0

    def check(client: Any, publication: str) -> dict[str, Any]:
        answer = client.post(f"/api/publications/{publication}/export/check", json={"options": OPTIONS})
        assert answer.status_code == 200, answer.text
        return answer.json()  # type: ignore[no-any-return]

    def codes(client: Any, publication: str) -> dict[str, dict[str, Any]]:
        return {item["code"]: item for item in check(client, publication)["problems"]}

    estimate = check(alice, mine)["portable"]["estimated_bytes"]
    # One byte short for acme with its stored export; globex keeps nothing, so its package fits.
    monkeypatch.setenv(portable_build.SERVER_TENANT_QUOTA_ENV, str(stored + estimate - 1))

    full = codes(alice, mine)["export.server_quota"]
    assert full["level"] == "error" and full["params"] == {
        "size": estimate,
        "used": stored,
        "limit": stored + estimate - 1,
    }
    refused = alice.post(f"/api/publications/{mine}/export-jobs", json={"options": OPTIONS})
    assert refused.status_code == 403 and refused.json()["code"] == "export.server_quota", refused.text
    assert "export.server_quota" not in codes(bob, theirs)  # globex keeps nothing
    assert built == []
    viewer = studio.member("acme", "vera", "viewer")
    assert viewer.delete(f"/api/exports/{source.json()['id']}").status_code == 403
    assert bob.delete(f"/api/exports/{source.json()['id']}").status_code == 404  # another tenant's export
    assert alice.delete(f"/api/exports/{source.json()['id']}").status_code == 204
    assert alice.get(f"/api/exports/{source.json()['id']}/download").status_code == 404
    assert alice.delete(f"/api/exports/{source.json()['id']}").status_code == 404
    assert "export.server_quota" not in codes(alice, mine)
    started = alice.post(f"/api/publications/{mine}/export-jobs", json={"options": OPTIONS})
    assert started.status_code == 202, started.text


def test_a_finished_package_above_the_quota_is_deleted(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """API-048: a package that would take the stored packages above the quota (others were kept meanwhile, or the
    estimate was low) is deleted and the build fails with ``export.server_quota``; the size limit comes first."""
    monkeypatch.delenv(portable_build.SERVER_MAX_BYTES_ENV, raising=False)
    monkeypatch.setenv(portable_build.SERVER_TENANT_QUOTA_ENV, "1000")
    package = tmp_path / "exp_x.zip"
    package.write_bytes(b"x" * 10)
    portable_build.keep_within_server_limits(_ctx("server", (990,)), package, 10)  # fits exactly
    assert package.exists()
    with pytest.raises(portable_build.NodestroError) as over:
        portable_build.keep_within_server_limits(_ctx("server", (991,)), package, 10)
    assert over.value.code == "export.server_quota" and over.value.params == {"size": 10, "used": 991, "limit": 1000}
    assert not package.exists()
    package.write_bytes(b"x" * 10)
    monkeypatch.setenv(portable_build.SERVER_MAX_BYTES_ENV, "5")
    with pytest.raises(portable_build.NodestroError) as large:
        portable_build.keep_within_server_limits(_ctx("server"), package, 10)
    assert large.value.code == "export.server_too_large" and not package.exists()
    package.write_bytes(b"x" * 10)
    portable_build.keep_within_server_limits(_ctx("desktop", (10**12,)), package, 10)  # not limited off a server
    assert package.exists()
