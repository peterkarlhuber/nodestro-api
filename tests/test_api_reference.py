"""The API reference page loads a vendored, exactly pinned Swagger UI and nothing from other sites (API2-01)."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import re

from fastapi.testclient import TestClient

from nodestro_api.api_reference import ASSETS, ASSETS_DIR, ASSETS_PATH, SWAGGER_UI_VERSION, VENDORED_SHA256


def test_the_reference_page_loads_only_its_own_files(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    page = client.get("/api/docs")
    assert page.status_code == 200
    assert "cdn.jsdelivr.net" not in page.text and "fastapi.tiangolo.com" not in page.text
    # Every script and style is a file of this origin; no inline script (the page works under script-src 'self').
    sources = re.findall(r'(?:src|href)="([^"]+)"', page.text)
    assert sources and all(source.startswith(ASSETS_PATH + "/") for source in sources)
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page.text) is None
    assert 'data-openapi-url="/api/openapi.json"' in page.text
    for source in sources:
        answer = client.get(source)
        assert answer.status_code == 200, source
        assert answer.headers["content-type"].split(";")[0] in ("text/javascript", "text/css", "image/png")
    # The init script switches the public validator off: no request to validator.swagger.io.
    assert "validatorUrl: null" in client.get(f"{ASSETS_PATH}/nodestro-init.js").text
    # Only the listed files are served.
    for name in ("LICENSE", "VENDORED.md", "../api.py", "swagger-ui-bundle.js.map"):
        assert client.get(f"{ASSETS_PATH}/{name}").status_code == 404, name
    assert client.get("/api/openapi.json").json()["info"]["title"] == "Nodestro"


def test_the_vendored_files_are_the_recorded_release() -> None:
    for name, digest in VENDORED_SHA256.items():
        assert hashlib.sha256((ASSETS_DIR / name).read_bytes()).hexdigest() == digest, name
    assert set(ASSETS) - set(VENDORED_SHA256) == {"nodestro-init.js"}
    record = (ASSETS_DIR / "VENDORED.md").read_text(encoding="utf-8")
    assert f"`swagger-ui-dist` {SWAGGER_UI_VERSION}" in record
    assert all(digest in record for digest in VENDORED_SHA256.values())
    assert "Apache License" in (ASSETS_DIR / "LICENSE").read_text(encoding="utf-8")
