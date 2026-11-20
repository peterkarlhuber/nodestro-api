"""The product host (N-01, ``docs/embedding.md``): an app from named router groups, as protected as the studio."""

from __future__ import annotations

from dataclasses import fields
import inspect
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
import pytest

from nodestro_api import __version__, host
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig
from nodestro_api.host import GROUPS, HOST_API_VERSION, Branding, HostSwitches, create_product_app
from nodestro_api.permissions import READ, RouteAccessError
from nodestro_api.router_groups import SLOTS

#: The three groups the test product is composed of.
SELECTED = ("settings", "knowledge", "assistant")
SECRET = "s" * 43


def _product_router() -> APIRouter:
    router = APIRouter(prefix="/api/product")

    @router.get("/hello", dependencies=READ)
    def hello(request: Request) -> dict[str, str]:
        return {"hello": request.app.title}

    return router


def _product(tmp_path: Path, **config: object) -> FastAPI:
    return create_product_app(
        "desktop",
        tmp_path / "data",
        [*SELECTED, _product_router()],
        Branding(title="Test Product", version="9.9.9", description="A product built on the host"),
        HostSwitches(scheduler=False),
        config=AppConfig(data_dir=tmp_path / "ignored", **config),  # type: ignore[arg-type]
        integrations=[],
    )


def _schema_paths(groups: set[str]) -> set[str]:
    """The documented paths of the registry's routers of ``groups`` that need no workspace."""
    found: set[str] = set()
    for slot in SLOTS:
        if slot.group in groups and not slot.needs_workspace:
            routes = slot.build(None).routes
            # OpenAPI writes ``{name:path}`` as ``{name}``.
            documented = [getattr(item, "path", "") for item in routes if getattr(item, "include_in_schema", False)]
            found |= {re.sub(r":\w+}", "}", path) for path in documented}
    return found


def test_a_product_app_serves_its_groups_and_its_own_routes(tmp_path: Path) -> None:
    with TestClient(_product(tmp_path), base_url="http://127.0.0.1") as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/meta").status_code == 200
        assert client.get("/api/settings").status_code == 200
        assert client.get("/api/knowledge").json() == []
        assert client.get("/api/inputs/formats").status_code == 200
        assert client.get("/api/assistant/conversations").status_code == 200
        assert client.get("/api/product/hello").json() == {"hello": "Test Product"}
    assert (tmp_path / "data").is_dir() and not (tmp_path / "ignored").exists()


def test_routes_of_groups_not_selected_answer_404(tmp_path: Path) -> None:
    with TestClient(_product(tmp_path), base_url="http://127.0.0.1") as client:
        for path in (
            "/api/workflows",
            "/api/library",
            "/api/local-models",
            "/api/tools",
            "/api/memory",
            "/api/skills",
            "/api/publications",
            "/api/published/anything",
            "/api/notifications",
            "/api/usage",
            "/api/search?q=x",
        ):
            answer = client.get(path)
            assert answer.status_code == 404, path
            assert answer.json()["code"] == "route.not_found", path
        assert client.post("/api/workflows", json={"name": "x"}).status_code == 404


def test_the_openapi_document_holds_only_the_selected_groups(tmp_path: Path) -> None:
    with TestClient(_product(tmp_path), base_url="http://127.0.0.1") as client:
        document = client.get("/api/openapi.json").json()
    assert document["info"] == {
        "title": "Test Product",
        "version": "9.9.9",
        "description": "A product built on the host",
    }
    expected = _schema_paths({*SELECTED, "core"}) | {"/api/product/hello"}
    assert set(document["paths"]) == expected
    assert not set(document["paths"]) & (_schema_paths(set(GROUPS) - {*SELECTED, "core"}) - expected)
    # Every studio operation keeps the documented error answer and a tag (API-016).
    for path, operations in document["paths"].items():
        for operation in operations.values():
            assert "4XX" in operation["responses"], path
            assert operation["tags"], path


def test_the_product_app_never_imports_the_studio(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from fastapi.testclient import TestClient
        from nodestro_api.context import AppConfig
        from nodestro_api.host import HostSwitches, create_product_app

        data = Path(sys.argv[1])
        app = create_product_app(
            "desktop", data, ["settings", "knowledge", "assistant"], switches=HostSwitches(scheduler=False),
            config=AppConfig(data_dir=data), integrations=[],
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            assert client.get("/api/health").status_code == 200
        studio = sorted(name for name in sys.modules if name.split(".")[0] == "nodestro_studio")
        assert not studio, studio
        print("clean")
        """
    )
    found = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "data")],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=120,
        check=False,
    )
    assert found.returncode == 0, found.stderr
    assert found.stdout.strip().endswith("clean")


def test_the_studio_app_serves_every_group(tmp_path: Path) -> None:
    studio = create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)
    try:
        assert _schema_paths(set(GROUPS)) <= set(studio.openapi()["paths"])
    finally:
        studio.state.nodestro.close()


# ---- the same protection as the studio ----------------------------------------------------------------------------


def _middleware(app: FastAPI) -> list[tuple[str, dict[str, object]]]:
    """Every middleware with its settings, outermost last."""
    return [(getattr(item.cls, "__name__", repr(item.cls)), dict(item.kwargs)) for item in app.user_middleware]


def test_a_product_app_has_the_studio_apps_guards(tmp_path: Path) -> None:
    for name, config in (
        ("plain", {}),
        ("launch secret", {"launch_secret": SECRET}),
        ("dev cors", {"dev_cors": True}),
    ):
        product = _product(tmp_path / name, **config)
        studio = create_app(AppConfig(data_dir=tmp_path / name / "studio", **config), integrations=[], scheduler=False)
        try:
            assert _middleware(product) == _middleware(studio), name
            assert "LocalAccessMiddleware" in [middleware for middleware, _ in _middleware(product)]
        finally:
            product.state.nodestro.close()
            studio.state.nodestro.close()


def test_a_product_app_refuses_foreign_origins_and_hosts(tmp_path: Path) -> None:
    with TestClient(_product(tmp_path), base_url="http://127.0.0.1") as client:
        answer = client.post("/api/knowledge/search", json={}, headers={"Origin": "https://evil.example"})
        assert answer.status_code == 403
        assert client.get("/api/settings", headers={"Host": "evil.example:8000"}).status_code in {400, 403}
        assert "x-content-type-options" in client.get("/api/health").headers


def test_a_product_app_needs_the_launch_secret(tmp_path: Path) -> None:
    with TestClient(_product(tmp_path, launch_secret=SECRET), base_url="http://127.0.0.1") as client:
        assert client.get("/api/health").status_code == 200
        for path in ("/api/settings", "/api/product/hello", "/api/openapi.json"):
            answer = client.get(path)
            assert answer.status_code == 401, path
            assert answer.json()["code"] == "access.launch_required"
        client.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
        assert client.get("/api/product/hello").status_code == 200


def test_a_product_route_without_an_access_requirement_stops_the_build(tmp_path: Path) -> None:
    unprotected = APIRouter()

    @unprotected.get("/api/product/open")
    def open_route() -> dict[str, str]:
        return {}

    with pytest.raises(RouteAccessError, match="GET /api/product/open"):
        create_product_app(
            "desktop",
            tmp_path / "data",
            ["settings", unprotected],
            switches=HostSwitches(scheduler=False),
            config=AppConfig(data_dir=tmp_path),
            integrations=[],
        )


def test_the_configuration_is_checked_as_for_the_studio(tmp_path: Path) -> None:
    def build(profile: str, **config: object) -> FastAPI:
        return create_product_app(
            profile,  # type: ignore[arg-type]
            tmp_path / "data",
            ["settings"],
            switches=HostSwitches(scheduler=False),
            config=AppConfig(data_dir=tmp_path, **config),  # type: ignore[arg-type]
            integrations=[],
        )

    with pytest.raises(NodestroError) as server:
        build("server")
    assert server.value.code == "profile.invalid"
    with pytest.raises(NodestroError) as network:
        build("desktop", host="0.0.0.0")
    assert network.value.code == "access.network_needs_login"
    with pytest.raises(NodestroError) as weak:
        build("desktop", launch_secret="short")
    assert weak.value.code == "access.launch_secret_weak"
    with pytest.raises(NodestroError) as keyless:
        build("local-container")
    assert keyless.value.code == "access.key_required"


def test_unknown_groups_and_other_objects_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown router groups: nothing"):
        create_product_app("desktop", tmp_path, ["settings", "nothing"], config=AppConfig(data_dir=tmp_path))
    with pytest.raises(TypeError, match="group name or an APIRouter"):
        create_product_app("desktop", tmp_path, [object()], config=AppConfig(data_dir=tmp_path))  # type: ignore[list-item]


# ---- the frozen interface -----------------------------------------------------------------------------------------


def test_the_host_interface_is_frozen() -> None:
    """A change of the composition interface breaks this test: raise HOST_API_VERSION and update docs/embedding.md."""
    assert HOST_API_VERSION == 1
    assert str(inspect.signature(create_product_app)) == (
        "(profile: 'ProfileKind', data_dir: 'Path | str', routers: 'Sequence[str | APIRouter]', "
        "branding: 'Branding | None' = None, switches: 'HostSwitches | None' = None, *, "
        "config: 'AppConfig | None' = None, integrations: 'Iterable[Integration] | None' = None, "
        "client_factory: 'Callable[[str], Any] | None' = None) -> 'FastAPI'"
    )
    assert GROUPS == (
        "mcp_server",
        "published",
        "app_contract",
        "publishing",
        "architect",
        "python",
        "automation",
        "assistant",
        "knowledge",
        "usage",
        "local_models",
        "agents",
        "skills",
        "inbox",
        "connections",
        "mcp",
        "budget",
        "approvals",
        "runs",
        "core",
        "settings",
        "workflows",
        "library",
        "tools",
        "references",
        "memory",
        "data",
    )
    assert frozenset({"core"}) == host.REQUIRED_GROUPS
    assert [(item.name, item.default) for item in fields(Branding)] == [
        ("title", "Nodestro"),
        ("version", __version__),
        ("description", ""),
    ]
    assert [(item.name, item.default) for item in fields(HostSwitches)] == [("scheduler", True)]
    assert sorted(host.__all__) == [
        "Branding",
        "GROUPS",
        "HOST_API_VERSION",
        "HostSwitches",
        "REQUIRED_GROUPS",
        "create_product_app",
        "install_guards",
        "service_lifespan",
    ]
