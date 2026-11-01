"""Operating profiles in the API, the permission of every route, and how they are enforced."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.profile import RuntimeProfile
import pytest
import uvicorn

from nodestro_api import cli
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig
from nodestro_api.permissions import (
    ALL_PERMISSIONS,
    EDIT,
    LOCAL_OWNER,
    READ,
    Access,
    Permission,
    Principal,
    RouteAccessError,
    check_routes,
    classify,
    nobody,
    resolver_for,
)
from nodestro_api.serving import KEY_AUTHENTICATED_ROUTES, KeyAuthenticatedRoute

WORKFLOW = {"name": "Rights", "nodes": [], "edges": []}


def _app(tmp_path: Path) -> FastAPI:
    return create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)


@pytest.fixture
def app(tmp_path: Path) -> Iterator[FastAPI]:
    built = _app(tmp_path)
    yield built
    built.state.nodestro.close()


# ---- profiles ------------------------------------------------------------------------------


def test_the_desktop_profile_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = AppConfig(data_dir=tmp_path)
    assert config.profile == RuntimeProfile.desktop(tmp_path)
    config.data_dir = tmp_path / "moved"  # the studio sets the data directory after from_env()
    assert config.profile.data_dir == tmp_path / "moved"
    monkeypatch.delenv("NODESTRO_PROFILE", raising=False)
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    assert AppConfig.from_env().profile == RuntimeProfile.desktop(tmp_path)


def test_the_workspace_opens_the_profile_of_the_configuration(app: FastAPI, tmp_path: Path) -> None:
    assert app.state.nodestro.profile == RuntimeProfile.desktop(tmp_path / "data")
    assert app.state.principal_resolver is resolver_for(app.state.nodestro.profile)


def test_the_environment_chooses_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NODESTRO_PROFILE", " Server ")
    assert AppConfig.from_env().profile_kind == "server"
    monkeypatch.setenv("NODESTRO_PROFILE", "production")
    with pytest.raises(NodestroError) as error:
        AppConfig.from_env()
    assert error.value.code == "profile.invalid"


@pytest.mark.parametrize(
    ("kind", "code"), [("server", "profile.invalid"), ("laptop", "profile.invalid")], ids=["server", "bad"]
)
def test_create_app_refuses_profiles_before_anything_opens(tmp_path: Path, kind: Any, code: str) -> None:
    data = tmp_path / "data"
    with pytest.raises(NodestroError) as error:
        create_app(AppConfig(data_dir=data, profile_kind=kind), integrations=[], scheduler=False)
    assert error.value.code == code
    assert not data.exists()


def test_create_app_without_a_configuration_follows_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NODESTRO_PROFILE", "server")
    with pytest.raises(NodestroError, match="server profile"):
        create_app(integrations=[], scheduler=False)
    assert not (tmp_path / "data").exists()


@pytest.fixture
def command_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[str]]:
    started: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append("run"))
    # The command line writes these into the environment; monkeypatch restores them afterwards.
    monkeypatch.setenv("NODESTRO_HOST", "127.0.0.1")
    for name in ("NODESTRO_DATA_DIR", "NODESTRO_PROFILE"):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("NODESTRO_ALLOWED_HOSTS", raising=False)
    monkeypatch.chdir(tmp_path)

    def run(*argv: str) -> list[str]:
        cli.main(["--data-dir", str(tmp_path / "data"), *argv])
        return started

    return run


def test_the_command_line_refuses_the_server_profile_before_touching_the_disk(
    command_line: Callable[..., list[str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit, match="nodestro-server command"):
        command_line("--profile", "server")
    with pytest.raises(SystemExit, match="not a profile"):
        command_line("--profile", "cloud")
    monkeypatch.setenv("NODESTRO_PROFILE", "server")
    with pytest.raises(SystemExit, match="server profile"):
        command_line()
    assert not (tmp_path / "data").exists()
    assert command_line("--profile", "desktop") == ["run"]


# ---- every route declares its permission ---------------------------------------------------------


def test_every_route_and_method_is_classified(app: FastAPI) -> None:
    found = check_routes(app)
    studio = [item for item in found if item.access in {"read", "edit", "run", "admin", "install", "public"}]
    assert Counter(item.access for item in studio) == {
        # read: + MCP stdio server environment (WP-8)
        "read": 99,  # + price catalog, status, history, reviews (PRICING-1); gate report (Q-3); case proposal (Q-4);
        # MCP status, servers, server, tools (WP-3); tool approval inbox, run questions, pre-grants, state (S-2b)
        "edit": 47,
        "run": 32,  # + deciding a tool call (S-2b)
        "admin": 42,  # + price sync, reviews, overrides, billing keys and reconciliation (PRICING-1); gate (Q-3);
        # MCP add, change, remove, discover, decision, events (WP-3); pre-grants, audit, activation (S-2b);
        # MCP OAuth start, callback, sign-out (WP-6); MCP stdio package plan (WP-8); MCP stats (WP-7)
        "install": 10,
        "public": 1,
    }
    assert all(item.path.startswith("/api/") for item in found if item.access != "generated")
    # Explicit exceptions: the health check, the key-protected published APIs, FastAPI's API reference.
    assert {item.path for item in found if item.access == "public"} == {"/api/health"}
    assert {item.path for item in found if item.access == "api_key"} and all(
        item.path.startswith("/api/published/") for item in found if item.access == "api_key"
    )
    assert {item.path for item in found if item.access == "generated"} == {
        "/api/openapi.json",
        "/api/docs",
        "/docs/oauth2-redirect",
    }


SENSITIVE = {
    ("PUT", "/api/settings"): "admin",
    ("POST", "/api/pricing/sync"): "admin",
    ("PUT", "/api/pricing/overrides/{provider}/{model:path}"): "admin",
    ("GET", "/api/pricing/billing-keys"): "admin",
    ("PUT", "/api/pricing/billing-keys/{provider}"): "admin",
    ("POST", "/api/pricing/reconciliation"): "admin",
    ("POST", "/api/backup/restore"): "install",
    ("GET", "/api/backup"): "install",
    ("GET", "/api/diagnostics"): "install",
    ("POST", "/api/api-keys"): "admin",
    ("GET", "/api/api-keys"): "admin",
    ("PUT", "/api/api-network"): "install",
    ("POST", "/api/workflows/{workflow_id}/api/publish"): "admin",
    ("PUT", "/api/publications/{publication_id}/gate"): "admin",
    ("POST", "/api/python/plans/{plan_id}/approve"): "admin",
    ("POST", "/api/tools/jobs/{job_id}/dependencies"): "admin",
    ("POST", "/api/workflows/{workflow_id}/runs"): "run",
    ("POST", "/api/runs/{run_id}/approvals/{node_id}"): "run",
    ("POST", "/api/runs/{run_id}/tool-approvals/{request_id}"): "run",
    ("POST", "/api/tool-pre-grants"): "admin",
    ("DELETE", "/api/tool-pre-grants/{scope}/{scope_id}/{grant_id}"): "admin",
    ("GET", "/api/tool-approvals/audit"): "admin",
    ("POST", "/api/tool-approvals/legacy"): "admin",
    ("GET", "/api/runs/{run_id}/tool-approvals"): "read",
    ("POST", "/api/tools/jobs"): "run",
    ("POST", "/api/tools/{name}/run"): "run",
    ("POST", "/api/tools/drafts/{draft_id}/test"): "run",
    ("POST", "/api/schedules"): "run",
    ("POST", "/api/assistant/conversations/{conversation_id}/messages"): "run",
    ("POST", "/api/knowledge"): "edit",
    ("POST", "/api/inputs"): "edit",
    ("PUT", "/api/workflows/{workflow_id}"): "edit",
    ("GET", "/api/runs/{run_id}/events"): "read",
    ("GET", "/api/architect/jobs/{job_id}/events"): "read",
    ("GET", "/api/assistant/conversations/{conversation_id}/messages/{message_id}/events"): "read",
    ("GET", "/api/knowledge/{file_id}/content"): "read",
    ("GET", "/api/used-by"): "read",
    ("GET", "/api/exports/{export_id}/download"): "read",
    ("GET", "/api/runs/{run_id}/files/{file_id}"): "read",
    ("POST", "/api/published/{slug}/runs"): "api_key",
}


def test_sensitive_routes_need_the_right_permission(app: FastAPI) -> None:
    access = {(item.method, item.path): item.access for item in check_routes(app)}
    assert {key: access[key] for key in SENSITIVE} == SENSITIVE


def test_a_route_without_a_requirement_stops_the_application(app: FastAPI) -> None:
    @app.get("/api/forgotten")
    def forgotten() -> str:
        return "?"

    with pytest.raises(RouteAccessError, match="GET /api/forgotten: no access requirement"):
        check_routes(app)


def test_the_published_prefix_exempts_only_the_key_authenticated_endpoints(app: FastAPI) -> None:
    keyed = {(item.method, item.path) for item in check_routes(app) if item.access == "api_key"}
    assert keyed == KEY_AUTHENTICATED_ROUTES
    assert len(keyed) == 18  # the 15 published API routes and the MCP server endpoint (POST, GET, DELETE)

    # A route that merely lives under the prefix is not exempt ...
    @app.get("/api/published/{slug}/debug")
    def debug(slug: str) -> str:
        return slug

    with pytest.raises(RouteAccessError, match=r"GET /api/published/\{slug\}/debug: not a key-authenticated"):
        check_routes(app)


def test_a_published_route_class_alone_is_not_enough(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        extra = APIRouter(prefix="/api/published/{slug}", route_class=KeyAuthenticatedRoute)

        @extra.delete("/runs")
        def wipe(slug: str) -> None:
            return None

        @extra.get("/runs")  # same path as a listed endpoint, other method: still listed per method
        def duplicate(slug: str) -> None:
            return None

        app.include_router(extra)
        _found, problems = classify(app)
        assert problems == [
            "DELETE /api/published/{slug}/runs: not a key-authenticated published endpoint "
            "(nodestro_api.serving.KEY_AUTHENTICATED_ROUTES)"
        ]
    finally:
        app.state.nodestro.close()


def test_included_routers_websockets_and_double_declarations_are_checked() -> None:
    def problems(router: APIRouter) -> list[str]:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(router)
        return classify(app)[1]

    plain = APIRouter(prefix="/api")

    @plain.websocket("/live")
    async def live(socket: WebSocket) -> None:
        await socket.close()

    assert problems(plain) == ["WEBSOCKET /api/live: no access requirement"]

    declared = APIRouter(prefix="/api")

    @declared.websocket("/live", dependencies=READ)
    async def declared_live(socket: WebSocket) -> None:
        await socket.close()

    @declared.get("/twice", dependencies=[*READ, *EDIT])
    def twice() -> None:
        return None

    assert problems(declared) == ["GET /api/twice: 2 access requirements"]
    # A requirement added when the router is included counts as well.
    included = APIRouter(prefix="/api")

    @included.get("/inherited")
    def inherited() -> None:
        return None

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(included, dependencies=READ)
    assert [(item.method, item.path, item.access) for item in check_routes(app)] == [("GET", "/api/inherited", "read")]
    with pytest.raises(RouteAccessError):
        app.mount("/files", FastAPI())
        check_routes(app)


def test_a_requirement_is_one_permission_or_explicitly_public() -> None:
    with pytest.raises(ValueError):
        Access(None)
    with pytest.raises(ValueError):
        Access(Permission.READ, public=True)


# ---- enforcement -----------------------------------------------------------------------------


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url="http://127.0.0.1") as found:
        yield found


def test_the_local_owner_may_do_everything(client: TestClient) -> None:
    assert LOCAL_OWNER.permissions == ALL_PERMISSIONS == set(Permission)
    assert client.post("/api/workflows", json=WORKFLOW).status_code == 201
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/api-keys").status_code == 200


def test_without_a_principal_every_studio_route_is_refused(app: FastAPI, client: TestClient) -> None:
    app.state.principal_resolver = nobody
    for method, path in (("GET", "/api/workflows"), ("POST", "/api/workflows"), ("GET", "/api/settings")):
        answer = client.request(method, path, json=WORKFLOW if method == "POST" else None)
        assert answer.status_code == 401, path
        assert answer.json()["code"] == "access.login_required"
    # Explicitly public, or authenticated separately with a key.
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/published/anything").json()["error"]["code"] != "access.login_required"


def test_a_missing_resolver_refuses_instead_of_allowing(app: FastAPI, client: TestClient) -> None:
    del app.state.principal_resolver
    assert client.get("/api/workflows").status_code == 401


def test_permissions_are_enforced_per_route(app: FastAPI, client: TestClient) -> None:
    reader = Principal(id="reader", permissions=frozenset({Permission.READ}))
    app.state.principal_resolver = lambda _connection: reader
    assert client.get("/api/workflows").status_code == 200
    for method, path, needed in (
        ("POST", "/api/workflows", "edit"),
        ("PUT", "/api/settings", "admin"),
        ("POST", "/api/backup/restore", "install"),
        ("POST", "/api/workflows/wf_missing/runs", "run"),
    ):
        answer = client.request(method, path, json={})
        assert answer.status_code == 403, path
        assert answer.json()["code"] == "access.forbidden"
        assert answer.json()["params"] == {"permission": needed}
    assert client.get("/api/workflows").json() == []
    editor = Principal(id="editor", permissions=frozenset({Permission.READ, Permission.EDIT}))
    app.state.principal_resolver = lambda _connection: editor
    assert client.post("/api/workflows", json=WORKFLOW).status_code == 201
    assert client.put("/api/settings", json={}).status_code == 403


def test_live_event_streams_are_protected_too(app: FastAPI, client: TestClient) -> None:
    app.state.principal_resolver = nobody
    assert client.get("/api/runs/run_missing/events").status_code == 401
    assert client.get("/api/architect/jobs/missing/events").status_code == 401
