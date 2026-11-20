"""The local session's protection comes from the shared library nodestro-session (N-02), without a change of
behavior: the existing tests (test_launch.py, test_local_access.py, test_handoff.py, test_local_container.py) cover
the contract; these tests show that the library is the one in use, and repeat the attack cases on a product app
(N-01 host)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
import nodestro_session
from nodestro_session.sessions import SessionStore
import pytest

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
