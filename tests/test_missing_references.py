"""A record another one refers to is gone: ``<kind>.not_found`` with the name the reference knew (API-053).

The studio names the missing item (``params.display``) instead of an id, where the reference recorded a name: the
workflow of a run, or the last name a deleted workflow had in its newest run. Without a name the answer keeps
``id`` and ``kind`` and has no ``display``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from nodestro_api.error_codes import collect
from nodestro_api.missing import workflow_display

from conftest import text
from test_runs import agent_node, create, start, wait_run

CASE = {"name": "Case", "input": "a"}


def _deleted_workflow_with_a_run(client: TestClient) -> str:
    workflow_id = create(client, [agent_node("one", "ONE")], [], name="Invoice check")
    run = start(client, workflow_id)
    wait_run(client, run["id"])
    assert client.delete(f"/api/workflows/{workflow_id}").status_code == 204
    return workflow_id


def _missing(response: Any, kind: str, item_id: str) -> dict[str, Any]:
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == f"{kind}.not_found"
    params: dict[str, Any] = body["params"]
    assert params["id"] == item_id and params["name"] == item_id and params["kind"] == kind
    return params


def test_a_deleted_workflow_is_named_by_its_last_run(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("done")] * 3})
    workflow_id = _deleted_workflow_with_a_run(client)

    suite = client.post("/api/benchmarks", json={"name": "Series", "workflow_id": workflow_id, "cases": []})
    assert _missing(suite, "workflow", workflow_id)["display"] == "Invoice check"
    parsed = client.post(
        "/api/batches/parse", data={"workflow_id": workflow_id}, files={"file": ("rows.csv", b"input\na\n")}
    )
    assert _missing(parsed, "workflow", workflow_id)["display"] == "Invoice check"
    batch = client.post("/api/batches", json={"workflow_id": workflow_id, "rows": [{"input": "a"}]})
    assert _missing(batch, "workflow", workflow_id)["display"] == "Invoice check"


def test_a_suite_whose_workflow_was_deleted_names_it(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"ONE": [text("done")] * 3})
    workflow_id = create(client, [agent_node("one", "ONE")], [], name="Contract review")
    run = start(client, workflow_id)
    wait_run(client, run["id"])
    suite = client.post("/api/benchmarks", json={"name": "Series", "workflow_id": workflow_id, "cases": [CASE]})
    assert suite.status_code == 201, suite.text
    # Deleting a workflow deletes its test series; a series can outlive it only by a race or in older data.
    client.app.state.nodestro.store.delete("workflows", workflow_id)  # type: ignore[attr-defined]
    started = client.post(f"/api/benchmarks/{suite.json()['id']}/plan", json={})
    assert _missing(started, "workflow", workflow_id)["display"] == "Contract review"


def test_without_a_recorded_name_there_is_no_display(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    never = client.post("/api/benchmarks", json={"name": "Series", "workflow_id": "wf_never", "cases": []})
    assert "display" not in _missing(never, "workflow", "wf_never")
    key = client.post("/api/api-keys", json={"name": "App", "scopes": ["run"], "publications": ["pub_gone"]})
    assert "display" not in _missing(key, "publication", "pub_gone")
    preview = client.post(
        "/api/workflows/preview",
        json={"workflow": {"name": "W", "nodes": [], "edges": []}, "node_id": "x", "run_id": "run_gone"},
    )
    _missing(preview, "run", "run_gone")
    # The route's own record keeps its answer (no display: the caller knows what it asked for).
    own = client.get("/api/workflows/wf_never")
    assert own.json()["params"] == {"name": "wf_never", "id": "wf_never", "kind": "workflow"}


def test_a_failing_name_lookup_never_turns_the_404_into_a_500() -> None:
    class Broken:
        def list_page(self, *_args: Any, **_kwargs: Any) -> Any:
            raise OSError("database gone")

    assert workflow_display(Broken(), "wf_1") == ""  # type: ignore[arg-type]


def test_the_catalog_lists_display_for_referenced_kinds() -> None:
    codes = collect().codes
    for kind in ("workflow", "run", "publication"):
        assert set(codes[f"{kind}.not_found"].params) >= {"id", "kind", "name", "display"}
