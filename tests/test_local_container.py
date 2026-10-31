"""Nodestro Local Docker (the ``local-container`` profile): access key, strict host and origin checks, the command.

The studio listens on the container's interfaces there, so these tests send requests as the host's browser does
(through a published loopback port), as other containers and other programs on the host do, and as web pages in
the person's browser do.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat

from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
import pytest

from nodestro_api import cli, local_container
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig
from nodestro_api.launch import LOCAL_COOKIE
from nodestro_api.permissions import LOCAL_OWNER, resolver_for

KEY = "k" * 43
ORIGIN = "http://localhost:8765"


def config_for(tmp_path: Path, **changes: object) -> AppConfig:
    values: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "host": "0.0.0.0",
        "profile_kind": "local-container",
        "access_key": KEY,
    }
    values.update(changes)
    return AppConfig(**values)  # type: ignore[arg-type]


def client_for(tmp_path: Path, base_url: str = ORIGIN, **changes: object) -> TestClient:
    app = create_app(config_for(tmp_path, **changes), integrations=[], scheduler=False)
    return TestClient(app, base_url=base_url)


def signed_in(client: TestClient) -> TestClient:
    answer = client.get("/api/launch", params={"token": KEY}, follow_redirects=False)
    assert answer.status_code == 200, answer.text
    return client


# ---- configuration ------------------------------------------------------------------------------------------------


def test_a_local_container_without_access_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NodestroError) as error:
        create_app(config_for(tmp_path, access_key=None), integrations=[], scheduler=False)
    assert error.value.code == "access.key_required"
    assert not (tmp_path / "data").exists()  # refused before anything is created


@pytest.mark.parametrize("hosts", [["nodestro.example.com"], ["*"], ["192.168.1.10"]])
def test_only_loopback_names_can_be_added(tmp_path: Path, hosts: list[str]) -> None:
    with pytest.raises(NodestroError) as error:
        create_app(config_for(tmp_path, allowed_hosts=hosts), integrations=[], scheduler=False)
    assert error.value.code == "access.network_needs_login"


def test_the_desktop_profile_still_refuses_every_interface(tmp_path: Path) -> None:
    with pytest.raises(NodestroError) as error:
        create_app(config_for(tmp_path, profile_kind="desktop"), integrations=[], scheduler=False)
    assert error.value.code == "access.network_needs_login"


def test_the_command_line_checks_the_same(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_PROFILE", "local-container")
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(local_container.ENV_KEY_FILE, raising=False)
    with pytest.raises(SystemExit, match="access key"):
        cli.serve("nodestro_api.app:create_app", argparse.Namespace(host="0.0.0.0", port=1, reload=False))


def test_one_person_holds_every_permission(tmp_path: Path) -> None:
    assert resolver_for(config_for(tmp_path).profile)(None) is LOCAL_OWNER  # type: ignore[arg-type]


# ---- the access key -----------------------------------------------------------------------------------------------


def test_without_the_key_only_health_and_published_apis_answer(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        for path in ("/api/workflows", "/api/settings", "/api/backup", "/api/openapi.json"):
            answer = client.get(path)
            assert answer.status_code == 401, path
            assert answer.json()["code"] == "access.launch_required"
        assert client.post("/api/workflows", json={"name": "x"}, headers={"Origin": ORIGIN}).status_code == 401
        assert client.get("/api/published/unknown").json()["error"]["code"] == "auth.missing"
        # A browser gets a page that says how to open Nodestro (no script, nothing else).
        page = client.get("/", headers={"Accept": "text/html"})
        assert page.status_code == 401 and "nodestro-local" in page.text
        assert page.headers["content-security-policy"].startswith("default-src 'none'")
        assert KEY not in page.text


def test_the_link_opens_any_number_of_browser_sessions(tmp_path: Path) -> None:
    with client_for(tmp_path) as first, client_for(tmp_path / "unused") as _:
        assert first.get("/api/launch", params={"token": "x" * 43}).json()["code"] == "access.launch_invalid"
        answer = first.get("/api/launch", params={"token": KEY}, follow_redirects=False)
        cookie = answer.headers["set-cookie"]
        assert cookie.startswith(f"{LOCAL_COOKIE}=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert KEY not in cookie
        assert first.get("/api/workflows").status_code == 200
        session = first.cookies[LOCAL_COOKIE]
        # A second browser (another cookie jar) opens its own session with the same link; both keep working.
        second = TestClient(first.app, base_url=ORIGIN)
        signed_in(second)
        assert second.cookies[LOCAL_COOKIE] != session
        assert first.get("/api/workflows").status_code == 200 and second.get("/api/workflows").status_code == 200
        # A guessed or old session cookie does not work.
        third = TestClient(first.app, base_url=ORIGIN, cookies={LOCAL_COOKIE: "guess"})
        assert third.get("/api/workflows").status_code == 401
        # The desktop's cookie name means nothing here (cookies are shared across localhost's ports).
        fourth = TestClient(first.app, base_url=ORIGIN, cookies={"nodestro_session": session})
        assert fourth.get("/api/workflows").status_code == 401


def test_scripts_use_the_key_as_bearer_token(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.get("/api/workflows", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
        assert client.get("/api/workflows", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/workflows", headers={"Authorization": f"Basic {KEY}"}).status_code == 401


# ---- host and origin ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://172.18.0.2:8080",  # another container, through the container's address
        "http://nodestro:8080",  # the container's name on its network
        "http://evil.example:8765",  # DNS rebinding
        "http://192.168.1.10:8765",  # the host's LAN address
    ],
)
def test_only_loopback_host_names_are_answered(tmp_path: Path, base_url: str) -> None:
    with client_for(tmp_path, base_url=base_url) as client:
        answer = client.get("/api/workflows", headers={"Authorization": f"Bearer {KEY}"})
        assert answer.status_code == 403 and answer.json()["code"] == "request.host_not_allowed"


@pytest.mark.parametrize("base_url", ["http://localhost:8765", "http://127.0.0.1:8765", "http://[::1]:8765"])
def test_the_host_s_loopback_is_answered(tmp_path: Path, base_url: str) -> None:
    with client_for(tmp_path, base_url=base_url) as client:
        assert client.get("/api/workflows", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3000",  # another program on localhost: same site, other origin
        "http://127.0.0.1:8765",  # another origin of the same port
        "http://evil.example",
        "null",
    ],
)
def test_changes_from_any_other_origin_are_refused_even_with_a_session(tmp_path: Path, origin: str) -> None:
    with client_for(tmp_path) as client:
        signed_in(client)
        answer = client.post("/api/workflows", json={"name": "x"}, headers={"Origin": origin})
        assert answer.status_code == 403 and answer.json()["code"] == "request.origin_not_allowed"
        assert client.post("/api/workflows", json={"name": "x"}, headers={"Origin": ORIGIN}).status_code in (200, 201)


def test_forwarded_requests_are_refused(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        signed_in(client)
        answer = client.get("/api/workflows", headers={"X-Forwarded-For": "203.0.113.9"})
        assert answer.json()["code"] == "request.forwarded_not_allowed"


def test_no_cors_for_development_servers(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        answer = client.options(
            "/api/workflows",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in answer.headers


def test_network_access_for_published_apis_cannot_be_switched_on(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        signed_in(client)
        answer = client.put("/api/api-network", json={"enabled": True, "port": 8800}, headers={"Origin": ORIGIN})
        assert answer.status_code >= 400 and answer.json()["code"] == "lan.unavailable"


def test_tool_code_runs_only_in_containers(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        signed_in(client)
        settings = client.get("/api/settings").json()
        assert settings["sandbox"] == "docker"
        assert settings["builtin_sandbox"]["available"] is False
        answer = client.put("/api/settings", json={"sandbox": "subprocess"}, headers={"Origin": ORIGIN})
        assert answer.json()["code"] == "sandbox.unavailable"


# ---- the key file and the command ---------------------------------------------------------------------------------


def test_the_key_file_is_created_once_with_owner_only_access(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "access-key"
    key = local_container.create_access_key(path)
    assert len(key) >= 43 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert local_container.create_access_key(path) == key  # kept
    assert local_container.read_access_key(path) == key
    assert local_container.create_access_key(path, replace=True) != key
    os.chmod(path, 0o644)
    with pytest.raises(NodestroError, match="0600"):
        local_container.read_access_key(path)
    path.write_text("short")
    os.chmod(path, 0o600)
    with pytest.raises(NodestroError, match="shorter"):
        local_container.read_access_key(path)


def test_the_link_names_the_host_loopback_and_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "access-key"
    monkeypatch.setenv(local_container.ENV_KEY_FILE, str(path))
    key = local_container.create_access_key(path)
    assert local_container.main(["link", "--port", "9123"]) == 0
    assert capsys.readouterr().out.strip() == f"http://127.0.0.1:9123/api/launch?token={key}"
    with pytest.raises(SystemExit, match="loopback"):
        local_container.main(["link", "--host", "192.168.1.10"])
    assert local_container.main(["rotate-key"]) == 0
    assert local_container.read_access_key(path) != key


def test_the_app_reads_the_key_file_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "access-key"
    key = local_container.create_access_key(path)
    monkeypatch.setenv(local_container.ENV_KEY_FILE, str(path))
    monkeypatch.setenv("NODESTRO_PROFILE", "local-container")
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "data"))
    config = AppConfig.from_env()
    assert config.access_key == key and config.profile_kind == "local-container"
