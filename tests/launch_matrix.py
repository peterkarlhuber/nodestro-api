"""The request matrix of the desktop launch's guards, for the differential test of the NS-002 switches.

:func:`record` sends the same requests, in the same order, to a product app (N-01 host) of the desktop profile with a
launch secret and to one without, and returns every answer: status, headers in their order and body, with only the
random session cookie values replaced. ``tests/vectors/launch-switches-off.json`` holds the answers of nodestro-api
master 6783a52 (before NS-002); ``tests/test_launch_hardening.py`` checks that the code with every switch off gives
the same bytes. Regenerate the file only from a commit before NS-002:

    python tests/launch_matrix.py > tests/vectors/launch-switches-off.json
"""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

SECRET = "s" * 43
ORIGIN = "http://127.0.0.1"
_COOKIE_VALUE = re.compile(r"(nodestro_session=)[^;]+")

#: (label, method, path, keyword arguments of the request). Each runs on the same client, in this order.
REQUESTS: list[tuple[str, str, str, dict[str, Any]]] = [
    ("health", "GET", "/api/health", {}),
    ("app discovery", "GET", "/api/app", {}),
    ("read without session", "GET", "/api/product/echo", {}),
    ("change without session", "POST", "/api/product/echo", {"json": {}, "headers": {"origin": ORIGIN}}),
    ("launch without token", "GET", "/api/launch", {}),
    ("launch with a wrong token", "GET", "/api/launch", {"params": {"token": "w" * 43}}),
    ("launch by post with the token in the query", "POST", "/api/launch", {"params": {"token": SECRET}}),
    (
        "launch by post with the token in json",
        "POST",
        "/api/launch",
        {"json": {"token": SECRET}, "headers": {"origin": ORIGIN}},
    ),
    ("logout page before the launch", "GET", "/api/logout", {}),
    ("logout before the launch", "POST", "/api/logout", {"headers": {"origin": ORIGIN}}),
    ("launch", "GET", "/api/launch", {"params": {"token": SECRET}}),
    ("read with session", "GET", "/api/product/echo", {}),
    ("change with session, no origin", "POST", "/api/product/echo", {"json": {}}),
    ("change with session and origin", "POST", "/api/product/echo", {"json": {}, "headers": {"origin": ORIGIN}}),
    (
        "change as text/plain",
        "POST",
        "/api/product/echo",
        {"content": b"{}", "headers": {"origin": ORIGIN, "content-type": "text/plain"}},
    ),
    (
        "change as a urlencoded form",
        "POST",
        "/api/product/echo",
        {"content": b"a=b", "headers": {"origin": ORIGIN, "content-type": "application/x-www-form-urlencoded"}},
    ),
    ("change without a body", "POST", "/api/product/echo", {"headers": {"origin": ORIGIN}}),
    (
        "change with a wrong csrf header",
        "POST",
        "/api/product/echo",
        {"json": {}, "headers": {"origin": ORIGIN, "x-nodestro-csrf": "w" * 43}},
    ),
    (
        "change from a foreign origin",
        "POST",
        "/api/product/echo",
        {"json": {}, "headers": {"origin": "http://evil.example"}},
    ),
    (
        "change from another port",
        "POST",
        "/api/product/echo",
        {"json": {}, "headers": {"origin": "http://127.0.0.1:9"}},
    ),
    ("delete", "DELETE", "/api/product/echo", {"headers": {"origin": ORIGIN}}),
    ("rebinding host", "GET", "/api/product/echo", {"headers": {"host": "evil.example"}}),
    ("forwarded", "GET", "/api/product/echo", {"headers": {"x-forwarded-for": "10.0.0.1"}}),
    ("launch again", "GET", "/api/launch", {"params": {"token": SECRET}}),
    ("launch again by post", "POST", "/api/launch", {"json": {"token": SECRET}, "headers": {"origin": ORIGIN}}),
    ("logout page", "GET", "/api/logout", {}),
    ("logout without origin", "POST", "/api/logout", {}),
    ("logout", "POST", "/api/logout", {"headers": {"origin": ORIGIN}}),
    ("read after the logout", "GET", "/api/product/echo", {}),
    ("change after the logout", "POST", "/api/product/echo", {"json": {}, "headers": {"origin": ORIGIN}}),
]


def product_app(data: Path, groups: tuple[str, ...] = ("settings",), **config: Any) -> FastAPI:
    """A desktop product app with one fixed route to read and change (no random values in its answers)."""
    from nodestro_api.context import AppConfig
    from nodestro_api.host import Branding, HostSwitches, create_product_app
    from nodestro_api.permissions import EDIT, READ

    router = APIRouter(prefix="/api/product")

    @router.get("/echo", dependencies=READ)
    def read(request: Request) -> dict[str, str]:
        return {"read": request.app.title}

    @router.post("/echo", dependencies=EDIT)
    def change(request: Request) -> dict[str, str]:
        return {"changed": request.app.title}

    return create_product_app(
        "desktop",
        data,
        [*groups, router],
        Branding(title="Matrix"),
        HostSwitches(scheduler=False),
        config=AppConfig(data_dir=data, **config),
        integrations=[],
    )


def _answers(app: FastAPI) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    with TestClient(app, base_url=ORIGIN) as client:
        for label, method, path, options in REQUESTS:
            answer = client.request(method, path, follow_redirects=False, **options)
            headers = [[key, _COOKIE_VALUE.sub(r"\1<random>", value)] for key, value in answer.headers.multi_items()]
            found.append({"request": label, "status": answer.status_code, "headers": headers, "body": answer.text})
    return found


def record(root: Path, make: Callable[..., FastAPI] = product_app) -> dict[str, list[dict[str, Any]]]:
    """The answers of the matrix for the desktop with a launch secret and without one."""
    return {
        "desktop with a launch secret": _answers(make(root / "secret", launch_secret=SECRET)),
        "desktop without a launch secret": _answers(make(root / "open")),
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        json.dump(record(Path(folder)), sys.stdout, indent=1, ensure_ascii=False)
        sys.stdout.write("\n")
