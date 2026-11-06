"""``GET /api/publication-overview`` on a server: one tenant never sees another tenant's publications."""

from __future__ import annotations

import pytest
from test_identity import Server, database, identity, server  # noqa: F401  (fixtures)

OVERVIEW = "/api/publication-overview"


@pytest.fixture
def backend() -> str:
    return "nodestro"


def test_a_tenant_sees_only_its_own_publications(request: pytest.FixtureRequest) -> None:
    studio: Server = request.getfixturevalue("server")
    owner = studio.member("acme", "alice", "owner")
    flow = {
        "name": "Tenant flow",
        "nodes": [
            {"id": "start", "type": "start", "start": {"fields": [{"name": "topic", "label": "T", "type": "text"}]}},
            {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "Write."}},
            {"id": "out", "type": "output"},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
    }
    created = owner.post("/api/workflows", json=flow)
    assert created.status_code == 201, created.text
    published = owner.post(f"/api/workflows/{created.json()['id']}/api/publish", json={})
    assert published.status_code == 201, published.text
    viewer = studio.member("acme", "victor", "viewer")
    assert [item["name"] for item in viewer.get(OVERVIEW).json()] == ["Tenant flow"]
    stranger = studio.member("globex", "gina", "owner")
    assert stranger.get(OVERVIEW).json() == []
