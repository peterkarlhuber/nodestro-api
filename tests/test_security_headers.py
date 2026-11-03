"""Security headers on every answer of the studio (API2-02)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from fastapi.testclient import TestClient

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig
from nodestro_api.frontend import mount_frontend
from nodestro_api.security_headers import CONTENT_SECURITY_POLICY, HSTS

EXPECTED = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


def _has_headers(headers: Mapping[str, str]) -> bool:
    return all(headers[name] == value for name, value in EXPECTED.items())


def test_the_policy_is_the_announced_one() -> None:
    for directive in (
        "default-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "connect-src 'self'",
    ):
        assert directive in CONTENT_SECURITY_POLICY.split("; "), directive
    assert "'unsafe-eval'" not in CONTENT_SECURITY_POLICY and "*" not in CONTENT_SECURITY_POLICY


def test_pages_api_answers_and_refusals_get_the_headers(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>studio</title>")
    app = create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)
    mount_frontend(app, static)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for path in ("/", "/workflows/abc", "/api/workflows", "/api/docs", "/api/nothing-here"):
            answer = client.get(path)
            assert _has_headers(answer.headers), path
            # The desktop on http://127.0.0.1: no HSTS.
            assert "strict-transport-security" not in answer.headers, path
        refused = client.get("/api/workflows", headers={"Host": "evil.example"})
        assert refused.status_code == 403 and _has_headers(refused.headers)
    # Served over HTTPS (a request that arrived over TLS): HSTS too.
    app = create_app(AppConfig(data_dir=tmp_path / "data2"), integrations=[], scheduler=False)
    with TestClient(app, base_url="https://127.0.0.1") as client:
        assert client.get("/api/workflows").headers["strict-transport-security"] == HSTS


def test_a_route_keeps_its_own_stricter_policy(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    uploaded = client.post("/api/inputs", files={"files": ("a.html", b"<script>alert(1)</script>", "text/html")})
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()[0]["file"]["id"]
    content = client.get(f"/api/inputs/{file_id}/content")
    assert content.status_code == 200
    assert content.headers["content-security-policy"] != CONTENT_SECURITY_POLICY
    assert "sandbox" in content.headers["content-security-policy"]
    # Files are previewed in a frame of the studio's own pages.
    assert content.headers["x-frame-options"] == "SAMEORIGIN"
