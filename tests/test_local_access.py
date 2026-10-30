"""Web pages in the user's browser cannot use the key-less studio API (DNS rebinding, cross-site requests)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from test_published_api import PUBLISHED, auth, finish, form_workflow, new_key, publish

from nodestro_api import local_access
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig
from nodestro_api.local_access import allowed_hosts, is_ip_address, is_loopback, listens_everywhere, split_host

from conftest import ScriptedClient, text


def test_loopback_names_and_host_headers() -> None:
    assert all(is_loopback(name) for name in ("localhost", "127.0.0.1", "127.4.5.6", "::1", "[::1]", "app.localhost"))
    assert not any(is_loopback(name) for name in ("evil.example", "192.168.1.20", "localhost.evil.example"))
    assert split_host("127.0.0.1:8000") == "127.0.0.1"
    assert split_host("[::1]:8000") == "::1"
    assert split_host("Studio.Example:443") == "studio.example"


@pytest.fixture
def computer_name(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(local_access.socket, "gethostname", lambda: "Werkstatt.fritz.box")
    return "werkstatt"


def test_allowed_hosts_follow_the_listening_address(computer_name: str) -> None:
    assert allowed_hosts("127.0.0.1") == frozenset()
    assert allowed_hosts("192.168.1.20") == {"192.168.1.20"}
    # Every interface: this computer's names, never "every host name" (DNS rebinding).
    for bind in ("0.0.0.0", "::", ""):
        assert listens_everywhere(bind)
        assert allowed_hosts(bind) == {"werkstatt", "werkstatt.fritz.box", "werkstatt.local"}
    assert not any(listens_everywhere(bind) for bind in (None, "127.0.0.1", "192.168.1.20"))
    assert allowed_hosts("0.0.0.0", ["studio.example"]) >= {"studio.example", "werkstatt"}  # type: ignore[operator]
    assert allowed_hosts("127.0.0.1", ["studio.example", " "]) == {"studio.example"}
    assert allowed_hosts("127.0.0.1", ["*"]) is None
    assert allowed_hosts("0.0.0.0", ["*"]) is None
    assert all(is_ip_address(host) for host in ("192.168.1.20", "10.0.0.1", "::1", "[fe80::1]", "0.0.0.0"))
    assert not any(is_ip_address(host) for host in ("evil.example", "192.168.1.20.evil.example", "", "1.2.3"))


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


def lan_client(tmp_path: Path, host: str = "0.0.0.0", base_url: str = "http://192.168.1.20:8000") -> TestClient:
    config = AppConfig(data_dir=tmp_path, host=host)
    return TestClient(create_app(config, integrations=[], scheduler=False), base_url=base_url)


def test_listening_on_every_interface_refuses_foreign_host_names(tmp_path: Path, computer_name: str) -> None:
    body = {"name": "Rebinding", "nodes": [], "edges": []}
    with lan_client(tmp_path) as client:
        # DNS rebinding against "nodestro --host 0.0.0.0": the page's name now points to this computer,
        # and the page is same-origin with itself, so the origin check alone cannot help.
        for host in ("evil.example:8000", "evil.example", "192.168.1.20.evil.example:8000", "localhost.evil.example"):
            answer = client.get("/api/settings", headers={"Host": host})
            assert answer.status_code == 403, host
            assert answer.json()["code"] == "request.host_not_allowed"
            origin = f"http://{host}"
            answer = client.post("/api/workflows", json=body, headers={"Host": host, "Origin": origin})
            assert answer.status_code == 403, host
        # A request without a Host header is no browser request; it is not answered either.
        assert client.get("/api/settings", headers={"Host": ""}).status_code == 403
        assert client.get("/api/workflows").json() == []


def test_listening_on_every_interface_answers_the_network(tmp_path: Path, computer_name: str) -> None:
    body = {"name": "LAN", "nodes": [], "edges": []}
    with lan_client(tmp_path) as client:
        # Other devices reach the studio under an address or this computer's name.
        hosts = ("192.168.1.20:8000", "10.1.2.3", "[fe80::1]:8000", "werkstatt:8000", "Werkstatt.local:8000")
        for host in (*hosts, "werkstatt.fritz.box", "127.0.0.1:8000", "localhost:8000", "[::1]:8000"):
            assert client.get("/api/settings", headers={"Host": host}).status_code == 200, host
        # The studio's own pages may change things, under whichever name or address it was opened.
        for host in hosts:
            answer = client.post("/api/workflows", json=body, headers={"Host": host, "Origin": f"http://{host}"})
            assert answer.status_code == 201, host
        # Clients without a browser send no Origin.
        assert client.post("/api/workflows", json=body).status_code == 201
        # A foreign page reaching the address directly is a cross-site request.
        for origin in ("https://evil.example", "http://10.9.9.9:8000", "null", "http://192.168.1.20.evil.example:8000"):
            answer = client.post("/api/workflows", json=body, headers={"Origin": origin})
            assert answer.status_code == 403, origin
            assert answer.json()["code"] == "request.origin_not_allowed"
        assert len(client.get("/api/workflows").json()) == len(hosts) + 1


def test_listening_on_every_interface_with_configured_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, computer_name: str
) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NODESTRO_HOST", "0.0.0.0")
    monkeypatch.setenv("NODESTRO_ALLOWED_HOSTS", "nodestro.home.arpa")
    with TestClient(
        create_app(AppConfig.from_env(), integrations=[], scheduler=False), base_url="http://nodestro.home.arpa"
    ) as client:
        assert client.get("/api/settings").status_code == 200
        body = {"name": "Heimnetz", "nodes": [], "edges": []}
        assert (
            client.post("/api/workflows", json=body, headers={"Origin": "http://nodestro.home.arpa"}).status_code == 201
        )
        assert client.get("/api/settings", headers={"Host": "evil.example"}).status_code == 403
    monkeypatch.setenv("NODESTRO_ALLOWED_HOSTS", "*")
    with TestClient(
        create_app(AppConfig.from_env(), integrations=[], scheduler=False), base_url="http://any.example"
    ) as client:
        assert client.get("/api/settings").status_code == 200  # explicitly every host name


def test_a_single_address_answers_no_other_address(tmp_path: Path) -> None:
    with lan_client(tmp_path, host="192.168.1.20") as client:
        assert client.get("/api/settings").status_code == 200
        assert client.get("/api/settings", headers={"Host": "10.1.2.3:8000"}).status_code == 403
        assert client.get("/api/settings", headers={"Host": "evil.example:8000"}).status_code == 403


def test_published_workflow_apis_keep_working_on_every_interface(tmp_path: Path, computer_name: str) -> None:
    scripted = ScriptedClient({"WRITER": [text("Angebot für ACME")] * 3})
    config = AppConfig(data_dir=tmp_path, host="0.0.0.0")
    app = create_app(config, client_factory=lambda _provider: scripted, integrations=[], scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        publication = publish(client, form_workflow(client))["publication"]
        token = new_key(client, publication["id"])
        slug = publication["slug"]
        run = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "ACME"}}, headers=auth(token))
        assert run.status_code == 202, run.text
        assert finish(client, slug, token, run.json()["run_id"])["status"] == "succeeded"
        assert client.get(f"{PUBLISHED}/{slug}", headers=auth(token)).status_code == 200
        assert client.get(f"{PUBLISHED}/{slug}").status_code == 401
        rebound = client.get(f"{PUBLISHED}/{slug}", headers={**auth(token), "Host": "evil.example:8000"})
        assert rebound.status_code == 403
