"""A server's tenants see the local model catalog, not the server's machine (API3-08)."""

from __future__ import annotations

import pytest
from test_identity import Server, database, identity, server  # noqa: F401  (fixtures)


@pytest.fixture
def backend() -> str:
    return "nodestro"


def test_tenants_do_not_see_the_servers_machine(request: pytest.FixtureRequest) -> None:
    studio: Server = request.getfixturevalue("server")
    for role in ("viewer", "owner"):
        client = studio.member("acme", f"user-{role}", role)
        overview = client.get("/api/local-models")
        assert overview.status_code == 200, overview.text
        body = overview.json()
        assert body["manageable"] is False
        assert body["hardware"] is None
        for field in ("base_url", "runtime_path", "models_dir", "free_bytes", "detected_external"):
            assert body["status"][field] is None, field
        # The catalog and the recommendations stay (same shape as on the desktop).
        assert body["catalog"] and {"name", "installed", "fits"} <= set(body["catalog"][0])
        assert isinstance(body["recommendations"], list) and "mode" in body["status"]


def test_tenants_get_no_advice_or_reasons_that_describe_the_machine(request: pytest.FixtureRequest) -> None:
    """UX handover B-1: memory, CPU and GPU must not reach tenants through advice codes or recommendation reasons."""
    from nodestro_runtime.local_models import Hardware

    from nodestro_api import api_local_models

    studio: Server = request.getfixturevalue("server")
    # A small machine without a GPU: the desktop shows advice and reasons for it.
    small = Hardware(system="Linux", machine="x86_64", ram_bytes=4 * 1024**3, cpus=2)
    monkeypatch = request.getfixturevalue("monkeypatch")
    monkeypatch.setattr("nodestro_runtime.local_service.LocalModelService.hardware", lambda self: small)
    client = studio.member("acme", "user-owner", "owner")
    body = client.get("/api/local-models").json()
    codes = {hint["code"] for hint in body["advice"]}
    assert not codes & api_local_models._MACHINE_ADVICE, codes
    reasons = {reason for found in body["recommendations"] for reason in found["reasons"]}
    assert not reasons & api_local_models._MACHINE_REASONS, reasons
    one = client.get("/api/local-models/recommend", params={"task": "classify"})
    assert one.status_code == 200, one.text
    assert not set(one.json()["reasons"]) & api_local_models._MACHINE_REASONS
