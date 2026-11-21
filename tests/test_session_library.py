"""The local session's protection comes from the shared library nodestro-session (N-02), without a change of
behavior: the existing tests (test_launch.py, test_local_access.py, test_handoff.py, test_local_container.py) cover
the contract; these tests show that the library is the one in use, and repeat the attack cases on a product app
(N-01 host)."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
import nodestro_session
from nodestro_session.sessions import SessionStore
import pytest
from starlette.types import Receive, Scope, Send

from nodestro_api import handoff, launch, local_access
from nodestro_api.context import AppConfig
from nodestro_api.host import Branding, HostSwitches, create_product_app
from nodestro_api.permissions import READ

SECRET = "s" * 43
ORIGIN = "http://127.0.0.1"


def _product(tmp_path: Path, **config: object) -> FastAPI:
    router = APIRouter(prefix="/api/product")

    @router.post("/echo", dependencies=READ)
    def echo(request: Request) -> dict[str, str]:
        return {"title": request.app.title}

    return create_product_app(
        "desktop",
        tmp_path / "data",
        ["settings", router],
        Branding(title="Test Product"),
        HostSwitches(scheduler=False),
        config=AppConfig(data_dir=tmp_path / "ignored", **config),  # type: ignore[arg-type]
        integrations=[],
    )


def _middleware(app: FastAPI) -> launch.LaunchSecretMiddleware:
    """The launch middleware of a started app's middleware stack."""
    layer = app.middleware_stack
    while layer is not None and not isinstance(layer, launch.LaunchSecretMiddleware):
        layer = getattr(layer, "app", None)
    assert isinstance(layer, launch.LaunchSecretMiddleware)
    return layer


def test_the_api_uses_the_library() -> None:
    hosts = nodestro_session.hosts
    for name in ("is_loopback", "split_host", "split_port", "is_ip_address", "listens_everywhere"):
        assert getattr(local_access, name) is getattr(hosts, name), name
    assert issubclass(launch.RedactLaunchToken, nodestro_session.SecretRedactor)
    middleware = launch.LaunchSecretMiddleware(lambda *_: None, secret=SECRET)  # type: ignore[arg-type]
    assert isinstance(middleware._sessions, SessionStore)


def test_the_handoff_is_read_by_the_library(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def read(fd: int, *, max_bytes: int) -> bytes:
        calls.append(fd)
        assert max_bytes == handoff.MAX_BYTES
        raise nodestro_session.HandoffError("descriptor 7 is not open (Bad file descriptor)")

    monkeypatch.setattr(handoff, "read_secrets_fd", read)
    with pytest.raises(NodestroError) as caught:
        handoff.read(7)
    assert calls == [7]
    assert caught.value.code == "access.handoff_invalid"
    assert "descriptor 7 is not open" in str(caught.value)


def test_only_digests_of_the_session_are_kept(tmp_path: Path) -> None:
    with TestClient(_product(tmp_path, launch_secret=SECRET), base_url=ORIGIN) as client:
        assert client.get("/api/launch", params={"token": SECRET}, follow_redirects=False).status_code == 200
        session = client.cookies.get(launch.COOKIE)
        assert session
        middleware = _middleware(client.app)  # type: ignore[arg-type]
        assert session not in repr(vars(middleware)) + repr(vars(middleware._sessions))
        assert len(middleware._sessions) == 1


def test_a_product_app_refuses_the_attack_cases(tmp_path: Path) -> None:
    with TestClient(_product(tmp_path, launch_secret=SECRET), base_url=ORIGIN) as client:
        # No session yet: every route but the health check is closed.
        assert client.get("/api/settings").json()["code"] == "access.launch_required"
        assert client.post("/api/product/echo", headers={"Origin": ORIGIN}).status_code == 401
        assert client.get("/api/launch", params={"token": "x" * 43}).json()["code"] == "access.launch_invalid"
        assert client.get("/api/launch", params={"token": SECRET}, follow_redirects=False).status_code == 200
        assert client.post("/api/product/echo", headers={"Origin": ORIGIN}).json() == {"title": "Test Product"}
        # The launch link works once.
        again = client.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
        assert again.status_code == 403 and again.json()["code"] == "access.launch_used"
        # DNS rebinding: a foreign Host, even with the session.
        rebound = client.get("/api/settings", headers={"Host": "evil.example"})
        assert rebound.status_code == 403 and rebound.json()["code"] == "request.host_not_allowed"
        # Cross-site and other local pages: a foreign Origin on a change, even with the session.
        for origin in ("http://evil.example", "http://127.0.0.1:9999", "null"):
            refused = client.post("/api/product/echo", headers={"Origin": origin})
            assert refused.status_code == 403 and refused.json()["code"] == "request.origin_not_allowed", origin
        # A proxy in front of it.
        forwarded = client.get("/api/settings", headers={"X-Forwarded-For": "10.0.0.1"})
        assert forwarded.json()["code"] == "request.forwarded_not_allowed"
        # Another client without the cookie, or with a guessed or the launch secret as cookie.
        other = TestClient(client.app, base_url=ORIGIN)
        assert other.get("/api/settings").status_code == 401
        for guess in ("guessed", SECRET):
            other.cookies.set(launch.COOKIE, guess)
            assert other.get("/api/settings").status_code == 401


def test_a_tossed_cookie_does_not_hide_the_real_session(tmp_path: Path) -> None:
    with TestClient(_product(tmp_path, launch_secret=SECRET), base_url=ORIGIN) as client:
        client.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
        real = client.cookies.get(launch.COOKIE)
        client.cookies.clear()
        both = {"Cookie": f"{launch.COOKIE}={real}; {launch.COOKIE}=tossed"}
        assert client.get("/api/settings", headers=both).status_code == 200
        only = {"Cookie": f"{launch.COOKIE}=tossed"}
        assert client.get("/api/settings", headers=only).status_code == 401


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "nodestro.example"])
def test_a_product_app_is_never_offered_to_the_network(tmp_path: Path, host: str) -> None:
    with pytest.raises(NodestroError) as caught:
        _product(tmp_path, host=host)
    assert caught.value.code == "access.network_needs_login"
    assert not (tmp_path / "data").exists()


def test_exported_projects_do_not_need_the_library() -> None:
    """Exports copy their modules byte for byte and install only what their lock names; nodestro-session is not on a
    package index, so no exported module may import it (the launch and access guards are the studio's)."""
    from nodestro_api import exporter

    dirs = {"nodestro_api": Path(exporter.__file__).resolve().parent}
    for extra in ((), exporter.PORTABLE_ROOTS):
        for name in exporter.runtime_modules(extra=extra):
            package, _, module = name.partition(".")
            if package != "nodestro_api":
                continue
            source = (dirs[package] / f"{module}.py").read_text(encoding="utf-8")
            assert "nodestro_session" not in source, name


# The same protections through the library, on the middleware itself (desktop: one launch; reusable key: the local
# container and the Local Docker profile). The values are the ones of launch.py before N-02: these tests (all but the
# ones that look for the library) also pass against launch.py and local_access.py of the API before this change.


def _guarded(**options: object) -> TestClient:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = launch.LaunchSecretMiddleware(app, secret=SECRET, **options)  # type: ignore[arg-type]
    return TestClient(middleware, base_url=ORIGIN)


def test_the_cookie_headers_are_byte_for_byte_the_old_ones() -> None:
    desktop = _guarded()
    answer = desktop.get("/api/launch", params={"token": SECRET})
    session = desktop.cookies.get(launch.COOKIE)
    assert answer.headers["set-cookie"] == f"{launch.COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/"
    reusable = _guarded(reusable=True, bearer=True, cookie=launch.LOCAL_COOKIE, max_age=24 * 3600)
    answer = reusable.get("/api/launch", params={"token": SECRET})
    session = reusable.cookies.get(launch.LOCAL_COOKIE)
    assert answer.headers["set-cookie"] == (
        f"{launch.LOCAL_COOKIE}={session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400"
    )
    out = reusable.post("/api/logout", headers={"Origin": ORIGIN})
    assert out.headers["set-cookie"] == f"{launch.LOCAL_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    assert reusable.get("/api/data").status_code == 401


def test_the_session_count_stays_bounded_and_the_oldest_ends_first() -> None:
    client = _guarded(reusable=True, bearer=True)
    sessions = []
    for _ in range(launch.MAX_SESSIONS + 1):
        client.cookies.clear()
        assert client.get("/api/launch", params={"token": SECRET}).status_code == 200
        sessions.append(client.cookies.get(launch.COOKIE))
    client.cookies.clear()
    middleware = client.app
    assert isinstance(middleware, launch.LaunchSecretMiddleware)
    assert len(middleware._sessions) == launch.MAX_SESSIONS
    assert client.get("/api/data", headers={"Cookie": f"{launch.COOKIE}={sessions[0]}"}).status_code == 401
    for session in (sessions[1], sessions[-1]):
        assert client.get("/api/data", headers={"Cookie": f"{launch.COOKIE}={session}"}).status_code == 200


@pytest.mark.parametrize(
    "value",
    [SECRET, SECRET[:-1], SECRET + "s", "s" * 129, "s" * 100_000, "", "a b", '"quoted"', "é" * 43],
)
def test_no_cookie_value_but_an_issued_session_opens_the_api(value: str) -> None:
    client = _guarded(reusable=True, bearer=True)
    client.get("/api/launch", params={"token": SECRET})
    client.cookies.clear()
    cookie = f"{launch.COOKIE}={value}".encode()
    assert client.get("/api/data", headers={b"Cookie": cookie}).status_code == 401


@pytest.mark.parametrize(
    "authorization",
    ["", "Bearer", f"Bearer {SECRET[:-1]}", f"Bearer {SECRET}x", f"Basic {SECRET}", f"Token {SECRET}"],
)
def test_only_the_exact_key_as_bearer_opens_the_api(authorization: str) -> None:
    client = _guarded(reusable=True, bearer=True)
    assert client.get("/api/data", headers={"Authorization": authorization}).status_code == 401
    assert client.get("/api/data", headers={"Authorization": f"bearer  {SECRET} "}).status_code == 200


def test_a_wrong_or_repeated_launch_opens_no_desktop_session() -> None:
    client = _guarded()
    for token in ("", SECRET[:-1], SECRET.upper()):
        assert client.get("/api/launch", params={"token": token}).json()["code"] == "access.launch_invalid"
    assert client.post("/api/launch", params={"token": SECRET}).json()["code"] == "access.launch_invalid"
    assert client.get("/api/data").status_code == 401
    assert client.get("/api/launch", params={"token": SECRET}).status_code == 200
    first = client.cookies.get(launch.COOKIE)
    client.cookies.clear()
    assert client.get("/api/launch", params={"token": SECRET}).json()["code"] == "access.launch_used"
    assert client.cookies.get(launch.COOKIE) is None
    assert client.get("/api/data", headers={"Cookie": f"{launch.COOKIE}={first}"}).status_code == 200


def test_the_log_filter_redacts_the_secret_and_every_token_parameter() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s"',
        ("127.0.0.1", "GET", f"/api/launch?token=abc&x={SECRET}"),
        None,
    )
    assert launch.RedactLaunchToken(SECRET).filter(record)
    text = record.getMessage()
    assert SECRET not in text and "abc" not in text
    assert "token=[redacted]" in text
