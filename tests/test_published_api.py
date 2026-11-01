"""Workflows as APIs: publishing, versions, keys, runs through the API, approvals, limits and network.

Every agent answers from a script; no test calls the Anthropic API.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import time
from typing import Any

from fastapi.testclient import TestClient
import pytest

from nodestro_api.permissions import check_routes
from nodestro_api.serving import KEY_AUTHENTICATED_ROUTES

from conftest import parsed, text
from test_flow import edge, save
from test_runs import agent_node

PUBLISHED = "/api/published"


# ---- helpers ------------------------------------------------------------------------------


def form_workflow(client: TestClient, *, approval: bool = False, budget: float | None = 1.0) -> str:
    start = {
        "id": "start",
        "type": "start",
        "start": {
            "fields": [
                {"name": "customer", "label": "Kunde", "type": "text", "required": True},
                {"name": "amount", "label": "Betrag", "type": "number", "required": False, "default": 10},
                {"name": "kind", "label": "Art", "type": "select", "options": ["offer", "invoice"], "default": "offer"},
            ]
        },
    }
    nodes: list[dict[str, Any]] = [start, agent_node("writer", "WRITER", task_template="Schreibe für {{customer}}")]
    edges = [edge("start", "writer")]
    if approval:
        nodes.append(
            {
                "id": "check",
                "type": "approval",
                "label": "Prüfen",
                "approval": {"instructions": "Passt das?", "on_reject": "stop", "require_comment_on_reject": True},
            }
        )
        edges += [edge("writer", "check"), edge("check", "out", "approved")]
    else:
        edges.append(edge("writer", "out"))
    nodes.append({"id": "out", "type": "output"})
    return save(client, nodes, edges, name="Angebot Generator", budget_usd=budget)


def publish(client: TestClient, workflow_id: str, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/workflows/{workflow_id}/api/publish", json=body)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def new_key(client: TestClient, publication_id: str, scopes: list[str] | None = None) -> str:
    response = client.post(
        "/api/api-keys",
        json={"name": "CRM", "scopes": scopes or ["run", "read", "approve"], "publications": [publication_id]},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def finish(client: TestClient, slug: str, token: str, run_id: str, timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"{PUBLISHED}/{slug}/runs/{run_id}", headers=auth(token)).json()
        if status["status"] not in ("queued", "running"):
            return status  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not settle")


@pytest.fixture
def published(make_app: Callable[..., TestClient]) -> Callable[..., tuple[TestClient, dict[str, Any], str]]:
    def factory(script: dict[str, list[Any]] | None = None, **workflow: Any) -> tuple[TestClient, dict[str, Any], str]:
        client = make_app(script or {"WRITER": [text("Angebot für ACME")] * 5})
        workflow_id = form_workflow(client, **workflow)
        result = publish(client, workflow_id, note="Erste Version")
        token = new_key(client, result["publication"]["id"])
        return client, result["publication"], token

    return factory


# ---- publishing and versions -----------------------------------------------------------------


def test_publish_creates_an_immutable_version(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    assert publication["slug"] == "angebot-generator"
    assert publication["active_release"] == 1
    workflow_id = publication["workflow_id"]
    release = client.get(f"/api/publications/{publication['id']}/releases/1").json()
    assert release["bundle"]["workflow"]["name"] == "Angebot Generator"
    digest = release["digest"]
    # Editing and saving the workflow changes nothing published.
    current = client.get(f"/api/workflows/{workflow_id}").json()
    current["name"] = "Umbenannt"
    current["nodes"][1]["agent"]["system_prompt"] = "CHANGED"
    assert client.put(f"/api/workflows/{workflow_id}", json=current).status_code == 200
    again = client.get(f"/api/publications/{publication['id']}/releases/1").json()
    assert again["digest"] == digest
    assert again["bundle"]["workflow"]["nodes"][1]["agent"]["system_prompt"] == "WRITER"
    started = client.post(
        f"{PUBLISHED}/{publication['slug']}/runs", json={"values": {"customer": "ACME"}}, headers=auth(token)
    )
    assert started.status_code == 202, started.text
    done = finish(client, publication["slug"], token, started.json()["run_id"])
    assert done["status"] == "succeeded"
    assert done["version"] == 1
    # Only publishing again creates version 2.
    second = publish(client, workflow_id, note="Neu")
    assert second["release"]["number"] == 2
    assert second["publication"]["active_release"] == 2
    overview = client.get(f"/api/workflows/{workflow_id}/api").json()
    assert [item["number"] for item in overview["releases"]] == [2, 1]
    assert overview["releases"][1]["runs"] == 1


def test_rollback_and_versioned_endpoint(published: Callable[..., Any]) -> None:
    client, publication, token = published({"WRITER": [text("v1")], "CHANGED": [text("v2"), text("v2b")]})
    workflow_id, slug = publication["workflow_id"], publication["slug"]
    current = client.get(f"/api/workflows/{workflow_id}").json()
    current["nodes"][1]["agent"]["system_prompt"] = "CHANGED"
    client.put(f"/api/workflows/{workflow_id}", json=current)
    publish(client, workflow_id)
    body = {"values": {"customer": "ACME"}}
    latest = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=auth(token)).json()
    assert finish(client, slug, token, latest["run_id"])["version"] == 2
    old = client.post(f"{PUBLISHED}/{slug}/versions/1/runs", json=body, headers=auth(token)).json()
    result = client.get(f"{PUBLISHED}/{slug}/runs/{old['run_id']}/result", headers=auth(token))
    finish(client, slug, token, old["run_id"])
    result = client.get(f"{PUBLISHED}/{slug}/runs/{old['run_id']}/result", headers=auth(token)).json()
    assert result["output"] == "v1"
    # Rollback: version 1 is active again without publishing.
    activated = client.post(f"/api/publications/{publication['id']}/activate", json={"number": 2 - 1}).json()
    assert activated["active_release"] == 1
    missing = client.post(f"{PUBLISHED}/{slug}/versions/9/runs", json=body, headers=auth(token))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "version.not_found"


def test_check_blocks_invalid_workflows(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "W", tools=["no_such_tool"]),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "writer"), edge("writer", "out")],
    )
    check = client.post(f"/api/workflows/{workflow_id}/api/check", json={}).json()
    assert check["ok"] is False
    blocked = client.post(f"/api/workflows/{workflow_id}/api/publish", json={})
    assert blocked.status_code == 400
    assert blocked.json()["code"] == "api.publish_blocked"
    assert client.get(f"/api/workflows/{workflow_id}/api").json()["publication"] is None


def test_check_reports_schemas_approvals_and_budget(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    workflow_id = form_workflow(client, approval=True, budget=None)
    client.put("/api/settings", json={"default_budget_usd": None})
    check = client.post(f"/api/workflows/{workflow_id}/api/check", json={}).json()
    assert check["ok"] is True
    codes = {item["code"] for item in check["problems"]}
    assert {"api.approvals", "api.no_budget"} <= codes
    assert check["approvals"] == 1
    assert "values" in check["input_schema"]["required"]
    values = next(iter(check["input_schema"]["$defs"].values()))
    assert values["required"] == ["customer"]


# ---- calling the API -----------------------------------------------------------------------


def test_run_status_result_and_events(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug = publication["slug"]
    response = client.post(
        f"{PUBLISHED}/{slug}/runs",
        json={"values": {"customer": "ACME", "amount": 250}},
        headers=auth(token),
    )
    assert response.status_code == 202
    assert response.headers["location"].endswith(response.json()["run_id"])
    run_id = response.json()["run_id"]
    done = finish(client, slug, token, run_id)
    assert done["status"] == "succeeded"
    result = client.get(f"{PUBLISHED}/{slug}/runs/{run_id}/result", headers=auth(token)).json()
    assert result["output"] == "Angebot für ACME"
    assert result["usage"]["requests"] == 1
    with client.stream("GET", f"{PUBLISHED}/{slug}/runs/{run_id}/events", headers=auth(token)) as stream:
        body = "".join(stream.iter_text())
    assert "event: status" in body and "event: end" in body and "succeeded" in body
    # The run is an ordinary run of the studio, marked as an API run.
    studio = client.get(f"/api/runs/{run_id}").json()
    assert studio["trigger"] == "api"
    assert studio["release_number"] == 1
    assert studio["values"]["amount"] == 250
    listed = client.get(f"{PUBLISHED}/{slug}/runs", headers=auth(token)).json()
    assert [item["run_id"] for item in listed] == [run_id]


def test_validation_errors_are_structured(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug = publication["slug"]
    missing = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {}}, headers=auth(token))
    assert missing.status_code == 422
    error = missing.json()["error"]
    assert error["code"] == "request.invalid"
    assert any(item["field"].endswith("customer") for item in error["params"]["errors"])
    wrong = client.post(
        f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A", "kind": "other"}}, headers=auth(token)
    )
    assert wrong.status_code == 422
    extra = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}, "x": 1}, headers=auth(token))
    assert extra.status_code == 422
    not_json = client.post(f"{PUBLISHED}/{slug}/runs", content=b"{", headers=auth(token))
    assert not_json.status_code == 400
    assert client.get(f"/api/publications/{publication['id']}/runs").json() == []


def test_free_text_workflow(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Antwort")]})
    workflow_id = save(
        client,
        [{"id": "start", "type": "start"}, agent_node("writer", "WRITER"), {"id": "out", "type": "output"}],
        [edge("start", "writer"), edge("writer", "out")],
        name="Freitext",
    )
    publication = publish(client, workflow_id)["publication"]
    token = new_key(client, publication["id"])
    started = client.post(f"{PUBLISHED}/freitext/runs", json={"input": "Hallo"}, headers=auth(token)).json()
    assert finish(client, "freitext", token, started["run_id"])["status"] == "succeeded"
    assert client.get(f"/api/runs/{started['run_id']}").json()["input"] == "Hallo"


def test_structured_output_is_returned_unchanged(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"CLASSIFY": [parsed(lambda model: model(category="billing", score=0.25))]})
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node(
                "classify",
                "CLASSIFY",
                output={
                    "mode": "structured",
                    "name": "Kind",
                    "fields": [{"name": "category", "type": "string"}, {"name": "score", "type": "number"}],
                },
            ),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "classify"), edge("classify", "out")],
        name="Klassifizierer",
    )
    publication = publish(client, workflow_id)["publication"]
    token = new_key(client, publication["id"])
    run_id = client.post(f"{PUBLISHED}/klassifizierer/runs", json={"input": "x"}, headers=auth(token)).json()["run_id"]
    finish(client, "klassifizierer", token, run_id)
    result = client.get(f"{PUBLISHED}/klassifizierer/runs/{run_id}/result", headers=auth(token)).json()
    assert result["output"] == {"category": "billing", "score": 0.25}
    schema = client.get(f"{PUBLISHED}/klassifizierer/openapi.json", headers=auth(token)).json()
    assert "RunResult" in json.dumps(schema) or "output" in json.dumps(schema)


# ---- keys and access ----------------------------------------------------------------------


def test_authentication_and_permissions(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug = publication["slug"]
    body = {"values": {"customer": "ACME"}}
    assert client.post(f"{PUBLISHED}/{slug}/runs", json=body).status_code == 401
    wrong = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=auth("ndk_000000000000_nope"))
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "auth.invalid"
    reader = new_key(client, publication["id"], ["read"])
    refused = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=auth(reader))
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "auth.scope"
    # X-API-Key works as well.
    assert client.get(f"{PUBLISHED}/{slug}", headers={"X-API-Key": reader}).status_code == 200
    # Revoked keys stop working at once.
    key_id = token.split("_")[1]
    assert client.post(f"/api/api-keys/{key_id}/revoke").status_code == 200
    assert client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=auth(token)).status_code == 401
    # Keys are never shown again, never stored in plain text.
    keys = client.get("/api/api-keys").json()
    assert all("token" not in item and "secret_hash" not in item for item in keys)
    stored = client.app.state.nodestro.store.get_raw("api_keys", key_id)  # type: ignore[attr-defined]
    assert stored["secret_hash"] and token.split("_", 2)[2] not in json.dumps(stored)


def test_every_published_endpoint_refuses_requests_without_a_valid_key(
    published: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walks every route the route check accepts under /api/published, so a new endpoint is tested automatically."""
    # The MCP server endpoint (WP-11) answers 404 while it is off; switched on, it must refuse without a key as well.
    monkeypatch.setenv("NODESTRO_MCP_SERVER", "1")
    client, publication, token = published()
    slug = publication["slug"]
    started = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "ACME"}}, headers=auth(token))
    run_id = started.json()["run_id"]
    finish(client, slug, token, run_id)
    values = {"slug": slug, "number": "1", "run_id": run_id, "file_id": "file_missing"}
    bodies = {"/runs": {"values": {"customer": "X"}}, "/approve": {"decision": "approved"}, "/resume": {}}
    routes = [item for item in check_routes(client.app) if item.access == "api_key"]  # type: ignore[arg-type]
    assert {(item.method, item.path) for item in routes} == KEY_AUTHENTICATED_ROUTES
    runs_before = client.get("/api/runs").json()
    for item in routes:
        path = item.path.format(**values)
        body = next((value for suffix, value in bodies.items() if item.path.endswith(suffix)), None)
        for headers, code in (({}, "auth.missing"), (auth("ndk_000000000000_nope"), "auth.invalid")):
            answer = client.request(item.method, path, json=body, headers=headers)
            assert answer.status_code == 401, (item.method, item.path, answer.text)
            assert answer.json()["error"]["code"] == code, (item.method, item.path)
    # Nothing was started, cancelled or decided.
    assert client.get("/api/runs").json() == runs_before


def test_keys_are_scoped_to_their_publications(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("a")] * 3})
    first = publish(client, form_workflow(client))["publication"]
    other_id = save(
        client,
        [{"id": "start", "type": "start"}, agent_node("writer", "WRITER"), {"id": "out", "type": "output"}],
        [edge("start", "writer"), edge("writer", "out")],
        name="Andere",
    )
    second = publish(client, other_id)["publication"]
    token = new_key(client, first["id"])
    foreign = client.post(f"{PUBLISHED}/{second['slug']}/runs", json={"input": "x"}, headers=auth(token))
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "publication.not_found"


def test_rotating_a_key(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    key_id = token.split("_")[1]
    rotated = client.post(f"/api/api-keys/{key_id}/rotate").json()
    assert rotated["key"]["replaces"] == key_id
    slug = publication["slug"]
    assert client.get(f"{PUBLISHED}/{slug}", headers=auth(token)).status_code == 401
    assert client.get(f"{PUBLISHED}/{slug}", headers=auth(rotated["token"])).status_code == 200


def test_secrets_never_appear_in_runs_or_errors(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug = publication["slug"]
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "ACME"}}, headers=auth(token)).json()[
        "run_id"
    ]
    finish(client, slug, token, run_id)
    secret = token.split("_", 2)[2]
    stored = client.get(f"/api/runs/{run_id}").text
    assert secret not in stored
    trace = client.get(f"/api/runs/{run_id}/trace")
    assert secret not in trace.text
    error = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": token}}, headers=auth("ndk_x"))
    assert secret not in error.text


def test_studio_port_only_answers_this_computer(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    remote = TestClient(client.app, client=("192.168.1.20", 50000), base_url="http://127.0.0.1")
    answer = remote.get(f"{PUBLISHED}/{publication['slug']}", headers=auth(token))
    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == "lan.not_enabled"


def test_switching_off_and_on(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug, pid = publication["slug"], publication["id"]
    client.post(f"/api/publications/{pid}/enabled", json={"enabled": False})
    off = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token))
    assert off.status_code == 503
    assert off.json()["error"]["code"] == "publication.disabled"
    client.post(f"/api/publications/{pid}/enabled", json={"enabled": True})
    on = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token))
    assert on.status_code == 202


# ---- reliability and limits ---------------------------------------------------------------


def test_idempotency_key_starts_one_run(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug = publication["slug"]
    headers = {**auth(token), "Idempotency-Key": "order-4711"}
    body = {"values": {"customer": "ACME"}}
    first = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers)
    again = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers)
    assert first.status_code == 202 and again.status_code == 200
    assert again.headers["idempotent-replayed"] == "true"
    assert first.json()["run_id"] == again.json()["run_id"]
    conflict = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "B"}}, headers=headers)
    assert conflict.status_code == 409
    finish(client, slug, token, first.json()["run_id"])
    assert len(client.get(f"/api/publications/{publication['id']}/runs").json()) == 1


def test_limits_refuse_runs(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug, pid = publication["slug"], publication["id"]
    limits = {**publication["limits"], "runs_per_minute": 1}
    client.patch(f"/api/publications/{pid}", json={"limits": limits})
    body = {"values": {"customer": "A"}}
    assert client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=auth(token)).status_code == 202
    limited = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=auth(token))
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "limit.runs"
    assert "retry-after" in limited.headers


def test_daily_budget_refuses_runs(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug, pid = publication["slug"], publication["id"]
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token)).json()[
        "run_id"
    ]
    finish(client, slug, token, run_id)
    client.patch(f"/api/publications/{pid}", json={"limits": {**publication["limits"], "daily_budget_usd": 0.0001}})
    refused = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token))
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "limit.daily_budget"
    usage = client.get(f"{PUBLISHED}/{slug}/usage", headers=auth(token)).json()
    assert usage["runs"] == 1


def test_sync_mode(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    slug, pid = publication["slug"], publication["id"]
    body = {"values": {"customer": "A"}}
    refused = client.post(f"{PUBLISHED}/{slug}/runs?wait=5", json=body, headers=auth(token))
    assert refused.status_code == 400
    client.patch(f"/api/publications/{pid}", json={"limits": {**publication["limits"], "sync_enabled": True}})
    answer = client.post(f"{PUBLISHED}/{slug}/runs?wait=5", json=body, headers=auth(token))
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "succeeded"
    assert answer.json()["output"] == "Angebot für ACME"


def test_sync_mode_is_refused_for_approvals(published: Callable[..., Any]) -> None:
    client, publication, _ = published(approval=True)
    refused = client.patch(
        f"/api/publications/{publication['id']}", json={"limits": {**publication["limits"], "sync_enabled": True}}
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "api.sync_with_approvals"


def test_missing_model_key_answers_503(make_app: Callable[..., TestClient]) -> None:
    from nodestro_runtime.providers import MissingApiKeyError

    def no_key(_provider: str) -> Any:
        raise MissingApiKeyError("anthropic")

    client = make_app({"WRITER": [text("x")]})
    publication = publish(client, form_workflow(client))["publication"]
    token = new_key(client, publication["id"])
    client.app.state.nodestro._client_factory = no_key  # type: ignore[attr-defined]
    answer = client.post(
        f"{PUBLISHED}/{publication['slug']}/runs", json={"values": {"customer": "A"}}, headers=auth(token)
    )
    assert answer.status_code == 503
    assert answer.json()["error"]["code"] == "model.unavailable"


# ---- approvals -----------------------------------------------------------------------------


def test_approval_through_the_api(published: Callable[..., Any]) -> None:
    client, publication, token = published(approval=True)
    slug = publication["slug"]
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token)).json()[
        "run_id"
    ]
    paused = finish(client, slug, token, run_id)
    assert paused["status"] == "awaiting_approval"
    approval = paused["approvals"][0]
    assert approval["approval_id"] == f"{run_id}:check"
    assert approval["instructions"] == "Passt das?"
    reader = new_key(client, publication["id"], ["read", "run"])
    refused = client.post(
        f"{PUBLISHED}/{slug}/runs/{run_id}/approve", json={"decision": "approved"}, headers=auth(reader)
    )
    assert refused.status_code == 403
    needs_comment = client.post(
        f"{PUBLISHED}/{slug}/runs/{run_id}/approve", json={"decision": "rejected"}, headers=auth(token)
    )
    assert needs_comment.status_code == 422
    decided = client.post(
        f"{PUBLISHED}/{slug}/runs/{run_id}/approve",
        json={"approval_id": approval["approval_id"], "decision": "approved", "comment": "ok"},
        headers=auth(token),
    )
    assert decided.status_code == 200, decided.text
    twice = client.post(
        f"{PUBLISHED}/{slug}/runs/{run_id}/approve",
        json={"approval_id": approval["approval_id"], "decision": "approved"},
        headers=auth(token),
    )
    assert twice.status_code == 409
    done = finish(client, slug, token, run_id)
    assert done["status"] == "succeeded"
    record = client.get(f"/api/runs/{run_id}").json()["nodes"]["check"]["approval"]
    assert record["decided_by"] == f"api:{token.split('_')[1]}"
    assert record["comment"] == "ok"


def test_studio_decides_an_api_approval(published: Callable[..., Any]) -> None:
    client, publication, token = published(approval=True)
    slug = publication["slug"]
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token)).json()[
        "run_id"
    ]
    finish(client, slug, token, run_id)
    overview = client.get(f"/api/workflows/{publication['workflow_id']}/api").json()
    assert overview["pending_approvals"] == 1
    answer = client.post(f"/api/runs/{run_id}/approvals/check", json={"decision": "approved"})
    assert answer.status_code == 200, answer.text
    assert finish(client, slug, token, run_id)["status"] == "succeeded"
    assert client.get(f"/api/runs/{run_id}").json()["nodes"]["check"]["approval"]["decided_by"] == "local"


def test_pending_approval_survives_a_restart(make_app: Callable[..., TestClient], tmp_path: Any) -> None:
    from nodestro_api.app import create_app
    from nodestro_api.context import AppConfig

    from conftest import ScriptedClient

    client = make_app({"WRITER": [text("Entwurf")]})
    publication = publish(client, form_workflow(client, approval=True))["publication"]
    token = new_key(client, publication["id"])
    slug = publication["slug"]
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token)).json()[
        "run_id"
    ]
    assert finish(client, slug, token, run_id)["status"] == "awaiting_approval"
    client.__exit__(None, None, None)
    fake = ScriptedClient({})
    app = create_app(AppConfig(data_dir=tmp_path / "data0"), client_factory=lambda _key: fake, scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1") as restarted:
        status = restarted.get(f"{PUBLISHED}/{slug}/runs/{run_id}", headers=auth(token)).json()
        assert status["status"] == "awaiting_approval"
        restarted.post(f"{PUBLISHED}/{slug}/runs/{run_id}/approve", json={"decision": "approved"}, headers=auth(token))
        assert finish(restarted, slug, token, run_id)["status"] == "succeeded"


# ---- studio test, docs, examples ------------------------------------------------------------


def test_studio_test_validates_and_runs(published: Callable[..., Any]) -> None:
    client, publication, _ = published()
    pid = publication["id"]
    dry = client.post(f"/api/publications/{pid}/test", json={"body": {"values": {"customer": "A"}}, "dry_run": True})
    assert dry.status_code == 200, dry.text
    assert dry.json()["request"]["values"]["amount"] == 10
    assert "Kunde: A" in dry.json()["input_text"]
    invalid = client.post(f"/api/publications/{pid}/test", json={"body": {"values": {}}, "dry_run": True})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "request.invalid"
    real = client.post(f"/api/publications/{pid}/test", json={"body": {"values": {"customer": "A"}}})
    assert real.status_code == 202, real.text
    run = client.get(f"/api/runs/{real.json()['run_id']}").json()
    assert run["trigger"] == "api" and run["api_key_id"] == "studio"


def test_docs_and_examples(published: Callable[..., Any]) -> None:
    client, publication, _ = published(approval=True)
    docs = client.get(f"/api/publications/{publication['id']}/docs").json()
    paths = docs["openapi"]["paths"]
    assert f"{PUBLISHED}/{publication['slug']}/runs" in paths
    assert "RunRequest" in json.dumps(docs["openapi"]["components"])
    assert "approve" in docs["examples"]["curl"]
    assert docs["examples"]["request"]["values"]["customer"]
    # The studio's internal endpoints are not part of the published description.
    assert not any(path.startswith("/api/workflows") for path in paths)


def test_deleting_the_workflow_ends_its_api(published: Callable[..., Any]) -> None:
    client, publication, token = published()
    assert client.delete(f"/api/workflows/{publication['workflow_id']}").status_code == 204
    gone = client.get(f"{PUBLISHED}/{publication['slug']}", headers=auth(token))
    assert gone.status_code in (401, 404)
    assert client.get("/api/publications").json() == []


def test_old_idempotency_records_are_deleted(published: Callable[..., Any]) -> None:
    """A-09: Idempotency-Key records past their 24 hours do not stay forever."""
    from datetime import datetime, timedelta, timezone

    client, publication, token = published()
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    store.put_raw("api_idempotency", "old-record", {"run_id": "run_old", "request": "x", "at": old})
    headers = {**auth(token), "Idempotency-Key": "order-1"}
    first = client.post(f"{PUBLISHED}/{publication['slug']}/runs", json={"values": {"customer": "A"}}, headers=headers)
    assert first.status_code == 202, first.text
    assert store.get_raw("api_idempotency", "old-record") is None
    kept = [key for key in _keys(store, "api_idempotency") if key != "old-record"]
    assert len(kept) == 1
    finish(client, publication["slug"], token, first.json()["run_id"])


def _keys(store: Any, collection: str) -> list[str]:
    from pydantic import BaseModel, ConfigDict

    class Anything(BaseModel):
        model_config = ConfigDict(extra="allow")

    return [key for key, _value in store.items(collection, Anything)]


def test_the_rate_limiter_forgets_idle_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """A-09: counters of clients without hits in the last minute are dropped (no unbounded growth)."""
    from nodestro_api import serving

    clock = [1000.0]
    monkeypatch.setattr(serving.time, "monotonic", lambda: clock[0])
    window = serving._Window()
    for client in range(500):
        assert window.allow(f"auth:10.0.{client // 256}.{client % 256}", 30) is None
    assert len(window._hits) == 500
    clock[0] += 61
    assert window.allow("auth:10.9.9.9", 30) is None
    assert list(window._hits) == ["auth:10.9.9.9"]
    for _ in range(29):
        assert window.allow("auth:10.9.9.9", 30) is None
    assert window.allow("auth:10.9.9.9", 30) is not None  # the limit itself still holds
