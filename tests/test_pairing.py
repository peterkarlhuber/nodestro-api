"""NAL-C4: pairing a browser by a one-time code, device rights and the self-issued certificate (docs/pairing.md)."""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import ssl
import sys
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from nodestro_api import pairing
from nodestro_api.pairing import Pairing, PairingError
from nodestro_api.serving import ApiError

HEADERS = {"X-Nodestro-UI": "1"}
CONTROL = {"X-Control-Token": "control-token"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---- codes ---------------------------------------------------------------------------------------------------------


def test_a_code_is_eight_crockford_characters_and_typing_is_forgiving() -> None:
    codes = {pairing.new_code() for _ in range(200)}
    assert all(len(code) == 8 and set(code) <= set(pairing.ALPHABET) for code in codes)
    assert len(codes) == 200 and len(pairing.ALPHABET) == 32  # 40 bits
    assert pairing.display("K7QDM2XA") == "K7QD-M2XA"
    assert pairing.normalize(" k7qd-m2xa ") == "K7QDM2XA"
    assert pairing.normalize("ILO0-1234") == "11001234"


def test_a_code_works_once_and_carries_its_scopes(tmp_path: Path) -> None:
    found = Pairing(tmp_path)
    code = found.create_code(name="Büro-PC", scopes=["run", "read", "approve"])
    device, secret = found.redeem(code.code.lower(), address="10.0.0.2")
    assert device.name == "Büro-PC" and device.scopes == ["run", "read", "approve"] and secret
    assert device.expires_at - device.created_at == timedelta(days=90)
    with pytest.raises(PairingError) as again:
        found.redeem(code.code, address="10.0.0.2")
    assert again.value.key == "pairing.code_invalid"
    # Only hashes are stored: neither the code nor the secret is in the file.
    stored = (tmp_path / pairing.DEVICES_FOLDER / pairing.DEVICES_FILE).read_text("utf-8")
    assert code.code not in stored and secret not in stored and device.id in stored


def test_codes_expire_and_never_grant_more_than_app_user_scopes(tmp_path: Path) -> None:
    clock = Clock()
    found = Pairing(tmp_path, clock=clock)
    code = found.create_code(minutes=2)
    clock.now += 121
    with pytest.raises(PairingError):
        found.redeem(code.code, address="a")
    for scopes in (["admin"], ["run", "operator"], []):
        with pytest.raises(PairingError) as refused:
            found.create_code(scopes=scopes) if scopes else found.create_code(scopes=["setup"])
        assert refused.value.key == "pairing.scopes_invalid"
    assert found.create_code(minutes=60).expires_at - datetime.now(UTC) <= timedelta(minutes=10, seconds=1)


def test_five_wrong_attempts_void_a_code_and_an_address_gets_twenty_an_hour(tmp_path: Path) -> None:
    clock = Clock()
    found = Pairing(tmp_path, clock=clock)
    code = found.create_code()
    for _ in range(5):
        with pytest.raises(PairingError):
            found.redeem("AAAAAAAA", address=f"10.0.0.{_}")
    with pytest.raises(PairingError) as void:
        found.redeem(code.code, address="10.0.0.9")
    assert void.value.key == "pairing.code_invalid"
    for _ in range(4):  # one open code at a time, so its five attempts never stop the address count
        fresh = found.create_code()
        for _ in range(5):
            with pytest.raises(PairingError):
                found.redeem("BBBBBBBB", address="10.0.0.1")
    fresh = found.create_code()
    with pytest.raises(PairingError) as limited:
        found.redeem(fresh.code, address="10.0.0.1")
    assert limited.value.key == "pairing.too_many_attempts" and limited.value.status == 429
    clock.now += 3601
    device, _secret = found.redeem(found.create_code().code, address="10.0.0.1")
    assert device.id.startswith("dev_")


def test_at_most_five_codes_are_open(tmp_path: Path) -> None:
    found = Pairing(tmp_path)
    for _ in range(5):
        found.create_code()
    with pytest.raises(PairingError) as refused:
        found.create_code()
    assert refused.value.key == "pairing.too_many_codes"


# ---- devices -------------------------------------------------------------------------------------------------------


def test_the_device_secret_rotates_and_an_old_one_revokes_the_device(tmp_path: Path) -> None:
    found = Pairing(tmp_path)
    device, first = found.redeem(found.create_code().code, address="a")
    renewed, second = found.renew(f"{device.id}.{first}")
    assert renewed.id == device.id and second != first
    third = found.renew(f"{device.id}.{second}")[1]
    # A copy of an older cookie (stolen) is used: the device is revoked, the owner must pair again.
    with pytest.raises(PairingError) as stolen:
        found.renew(f"{device.id}.{first}")
    assert stolen.value.key == "pairing.device_revoked"
    with pytest.raises(PairingError):
        found.renew(f"{device.id}.{third}")
    assert found.device(device.id) is None
    for cookie in ("", "dev_x", f"{device.id}.", "x.y"):
        with pytest.raises(PairingError):
            found.renew(cookie)


def test_a_saved_device_list_is_current_even_if_the_file_time_does_not_change(tmp_path: Path) -> None:
    import os

    found = Pairing(tmp_path)
    device, secret = found.redeem(found.create_code().code, address="a")
    path = tmp_path / pairing.DEVICES_FOLDER / pairing.DEVICES_FILE
    stamp = path.stat().st_mtime_ns
    for _ in range(3):
        secret = found.renew(f"{device.id}.{secret}")[1]
        os.utime(path, ns=(stamp, stamp))  # a file system with coarse time stamps (FAT32)
    assert found.renew(f"{device.id}.{secret}")[0].id == device.id


def test_names_from_a_browser_lose_control_characters(tmp_path: Path) -> None:
    found = Pairing(tmp_path)
    device, _ = found.redeem(
        found.create_code().code, address="a", name="Lap\x1b[2Jtop\nrevoked: no\u202e", agent="UA\r\nX"
    )
    assert device.name == "Lap [2Jtop revoked: no" and device.agent == "UA X"
    assert pairing.clean("\x00\x07", 60) == ""


def test_new_addresses_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pairing, "MAX_ADDRESSES", 3)
    found = Pairing(tmp_path)
    found.create_code()
    for index in range(3):
        with pytest.raises(PairingError) as wrong:
            found.redeem("AAAAAAAA", address=f"2001:db8::{index}")
        assert wrong.value.key == "pairing.code_invalid"
    with pytest.raises(PairingError) as capped:
        found.redeem("AAAAAAAA", address="2001:db8::99")
    assert capped.value.key == "pairing.too_many_attempts"


def test_a_restore_keeps_revocations(tmp_path: Path) -> None:
    import shutil

    found = Pairing(tmp_path)
    device, _ = found.redeem(found.create_code().code, address="a")
    path = tmp_path / pairing.DEVICES_FOLDER / pairing.DEVICES_FILE
    saved = tmp_path / "backup.json"
    shutil.copy(path, saved)
    found.revoke(device.id)
    before = found.devices()
    shutil.copy(saved, path)  # the backup made before the revocation is restored
    assert Pairing(tmp_path).device(device.id) is not None
    assert pairing.keep_revocations(tmp_path, before) == 1
    assert Pairing(tmp_path).device(device.id) is None


def test_devices_survive_a_restart_and_can_be_revoked_while_stopped(tmp_path: Path) -> None:
    found = Pairing(tmp_path)
    one, secret = found.redeem(found.create_code().code, address="a")
    two, _ = found.redeem(found.create_code().code, address="a")
    again = Pairing(tmp_path)
    assert {item.id for item in again.devices()} == {one.id, two.id}
    assert again.renew(f"{one.id}.{secret}")[0].id == one.id
    assert pairing.revoke_offline(tmp_path, two.id) == 1
    assert again.device(two.id) is None and again.device(one.id) is not None
    assert pairing.revoke_offline(tmp_path, None) == 1
    assert found.device(one.id) is None


def test_an_expired_device_and_a_damaged_file_pair_nothing(tmp_path: Path) -> None:
    found = Pairing(tmp_path)
    device, secret = found.redeem(found.create_code().code, address="a")
    data = json.loads((tmp_path / pairing.DEVICES_FOLDER / pairing.DEVICES_FILE).read_text("utf-8"))
    data["devices"][0]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    (tmp_path / pairing.DEVICES_FOLDER / pairing.DEVICES_FILE).write_text(json.dumps(data), "utf-8")
    assert found.device(device.id) is None
    with pytest.raises(PairingError):
        found.renew(f"{device.id}.{secret}")
    (tmp_path / pairing.DEVICES_FOLDER / pairing.DEVICES_FILE).write_text("{broken", "utf-8")
    assert found.devices() == [] and found.device(device.id) is None


def test_the_switch_is_off_unless_set() -> None:
    assert not pairing.enabled({})
    assert not pairing.enabled({"NODESTRO_PAIRING": "off"})
    assert pairing.enabled({"NODESTRO_PAIRING": "on"}) and pairing.enabled({"NODESTRO_PAIRING": "1"})


# ---- the interface's endpoints -------------------------------------------------------------------------------------


def _app(tmp_path: Path, *, on: bool = True) -> tuple[FastAPI, Any]:
    from fastapi.responses import JSONResponse

    from nodestro_api.webui_server import UiOptions, WebUi, build_router

    host = SimpleNamespace(served=SimpleNamespace(publication_id="pub_1", name="App", description="", number=1))
    ui = WebUi(
        host,  # type: ignore[arg-type]
        UiOptions(control_token="control-token", secure_cookies=True, tls_mode="self_signed", tls_fingerprint="AB"),
        pairing=Pairing(tmp_path) if on else None,
    )
    app = FastAPI()

    @app.exception_handler(ApiError)
    async def failed(_request: Any, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": {"status": exc.status, "code": exc.code}}, exc.status)

    app.include_router(build_router(ui))

    @app.get("/whoami")
    def whoami(request: Request) -> Any:
        found = ui.identity(request)
        return {"identity": found.model_dump() if found else None}

    return app, ui


def _control_code(client: TestClient, **body: Any) -> dict[str, Any]:
    # The launcher asks over loopback with its control secret.
    with TestClient(client.app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)) as local:  # type: ignore[arg-type]
        answer = local.post("/ui/control/pair", json=body, headers=CONTROL)
    assert answer.status_code == 200, answer.text
    return answer.json()


@contextlib.contextmanager
def _browser(app: FastAPI, base_url: str = "https://backend.local:8800") -> Iterator[TestClient]:
    with TestClient(app, base_url=base_url) as client:
        yield client


def test_pairing_over_https_gives_an_app_user_session_and_a_device_cookie(tmp_path: Path) -> None:
    app, _ui = _app(tmp_path)
    with _browser(app) as browser:
        code = _control_code(browser, name="Büro-PC", scopes=["run", "read"])
        assert code["display"][4] == "-" and code["tls"] == {"mode": "self_signed", "fingerprint": "AB"}
        answer = browser.post("/ui/api/pair", json={"code": code["display"]}, headers=HEADERS)
        assert answer.status_code == 200, answer.text
        assert answer.json()["device"]["name"] == "Büro-PC" and answer.json()["operator"] is False
        cookies = answer.headers.get_list("set-cookie")
        device_cookie = next(item for item in cookies if item.startswith("nodestro_device="))
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/ui/api/device"):
            assert flag.lower() in device_cookie.lower(), device_cookie
        assert browser.get("/whoami").json()["identity"]["scopes"] == ["run", "read"]
        boot = browser.get("/ui/api/bootstrap").json()
        assert boot["session"]["device"] is True and boot["pairing"] is True
        # A device session is never the operator's: the settings and the launcher's control stay closed.
        assert browser.get("/ui/api/setup").status_code == 403
        assert browser.post("/ui/control/stop", headers=CONTROL).status_code == 404
        assert browser.post("/ui/control/pair", json={}, headers=CONTROL).status_code == 404


def test_pairing_is_refused_without_tls_without_the_switch_and_across_sites(tmp_path: Path) -> None:
    app, _ui = _app(tmp_path)
    with _browser(app, "http://backend.local:8800") as plain:
        code = _control_code(plain)
        refused = plain.post("/ui/api/pair", json={"code": code["code"]}, headers=HEADERS)
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "pairing.tls_required"
    with _browser(app) as browser:
        assert browser.post("/ui/api/pair", json={"code": code["code"]}).status_code == 403  # no same-origin header
        other = {**HEADERS, "Origin": "https://evil.example"}
        assert browser.post("/ui/api/pair", json={"code": code["code"]}, headers=other).status_code == 403
        assert browser.post("/ui/api/pair", json={"code": code["code"]}, headers=HEADERS).status_code == 200
    off, _ = _app(tmp_path / "off", on=False)
    with _browser(off) as browser:
        answer = browser.post("/ui/api/pair", json={"code": "AAAAAAAA"}, headers=HEADERS)
        assert answer.status_code == 404 and answer.json()["error"]["code"] == "pairing.disabled"
        assert browser.get("/ui/api/bootstrap").json()["pairing"] is False


def test_a_paired_browser_renews_its_session_and_loses_it_when_revoked(tmp_path: Path) -> None:
    app, ui = _app(tmp_path)
    with _browser(app) as browser:
        code = _control_code(browser)
        device = browser.post("/ui/api/pair", json={"code": code["code"]}, headers=HEADERS).json()["device"]
        # The backend restarted: the session is gone, the device cookie brings it back (and rotates).
        ui.sessions = type(ui.sessions)()
        assert browser.get("/whoami").json()["identity"] is None
        old = browser.cookies.get("nodestro_device", path="/ui/api/device")
        renewed = browser.post("/ui/api/device/session", headers=HEADERS)
        assert renewed.status_code == 200, renewed.text
        assert browser.cookies.get("nodestro_device", path="/ui/api/device") != old
        assert browser.get("/whoami").json()["identity"]["id"] == device["id"]
        # The operator revokes the device: its session ends at once.
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5000)) as local:
            listed = local.get("/ui/control/devices", headers=CONTROL).json()["devices"]
            assert [item["id"] for item in listed] == [device["id"]] and "secret_hash" not in listed[0]
            assert local.post("/ui/control/devices/revoke", json={"id": device["id"]}, headers=CONTROL).json() == {
                "revoked": 1
            }
        assert browser.get("/whoami").json()["identity"] is None
        again = browser.post("/ui/api/device/session", headers=HEADERS)
        assert again.status_code == 401 and again.json()["error"]["code"] == "pairing.device_revoked"


def test_a_stolen_old_device_cookie_revokes_the_device(tmp_path: Path) -> None:
    app, _ui = _app(tmp_path)
    with _browser(app) as browser:
        code = _control_code(browser)
        browser.post("/ui/api/pair", json={"code": code["code"]}, headers=HEADERS)
        stolen = browser.cookies.get("nodestro_device", path="/ui/api/device")
        assert browser.post("/ui/api/device/session", headers=HEADERS).status_code == 200
    with _browser(app) as thief:
        thief.cookies.set("nodestro_device", stolen, domain="backend.local", path="/ui/api/device")
        assert thief.post("/ui/api/device/session", headers=HEADERS).status_code == 401
    with _browser(app) as owner:
        owner.cookies.set("nodestro_device", stolen, domain="backend.local", path="/ui/api/device")
        assert owner.post("/ui/api/device/session", headers=HEADERS).status_code == 401


def test_a_paired_browser_can_remove_itself(tmp_path: Path) -> None:
    app, ui = _app(tmp_path)
    with _browser(app) as browser:
        code = _control_code(browser)
        device = browser.post("/ui/api/pair", json={"code": code["code"]}, headers=HEADERS).json()["device"]
        assert browser.delete("/ui/api/device", headers=HEADERS).json() == {"removed": True}
        assert ui.pairing.device(device["id"]) is None
        assert browser.get("/whoami").json()["identity"] is None


def test_wrong_codes_are_counted_like_failed_sign_ins(tmp_path: Path) -> None:
    app, ui = _app(tmp_path)
    failed: list[str] = []
    ui.auth_failed = failed.append
    with _browser(app) as browser:
        _control_code(browser)
        answer = browser.post("/ui/api/pair", json={"code": "WRONG000"}, headers=HEADERS)
        assert answer.status_code == 400 and answer.json()["error"]["code"] == "pairing.code_invalid"
    assert failed == ["testclient"]


# ---- the self-issued certificate -----------------------------------------------------------------------------------


def test_the_self_issued_certificate_names_this_computer_and_is_kept(tmp_path: Path) -> None:
    from nodestro_api import tls_selfsigned

    if not tls_selfsigned.available():
        pytest.skip("cryptography is not installed")
    from cryptography import x509

    cert, key, fingerprint = tls_selfsigned.ensure(tmp_path)
    assert len(fingerprint.split(":")) == 32
    loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    names = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    assert (loaded.not_valid_after_utc - loaded.not_valid_before_utc).days == 397
    if sys.platform != "win32":
        assert key.stat().st_mode & 0o777 == 0o600
    # Kept while valid; renewed 30 days before it expires.
    assert tls_selfsigned.ensure(tmp_path)[2] == fingerprint
    later = datetime.now(UTC) + timedelta(days=370)
    assert tls_selfsigned.ensure(tmp_path, now=later)[2] != fingerprint
    # The fingerprint is the one of the DER certificate (what a browser shows).
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)


def test_discovery_names_the_tls_mode_and_pairing(tmp_path: Path) -> None:
    from nodestro_api.app_contract import AppDiscovery, TlsInfo

    found = TlsInfo.model_validate({"enabled": True, "mode": "self_signed", "fingerprint": "AB:" * 31 + "AB"})
    assert AppDiscovery(backend={"id": "x"}, tls=found).model_dump(exclude_none=True)["tls"]["mode"] == "self_signed"


def test_the_package_contains_cryptography_where_a_wheel_exists() -> None:
    from packaging.requirements import Requirement
    from test_portable import _release_bundle

    from nodestro_api import exporter, portable_build

    from test_flow import edge

    bundle = _release_bundle(
        [{"id": "start", "type": "start"}, {"id": "out", "type": "output"}], [edge("start", "out")]
    )
    bundle.python = []
    direct, _ = portable_build.requirements(exporter.ExportOptions(kind="portable"), bundle)
    found = next(Requirement(item) for item in direct if item.startswith("cryptography"))
    assert found.marker is not None
    platforms = {
        ("linux", "x86_64"): True,
        ("win32", "AMD64"): True,
        ("win32", "ARM64"): False,
        ("darwin", "arm64"): True,
        ("darwin", "x86_64"): False,
    }
    for (system, machine), expected in platforms.items():
        assert found.marker.evaluate({"sys_platform": system, "platform_machine": machine}) is expected


def test_a_portable_package_carries_the_pairing_modules() -> None:
    from nodestro_api import exporter

    modules = set(exporter.runtime_modules(None, exporter.PORTABLE_ROOTS))
    assert {"nodestro_api.pairing", "nodestro_api.tls_selfsigned", "nodestro_api.portable_ops"} <= modules
