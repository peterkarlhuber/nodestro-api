"""Web pages in the user's browser cannot use the key-less studio API (DNS rebinding, cross-site requests)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
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


def test_loopback_host_names_can_be_added(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NODESTRO_ALLOWED_HOSTS", "studio.localhost")
    config = AppConfig.from_env()
    assert config.allowed_hosts == ["studio.localhost"]
    with TestClient(create_app(config, integrations=[], scheduler=False), base_url="http://studio.localhost") as client:
        assert client.get("/api/workflows").status_code == 200
        body = {"name": "Alias", "nodes": [], "edges": []}
        headers = {"Origin": "http://studio.localhost"}
        assert client.post("/api/workflows", json=body, headers=headers).status_code == 201
        assert client.get("/api/workflows", headers={"Host": "other.example"}).status_code == 403


# ---- no login, so never on the network ------------------------------------------------------------

NETWORK_BINDINGS = ("0.0.0.0", "::", "", "192.168.1.20", "10.0.0.5", "fe80::1", "nodestro.example", "werkstatt")


@pytest.mark.parametrize("host", NETWORK_BINDINGS)
def test_the_studio_api_is_not_offered_to_the_network(tmp_path: Path, host: str) -> None:
    """Host and Origin checks are no login: listening anywhere but loopback is refused before anything opens."""
    data = tmp_path / "data"
    with pytest.raises(NodestroError) as error:
        create_app(AppConfig(data_dir=data, host=host), integrations=[], scheduler=False)
    assert error.value.code == "access.network_needs_login"
    assert error.value.params["host"] == host
    assert not data.exists()


@pytest.mark.parametrize("host", ("127.0.0.1", "127.0.0.2", "::1", "localhost", "LOCALHOST"))
def test_loopback_addresses_are_accepted(tmp_path: Path, host: str) -> None:
    create_app(AppConfig(data_dir=tmp_path, host=host), integrations=[], scheduler=False).state.nodestro.close()


@pytest.mark.parametrize("names", (["studio.example"], ["*"], ["studio.localhost", "nodestro.home.arpa"], ["10.0.0.5"]))
def test_reverse_proxy_host_names_are_refused(tmp_path: Path, names: list[str]) -> None:
    """A proxy would pass the network through to the login-less API, whatever address it forwards to."""
    data = tmp_path / "data"
    with pytest.raises(NodestroError) as error:
        create_app(AppConfig(data_dir=data, allowed_hosts=names), integrations=[], scheduler=False)
    assert error.value.code == "access.network_needs_login"
    assert error.value.params["hosts"] == [name for name in names if name != "studio.localhost"]
    assert not data.exists()


def test_the_environment_cannot_expose_the_studio_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NODESTRO_HOST", "0.0.0.0")
    with pytest.raises(NodestroError, match="no login"):
        create_app(integrations=[], scheduler=False)
    monkeypatch.setenv("NODESTRO_HOST", "127.0.0.1")
    monkeypatch.setenv("NODESTRO_ALLOWED_HOSTS", "*")
    with pytest.raises(NodestroError, match="NODESTRO_ALLOWED_HOSTS"):
        create_app(integrations=[], scheduler=False)
    assert not (tmp_path / "data").exists()


def test_the_command_line_refuses_network_bindings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from nodestro_api import cli

    started: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append("run"))
    monkeypatch.delenv("NODESTRO_ALLOWED_HOSTS", raising=False)
    # The command line writes these into the environment; monkeypatch restores them afterwards.
    monkeypatch.setenv("NODESTRO_HOST", "127.0.0.1")
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    for host in ("0.0.0.0", "::", "192.168.1.20"):
        with pytest.raises(SystemExit, match="no login"):
            cli.main(["--host", host, "--data-dir", str(tmp_path / "data")])
    assert started == []
    cli.main(["--host", "127.0.0.1", "--data-dir", str(tmp_path / "data")])
    assert started == ["run"]


FORWARDED = (
    {"X-Forwarded-For": "192.168.1.30"},
    {"Forwarded": "for=192.168.1.30"},
    {"X-Real-IP": "10.0.0.9"},
    {"X-Forwarded-Host": "nodestro.example"},
)


def test_requests_forwarded_by_a_proxy_are_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    for headers in FORWARDED:
        answer = client.get("/api/settings", headers=headers)
        assert answer.status_code == 403, headers
        assert answer.json()["code"] == "request.forwarded_not_allowed"
        answer = client.post("/api/workflows", json={"name": "Proxy", "nodes": [], "edges": []}, headers=headers)
        assert answer.status_code == 403, headers
    assert client.get("/api/workflows").json() == []


def test_published_workflow_apis_stay_reachable_with_their_keys(tmp_path: Path) -> None:
    """Published APIs authenticate with keys: also through a proxy on this computer (the network listener
    serves them to other devices, see test_published_api)."""
    scripted = ScriptedClient({"WRITER": [text("Angebot für ACME")] * 3})
    app = create_app(
        AppConfig(data_dir=tmp_path), client_factory=lambda _provider: scripted, integrations=[], scheduler=False
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        publication = publish(client, form_workflow(client))["publication"]
        token = new_key(client, publication["id"])
        slug = publication["slug"]
        proxied = {**auth(token), "X-Forwarded-For": "192.168.1.30"}
        run = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "ACME"}}, headers=proxied)
        assert run.status_code == 202, run.text
        assert finish(client, slug, token, run.json()["run_id"])["status"] == "succeeded"
        assert client.get(f"{PUBLISHED}/{slug}", headers=proxied).status_code == 200
        assert client.get(f"{PUBLISHED}/{slug}", headers={"X-Forwarded-For": "192.168.1.30"}).status_code == 401
        rebound = client.get(f"{PUBLISHED}/{slug}", headers={**auth(token), "Host": "evil.example:8000"})
        assert rebound.status_code == 403
