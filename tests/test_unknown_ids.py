"""Unknown ids answer 404 instead of a 2xx (API2-07); paging limits start at 1."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

FLOW = {"name": "Known", "nodes": [{"id": "start", "type": "start"}], "edges": []}


def test_unknown_ids_are_not_found(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    for method, path, body, code in (
        ("POST", "/api/runs/run_unknown/cancel", None, "run.not_found"),
        ("GET", "/api/benchmarks/suite_unknown/runs", None, "not_found"),
        ("GET", "/api/publications/pub_unknown/guides", None, "publication.not_found"),
        ("PUT", "/api/organize/workflow/wf_unknown", {"favorite": True}, "not_found"),
        ("PUT", "/api/organize/template/tpl_unknown", {"favorite": True}, "not_found"),
        ("PUT", "/api/organize/tool/no_such_tool", {"favorite": True}, "not_found"),
        ("DELETE", "/api/python/environments/workflow/wf_unknown", None, "not_found"),
    ):
        answer = client.request(method, path, json=body)
        assert answer.status_code == 404, (method, path, answer.status_code, answer.text)
        assert answer.json()["code"] == code, (method, path)
    # Nothing was stored for the unknown objects.
    assert client.get("/api/organize").json()["items"] == {}


def test_known_ids_still_work(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    workflow = client.post("/api/workflows", json=FLOW).json()
    answer = client.put(f"/api/organize/workflow/{workflow['id']}", json={"favorite": True, "tags": ["a"]})
    assert answer.status_code == 200 and answer.json()["favorite"] is True
    assert f"workflow:{workflow['id']}" in client.get("/api/organize").json()["items"]


def test_limits_start_at_one(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    for path in ("/api/runs?limit=0", "/api/runs?limit=-1", "/api/tool-approvals/audit?limit=0"):
        answer = client.get(path)
        assert answer.status_code == 422 and answer.json()["code"] == "request.invalid", path
    assert client.get("/api/runs?limit=1").status_code == 200
