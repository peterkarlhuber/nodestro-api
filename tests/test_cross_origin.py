"""NAL-C5: the standard interface from another origin: allowed origins, the token profile and CORS
(docs/cross-origin.md)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.testing import ScriptedClient
import pytest
from test_export import _download, _export, _extract
from test_published_api import new_key, publish

from nodestro_api import cross_origin
from nodestro_api.cross_origin import AccessTokens, AllowedOrigins, normalize_origin
from nodestro_api.pairing import Pairing, PairingError
from nodestro_api.serving import hash_secret, parse_token

from conftest import text
from test_flow import edge, save
from test_runs import agent_node

APP = "https://apps.example"
OTHER = "https://other.example"
BACKEND = "https://backend.local:8800"
CONTROL = {"X-Control-Token": "control-token"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---- origins and tokens --------------------------------------------------------------------------------------------


def test_an_origin_is_exact_https_or_loopback_http() -> None:
    assert normalize_origin("https://Apps.Example") == "https://apps.example"
    assert normalize_origin("https://apps.example:443/") == "https://apps.example"
    assert normalize_origin("https://apps.example:8443") == "https://apps.example:8443"
    assert normalize_origin("http://localhost:5173") == "http://localhost:5173"
    assert normalize_origin("http://[::1]:5173") == "http://[::1]:5173"
    assert normalize_origin("https://bücher.example") == "https://xn--bcher-kva.example"
    for bad in (
        "",
        "null",
        "*",
        "https://*.example",
        "http://apps.example",
        "https://apps.example/ui",
        "https://apps.example?x=1",
        "https://user@apps.example",
        "ftp://apps.example",
        "apps.example",
        "https://apps.example:99999",
    ):
        with pytest.raises(ValueError):
            normalize_origin(bad)


def test_allowed_origins_are_kept_in_a_file_and_a_damaged_one_allows_nothing(tmp_path: Path) -> None:
    allowed = AllowedOrigins(tmp_path)
    assert allowed.list() == [] and not allowed.allowed(APP)
    assert allowed.add("https://APPS.example/") == APP
    allowed.add(APP)
    assert allowed.list() == [APP] and allowed.allowed(APP) and not allowed.allowed(OTHER)
    # Another process (the launcher) changes the file: the running application sees it.
    AllowedOrigins(tmp_path).add(OTHER)
    assert allowed.allowed(OTHER)
    assert allowed.remove(OTHER) == OTHER and allowed.remove(OTHER) is None and not allowed.allowed(OTHER)
    (tmp_path / "settings" / "allowed-origins.json").write_text('{"origins": ["*", "http://evil.example"]', "utf-8")
    assert allowed.list() == [] and not allowed.allowed(APP)
    (tmp_path / "settings" / "allowed-origins.json").write_text(
        json.dumps({"origins": ["*", "http://evil.example", APP]}), "utf-8"
    )
    assert allowed.list() == [APP]


def test_access_tokens_expire_and_are_capped_per_device() -> None:
    clock = Clock()
    tokens = AccessTokens(clock=clock)
    first = tokens.issue("dev_1", APP)
    assert first.startswith("nat_") and tokens.get(first) is not None
    clock.now += cross_origin.ACCESS_SECONDS
    assert tokens.get(first) is None
    issued = [tokens.issue("dev_1", APP) for _ in range(cross_origin.TOKENS_PER_DEVICE + 2)]
    assert [tokens.get(item) is not None for item in issued] == [False, False] + [True] * cross_origin.TOKENS_PER_DEVICE
    other = tokens.issue("dev_2", APP)
    tokens.drop_device("dev_1")
    assert all(tokens.get(item) is None for item in issued) and tokens.get(other) is not None


def test_the_switch_is_off_unless_set() -> None:
    assert not cross_origin.enabled({}) and not cross_origin.enabled({"NODESTRO_CROSS_ORIGIN": "off"})
    assert cross_origin.enabled({"NODESTRO_CROSS_ORIGIN": "on"})


def test_a_device_renews_only_where_it_was_paired(tmp_path: Path) -> None:
    found = Pairing(tmp_path)
    device, secret = found.redeem(found.create_code().code, address="10.0.0.2", origin=APP)
    assert device.origin == APP
    for wrong in (None, OTHER):
        with pytest.raises(PairingError):
            found.renew(f"{device.id}.{secret}", origin=wrong)
    # Refusing another place is not a reuse: the device still works where it was paired.
    _, fresh = found.renew(f"{device.id}.{secret}", origin=APP)
    assert fresh and found.device(device.id) is not None
    assert found.revoke_origin(APP) == 1 and found.device(device.id) is None


# ---- the application ------------------------------------------------------------------------------------------------


@pytest.fixture
def exported(make_app: Callable[..., TestClient], tmp_path: Path) -> SimpleNamespace:
    studio = make_app({"WRITER": [text("# Bericht\n\nAlles gut.")] * 3})
    workflow_id = save(
        studio,
        [{"id": "start", "type": "start"}, agent_node("writer", "WRITER"), {"id": "out", "type": "output"}],
        [edge("start", "writer"), edge("writer", "out")],
        name="Bericht",
    )
    publication = publish(studio, workflow_id)["publication"]
    token = new_key(studio, publication["id"], ["run", "read"])
    project = _extract(_download(studio, _export(studio, publication["id"], web_ui=True)["id"]), tmp_path / "export")
    key_id, secret = parse_token(token)  # type: ignore[misc]
    keys = f"{key_id}:{hash_secret(secret)}:run+read"
    return SimpleNamespace(project=project, slug=publication["slug"], token=token, keys=keys)


@contextlib.contextmanager
def _app(exported: SimpleNamespace, data_dir: Path, *, on: bool = True) -> Iterator[tuple[Any, Any]]:
    """The exported app as the launcher serves it with pairing and (``on``) the cross-origin profile."""
    from nodestro_api.cross_origin import CorsMiddleware, CrossOrigin
    from nodestro_api.standalone import StandaloneSettings, create_app
    from nodestro_api.webui_server import UiOptions, WebUi

    holder: dict[str, Any] = {}

    def web_ui(host: Any, core: Any) -> Any:
        pairing = Pairing(data_dir)
        holder["ui"] = WebUi(
            host,
            UiOptions(control_token="control-token", secure_cookies=True, tls_mode="certificate"),
            auth_failed=core.auth_failed,
            pairing=pairing,
            cross_origin=CrossOrigin(data_dir, pairing) if on else None,
        )
        return holder["ui"]

    settings = StandaloneSettings.from_env({"DATA_DIR": str(data_dir), "API_KEYS": exported.keys})
    fake = ScriptedClient({"WRITER": [text("# Bericht\n\nAlles gut.")] * 5})
    app = create_app(exported.project, settings=settings, client_factory=lambda _provider: fake, web_ui=web_ui)
    if holder["ui"].cross_origin is not None:
        app.add_middleware(
            CorsMiddleware, cross_origin=holder["ui"].cross_origin, app_prefix=f"/api/published/{exported.slug}"
        )
    if holder["ui"].cross_origin is not None:
        holder["ui"].cross_origin.origins.add(APP)
    yield app, holder["ui"]


def _code(app: Any) -> str:
    # Without a context: the application's lifespan belongs to the test's main client.
    local = TestClient(app, base_url="http://127.0.0.1:8800", client=("127.0.0.1", 5000))
    answer = local.post("/ui/control/pair", json={"scopes": ["run", "read"]}, headers=CONTROL)
    assert answer.status_code == 200, answer.text
    return str(answer.json()["code"])


def _cookie(ui: Any) -> str:
    return cross_origin.cookie_name(ui.host.instance().id)


def _page(origin: str = APP, **extra: str) -> dict[str, str]:
    return {"Origin": origin, "X-Nodestro-App": "1", **extra}


def _grant(client: TestClient, code: str, origin: str = APP) -> Any:
    return client.post(
        "/api/app/token", json={"grant_type": "pairing_code", "code": code, "name": "Laptop"}, headers=_page(origin)
    )


def test_pairing_from_an_allowed_origin_gives_a_token_for_the_app_only(
    exported: SimpleNamespace, tmp_path: Path
) -> None:
    with _app(exported, tmp_path / "data") as (app, ui), TestClient(app, base_url=BACKEND) as page:
        # Discovery without a credential: the page learns the contract and that tokens are offered.
        found = page.get("/api/app", headers={"Origin": APP})
        assert found.status_code == 200 and "token" in found.json()["auth"]["methods"]
        assert found.headers["access-control-allow-origin"] == APP and found.headers["vary"] == "Origin"
        assert "access-control-allow-credentials" not in found.headers
        answer = _grant(page, _code(app))
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["token_type"] == "Bearer" and body["expires_in"] == 900 and body["access_token"].startswith("nat_")
        assert (
            body["device"]["scopes"] == ["run", "read"] and body["app"]["api_base"] == f"/api/published/{exported.slug}"
        )
        assert "output_dir" not in body["app"] or "path" not in body["app"]["output_dir"]
        assert answer.headers["access-control-allow-credentials"] == "true"
        cookie = next(item for item in answer.headers.get_list("set-cookie") if item.startswith(_cookie(ui) + "="))
        for flag in ("HttpOnly", "Secure", "SameSite=None", "Partitioned", "Path=/api/app/token"):
            assert flag in cookie, cookie
        bearer = {"Origin": APP, "Authorization": f"Bearer {body['access_token']}"}
        info = page.get(f"/api/published/{exported.slug}", headers=bearer)
        assert info.status_code == 200 and info.headers["access-control-allow-origin"] == APP
        assert "Nodestro-App-Contract" in info.headers["access-control-expose-headers"]
        signed = page.get("/api/app", headers=bearer).json()
        assert [item["slug"] for item in signed["apps"]] == [exported.slug]
        # Never the interface's own endpoints, never the operator, never without the page's origin.
        assert page.get("/ui/api/config", headers=bearer).status_code == 401
        assert page.get("/ui/api/setup", headers=bearer).status_code == 401
        assert page.get(f"/api/published/{exported.slug}", headers={**bearer, "Origin": OTHER}).status_code == 403
        no_origin = {"Authorization": bearer["Authorization"]}
        assert page.get(f"/api/published/{exported.slug}", headers=no_origin).status_code == 401
        # The device is listed with its origin.
        listed = ui.pairing.devices()
        assert [item.origin for item in listed] == [APP]


def test_cors_answers_allowed_origins_only_and_never_with_keys_or_cookies(
    exported: SimpleNamespace, tmp_path: Path
) -> None:
    with _app(exported, tmp_path / "data") as (app, _ui), TestClient(app, base_url=BACKEND) as page:
        preflight = {"Origin": APP, "Access-Control-Request-Method": "POST"}
        allowed = page.options("/api/app/token", headers=preflight)
        assert allowed.status_code == 204 and allowed.headers["access-control-allow-origin"] == APP
        assert "X-Nodestro-App" in allowed.headers["access-control-allow-headers"]
        assert allowed.headers["access-control-allow-credentials"] == "true"
        runs = page.options(f"/api/published/{exported.slug}/runs", headers=preflight)
        assert runs.status_code == 204 and "access-control-allow-credentials" not in runs.headers
        refused = page.options("/api/app/token", headers={**preflight, "Origin": OTHER})
        assert refused.status_code == 403 and "access-control-allow-origin" not in refused.headers
        assert page.get("/api/app", headers={"Origin": OTHER}).json()["error"]["code"] == "cors.origin_not_allowed"
        # Other paths never answer another origin.
        for path in ("/ui/api/bootstrap", "/health", "/"):
            assert "access-control-allow-origin" not in page.get(path, headers={"Origin": APP}).headers
        assert page.options("/ui/api/session", headers=preflight).status_code in (404, 405)
        # An API key from another origin's page is refused; the same key without Origin (a server) still works.
        key = {"Origin": APP, "Authorization": f"Bearer {exported.token}"}
        refused_key = page.get(f"/api/published/{exported.slug}", headers=key)
        assert refused_key.status_code == 401 and refused_key.json()["error"]["code"] == "auth.token_required"
        assert (
            page.get(
                f"/api/published/{exported.slug}", headers={"X-API-Key": exported.token, "Origin": APP}
            ).status_code
            == 401
        )
        server = page.get(f"/api/published/{exported.slug}", headers={"Authorization": f"Bearer {exported.token}"})
        assert server.status_code == 200
        # The interface's own session cookie never authenticates a request from another origin.
        page.cookies.set("nodestro_app_session", "anything", domain="backend.local")
        assert page.get(f"/api/published/{exported.slug}", headers={"Origin": APP}).status_code == 401
        # The backend's own origin is not cross-origin: unchanged.
        own = page.get("/api/app", headers={"Origin": BACKEND})
        assert own.status_code == 200 and "access-control-allow-origin" not in own.headers
        # Every answer of a CORS path varies by Origin, refusals and answers without one included.
        assert own.headers["vary"] == "Origin" and page.get("/api/app").headers["vary"] == "Origin"
        assert page.get("/api/app", headers={"Origin": OTHER}).headers["vary"] == "Origin"


def test_token_requests_need_the_header_an_allowed_origin_and_tls(exported: SimpleNamespace, tmp_path: Path) -> None:
    with _app(exported, tmp_path / "data") as (app, _ui), TestClient(app, base_url=BACKEND) as page:
        code = _code(app)
        body = {"grant_type": "pairing_code", "code": code}
        missing = page.post("/api/app/token", json=body, headers={"Origin": APP})
        assert missing.json()["error"]["code"] == "token.header_required"
        assert page.post("/api/app/token", json=body, headers=_page(OTHER)).status_code == 403
        assert page.post("/api/app/token", json=body, headers={"X-Nodestro-App": "1"}).status_code == 403
        own = page.post("/api/app/token", json=body, headers=_page(BACKEND))
        assert own.json()["error"]["code"] == "cors.origin_not_allowed"
        wrong = page.post("/api/app/token", json={"grant_type": "api_key"}, headers=_page())
        assert wrong.json()["error"]["code"] == "token.grant_invalid"
        refused = _grant(TestClient(app, base_url="http://backend.local:8800"), code)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "pairing.tls_required"
        assert _grant(page, code).status_code == 200


def test_refresh_rotates_and_a_stolen_old_cookie_revokes_the_device(exported: SimpleNamespace, tmp_path: Path) -> None:
    with _app(exported, tmp_path / "data") as (app, ui), TestClient(app, base_url=BACKEND) as page:
        first = _grant(page, _code(app)).json()
        stolen = page.cookies.get(_cookie(ui))
        renewed = page.post("/api/app/token/refresh", headers=_page())
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["access_token"] != first["access_token"]
        assert page.cookies.get(_cookie(ui)) != stolen
        # From another allowed origin the device does not renew (and that is not a reuse).
        ui.cross_origin.origins.add(OTHER)
        assert page.post("/api/app/token/refresh", headers=_page(OTHER)).status_code == 401
        current = page.cookies.get(_cookie(ui))
        thief = TestClient(app, base_url=BACKEND)
        thief.cookies.set(_cookie(ui), stolen, domain="backend.local", path="/api/app/token")
        refused = thief.post("/api/app/token/refresh", headers=_page())
        assert refused.status_code == 401 and refused.json()["error"]["code"] == "pairing.device_revoked"
        assert "Max-Age=0" in refused.headers["set-cookie"]
        owner = TestClient(app, base_url=BACKEND)
        owner.cookies.set(_cookie(ui), current, domain="backend.local", path="/api/app/token")
        assert owner.post("/api/app/token/refresh", headers=_page()).status_code == 401
        bearer = {"Origin": APP, "Authorization": f"Bearer {first['access_token']}"}
        assert owner.get(f"/api/published/{exported.slug}", headers=bearer).status_code == 401


def test_revocation_and_a_removed_origin_end_access_at_once(exported: SimpleNamespace, tmp_path: Path) -> None:
    with _app(exported, tmp_path / "data") as (app, ui), TestClient(app, base_url=BACKEND) as page:
        token = _grant(page, _code(app)).json()["access_token"]
        bearer = {"Origin": APP, "Authorization": f"Bearer {token}"}
        info = f"/api/published/{exported.slug}"
        assert page.get(info, headers=bearer).status_code == 200
        # The page removes its own device.
        removed = page.delete("/api/app/token", headers={**_page(), "Authorization": f"Bearer {token}"})
        assert removed.status_code == 200 and "Max-Age=0" in removed.headers["set-cookie"]
        assert page.get(info, headers=bearer).status_code == 401
        assert page.post("/api/app/token/refresh", headers=_page()).status_code == 401
        # The operator removes the origin: tokens stop at once, the devices of that origin are revoked.
        token = _grant(page, _code(app)).json()["access_token"]
        bearer["Authorization"] = f"Bearer {token}"
        assert page.get(info, headers=bearer).status_code == 200
        ui.cross_origin.origins.remove(APP)
        assert page.get(info, headers=bearer).status_code == 403
        local = TestClient(app, base_url="http://127.0.0.1:8800", client=("127.0.0.1", 5000))
        answer = local.post("/ui/control/devices/revoke", json={"origin": APP}, headers=CONTROL)
        assert answer.json() == {"revoked": 1}
        ui.cross_origin.origins.add(APP)
        assert page.get(info, headers=bearer).status_code == 401
        assert all(item.revoked_at is not None for item in ui.pairing.devices())
        # The operator revokes every device: tokens of new devices end too.
        token = _grant(page, _code(app)).json()["access_token"]
        local.post("/ui/control/devices/revoke", json={"all": True}, headers=CONTROL)
        assert page.get(info, headers={"Origin": APP, "Authorization": f"Bearer {token}"}).status_code == 401


def test_a_device_of_another_origin_never_renews_an_embedded_session(exported: SimpleNamespace, tmp_path: Path) -> None:
    with _app(exported, tmp_path / "data") as (app, ui), TestClient(app, base_url=BACKEND) as page:
        _grant(page, _code(app))
        cookie = page.cookies.get(_cookie(ui))
        page.cookies.set("nodestro_device", cookie, domain="backend.local", path="/ui/api/device")
        answer = page.post("/ui/api/device/session", headers={"X-Nodestro-UI": "1"})
        assert answer.status_code == 401
        # And an embedded device never gets a token.
        own = page.post("/ui/api/pair", json={"code": _code(app)}, headers={"X-Nodestro-UI": "1"})
        assert own.status_code == 200
        embedded = page.cookies.get("nodestro_device", path="/ui/api/device")
        page.cookies.set(_cookie(ui), embedded, domain="backend.local", path="/api/app/token")
        assert page.post("/api/app/token/refresh", headers=_page()).status_code == 401


def test_without_the_switch_nothing_answers_another_origin(exported: SimpleNamespace, tmp_path: Path) -> None:
    with _app(exported, tmp_path / "data", on=False) as (app, _ui), TestClient(app, base_url=BACKEND) as page:
        found = page.get("/api/app", headers={"Origin": APP})
        assert "token" not in found.json()["auth"]["methods"] and "access-control-allow-origin" not in found.headers
        refused = _grant(page, _code(app))
        assert refused.status_code == 404 and refused.json()["error"]["code"] == "token.disabled"
        preflight = page.options("/api/app/token", headers={"Origin": APP, "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in preflight.headers
        fake = {"Origin": APP, "Authorization": "Bearer nat_" + "A" * 43}
        assert page.get(f"/api/published/{exported.slug}", headers=fake).status_code == 401


def test_the_launcher_manages_allowed_origins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from nodestro_api import portable

    settings = portable.Settings(tmp_path, values={"data": {"data_dir": "data"}}, program=tmp_path)
    parse = portable.parser().parse_args
    assert portable.origins_command(parse(["origins", "add", "https://Apps.example/"]), settings) == 0
    assert portable.origins_command(parse(["origins", "add", "http://apps.example"]), settings) == 2
    assert portable.origins_command(parse(["origins", "list"]), settings) == 0
    assert f"Allowed: {APP}" in capsys.readouterr().out and AllowedOrigins(settings.data_dir).list() == [APP]
    store = Pairing(settings.data_dir)
    made, _secret = store.redeem(store.create_code().code, address="10.0.0.2", origin=APP)
    assert portable.origins_command(parse(["origins", "remove", APP]), settings) == 0
    assert Pairing(settings.data_dir).device(made.id) is None
    assert portable.origins_command(parse(["origins", "remove", APP]), settings) == 1
    # A pairing link for the interface needs a valid interface address (checked before a code is made).
    for bad in ("http://apps.example/", "apps.example", "https://user@apps.example/"):
        assert portable.pair_command(parse(["pair", "--ui", bad]), settings) == 2
    assert portable.pair_command(parse(["pair", "--ui", "https://apps.example/ui/"]), settings) == 1  # not running


def test_a_browser_that_never_paired_does_not_count_as_a_failure(exported: SimpleNamespace, tmp_path: Path) -> None:
    with _app(exported, tmp_path / "data") as (app, ui), TestClient(app, base_url=BACKEND) as page:
        counted: list[str] = []
        ui.auth_failed = counted.append
        assert page.post("/api/app/token/refresh", headers=_page()).status_code == 401
        assert counted == []
        page.cookies.set(_cookie(ui), "dev_x.wrong", domain="backend.local", path="/api/app/token")
        assert page.post("/api/app/token/refresh", headers=_page()).status_code == 401
        assert len(counted) == 1
