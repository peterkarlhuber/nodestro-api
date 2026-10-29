"""Web pages in the user's browser cannot use the key-less studio API (DNS rebinding, cross-site requests)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig
from nodestro_api.local_access import allowed_hosts, is_loopback, split_host


def test_loopback_names_and_host_headers() -> None:
    assert all(is_loopback(name) for name in ("localhost", "127.0.0.1", "127.4.5.6", "::1", "[::1]", "app.localhost"))
    assert not any(is_loopback(name) for name in ("evil.example", "192.168.1.20", "localhost.evil.example"))
    assert split_host("127.0.0.1:8000") == "127.0.0.1"
    assert split_host("[::1]:8000") == "::1"
    assert split_host("Studio.Example:443") == "studio.example"


def test_allowed_hosts_follow_the_listening_address() -> None:
    assert allowed_hosts("127.0.0.1") == frozenset()
    assert allowed_hosts("192.168.1.20") == {"192.168.1.20"}
    assert allowed_hosts("0.0.0.0") is None
    assert allowed_hosts("127.0.0.1", ["studio.example", " "]) == {"studio.example"}
    assert allowed_hosts("127.0.0.1", ["*"]) is None


def test_a_foreign_host_name_is_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    assert client.get("/api/workflows").status_code == 200
    # DNS rebinding: the page's own name now points to 127.0.0.1.
    answer = client.get("/api/settings", headers={"Host": "evil.example:8000"})
    assert answer.status_code == 403
    assert answer.json()["code"] == "request.host_not_allowed"
    for host in ("localhost:8000", "[::1]:8000"):
        assert client.get("/api/workflows", headers={"Host": host}).status_code == 200


def test_changes_from_a_foreign_web_page_are_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    body = {"name": "Von außen", "nodes": [], "edges": []}
    for origin in ("https://evil.example", "null"):
        answer = client.post("/api/workflows", json=body, headers={"Origin": origin})
        assert answer.status_code == 403, origin
        assert answer.json()["code"] == "request.origin_not_allowed"
    assert client.get("/api/workflows").json() == []
    # The studio's own pages, the development server and clients without a browser are accepted.
    for headers in ({"Origin": "http://127.0.0.1"}, {"Origin": "http://localhost:5173"}, {}):
        assert client.post("/api/workflows", json=body, headers=headers).status_code == 201
    # Reading needs no origin check: the browser does not let a foreign page read the answer.
    assert client.get("/api/workflows", headers={"Origin": "https://evil.example"}).status_code == 200


def test_configured_host_names_are_answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NODESTRO_ALLOWED_HOSTS", "studio.example")
    config = AppConfig.from_env()
    assert config.allowed_hosts == ["studio.example"]
    with TestClient(create_app(config, integrations=[], scheduler=False), base_url="https://studio.example") as client:
        assert client.get("/api/workflows").status_code == 200
        body = {"name": "Proxy", "nodes": [], "edges": []}
        assert client.post("/api/workflows", json=body, headers={"Origin": "https://studio.example"}).status_code == 201
        assert client.get("/api/workflows", headers={"Host": "other.example"}).status_code == 403


def test_listening_on_every_interface_answers_every_host_name(tmp_path: Path) -> None:
    config = AppConfig(data_dir=tmp_path, host="0.0.0.0")
    with TestClient(
        create_app(config, integrations=[], scheduler=False), base_url="http://192.168.1.20:8000"
    ) as client:
        assert client.get("/api/workflows").status_code == 200
        body = {"name": "LAN", "nodes": [], "edges": []}
        assert (
            client.post("/api/workflows", json=body, headers={"Origin": "http://192.168.1.20:8000"}).status_code == 201
        )
        assert client.post("/api/workflows", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
