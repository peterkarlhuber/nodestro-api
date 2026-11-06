"""One error shape for the studio API: validation, unknown routes, wrong methods, malformed JSON (API2-06, DX-22)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient


def _shape(body: dict[str, object]) -> bool:
    return set(body) == {"detail", "code", "params"} and isinstance(body["detail"], str)


def test_validation_errors_have_the_studio_shape_without_input_echo(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    secret = "S3CRET-" + "q" * 300
    answer = client.get("/api/search", params={"q": secret})
    assert answer.status_code == 422
    body = answer.json()
    assert _shape(body) and body["code"] == "request.invalid"
    assert body["params"]["errors"] == [
        {"loc": ["query", "q"], "msg": "String should have at most 200 characters", "type": "string_too_long"}
    ]
    assert "S3CRET" not in answer.text
    wrong = client.post("/api/workflows", json={"name": "S3CRET-NAME", "nodes": "S3CRET-NODES", "edges": []})
    assert wrong.status_code == 422 and wrong.json()["code"] == "request.invalid"
    assert wrong.json()["params"]["errors"][0]["loc"] == ["body", "nodes"]
    assert "S3CRET" not in wrong.text


def test_unknown_routes_methods_and_malformed_json(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    missing = client.get("/api/no-such-route")
    assert missing.status_code == 404 and _shape(missing.json()) and missing.json()["code"] == "route.not_found"
    method = client.delete("/api/meta")
    assert method.status_code == 405 and _shape(method.json()) and method.json()["code"] == "method.not_allowed"
    assert method.headers["allow"] == "GET"
    broken = client.post("/api/workflows", content=b'{"name": ', headers={"content-type": "application/json"})
    assert broken.status_code == 400 and _shape(broken.json()) and broken.json()["code"] == "request.malformed"
    # A known route with an unknown id answers with the code of its kind (DX handover B12); ``name`` stays.
    unknown = client.get("/api/workflows/nope")
    assert unknown.status_code == 404 and _shape(unknown.json())
    assert unknown.json()["code"] == "workflow.not_found"
    assert unknown.json()["params"] == {"name": "nope", "id": "nope", "kind": "workflow"}
    assert unknown.json()["detail"] == "Workflow 'nope' does not exist"
    run = client.get("/api/runs/run_nope").json()
    assert (run["code"], run["params"]["kind"]) == ("run.not_found", "run")
    publication = client.get("/api/publications/pub_nope/runs").json()
    assert publication["code"] == "publication.not_found"


def test_published_apis_keep_their_error_format(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    answer = client.post("/api/published/nothing/runs", json={})
    assert answer.status_code in (401, 404)
    assert set(answer.json()) == {"error"}


def test_the_openapi_document_declares_the_error_answer(make_app: Callable[..., TestClient]) -> None:
    document = make_app().get("/api/openapi.json").json()
    error = document["components"]["schemas"]["ErrorResponse"]
    assert error["required"] == ["detail", "code", "params"]
    studio = {
        (path, method): operation
        for path, operations in document["paths"].items()
        if not path.startswith("/api/published")
        for method, operation in operations.items()
    }
    assert len(studio) > 200
    for (path, method), operation in studio.items():
        assert operation["responses"]["4XX"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }, (method, path)
        assert "422" not in operation["responses"], (method, path)


def test_every_studio_operation_has_a_tag(make_app: Callable[..., TestClient]) -> None:
    """API-016: ``/api/docs`` groups the studio operations instead of one flat list."""
    from nodestro_api.api import DEFAULT_TAG, PATH_TAGS, path_tag

    document = make_app().get("/api/openapi.json").json()
    tags: dict[str, set[str]] = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            assert operation.get("tags"), (method, path)
            for tag in operation["tags"]:
                tags.setdefault(tag, set()).add(path)
    assert {"workflows", "runs", "publications", "local models", "MCP", "pricing"} <= set(tags)
    assert "/api/workflows/{workflow_id}" in tags["workflows"] and "/api/runs/{run_id}" in tags["runs"]
    assert "/api/published/{slug}/runs" in tags["published workflow"]
    # The fallback stays small: a new kind of route gets a tag of its own.
    assert len(tags[DEFAULT_TAG]) <= 12, sorted(tags[DEFAULT_TAG])
    assert path_tag("/api/mcp/servers") == "MCP" and path_tag("/api/health") == DEFAULT_TAG
    assert set(PATH_TAGS.values()) <= set(tags)


def test_published_routes_declare_their_error_answers(make_app: Callable[..., TestClient]) -> None:
    """API-016: the published routes document every error they answer with, in their own format."""
    document = make_app().get("/api/openapi.json").json()
    error = document["components"]["schemas"]["PublishedError"]
    assert error["required"] == ["error"]
    detail = document["components"]["schemas"]["PublishedErrorDetail"]
    assert set(detail["required"]) == {"status", "code", "message"}
    published = {
        (path, method): operation["responses"]
        for path, operations in document["paths"].items()
        if path.startswith("/api/published")
        for method, operation in operations.items()
    }
    assert len(published) >= 13
    for (path, method), responses in published.items():
        assert {"401", "403", "404", "429"} <= set(responses), (method, path)
        for status, answer in responses.items():
            if status.startswith(("4", "5")):
                schema = answer["content"]["application/json"]["schema"]
                assert schema == {"$ref": "#/components/schemas/PublishedError"}, (method, path, status)
    start = published[("/api/published/{slug}/runs", "post")]
    assert {"400", "409", "413", "422", "503"} <= set(start)
    assert "409" in published[("/api/published/{slug}/runs/{run_id}/result", "get")]
