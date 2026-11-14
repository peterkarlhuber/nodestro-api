"""The app API contract ``nodestro-app/1`` (NAL-C1, ``docs/app-api/v1.md``) against every kind of backend.

One test publication (a form with text, choice and file fields, an agent that answers from a script, an approval) is
served by the desktop studio, its network listener (a real port), an exported source project and the web interface of
a portable package. Every test runs against each of them and checks the same things: each answer against its JSON
Schema in ``contracts/app-api/v1``, the ``Nodestro-App-Contract`` header, that the announced capabilities are served,
the error envelope, and what discovery shows with and without credentials.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any

from fastapi.testclient import TestClient
import httpx
import jsonschema
import pytest
from referencing import Registry, Resource
from test_export import _download, _export, _exported_app, _extract
from test_portable import _ui_app
from test_published_api import PUBLISHED, auth, new_key, publish
from test_published_release import _free_port

from nodestro_api import app_contract

from conftest import ScriptedClient, text
from test_flow import edge, save
from test_runs import agent_node

SCHEMAS = Path(__file__).resolve().parents[2] / "contracts" / "app-api" / "v1"
HEADER = "Nodestro-App-Contract"
KINDS = ("desktop", "network", "export", "portable")
SCRIPT = {"WRITER": [text("Offer for ACME")] * 20}
FILE = {"name": "note.txt", "media_type": "text/plain", "content_base64": "SGVsbG8="}
UI_HEADERS = {"X-Nodestro-UI": "1", "Origin": "http://127.0.0.1:8800"}


# ---- schemas ----------------------------------------------------------------------------------------------------


def _load_schemas() -> tuple[dict[str, dict[str, Any]], Registry]:
    schemas = {path.name: json.loads(path.read_text("utf-8")) for path in sorted(SCHEMAS.glob("*.schema.json"))}
    registry: Registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return schemas, registry


SCHEMA_FILES, REGISTRY = _load_schemas()


def conforms(answer: Any, schema: str, status: int | tuple[int, ...] = 200) -> Any:
    """The answer's JSON after checking its status, the contract header and its schema."""
    expected = (status,) if isinstance(status, int) else status
    assert answer.status_code in expected, answer.text
    assert answer.headers.get(HEADER) == "1", f"{answer.request.method} {answer.request.url}: no contract header"
    body = answer.json()
    validator = jsonschema.Draft202012Validator(SCHEMA_FILES[f"{schema}.schema.json"], registry=REGISTRY)
    errors = sorted(validator.iter_errors(body), key=lambda error: list(error.path))
    assert not errors, f"{schema}: " + "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
    return body


def refused(answer: Any, status: int, code: str) -> dict[str, Any]:
    body: dict[str, Any] = conforms(answer, "error", status)
    assert body["error"]["code"] == code and body["error"]["status"] == status, body
    return body


def test_the_schemas_are_valid_and_reference_each_other() -> None:
    assert len(SCHEMA_FILES) >= 10
    for name, schema in SCHEMA_FILES.items():
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$id"].endswith(f"/v1/{name}")


# ---- the backends -------------------------------------------------------------------------------------------------


@dataclass
class Backend:
    kind: str
    #: The backend's client (a TestClient, or an httpx client of the network listener's real port).
    api: Any
    slug: str
    #: A key bound to the test publication (run, read, approve).
    token: str
    studio: TestClient
    publication: dict[str, Any]
    ui: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


def contract_workflow(client: TestClient, name: str = "Contract App") -> str:
    start = {
        "id": "start",
        "type": "start",
        "start": {
            "fields": [
                {"name": "customer", "label": "Customer", "type": "text", "required": True, "description": "Who"},
                {"name": "kind", "label": "Kind", "type": "select", "options": ["offer", "bill"], "default": "offer"},
                {
                    "name": "documents",
                    "label": "Documents",
                    "type": "file",
                    "accept": ["pdf", "text"],
                    "max_files": 3,
                    "max_size_mb": 5,
                },
            ]
        },
    }
    nodes = [
        start,
        agent_node("writer", "WRITER", task_template="Offer for {{customer}}"),
        {"id": "check", "type": "approval", "label": "Check", "approval": {"instructions": "OK?", "on_reject": "stop"}},
        {"id": "out", "type": "output"},
    ]
    edges = [edge("start", "writer"), edge("writer", "check"), edge("check", "out", "approved")]
    return save(client, nodes, edges, name=name)


@contextlib.contextmanager
def _network(studio: TestClient) -> Iterator[httpx.Client]:
    port = _free_port()
    started = studio.put("/api/api-network", json={"enabled": True, "host": "127.0.0.1", "port": port})
    assert started.status_code == 200 and started.json()["running"], started.text
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as remote:
            yield remote
    finally:
        studio.put("/api/api-network", json={"enabled": False, "host": "127.0.0.1", "port": port})


@contextlib.contextmanager
def open_backend(kind: str, make_app: Callable[..., TestClient], tmp_path: Path) -> Iterator[Backend]:
    """The test publication served by a backend of ``kind`` (see :data:`KINDS`)."""
    studio = make_app(SCRIPT)
    publication = publish(studio, contract_workflow(studio))["publication"]
    slug = publication["slug"]
    if kind == "desktop":
        yield Backend(kind, studio, slug, new_key(studio, publication["id"]), studio, publication)
        return
    if kind == "network":
        studio.patch(f"/api/publications/{publication['id']}", json={"network": True})
        with _network(studio) as remote:
            yield Backend(kind, remote, slug, new_key(studio, publication["id"]), studio, publication)
        return
    from nodestro_api.serving import hash_secret
    from nodestro_api.serving import new_key as make_token

    key_id, secret, token = make_token()
    keys = f"{key_id}:{hash_secret(secret)}:run+read+approve"
    record = _export(studio, publication["id"], **({"web_ui": True, "language": "de"} if kind == "portable" else {}))
    project = _extract(_download(studio, record["id"]), tmp_path / "export")
    scripted = ScriptedClient(SCRIPT)
    if kind == "export":
        with _exported_app(project, tmp_path / "export-data", scripted, keys) as api:
            yield Backend(kind, api, slug, token, studio, publication, extra={"data_dir": tmp_path / "export-data"})
    else:
        with _ui_app(project, tmp_path / "portable-data", scripted, keys) as (api, ui):
            yield Backend(kind, api, slug, token, studio, publication, ui=ui)


@pytest.fixture(params=KINDS)
def backend(request: pytest.FixtureRequest, make_app: Callable[..., TestClient], tmp_path: Path) -> Iterator[Backend]:
    with open_backend(request.param, make_app, tmp_path) as served:
        yield served


def settle(backend: Backend, run_id: str, *, leave: tuple[str, ...] = ("queued", "running")) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        answer = backend.api.get(f"{PUBLISHED}/{backend.slug}/runs/{run_id}", headers=auth(backend.token))
        status: dict[str, Any] = conforms(answer, "run-status")
        if status["status"] not in leave:
            return status
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not settle")


# ---- discovery ----------------------------------------------------------------------------------------------------


def test_discovery_without_credentials_is_minimal(backend: Backend) -> None:
    body = conforms(backend.api.get("/api/app"), "discovery")
    assert set(body) == {"contract", "backend", "auth", "tls"}
    assert body["contract"] == {"name": "nodestro-app", "versions": [1], "current": 1}
    assert set(body["backend"]) == {"id", "name"} and body["backend"]["name"] == ""
    assert body["tls"] == {"enabled": False}
    assert "api_key" in body["auth"]["methods"]
    # No app names, no publications, no versions.
    text_ = json.dumps(body)
    assert backend.slug not in text_ and backend.publication["name"] not in text_ and 'version"' not in text_
    # The id is stable.
    assert conforms(backend.api.get("/api/app"), "discovery")["backend"]["id"] == body["backend"]["id"]


def test_discovery_with_a_key_lists_its_app_and_the_capabilities(backend: Backend) -> None:
    anonymous = backend.api.get("/api/app").json()
    body = conforms(backend.api.get("/api/app", headers=auth(backend.token)), "discovery")
    assert body["backend"]["id"] == anonymous["backend"]["id"]
    assert body["backend"]["version"] and body["backend"]["runtime_version"]
    assert body["capabilities"] == list(app_contract.SERVED_CAPABILITIES)
    assert set(body["capabilities"]) <= set(app_contract.CAPABILITIES)
    assert "notifications" not in body["capabilities"] and "pairing" not in body["capabilities"]
    assert [app["slug"] for app in body["apps"]] == [backend.slug]
    app = body["apps"][0]
    assert app["name"] == "Contract App" and app["release"] == 1
    info = conforms(backend.api.get(app["links"]["info"], headers=auth(backend.token)), "info")
    assert info["publication"] == backend.slug
    openapi = backend.api.get(app["links"]["openapi"], headers=auth(backend.token))
    assert openapi.status_code == 200 and openapi.headers[HEADER] == "1" and openapi.json()["openapi"].startswith("3.")


def test_discovery_refuses_a_wrong_key_instead_of_ignoring_it(backend: Backend) -> None:
    refused(backend.api.get("/api/app", headers=auth("ndk_000000000000_" + "x" * 40)), 401, "auth.invalid")
    refused(backend.api.get("/api/app", headers={"X-API-Key": "not-a-key"}), 401, "auth.invalid")


def test_auth_methods_name_what_the_backend_offers(backend: Backend) -> None:
    methods = backend.api.get("/api/app").json()["auth"]["methods"]
    expected = {
        "desktop": ["api_key"],
        "network": ["api_key"],
        "export": ["api_key"],  # exported without the web interface
        # The web interface's sign-in, and the launcher's one-time link for the operator.
        "portable": ["api_key", "session", "launch_link"],
    }
    assert methods == expected[backend.kind]


# ---- every route of the contract ----------------------------------------------------------------------------------


def test_every_contract_route_answers_in_its_schema_with_the_header(backend: Backend) -> None:
    api, slug, key = backend.api, backend.slug, auth(backend.token)
    base = f"{PUBLISHED}/{slug}"
    info = conforms(api.get(base, headers=key), "info")
    assert info["form_version"] == 1
    # files.upload: a file in the form.
    body = {"values": {"customer": "ACME", "documents": [FILE]}}
    started = conforms(api.post(f"{base}/runs", json=body, headers=key), "run-status", 202)
    run_id = started["run_id"]
    paused = settle(backend, run_id)
    assert paused["status"] == "awaiting_approval", paused
    # approvals
    waiting = conforms(api.get(f"{base}/runs/{run_id}/approvals", headers=key), "approvals")
    assert len(waiting) == 1
    decided = conforms(
        api.post(f"{base}/runs/{run_id}/approve", json={"decision": "approved"}, headers=key), "run-status"
    )
    assert decided["run_id"] == run_id
    done = settle(backend, run_id, leave=("queued", "running", "awaiting_approval"))
    assert done["status"] == "succeeded", done
    result = conforms(api.get(f"{base}/runs/{run_id}/result", headers=key), "run-result")
    assert result["output"] == "Offer for ACME"
    # files.download
    conforms(api.get(f"{base}/runs/{run_id}/files", headers=key), "files")
    refused(api.get(f"{base}/runs/{run_id}/files/file_missing", headers=key), 404, "file.not_found")
    # history, usage
    runs = conforms(api.get(f"{base}/runs?limit=5", headers=key), "run-list")
    assert [item["run_id"] for item in runs] == [run_id]
    usage = conforms(api.get(f"{base}/usage", headers=key), "usage")
    assert usage["runs"] == 1
    # cancel and resume answer (a finished run cannot be cancelled, a successful one not continued)
    refused(api.post(f"{base}/runs/{run_id}/cancel", headers=key), 409, "run.finished")
    refused(api.post(f"{base}/runs/{run_id}/resume", json={}, headers=key), 409, "resume.not_failed")
    # runs.sync: the parameter is served; this app does not allow waiting
    assert info["sync"]["enabled"] is False
    refused(api.post(f"{base}/runs?wait=1", json=body, headers=key), 400, "sync.disabled")
    # events.sse: a finished run's stream is its status and the end
    events = api.get(f"{base}/runs/{run_id}/events", headers=key)
    assert events.status_code == 200 and events.headers[HEADER] == "1"
    assert events.headers["content-type"].startswith("text/event-stream")
    blocks = [block for block in events.text.split("\n\n") if block.strip()]
    first = dict(line.split(": ", 1) for line in blocks[0].splitlines())
    assert first["event"] == "status"
    status = json.loads(first["data"])
    validator = jsonschema.Draft202012Validator(SCHEMA_FILES["run-status.schema.json"], registry=REGISTRY)
    assert not list(validator.iter_errors(status))
    assert blocks[-1].startswith("event: end")
    # the release-specific start
    again = conforms(api.post(f"{base}/versions/1/runs", json=body, headers=key), "run-status", 202)
    settle(backend, again["run_id"])


def test_errors_have_one_envelope(backend: Backend) -> None:
    api, base, key = backend.api, f"{PUBLISHED}/{backend.slug}", auth(backend.token)
    refused(api.get(base), 401, "auth.missing")
    refused(api.get(base, headers=auth("ndk_000000000000_" + "x" * 40)), 401, "auth.invalid")
    refused(api.get(f"{PUBLISHED}/no-such-app", headers=key), 404, "publication.not_found")
    refused(api.get(f"{base}/runs/run_missing", headers=key), 404, "run.not_found")
    refused(api.post(f"{base}/runs", json={"values": {"kind": "offer"}}, headers=key), 422, "request.invalid")
    missing = api.post(f"{base}/versions/9/runs", json={"values": {"customer": "A"}}, headers=key)
    refused(missing, 404, "version.not_found")


def test_the_form_can_be_rendered_from_the_info_alone(backend: Backend) -> None:
    info = conforms(backend.api.get(f"{PUBLISHED}/{backend.slug}", headers=auth(backend.token)), "info")
    customer, kind, documents = info["form"]
    assert customer == {
        "name": "customer",
        "type": "text",
        "label": "Customer",
        "help": "Who",
        "required": True,
        "default": None,
        "options": [],
        "multiple": False,
        "accept": [],
        "accept_formats": [],
        "max_mb": None,
        "max_files": None,
    }
    assert kind["type"] == "select" and kind["options"] == ["offer", "bill"] and kind["default"] == "offer"
    assert documents["type"] == "file" and documents["multiple"] is True
    assert documents["max_mb"] == 5 and documents["max_files"] == 3
    assert documents["accept_formats"] == ["pdf", "text"]
    assert ".pdf" in documents["accept"] and ".txt" in documents["accept"]
    assert all(item.startswith(".") for item in documents["accept"])
    assert {item["type"] for item in info["form"]} <= set(app_contract.FORM_FIELD_TYPES)


def test_no_contract_header_outside_the_contract(backend: Backend) -> None:
    health = "/api/health" if backend.kind == "desktop" else "/health"
    answer = backend.api.get(health)
    assert answer.status_code == 200 and HEADER not in answer.headers


# ---- visibility ---------------------------------------------------------------------------------------------------


def test_a_key_sees_only_its_own_apps(make_app: Callable[..., TestClient]) -> None:
    studio = make_app(SCRIPT)
    first = publish(studio, contract_workflow(studio, "App A"))["publication"]
    second = publish(studio, contract_workflow(studio, "App B"))["publication"]
    key_a = new_key(studio, first["id"])
    key_b = new_key(studio, second["id"])
    both = studio.post(
        "/api/api-keys", json={"name": "Both", "scopes": ["read"], "publications": [first["id"], second["id"]]}
    ).json()["token"]

    def apps(client: Any, token: str) -> list[str]:
        return [app["slug"] for app in conforms(client.get("/api/app", headers=auth(token)), "discovery")["apps"]]

    assert apps(studio, key_a) == ["app-a"]
    assert apps(studio, key_b) == ["app-b"]
    assert apps(studio, both) == ["app-a", "app-b"]
    refused(studio.get(f"{PUBLISHED}/app-b", headers=auth(key_a)), 404, "publication.not_found")
    # On the network listener only the apps shared there.
    studio.patch(f"/api/publications/{second['id']}", json={"network": True})
    with _network(studio) as remote:
        assert apps(remote, both) == ["app-b"]
        assert apps(remote, key_a) == []
        assert set(remote.get("/api/app").json()) == {"contract", "backend", "auth", "tls"}
    # A revoked key gets 401, not the minimal answer.
    assert studio.post(f"/api/api-keys/{key_a.split('_')[1]}/revoke").status_code == 200
    refused(studio.get("/api/app", headers=auth(key_a)), 401, "auth.invalid")


def test_a_deleted_app_stays_out_of_sight(make_app: Callable[..., TestClient]) -> None:
    studio = make_app(SCRIPT)
    publication = publish(studio, contract_workflow(studio))["publication"]
    key = new_key(studio, publication["id"])
    assert studio.delete(f"/api/publications/{publication['id']}").status_code == 204
    assert conforms(studio.get("/api/app", headers=auth(key)), "discovery").get("apps", []) == []


# ---- backend identity ---------------------------------------------------------------------------------------------


def test_the_backend_id_survives_a_restart_and_the_operator_names_it(
    make_app: Callable[..., TestClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    studio = make_app(SCRIPT)
    data_dir = studio.app.state.nodestro.config.data_dir  # type: ignore[attr-defined]
    first = studio.get("/api/app").json()["backend"]
    stored = json.loads((data_dir / "instance.json").read_text("utf-8"))
    assert stored["id"] == first["id"] and first["id"].startswith("bk_")
    again = app_contract.load_instance(data_dir)
    assert again.id == first["id"] and again.name == ""
    monkeypatch.setenv(app_contract.BACKEND_NAME_ENV, "Office server")
    assert app_contract.load_instance(data_dir).name == "Office server"
    # An unreadable file is kept aside; the backend gets a new id.
    (data_dir / "instance.json").write_text("{broken", "utf-8")
    renewed = app_contract.load_instance(data_dir)
    assert renewed.id != first["id"] and (data_dir / "instance.json.invalid").is_file()
    # Another installation has another id.
    other = app_contract.load_instance(tmp_path / "elsewhere")
    assert other.id not in (first["id"], renewed.id)


def test_the_exported_backend_keeps_its_id_in_its_data_directory(
    make_app: Callable[..., TestClient], tmp_path: Path
) -> None:
    with open_backend("export", make_app, tmp_path) as backend:
        check_exported_id(backend)


def check_exported_id(backend: Backend) -> None:
    body = backend.api.get("/api/app").json()
    stored = json.loads((backend.extra["data_dir"] / "instance.json").read_text("utf-8"))
    assert stored["id"] == body["backend"]["id"]


# ---- the embedded interface ---------------------------------------------------------------------------------------


def test_the_interface_session_is_a_contract_route_and_signs_in_to_discovery(
    make_app: Callable[..., TestClient], tmp_path: Path
) -> None:
    with open_backend("portable", make_app, tmp_path) as backend:
        check_interface_session(backend)


def check_interface_session(backend: Backend) -> None:
    api = backend.api
    signed = conforms(api.post("/ui/api/session", json={"api_key": backend.token}, headers=UI_HEADERS), "session")
    assert signed["operator"] is False
    found = conforms(api.get("/api/app"), "discovery")
    assert [app["slug"] for app in found["apps"]] == [backend.slug]
    # The interface reads the same form mapping as the info.
    config = api.get("/ui/api/config").json()
    form = conforms(api.get(f"{PUBLISHED}/{backend.slug}"), "info")["form"]
    assert [(item["name"], item["type"], item["label"]) for item in config["fields"]] == [
        (item["name"], item["type"], item["label"]) for item in form
    ]
    documents = config["fields"][2]
    assert documents["accept_extensions"] == form[2]["accept"] and documents["max_size_mb"] == form[2]["max_mb"]
    out = conforms(api.delete("/ui/api/session", headers=UI_HEADERS), "session")
    assert out == {"signed_out": True}
    assert set(conforms(api.get("/api/app"), "discovery")) == {"contract", "backend", "auth", "tls"}
    refused(api.post("/ui/api/session", json={"api_key": "ndk_wrong"}, headers=UI_HEADERS), 401, "auth.invalid")


# ---- limits and versions ------------------------------------------------------------------------------------------


def test_discovery_is_rate_limited_like_the_published_api(make_app: Callable[..., TestClient]) -> None:
    from nodestro_api.app_discovery import ANONYMOUS_REQUESTS_PER_MINUTE

    studio = make_app({})
    for _ in range(ANONYMOUS_REQUESTS_PER_MINUTE):
        assert studio.get("/api/app").status_code == 200
    answer = refused(studio.get("/api/app"), 429, "limit.requests")
    assert answer["error"]["params"]["limit"] == ANONYMOUS_REQUESTS_PER_MINUTE
    assert int(studio.get("/api/app").headers["retry-after"]) >= 1


def _common_version(discovery: dict[str, Any], supported: set[int]) -> int | None:
    """What a client does with the discovery: the highest version both sides serve, or none (incompatible)."""
    shared = set(discovery["contract"]["versions"]) & supported
    return max(shared) if shared else None


def test_a_client_recognises_a_backend_without_a_common_version(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    studio = make_app({})
    assert _common_version(studio.get("/api/app").json(), {1}) == 1
    monkeypatch.setattr(app_contract, "CONTRACT_VERSIONS", (2,))
    monkeypatch.setattr(app_contract, "CONTRACT_CURRENT", 2)
    simulated = conforms(studio.get("/api/app"), "discovery")
    assert simulated["contract"]["versions"] == [2]
    assert _common_version(simulated, {1}) is None
