"""Starting a run with an Idempotency-Key starts it once (API2-08)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from conftest import text
from test_runs import agent_node, create


def test_a_retry_with_the_same_key_returns_the_same_run(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("done")] * 5})
    workflow_id = create(client, [agent_node("one", "ONE")], [])
    path = f"/api/workflows/{workflow_id}/runs"
    first = client.post(path, json={"input": "a"}, headers={"Idempotency-Key": "order-1"})
    assert first.status_code == 201 and "idempotent-replayed" not in first.headers
    again = client.post(path, json={"input": "a"}, headers={"Idempotency-Key": "order-1"})
    assert again.status_code == 200 and again.headers["idempotent-replayed"] == "true"
    assert again.json()["id"] == first.json()["id"]
    # The same key for a different request is a conflict; another key or none starts a new run.
    conflict = client.post(path, json={"input": "b"}, headers={"Idempotency-Key": "order-1"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency.conflict"
    assert set(conflict.json()) == {"detail", "code", "params"}
    other = client.post(path, json={"input": "a"}, headers={"Idempotency-Key": "order-2"})
    plain = client.post(path, json={"input": "a"})
    assert other.status_code == 201 and plain.status_code == 201
    ids = {first.json()["id"], other.json()["id"], plain.json()["id"]}
    assert len(ids) == 3
    assert len(client.get("/api/runs", params={"workflow_id": workflow_id}).json()) == 3


def test_the_header_is_documented(make_app: Callable[..., TestClient]) -> None:
    operation = make_app().get("/api/openapi.json").json()["paths"]["/api/workflows/{workflow_id}/runs"]["post"]
    assert any(item["name"] == "idempotency-key" and item["in"] == "header" for item in operation["parameters"])
    assert "200" in operation["responses"] and "201" in operation["responses"]
