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
