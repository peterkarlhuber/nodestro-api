"""Every FastAPI application this package builds checks the protection of every route when it is built (MUP-ONA A5,
PS-5): a route without a declared requirement stops the build (deny by default)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from fastapi import FastAPI
import pytest
import test_identity
import test_portable

import nodestro_api
from nodestro_api import app as studio_app
from nodestro_api import network, server_app, standalone
from nodestro_api.context import AppConfig
from nodestro_api.permissions import RouteAccessError, classify
from nodestro_api.webui_server import WEB_UI_ROUTES

# The fixtures that build a published release and a server tenant app (PostgreSQL).
published = test_portable.published
ui_project = test_portable.ui_project
database = test_identity.database
identity = test_identity.identity

PACKAGE = Path(nodestro_api.__file__).resolve().parent

#: Every function that builds an application that is served, and calls ``check_routes`` on it.
FACTORIES = {
    "app.py:create_app": "the desktop studio API",
    "server_app.py:create_tenant_app": "the server's studio API (logins, tenants)",
    "network.py:network_app": "the network listener of published APIs",
    "standalone.py:create_app": "an exported project (published API, optional web interface)",
}
#: Constructions of ``FastAPI`` that are never served, with the reason.
NOT_SERVED = {
    "serving.py:openapi_document": "builds the OpenAPI document of one release; the application is never served",
}


def _constructions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef | None]:
    """``file:function`` of every ``FastAPI(...)`` call in the package (``file:<module>`` outside a function)."""
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef | None] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        name = path.relative_to(PACKAGE).as_posix()

        def visit(node: ast.AST, owner: ast.FunctionDef | ast.AsyncFunctionDef | None, name: str = name) -> None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and owner is None:
                owner = node
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                if called == "FastAPI":
                    found[f"{name}:{owner.name if owner else '<module>'}"] = owner
            for child in ast.iter_child_nodes(node):
                visit(child, owner)

        visit(tree, None)
    return found


def _calls_check_routes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "check_routes"
        for node in ast.walk(function)
    )


# MUP-ONA T-API-ROUTE-2
def test_every_fastapi_construction_is_a_checked_factory() -> None:
    found = _constructions()
    assert set(found) == set(FACTORIES) | set(NOT_SERVED), "a new FastAPI(...) needs check_routes and a FACTORIES entry"
    for name in FACTORIES:
        function = found[name]
        assert function is not None and _calls_check_routes(function), f"{name} does not call check_routes"


class _Tripwire(FastAPI):
    """An application that gets a route without an access requirement as soon as it is constructed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        @self.get("/mup-ona-unprotected")
        def unprotected() -> dict[str, str]:
            return {}


# MUP-ONA T-API-ROUTE-1
def test_the_desktop_app_is_not_built_with_an_unprotected_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio_app, "FastAPI", _Tripwire)
    with pytest.raises(RouteAccessError, match="GET /mup-ona-unprotected"):
        studio_app.create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)


# MUP-ONA T-API-ROUTE-1
def test_the_network_listener_is_not_built_with_an_unprotected_route(monkeypatch: pytest.MonkeyPatch) -> None:
    assert {item.path: item.access for item in classify(network.network_app(object()))[0]}["/health"] == "public"  # type: ignore[arg-type]
    monkeypatch.setattr(network, "FastAPI", _Tripwire)
    with pytest.raises(RouteAccessError, match="GET /mup-ona-unprotected"):
        network.network_app(object())  # type: ignore[arg-type]


# MUP-ONA T-API-ROUTE-1
def test_an_exported_app_is_not_built_with_an_unprotected_route(
    ui_project: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nodestro_api.webui_server import UiOptions, WebUi

    _studio, _publication, project = ui_project
    settings = standalone.StandaloneSettings.from_env({"DATA_DIR": str(tmp_path / "data"), "API_KEYS": ""})

    def web_ui(host: Any, _core: Any) -> Any:
        return WebUi(host, UiOptions(language="en", control_token="control-token", output_dir=str(tmp_path / "out")))

    built = standalone.create_app(project, settings=settings, web_ui=web_ui)
    access = {(item.method, item.path): item.access for item in classify(built, WEB_UI_ROUTES)[0]}
    assert access[("GET", "/health")] == "public" and access[("GET", "/ui/api/config")] == "own"
    assert {key for key, value in access.items() if value == "own"} == set(WEB_UI_ROUTES)
    monkeypatch.setattr(standalone, "FastAPI", _Tripwire)
    with pytest.raises(RouteAccessError, match="GET /mup-ona-unprotected"):
        standalone.create_app(project, settings=settings, web_ui=web_ui)


# MUP-ONA T-API-ROUTE-1
@pytest.mark.parametrize("backend", ["nodestro"])
def test_the_server_app_is_not_built_with_an_unprotected_route(
    database: Any, identity: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stub_oidc

    provider = stub_oidc.StubProvider()
    test_identity.tenant_app(identity, database, provider, tmp_path / "checked")  # builds with every route checked
    monkeypatch.setattr(server_app, "FastAPI", _Tripwire)
    with pytest.raises(RouteAccessError, match="GET /mup-ona-unprotected"):
        test_identity.tenant_app(identity, database, provider, tmp_path / "tripwire")


# MUP-ONA T-API-ROUTE-1
def test_web_interface_routes_need_an_entry() -> None:
    """A route of the web interface that is not in WEB_UI_ROUTES stops the build like any other."""
    app = FastAPI()

    @app.get("/ui/api/new")
    def new() -> None:
        return None

    _found, problems = classify(app, WEB_UI_ROUTES)
    assert problems == ["GET /ui/api/new: no access requirement"]
