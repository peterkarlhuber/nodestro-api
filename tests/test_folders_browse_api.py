"""The folder browser of the studio API (N-11, ``POST /api/folders/browse``): folder names only, never files, no
system folders, nothing without the authenticated local session, CSRF protection like every change, and links or
``..`` that cannot lead to a folder that is not shown."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.folders import FOLDERS_ENV
from orkexa.fs import system_folders
import pytest

from nodestro_api import launch
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig
from nodestro_api.launch import LaunchHardening
from nodestro_api.permissions import Permission, Principal

BROWSE = "/api/folders/browse"
SECRET = "s" * 43
ORIGIN = "http://127.0.0.1"
OWN = {"origin": ORIGIN}


def _client(tmp_path: Path, **config: Any) -> TestClient:
    app = create_app(AppConfig(data_dir=tmp_path / "data", **config), integrations=[], scheduler=False)
    return TestClient(app, base_url=ORIGIN)


def _launched(client: TestClient) -> TestClient:
    answer = client.get("/api/launch", params={"token": SECRET}, follow_redirects=False)
    assert answer.status_code == 200, answer.text
    return client


@pytest.fixture
def switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FOLDERS_ENV, "1")


@pytest.fixture
def window(tmp_path: Path, switched_on: None) -> Iterator[TestClient]:
    """The desktop window: the launch secret exchanged for the session cookie."""
    with _client(tmp_path, launch_secret=SECRET) as client:
        yield _launched(client)


@pytest.fixture
def place(tmp_path: Path) -> Path:
    root = tmp_path / "place"
    for name in ("Beta", "alpha", ".hidden", "alpha/inner"):
        (root / name).mkdir(parents=True)
    (root / "secret-plan.txt").write_text("canary-content-7731", encoding="utf-8")
    (root / "alpha" / "offer.pdf").write_bytes(b"%PDF-1.4 canary-content-7731")
    return root


def _browse(client: TestClient, path: str | None, **headers: str) -> Any:
    return client.post(BROWSE, json={"path": path}, headers={**OWN, **headers})


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links cannot be made here")


# ---- folder names only ------------------------------------------------------------------------------------------


def test_the_browser_answers_folder_names_never_files(window: TestClient, place: Path) -> None:
    answer = _browse(window, str(place))
    assert answer.status_code == 200, answer.text
    view = answer.json()
    assert [folder["name"] for folder in view["folders"]] == ["alpha", "Beta"]
    assert all(folder["link"] is False for folder in view["folders"])
    assert Path(view["path"]).samefile(place) and Path(view["parent"]).samefile(place.parent)
    assert view["truncated"] is False and view["separator"] == os.sep
    assert {place["kind"] for place in view["places"]} <= {"home", "root", "drive", "media"}
    text = answer.text
    assert "secret-plan" not in text and "offer.pdf" not in text and "canary-content" not in text
    inner = _browse(window, view["folders"][0]["path"]).json()
    assert [folder["name"] for folder in inner["folders"]] == ["inner"] and "offer" not in json.dumps(inner)


def test_without_a_path_the_browser_starts_at_home(window: TestClient) -> None:
    answer = window.post(BROWSE, json={}, headers=OWN)
    if answer.status_code == 422:  # the home folder of this test's user lies in a system folder it does not own
        assert answer.json()["params"]["reason"] == "hidden"
        return
    assert answer.status_code == 200, answer.text
    assert Path(answer.json()["path"]).samefile(Path.home())


# ---- no system folders, no data folder ------------------------------------------------------------------------


def test_system_folders_are_neither_listed_nor_opened(window: TestClient, tmp_path: Path) -> None:
    top = os.path.abspath(os.sep)
    shown = {Path(folder["path"]) for folder in _browse(window, top).json()["folders"]}
    for system in system_folders():
        if Path(system).parent == Path(top) and Path(system) != Path.home():
            assert Path(system) not in shown, system
    existing = [folder for folder in system_folders() if Path(folder).is_dir() and Path(folder) != Path.home()]
    assert existing, "this computer has no system folder to test"
    for folder in existing:
        refused = _browse(window, folder)
        assert (refused.status_code, refused.json()["code"]) == (422, "folder.not_browsable"), folder
        assert refused.json()["params"] == {"reason": "hidden"}, folder


def test_the_data_folder_is_neither_listed_nor_opened(window: TestClient, tmp_path: Path, place: Path) -> None:
    names = [folder["name"] for folder in _browse(window, str(tmp_path)).json()["folders"]]
    assert "place" in names and "data" not in names
    refused = _browse(window, str(tmp_path / "data"))
    assert (refused.status_code, refused.json()["params"]) == (422, {"reason": "hidden"})


def test_links_and_parent_references_cannot_reach_a_hidden_folder(
    window: TestClient, place: Path, tmp_path: Path
) -> None:
    system = next(Path(folder) for folder in system_folders() if Path(folder).is_dir() and Path(folder) != Path.home())
    _symlink(place / "escape", system)
    _symlink(place / "to-data", tmp_path / "data")
    _symlink(place / "to-alpha", place / "alpha")
    folders = {folder["name"]: folder for folder in _browse(window, str(place)).json()["folders"]}
    assert set(folders) == {"alpha", "Beta", "to-alpha"}
    assert folders["to-alpha"]["link"] is True and Path(folders["to-alpha"]["path"]).samefile(place / "alpha")
    assert folders["to-alpha"]["path"] != str(place / "to-alpha")  # the target, never the link
    for path in (place / "escape", place / "to-data", place / "escape" / "x"):
        refused = _browse(window, str(path))
        assert refused.status_code == 422 and refused.json()["code"] == "folder.not_browsable", path
    climb = os.sep.join([str(place), *([".."] * len(place.parts)), *system.parts[1:]])
    refused = _browse(window, climb)
    assert (refused.status_code, refused.json()["params"]) == (422, {"reason": "hidden"})
    blocked = place / ".ssh"
    blocked.mkdir()
    _symlink(place / "keys", blocked)
    assert "keys" not in {folder["name"] for folder in _browse(window, str(place)).json()["folders"]}
    assert _browse(window, str(place / "keys")).json()["params"] == {"reason": "hidden"}


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("relative/folder", "invalid_path"),
        ("{place}/secret-plan.txt", "not_a_folder"),
        ("{place}/missing", "not_found"),
        ("{place}/\u0001", "invalid_path"),
        ("//server/share", "network_path"),
    ],
)
def test_paths_that_are_no_folder_are_refused_with_a_reason(
    window: TestClient, place: Path, path: str, reason: str
) -> None:
    refused = _browse(window, path.format(place=place))
    assert (refused.status_code, refused.json()["code"]) == (422, "folder.not_browsable"), refused.text
    assert refused.json()["params"] == {"reason": reason}
    assert "canary" not in refused.text


# ---- nothing without the session ------------------------------------------------------------------------------


def test_nothing_without_the_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, place: Path) -> None:
    monkeypatch.delenv(FOLDERS_ENV, raising=False)
    with _client(tmp_path, launch_secret=SECRET) as client:
        refused = _browse(_launched(client), str(place))
        assert (refused.status_code, refused.json()["code"]) == (404, "folder.disabled")


def test_nothing_without_the_session(tmp_path: Path, switched_on: None, place: Path) -> None:
    # The window's backend, but a client without the session cookie (another program, another browser).
    with _client(tmp_path, launch_secret=SECRET) as client:
        refused = _browse(client, str(place))
        assert (refused.status_code, refused.json()["code"]) == (401, "access.launch_required")
        client.cookies.set(launch.COOKIE, "guessed" * 8)
        assert _browse(client, str(place)).status_code == 401
    # No launch secret at all (the nodestro command in a terminal): there is no authenticated session.
    with _client(tmp_path / "terminal") as terminal:
        refused = _browse(terminal, str(place))
        assert (refused.status_code, refused.json()["code"]) == (403, "folder.session_required")
        assert "alpha" not in refused.text


def test_only_an_admin_may_browse(window: TestClient, place: Path) -> None:
    reader = Principal(id="reader", permissions=frozenset({Permission.READ, Permission.EDIT, Permission.RUN}))
    window.app.state.principal_resolver = lambda _connection: reader  # type: ignore[attr-defined]
    refused = _browse(window, str(place))
    assert (refused.status_code, refused.json()["params"]) == (403, {"permission": "admin"})


# ---- CSRF ---------------------------------------------------------------------------------------------------------


def test_a_request_from_another_site_is_refused(window: TestClient, place: Path) -> None:
    foreign = window.post(BROWSE, json={"path": str(place)}, headers={"origin": "http://evil.example"})
    assert (foreign.status_code, foreign.json()["code"]) == (403, "request.origin_not_allowed")
    other_port = window.post(BROWSE, json={"path": str(place)}, headers={"origin": "http://127.0.0.1:5999"})
    assert other_port.status_code == 403
    for site in ("cross-site", "same-site"):
        refused = _browse(window, str(place), **{"sec-fetch-site": site})
        assert (refused.status_code, refused.json()["code"]) == (403, "request.cross_site"), site
    assert _browse(window, str(place), **{"sec-fetch-site": "same-origin"}).status_code == 200
    # A form or a text body (what a page can send without preflight) is no JSON object.
    form = window.post(BROWSE, content=f"path={place}", headers={**OWN, "content-type": "text/plain"})
    assert form.status_code == 422 and "alpha" not in form.text
    # Reading is not offered as a GET: a link or an image cannot list folders.
    assert window.get(BROWSE, params={"path": str(place)}).status_code in (404, 405)


def test_with_the_csrf_switch_the_sessions_csrf_value_is_required(
    tmp_path: Path, switched_on: None, place: Path
) -> None:
    import re

    with _client(tmp_path, launch_secret=SECRET, hardening=LaunchHardening(csrf=True)) as client:
        page = client.get("/api/launch", params={"token": SECRET})
        found = re.search(r'<script type="application/json" id="launch-session">(.*?)</script>', page.text)
        assert found is not None
        csrf = json.loads(found.group(1))["csrf"]
        refused = _browse(client, str(place))
        assert (refused.status_code, refused.json()["code"]) == (403, "access.csrf_invalid")
        wrong = _browse(client, str(place), **{launch.CSRF_HEADER: "w" * 43})
        assert wrong.status_code == 403
        no_origin = client.post(BROWSE, json={"path": str(place)}, headers={launch.CSRF_HEADER: csrf})
        assert (no_origin.status_code, no_origin.json()["code"]) == (403, "request.origin_required")
        assert _browse(client, str(place), **{launch.CSRF_HEADER: csrf}).status_code == 200


# ---- Nodestro Local Docker: the access key's sessions ------------------------------------------------------------


def test_the_local_containers_access_key_is_a_session(tmp_path: Path, switched_on: None, place: Path) -> None:
    from test_local_container import KEY, client_for, signed_in
    from test_local_container import ORIGIN as LOCAL_ORIGIN

    with client_for(tmp_path) as client:
        headers = {"origin": LOCAL_ORIGIN}
        assert client.post(BROWSE, json={"path": str(place)}, headers=headers).status_code == 401
        bearer = client.post(BROWSE, json={"path": str(place)}, headers={**headers, "authorization": f"Bearer {KEY}"})
        assert bearer.status_code == 200, bearer.text
        browser = signed_in(client).post(BROWSE, json={"path": str(place)}, headers=headers)
        assert browser.status_code == 200 and [item["name"] for item in browser.json()["folders"]] == ["alpha", "Beta"]


def test_the_middleware_marks_only_requests_with_a_session() -> None:
    """The scope key cannot come from a request: only the launch middleware sets it, after the session check."""
    seen: list[Any] = []

    async def app(scope: Any, receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        seen.append(scope.get(launch.SESSION_SCOPE_KEY))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = launch.LaunchSecretMiddleware(app, secret=SECRET)
    client = TestClient(middleware, base_url=ORIGIN)  # type: ignore[arg-type]
    assert client.get("/api/x", headers={launch.SESSION_SCOPE_KEY: "cookie"}).status_code == 401
    assert client.get("/api/health").status_code == 204 and seen == [None]
    assert client.get("/api/launch", params={"token": SECRET}).status_code == 200
    assert client.get("/api/x").status_code == 204 and seen == [None, "cookie"]
