"""An explicitly requested API name is never changed (DX-21)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from test_published_api import form_workflow, publish


def test_an_explicit_slug_that_is_taken_is_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    first = publish(client, form_workflow(client), slug="orders")["publication"]
    assert first["slug"] == "orders"
    other = form_workflow(client)
    answer = client.post(f"/api/workflows/{other}/api/publish", json={"slug": "orders"})
    assert answer.status_code == 409
    assert answer.json() == {
        "detail": "Another API is already called orders",
        "code": "publication.slug_taken",
        "params": {"slug": "orders"},
    }
    assert [item["workflow_id"] for item in client.get("/api/publications").json()] == [first["workflow_id"]]
    # Without a requested name the workflow's name gets a number when it is taken (as before).
    third = publish(client, form_workflow(client))["publication"]
    fourth = publish(client, form_workflow(client))["publication"]
    assert third["slug"] == "angebot-generator" and fourth["slug"] == "angebot-generator-2"
    # The other workflow can still be published under a free name.
    assert publish(client, other, slug="orders-eu")["publication"]["slug"] == "orders-eu"
