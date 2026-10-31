"""The desktop app's per-launch secret: only the window that opened the launch link uses the studio API."""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
import pytest

from nodestro_api import cli, launch
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

SECRET = "s" * 43


def client_for(tmp_path: Path, secret: str | None = SECRET) -> TestClient:
    app = create_app(
        AppConfig(data_dir=tmp_path / "data", launch_secret=secret),
        integrations=[],
        scheduler=False,
    )
    return TestClient(app, base_url="http://127.0.0.1")


def test_without_the_launch_only_health_and_published_apis_answer(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.get("/api/health").status_code == 200
        for path in ("/api/workflows", "/api/settings", "/", "/api/docs", "/api/openapi.json"):
            answer = client.get(path)
            assert answer.status_code == 401, path
            assert answer.json()["code"] == "access.launch_required"
        assert client.post("/api/workflows", json={"name": "x"}).status_code == 401
        # Published workflow APIs keep their own keys: no session needed, the key is still checked.
        assert client.get("/api/published/unknown").json()["error"]["code"] == "auth.missing"


def test_the_launch_link_works_once_and_sets_a_strict_cookie(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.get("/api/launch", params={"token": "wrong" * 10}, follow_redirects=False).json()["code"] == (
            "access.launch_invalid"
        )
        assert client.get("/api/launch", follow_redirects=False).status_code == 403
        answer = client.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
        assert answer.status_code == 200
        # Continues from the backend's own origin, so the strict cookie goes along (no cross-site redirect).
        assert 'http-equiv="refresh" content="0;url=/"' in answer.text
        assert answer.headers["referrer-policy"] == "no-referrer"
        cookie = answer.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
        assert SECRET not in cookie
        assert client.get("/api/workflows").status_code == 200
        # The secret cannot be used again, not even by the window itself.
        again = client.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
        assert again.status_code == 403
        assert again.json()["code"] == "access.launch_used"
        assert client.get("/api/workflows").status_code == 200


def test_another_client_without_the_cookie_is_refused(tmp_path: Path) -> None:
    with client_for(tmp_path) as window:
        window.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
        other = TestClient(window.app, base_url="http://127.0.0.1")
        assert other.get("/api/workflows").status_code == 401
        other.cookies.set(launch.COOKIE, "guessed")
        assert other.get("/api/workflows").status_code == 401
        other.cookies.set(launch.COOKIE, SECRET)
        assert other.get("/api/workflows").status_code == 401


def test_a_post_to_the_launch_link_is_refused(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert client.post("/api/launch", params={"token": SECRET}).status_code == 403
        assert client.get("/api/workflows").status_code == 401


def test_the_host_check_still_runs_first(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        client.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
        answer = client.get("/api/workflows", headers={"Host": "evil.example"})
        assert answer.status_code == 403
        assert answer.json()["code"] == "request.host_not_allowed"


def test_no_secret_changes_nothing(tmp_path: Path) -> None:
    with client_for(tmp_path, secret=None) as client:
        assert client.get("/api/workflows").status_code == 200
        assert client.get("/api/launch").status_code == 404


def test_a_short_secret_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NodestroError) as error:
        client_for(tmp_path, secret="short")
    assert error.value.code == "access.launch_secret_weak"


def test_the_secret_is_taken_out_of_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launch, "_taken", launch._UNSET)
    monkeypatch.setenv(launch.ENV_VAR, SECRET)
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    assert AppConfig.from_env().launch_secret == SECRET
    assert launch.ENV_VAR not in __import__("os").environ
    # Later reads (the command line reads the configuration several times) keep the secret.
    assert AppConfig.from_env().launch_secret == SECRET
    assert SECRET not in repr(AppConfig.from_env())


def test_reload_is_refused_with_a_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launch, "_taken", SECRET)
    monkeypatch.setenv("NODESTRO_DATA_DIR", str(tmp_path))
    args = argparse.Namespace(host="127.0.0.1", port=0, reload=True)
    with pytest.raises(SystemExit, match="--reload"):
        cli.serve("nodestro_api.app:create_app", args)
    monkeypatch.setattr(launch, "_taken", "short")
    args.reload = False
    with pytest.raises(SystemExit, match="at least 32"):
        cli.serve("nodestro_api.app:create_app", args)


def test_the_launch_token_never_reaches_the_access_log(tmp_path: Path) -> None:
    import io
    import logging

    from uvicorn.logging import AccessFormatter

    with client_for(tmp_path):
        access = logging.getLogger("uvicorn.access")
        uvicorn_format, plain = io.StringIO(), io.StringIO()
        handlers = [logging.StreamHandler(uvicorn_format), logging.StreamHandler(plain)]
        # uvicorn's own formatter unpacks the record's arguments (client, method, path, version, status).
        handlers[0].setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
        level = access.level
        access.setLevel(logging.INFO)
        access.addHandler(handlers[0])
        try:
            access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", f"/api/launch?token={SECRET}&x=1", "1.1", 200)
        finally:
            access.removeHandler(handlers[0])
        access.addHandler(handlers[1])
        try:
            access.info("anything with %s", SECRET)
            access.info(f"or in the message {SECRET}")
        finally:
            access.removeHandler(handlers[1])
            access.setLevel(level)
    assert '127.0.0.1:5 - "GET /api/launch?token=[redacted]&x=1 HTTP/1.1" 200' in uvicorn_format.getvalue()
    assert plain.getvalue().count("[redacted]") == 2
    assert SECRET not in uvicorn_format.getvalue() + plain.getvalue()
