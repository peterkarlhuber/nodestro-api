"""NS-002: the hardening steps of the desktop launch (nodestro-session ``docs/design.md`` 5.2), each behind its own
switch that is off by default.

- With every switch off the answers are byte for byte the ones before NS-002
  (``tests/vectors/launch-switches-off.json``, recorded from master 6783a52 by ``tests/launch_matrix.py``), and the
  existing tests run unchanged.
- With each switch on, its attack cases: an expired launch secret, changes without ``Origin``, with a form or
  ``text/plain`` body or without the CSRF value, a cookie without the CSRF value (another local program on another
  port gets the cookie, never the value), the sign-out, a terminal start without a secret, the secret in the query
  when it belongs in the fragment, and the loopback binding.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from launch_matrix import ORIGIN, SECRET, product_app, record
from nodestro_runtime.problems import NodestroError
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

from nodestro_api import cli, launch
from nodestro_api.context import AppConfig
from nodestro_api.launch import LaunchHardening, LaunchSecretMiddleware
from nodestro_api.local_access import LocalAccessMiddleware

VECTORS = Path(__file__).parent / "vectors" / "launch-switches-off.json"
SWITCHES = (
    launch.TTL_ENV,
    launch.CSRF_ENV,
    launch.LOGOUT_ENV,
    launch.TERMINAL_ENV,
    launch.FRAGMENT_ENV,
    launch.BIND_ENV,
)
OWN = {"origin": ORIGIN}


@pytest.fixture(autouse=True)
def _no_switches(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)
    yield
    launch.reset_terminal_secret()
    bound = cli._bound
    if bound is not None:
        bound.close()
        cli._bound = None


def app_with(tmp_path: Path, **switches: Any) -> FastAPI:
    return product_app(tmp_path / "data", launch_secret=SECRET, hardening=LaunchHardening(**switches))


def launched(client: TestClient) -> dict[str, Any]:
    """Launch with the query link and return what the launch page leaves for the studio."""
    answer = client.get("/api/launch", params={"token": SECRET})
    assert answer.status_code == 200, answer.text
    found = re.search(r'<script type="application/json" id="launch-session">(.*?)</script>', answer.text)
    assert found is not None
    handed: dict[str, Any] = json.loads(found.group(1))
    return handed


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _ok(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"ok": True})(scope, receive, send)


def bare(clock: Clock, **switches: Any) -> TestClient:
    """The launch middleware alone, in front of an app that answers ``{"ok": true}`` (for the clock)."""
    app = Starlette(
        routes=[Route("/{path:path}", _endpoint, methods=["GET", "POST"])],
        middleware=[
            Middleware(LaunchSecretMiddleware, secret=SECRET, clock=clock, hardening=LaunchHardening(**switches))
        ],
    )
    return TestClient(app, base_url=ORIGIN)


async def _endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


# --- every switch off ------------------------------------------------------------------------------------------------


def test_with_every_switch_off_the_answers_are_the_ones_before_ns002(tmp_path: Path) -> None:
    expected = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert record(tmp_path) == expected


def test_switches_set_to_zero_or_empty_are_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("0", "", " ", "no", "true", "on"):
        environment = {name: value for name in SWITCHES if name != launch.TTL_ENV}
        environment[launch.TTL_ENV] = "0" if value.strip() not in ("",) else value
        assert LaunchHardening.from_env(environment) == LaunchHardening(), value
    for name in SWITCHES:
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path / "env"))
    assert AppConfig.from_env().hardening == LaunchHardening()
    expected = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert (
        record(tmp_path, lambda data, **config: product_app(data, hardening=LaunchHardening.from_env(), **config))
        == expected
    )


def test_each_switch_is_read_from_its_variable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    for name, field, value in (
        (launch.CSRF_ENV, "csrf", True),
        (launch.LOGOUT_ENV, "logout", True),
        (launch.TERMINAL_ENV, "terminal", True),
        (launch.FRAGMENT_ENV, "fragment", True),
        (launch.BIND_ENV, "bind_loopback", True),
        (launch.TTL_ENV, "ttl", 30.0),
    ):
        monkeypatch.setenv(name, "30" if name == launch.TTL_ENV else "1")
        assert getattr(AppConfig.from_env().hardening, field) == value, name
        monkeypatch.delenv(name)
        assert AppConfig.from_env().hardening == LaunchHardening()


@pytest.mark.parametrize("value", ["-1", "abc", "nan", "inf", "3601", "1e9", "true", "on", "yes", "5m"])
def test_an_invalid_ttl_is_refused(value: str) -> None:
    with pytest.raises(NodestroError) as refused:
        LaunchHardening.from_env({launch.TTL_ENV: value})
    assert refused.value.code == "access.launch_ttl_invalid"
    assert "0, false, off or no" in str(refused.value)  # the message names the ways to turn the limit off


@pytest.mark.parametrize("value", ["", " ", "0", "0.0", "00", "-0", "0e0", "false", "off", "no", "OFF", " No "])
def test_every_off_spelling_of_the_ttl_is_off(value: str) -> None:
    """Review L-2: a spelling meant to turn the limit off never stops the start (garbage still does, above)."""
    assert LaunchHardening.from_env({launch.TTL_ENV: value}) == LaunchHardening()


@pytest.mark.parametrize(("value", "seconds"), [("30", 30.0), (" 0.5 ", 0.5), ("3600", 3600.0), ("1e2", 100.0)])
def test_a_ttl_in_range_is_taken(value: str, seconds: float) -> None:
    assert LaunchHardening.from_env({launch.TTL_ENV: value}).ttl == seconds


def test_nodestro_local_docker_never_takes_the_switches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in SWITCHES:
        monkeypatch.setenv(name, "30" if name == launch.TTL_ENV else "1")
    monkeypatch.setenv("NODESTRO_PROFILE", "local-container")
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    assert AppConfig.from_env().hardening == LaunchHardening()
    middleware = LaunchSecretMiddleware(_ok, secret=SECRET, reusable=True, hardening=LaunchHardening(csrf=True, ttl=1))
    assert middleware.hardening == LaunchHardening()


@pytest.mark.parametrize("kind", ["server", "local-container"])
def test_only_the_desktop_profile_reads_the_switches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    """Review I-1: another profile neither takes the switches nor stops on an invalid value of one."""
    for name in SWITCHES:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv(launch.TTL_ENV, "abc")
    monkeypatch.setenv("NODESTRO_PROFILE", kind)
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    if kind == "local-container":
        monkeypatch.setenv("NODESTRO_BROWSER_HOST", "localhost")
    assert AppConfig.from_env().hardening == LaunchHardening()
    monkeypatch.setenv("NODESTRO_PROFILE", "desktop")
    with pytest.raises(NodestroError):
        AppConfig.from_env()


# --- launch-secret TTL -----------------------------------------------------------------------------------------------


def test_the_launch_secret_expires_after_its_ttl() -> None:
    clock = Clock()
    with bare(clock, ttl=30) as client:
        clock.now += 30
        answer = client.get("/api/launch", params={"token": SECRET})
        assert (answer.status_code, answer.json()["code"]) == (403, "access.launch_expired")
        assert "set-cookie" not in answer.headers
        assert client.get("/api/anything").status_code == 401
        # It stays dead; a wrong token is still only invalid.
        clock.now += 1000
        assert client.get("/api/launch", params={"token": SECRET}).json()["code"] == "access.launch_expired"
        assert client.get("/api/launch", params={"token": "w" * 43}).json()["code"] == "access.launch_invalid"


def test_a_launch_within_the_ttl_works_and_its_session_outlives_the_ttl() -> None:
    clock = Clock()
    with bare(clock, ttl=30) as client:
        clock.now += 29
        assert client.get("/api/launch", params={"token": SECRET}).status_code == 200
        clock.now += 10_000
        assert client.get("/api/anything").json() == {"ok": True}
        assert client.get("/api/launch", params={"token": SECRET}).json()["code"] == "access.launch_used"


def test_the_ttl_applies_to_the_fragment_exchange_too() -> None:
    clock = Clock()
    with bare(clock, ttl=5, fragment=True) as client:
        clock.now += 5
        answer = client.post("/api/launch", json={"token": SECRET}, headers=OWN)
        assert (answer.status_code, answer.json()["code"]) == (403, "access.launch_expired")


def test_the_ttl_counts_from_the_first_answer_of_the_real_app(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, ttl=0.2), base_url=ORIGIN) as client:
        assert client.get("/api/health").status_code == 200
        time.sleep(0.3)
        assert client.get("/api/launch", params={"token": SECRET}).json()["code"] == "access.launch_expired"


# --- CSRF header + JSON only + required Origin -----------------------------------------------------------------------


def test_the_launch_page_hands_the_csrf_value_to_the_studio_and_allows_only_its_script(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, csrf=True), base_url=ORIGIN) as client:
        answer = client.get("/api/launch", params={"token": SECRET})
        handed = launched(TestClient(app_with(tmp_path / "other", csrf=True), base_url=ORIGIN))
        assert set(handed) == {"csrf", "logout"} and handed["logout"] is False
        assert isinstance(handed["csrf"], str) and len(handed["csrf"]) >= 43
        assert SECRET not in answer.text
        policy = answer.headers["content-security-policy"]
        assert policy.startswith("default-src 'none'; script-src 'sha256-")
        scripts = re.findall(r"<script>(.*?)</script>", answer.text, flags=re.S)
        assert len(scripts) == 1
        digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
        assert f"'sha256-{digest}'" in policy
        assert launch.SESSION_STORAGE_KEY in scripts[0]
        assert answer.headers["cache-control"] == "no-store"
        assert answer.headers["referrer-policy"] == "no-referrer"
        cookie = answer.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_changes_need_origin_a_json_body_and_the_csrf_value(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, csrf=True), base_url=ORIGIN) as client:
        csrf = launched(client)["csrf"]
        good = {**OWN, launch.CSRF_HEADER: csrf}
        # Reading needs no CSRF value.
        assert client.get("/api/product/echo").status_code == 200
        cases: list[tuple[str, dict[str, Any], int, str]] = [
            ("no origin", {"json": {}, "headers": {launch.CSRF_HEADER: csrf}}, 403, "request.origin_required"),
            ("no csrf", {"json": {}, "headers": OWN}, 403, "access.csrf_invalid"),
            ("wrong csrf", {"json": {}, "headers": {**OWN, launch.CSRF_HEADER: "w" * 43}}, 403, "access.csrf_invalid"),
            ("empty csrf", {"json": {}, "headers": {**OWN, launch.CSRF_HEADER: ""}}, 403, "access.csrf_invalid"),
            ("long csrf", {"json": {}, "headers": {**OWN, launch.CSRF_HEADER: "x" * 5000}}, 403, "access.csrf_invalid"),
            (
                "text/plain",
                {"content": b"{}", "headers": {**good, "content-type": "text/plain"}},
                415,
                "request.json_only",
            ),
            (
                "form",
                {"content": b"a=b", "headers": {**good, "content-type": "application/x-www-form-urlencoded"}},
                415,
                "request.json_only",
            ),
            ("body without a type", {"content": b"{}", "headers": good}, 415, "request.json_only"),
            (
                "two types",
                {
                    "content": b"{}",
                    "headers": [*good.items(), ("content-type", "application/json"), ("content-type", "text/plain")],
                },
                415,
                "request.json_only",
            ),
        ]
        for label, options, status, code in cases:
            answer = client.post("/api/product/echo", **options)
            assert (answer.status_code, answer.json()["code"]) == (status, code), label
        two = [("origin", ORIGIN), (launch.CSRF_HEADER, csrf), (launch.CSRF_HEADER, csrf)]
        assert client.post("/api/product/echo", json={}, headers=two).json()["code"] == "access.csrf_invalid"
        # A foreign origin is refused by the outer check before.
        foreign = {**good, "origin": "http://127.0.0.1:9"}
        assert client.post("/api/product/echo", json={}, headers=foreign).json()["code"] == "request.origin_not_allowed"
        # What the studio sends passes: JSON, a form with files, and no body.
        assert client.post("/api/product/echo", json={}, headers=good).json() == {"changed": "Matrix"}
        assert client.post("/api/product/echo", files={"f": ("a.txt", b"x")}, headers=good).status_code == 200
        assert client.post("/api/product/echo", headers=good).status_code == 200
        assert client.delete("/api/product/echo", headers=good).status_code == 405  # the route's own answer


def test_a_cookie_without_the_csrf_value_changes_nothing(tmp_path: Path) -> None:
    """Another local program on another port of 127.0.0.1 receives the session cookie from the browser (cookies are
    not port-scoped), but never the CSRF value, which lives in the window's sessionStorage."""
    with TestClient(app_with(tmp_path, csrf=True), base_url=ORIGIN) as window:
        launched(window)
        thief = TestClient(window.app, base_url=ORIGIN)
        thief.cookies.update(window.cookies)
        assert thief.get("/api/product/echo").status_code == 200  # reading was possible before as well
        answer = thief.post("/api/product/echo", json={}, headers=OWN)
        assert (answer.status_code, answer.json()["code"]) == (403, "access.csrf_invalid")


def test_the_csrf_value_of_another_backend_does_not_count(tmp_path: Path) -> None:
    other = launched(TestClient(app_with(tmp_path / "other", csrf=True), base_url=ORIGIN))["csrf"]
    with TestClient(app_with(tmp_path, csrf=True), base_url=ORIGIN) as client:
        launched(client)
        answer = client.post("/api/product/echo", json={}, headers={**OWN, launch.CSRF_HEADER: other})
        assert answer.json()["code"] == "access.csrf_invalid"


def test_open_paths_keep_their_own_protection(tmp_path: Path) -> None:
    app = product_app(
        tmp_path / "data", ("settings", "published"), launch_secret=SECRET, hardening=LaunchHardening(csrf=True)
    )
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/health").status_code == 200
        # Published APIs answer with their keys, without Origin, CSRF value or JSON.
        answer = client.post("/api/published/unknown/runs", content=b"x", headers={"content-type": "text/plain"})
        assert answer.json()["error"]["code"] == "auth.missing"
        # Without a session a change is refused as before (no session, no CSRF question).
        assert client.post("/api/product/echo", json={}, headers=OWN).json()["code"] == "access.launch_required"


# --- logout on the desktop -------------------------------------------------------------------------------------------


def test_the_desktop_logout_ends_the_session(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, logout=True), base_url=ORIGIN) as client:
        assert client.post("/api/logout", headers=OWN).json()["code"] == "access.launch_required"
        assert launched(client) == {"csrf": None, "logout": True}
        assert client.get("/api/logout").json()["code"] == "access.logout_method"
        assert client.post("/api/logout").json()["code"] == "request.origin_required"
        foreign = client.post("/api/logout", headers={"origin": "http://evil.example"})
        assert foreign.json()["code"] == "request.origin_not_allowed"
        assert client.get("/api/product/echo").status_code == 200
        answer = client.post("/api/logout", headers=OWN)
        assert answer.status_code == 200 and answer.json() == {"ended": 1}
        assert "Max-Age=0" in answer.headers["set-cookie"] or "expires=" in answer.headers["set-cookie"].lower()
        assert client.get("/api/product/echo").json()["code"] == "access.launch_required"
        # A copy of the old cookie opens nothing either, and the launch link stays used.
        assert client.get("/api/launch", params={"token": SECRET}).json()["code"] == "access.launch_used"


def test_the_old_cookie_is_dead_after_the_logout(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, logout=True), base_url=ORIGIN) as client:
        launched(client)
        copy = TestClient(client.app, base_url=ORIGIN)
        copy.cookies.update(client.cookies)
        client.post("/api/logout", headers=OWN)
        assert copy.get("/api/product/echo").json()["code"] == "access.launch_required"


def test_with_csrf_the_logout_needs_the_csrf_value(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, logout=True, csrf=True), base_url=ORIGIN) as client:
        csrf = launched(client)["csrf"]
        assert client.post("/api/logout", headers=OWN).json()["code"] == "access.csrf_invalid"
        assert client.get("/api/product/echo").status_code == 200
        assert client.post("/api/logout", headers={**OWN, launch.CSRF_HEADER: csrf}).json() == {"ended": 1}


# --- token in the fragment -------------------------------------------------------------------------------------------


def test_the_fragment_launch_page_holds_no_secret(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, fragment=True), base_url=ORIGIN) as client:
        page = client.get("/api/launch")
        assert page.status_code == 200 and "set-cookie" not in page.headers
        assert "launch-session" not in page.text.split("<script>")[0]
        assert "history.replaceState" in page.text and "#token=" in page.text
        assert page.headers["content-security-policy"].startswith("default-src 'none'; script-src 'sha256-")
        assert client.get("/api/product/echo").status_code == 401


def test_the_secret_in_the_query_is_refused_and_not_used_up(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, fragment=True), base_url=ORIGIN) as client:
        for query in ({"token": SECRET}, {"token": ""}, {"token": SECRET, "x": "1"}):
            answer = client.get("/api/launch", params=query)
            assert (answer.status_code, answer.json()["code"]) == (403, "access.launch_invalid"), query
            assert "set-cookie" not in answer.headers
        assert (
            client.post("/api/launch", params={"token": SECRET}, json={"token": SECRET}, headers=OWN).status_code == 403
        )
        # The secret still works the right way.
        answer = client.post("/api/launch", json={"token": SECRET}, headers=OWN)
        assert answer.status_code == 200 and answer.json() == {"csrf": None, "logout": False}
        assert client.get("/api/product/echo").status_code == 200


def test_the_fragment_exchange_refuses_what_a_page_could_send(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, fragment=True), base_url=ORIGIN) as client:
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("no origin", {"json": {"token": SECRET}}, "request.origin_required"),
            ("form", {"data": {"token": SECRET}, "headers": OWN}, "request.json_only"),
            (
                "text",
                {"content": json.dumps({"token": SECRET}), "headers": {**OWN, "content-type": "text/plain"}},
                "request.json_only",
            ),
            ("wrong", {"json": {"token": "w" * 43}, "headers": OWN}, "access.launch_invalid"),
            ("not a string", {"json": {"token": 1}, "headers": OWN}, "access.launch_invalid"),
            ("not an object", {"json": [SECRET], "headers": OWN}, "access.launch_invalid"),
            (
                "not json",
                {"content": b"{", "headers": {**OWN, "content-type": "application/json"}},
                "access.launch_invalid",
            ),
            (
                "too long",
                {"json": {"token": SECRET, "pad": "x" * launch.MAX_LAUNCH_BODY}, "headers": OWN},
                "access.launch_invalid",
            ),
        ]
        for label, options, code in cases:
            answer = client.post("/api/launch", **options)
            assert answer.json()["code"] == code, label
            assert "set-cookie" not in answer.headers, label
        foreign = client.post("/api/launch", json={"token": SECRET}, headers={"origin": "http://evil.example"})
        assert foreign.json()["code"] == "request.origin_not_allowed"
        assert client.put("/api/launch", json={"token": SECRET}, headers=OWN).json()["code"] == "access.launch_invalid"
        assert client.post("/api/launch", json={"token": SECRET}, headers=OWN).status_code == 200
        again = client.post("/api/launch", json={"token": SECRET}, headers=OWN)
        assert again.json()["code"] == "access.launch_used"


def test_fragment_csrf_and_logout_together(tmp_path: Path) -> None:
    with TestClient(app_with(tmp_path, fragment=True, csrf=True, logout=True, ttl=60), base_url=ORIGIN) as client:
        handed = client.post("/api/launch", json={"token": SECRET}, headers=OWN).json()
        assert handed["logout"] is True and isinstance(handed["csrf"], str)
        good = {**OWN, launch.CSRF_HEADER: handed["csrf"]}
        assert client.post("/api/product/echo", json={}, headers=OWN).json()["code"] == "access.csrf_invalid"
        assert client.post("/api/product/echo", json={}, headers=good).status_code == 200
        assert client.post("/api/logout", headers=good).json() == {"ended": 1}
        assert client.get("/api/product/echo").status_code == 401


# --- no terminal mode without a secret --------------------------------------------------------------------------------


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str, **env: str) -> tuple[argparse.Namespace, str]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launch, "_taken", launch._UNSET)
    for key in ("NODESTRO_PROFILE", "NODESTRO_HOST", launch.ENV_VAR):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    args = cli.parser("nodestro", "test").parse_args(["--data-dir", str(tmp_path / "data"), *argv])
    return args, cli.prepare(args)


def test_without_the_switch_the_terminal_start_stays_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, url = _prepare(monkeypatch, tmp_path, "--port", "8123")
    assert url == "http://localhost:8123"
    assert launch.launch_secret_from_env() is None
    assert cli.terminal_launch(url, write_file=True) is None


def test_the_terminal_start_issues_a_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, url = _prepare(monkeypatch, tmp_path, "--port", "8123", **{launch.TERMINAL_ENV: "1"})
    secret = launch.launch_secret_from_env()
    assert secret is not None and re.fullmatch(r"[0-9a-f]{64}", secret)
    assert secret not in "".join(os.environ.values())
    found = cli.terminal_launch(url, write_file=True)
    assert found is not None
    link, file = found
    assert link == f"http://localhost:8123/api/launch?token={secret}"
    assert file is not None and file.parent == tmp_path / "data" / "launch"
    if os.name == "posix":
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    assert link in file.read_text(encoding="utf-8")
    # The studio of this process takes the secret: only the launch link opens it, and it removes the file.
    config = AppConfig.from_env()
    assert config.launch_secret == secret
    with TestClient(product_app(tmp_path / "app", launch_secret=config.launch_secret), base_url=ORIGIN) as client:
        assert client.get("/api/product/echo").status_code == 401
        assert client.get("/api/launch", params={"token": secret}).status_code == 200
        assert client.get("/api/product/echo").status_code == 200
    assert not file.exists()


def test_the_terminal_link_carries_the_secret_in_the_fragment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, url = _prepare(monkeypatch, tmp_path, **{launch.TERMINAL_ENV: "1", launch.FRAGMENT_ENV: "1"})
    found = cli.terminal_launch(url, write_file=False)
    assert found is not None and found[1] is None
    assert found[0] == f"{url}/api/launch#token={launch.launch_secret_from_env()}"


def test_a_secret_of_the_desktop_app_or_operator_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, url = _prepare(monkeypatch, tmp_path, **{launch.TERMINAL_ENV: "1", launch.ENV_VAR: SECRET})
    assert launch.launch_secret_from_env() == SECRET
    assert launch._terminal is None
    assert cli.terminal_launch(url, write_file=True) is None


def test_the_terminal_secret_refuses_reload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args, _ = _prepare(monkeypatch, tmp_path, "--reload", **{launch.TERMINAL_ENV: "1"})
    with pytest.raises(SystemExit, match="--reload"):
        cli.serve("nodestro_api.app:create_app", args)


def test_an_unsafe_launch_folder_writes_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, url = _prepare(monkeypatch, tmp_path, **{launch.TERMINAL_ENV: "1"})
    folder = tmp_path / "data" / "launch"
    folder.mkdir(parents=True)
    if os.name == "posix":
        folder.chmod(0o777)
        found = cli.terminal_launch(url, write_file=True)
        assert found is not None and found[1] is None and not list(folder.iterdir())


# --- bind_loopback for the desktop backend ---------------------------------------------------------------------------


def test_the_backend_binds_a_random_loopback_address(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    args, url = _prepare(monkeypatch, tmp_path, "--port", "0", **{launch.BIND_ENV: "1"})
    bound = cli._bound
    assert bound is not None
    assert url == f"http://{bound.host}:{bound.port}" and bound.port > 0
    assert bound.host.startswith("127.") and (args.host, args.port) == (bound.host, bound.port)
    assert os.environ["NODESTRO_HOST"] == bound.host
    # Nobody else gets the port on 127.0.0.1's neighbours or on [::1].
    if bound.v6_guard is not None:
        with socket.socket(socket.AF_INET6) as other, pytest.raises(OSError):
            other.bind(("::1", bound.port))
    served: list[Any] = []

    class Server:
        def __init__(self, config: Any) -> None:
            self.started = False

        async def startup(self, sockets: Any = None) -> None:
            pass

        def run(self, sockets: Any = None) -> None:
            served.append(sockets)

    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(cli, "require_free_port", lambda args: pytest.fail("the held port was checked"))
    cli.serve("nodestro_api.app:create_app", args, ready=lambda: None)
    assert served == [[bound.listener]]


def test_bind_loopback_refuses_reload_and_leaves_other_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--reload"):
        _prepare(monkeypatch, tmp_path, "--reload", **{launch.BIND_ENV: "1"})
    assert cli._bound is None
    _, url = _prepare(monkeypatch, tmp_path, "--host", "::1", "--port", "8124", **{launch.BIND_ENV: "1"})
    assert cli._bound is None and url == "http://::1:8124"


def test_bind_loopback_stops_on_a_taken_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from nodestro_session.bind import PortUnavailableError

    def taken(port: int) -> Any:
        raise PortUnavailableError("taken")

    monkeypatch.setattr("nodestro_session.bind.bind_loopback", taken)
    with pytest.raises(SystemExit) as stopped:
        _prepare(monkeypatch, tmp_path, "--port", "8125", **{launch.BIND_ENV: "1"})
    assert stopped.value.code == 2


def test_terminal_start_end_to_end(tmp_path: Path) -> None:
    """A real ``nodestro-api`` process with the terminal secret and the loopback binding: it prints the link, answers
    only on its random address, and only the link opens a session."""
    env = {
        **os.environ,
        "NODESTRO_DATA_DIR": str(tmp_path / "data"),
        launch.TERMINAL_ENV: "1",
        launch.BIND_ENV: "1",
        launch.CSRF_ENV: "1",
    }
    for key in ("NODESTRO_PROFILE", "NODESTRO_HOST", launch.ENV_VAR, "PYTHONUNBUFFERED"):
        env.pop(key, None)
    process = subprocess.Popen(
        [sys.executable, "-c", "from nodestro_api.cli import main; main(['--port', '0'])"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    try:
        assert process.stdout is not None
        lines: queue.Queue[str] = queue.Queue()
        stdout = process.stdout
        threading.Thread(target=lambda: [lines.put(line) for line in stdout], daemon=True).start()
        seen: list[str] = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not any("once with this link" in line for line in seen):
            try:
                seen.append(lines.get(timeout=max(0.1, deadline - time.monotonic())))
            except queue.Empty:
                break
        running = next(line for line in seen if "running at" in line)
        base = re.search(r"running at (http://127\.[0-9.]+:[0-9]+)/api", running)
        assert base is not None, running
        link = next(line for line in seen if "once with this link" in line).rsplit(" ", 1)[1].strip()
        assert link.startswith(f"{base.group(1)}/api/launch?token=")
        with httpx.Client(base_url=base.group(1)) as client:
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/settings").status_code == 401
            page = client.get(link)
            assert page.status_code == 200 and "launch-session" in page.text
            assert client.get("/api/settings").status_code == 200
            assert client.get(link).json()["code"] == "access.launch_used"
    finally:
        process.terminate()
        process.wait(timeout=30)


# --- review fixes ---------------------------------------------------------------------------------------------------


SOCKET = ORIGIN.replace("http", "ws", 1) + "/api/ws"  # the test client would name the host testserver


async def _socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text("open")
    await websocket.close()


def _with_socket(**switches: Any) -> TestClient:
    """The outer Origin check and the launch middleware in front of one HTTP and one WebSocket route."""
    app = Starlette(
        routes=[Route("/api/x", _endpoint, methods=["GET", "POST"]), WebSocketRoute("/api/ws", _socket)],
        middleware=[
            Middleware(LocalAccessMiddleware, hosts=None, strict_origin=True),
            Middleware(LaunchSecretMiddleware, secret=SECRET, hardening=LaunchHardening(**switches)),
        ],
    )
    return TestClient(app, base_url=ORIGIN)


def _session_cookie(client: TestClient) -> dict[str, str]:
    """The window's cookie as a header (the test client's WebSocket does not send its cookie jar)."""
    return {"cookie": f"{launch.COOKIE}={client.cookies.get(launch.COOKIE)}"}


def test_with_csrf_a_websocket_without_origin_is_refused() -> None:
    """Review L-1: the outer check refuses only a foreign Origin; a WebSocket with the cookie and no Origin passed."""
    with _with_socket(csrf=True) as client:
        assert client.get("/api/launch", params={"token": SECRET}).status_code == 200
        cookie = _session_cookie(client)
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(SOCKET, headers=cookie) as socket:
            socket.receive_text()
        with client.websocket_connect(SOCKET, headers={**cookie, "origin": ORIGIN}) as socket:
            assert socket.receive_text() == "open"
        foreign = {**cookie, "origin": "http://127.0.0.1:9"}
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(SOCKET, headers=foreign) as socket:
            socket.receive_text()


def test_without_csrf_a_websocket_without_origin_passes_as_before() -> None:
    with _with_socket() as client:
        assert client.get("/api/launch", params={"token": SECRET}).status_code == 200
        with client.websocket_connect(SOCKET, headers=_session_cookie(client)) as socket:
            assert socket.receive_text() == "open"
