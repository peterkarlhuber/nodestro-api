"""Client addresses behind a reverse proxy (MUP-ONA A3, PS-3): the server profile reports a proxy setting without
``FORWARDED_ALLOW_IPS``, per-address limits count clients separately when the proxy passes their address, and failed
keys of published APIs are counted per key, so one client's wrong key does not lock out the others."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nodestro_runtime.problems import NodestroError
import pytest
from starlette.requests import Request
import test_identity
import test_server_command

from nodestro_api import server as command
from nodestro_api.serving import ApiError, KeyIdentity, ServingCore

# The PostgreSQL fixtures of the identity and server command tests (a fresh schema each).
database = test_identity.database
identity = test_identity.identity
schema = test_server_command.schema


def _environ(**values: str) -> dict[str, str]:
    return dict(values)


# MUP-ONA T-API-PROXY-1
def test_a_proxy_without_forwarded_allow_ips_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    assert command.client_address_problem(_environ()) is None
    assert command.client_address_problem(_environ(NODESTRO_BEHIND_PROXY="1", FORWARDED_ALLOW_IPS="10.0.0.2")) is None
    problem = command.client_address_problem(_environ(NODESTRO_BEHIND_PROXY="1"))
    assert problem is not None and problem.startswith("proxy.client_address_unknown: ")
    assert "FORWARDED_ALLOW_IPS" in problem
    # Default: a warning in the log, the API starts.
    with caplog.at_level(logging.WARNING, logger="nodestro_api.server"):
        command.require_client_address(_environ(NODESTRO_BEHIND_PROXY="true", FORWARDED_ALLOW_IPS=" "))
    assert any("proxy.client_address_unknown" in record.getMessage() for record in caplog.records)
    # Opt-in: the start is refused.
    with pytest.raises(NodestroError) as refused:
        command.require_client_address(_environ(NODESTRO_BEHIND_PROXY="1", NODESTRO_REQUIRE_CLIENT_ADDRESS="1"))
    assert refused.value.code == "proxy.client_address_unknown"
    # Without the proxy setting nothing is required.
    command.require_client_address(_environ(NODESTRO_REQUIRE_CLIENT_ADDRESS="1"))


# MUP-ONA T-API-PROXY-1
def test_check_warns_or_refuses_a_proxy_without_client_addresses(
    schema: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command, "_require_containers", lambda _server: None)
    monkeypatch.setattr(command, "_superuser_role", lambda _server, _database: None)
    monkeypatch.setattr(command, "_pgvector_warning", lambda _database: None)  # RT-048, tested on its own
    loaded = command.load(test_server_command._environment(tmp_path))
    command.migrate(loaded, backup_dir=None, backup_taken=False)
    monkeypatch.setenv("NODESTRO_BEHIND_PROXY", "1")
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    warnings: list[str] = []
    assert command.check(loaded, warnings=warnings, identity_provider=False) == []
    assert [item.split(":")[0] for item in warnings] == ["proxy.client_address_unknown"]
    monkeypatch.setenv("NODESTRO_REQUIRE_CLIENT_ADDRESS", "1")
    warnings.clear()
    problems = command.check(loaded, warnings=warnings, identity_provider=False)
    assert [item.split(":")[0] for item in problems] == ["proxy.client_address_unknown"] and warnings == []
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.2")
    assert command.check(loaded, warnings=warnings, identity_provider=False) == [] and warnings == []


# ---- published API keys ------------------------------------------------------------------------------------------

GOOD_X = ("aaaaaaaa0001", "x" * 40)
GOOD_Y = ("bbbbbbbb0002", "y" * 40)


def _core() -> ServingCore:
    keys = {GOOD_X: KeyIdentity(id=GOOD_X[0]), GOOD_Y: KeyIdentity(id=GOOD_Y[0])}
    host: Any = SimpleNamespace(verify=lambda key_id, secret: keys.get((key_id, secret)), session_identity=None)
    return ServingCore(host)


def _request(token: str, client: str = "10.0.0.2") -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (client, 50000)})


def _status(core: ServingCore, token: str, client: str = "10.0.0.2") -> int:
    try:
        core.authenticate(_request(token, client))
    except ApiError as error:
        return error.status
    return 200


# MUP-ONA T-API-PROXY-3
def test_failed_keys_against_one_key_do_not_lock_out_another_key() -> None:
    core = _core()
    wrong_x = f"ndk_{GOOD_X[0]}_{'w' * 40}"
    # Every client shares the proxy's address 10.0.0.2.
    assert [_status(core, wrong_x) for _ in range(core.MAX_AUTH_FAILURES)] == [401] * core.MAX_AUTH_FAILURES
    assert _status(core, wrong_x) == 429  # key X is throttled
    assert _status(core, f"ndk_{GOOD_Y[0]}_{'w' * 40}") == 401  # a typo of key Y is not throttled by X's failures
    assert _status(core, f"ndk_{GOOD_Y[0]}_{GOOD_Y[1]}") == 200
    assert _status(core, f"ndk_{GOOD_X[0]}_{GOOD_X[1]}") == 200  # a valid key is never throttled by failures


# MUP-ONA T-API-PROXY-3
def test_malformed_keys_and_key_spraying_are_still_limited_per_address() -> None:
    core = _core()
    assert [_status(core, "garbage") for _ in range(core.MAX_AUTH_FAILURES)] == [401] * core.MAX_AUTH_FAILURES
    assert _status(core, "garbage") == 429
    assert _status(core, "garbage", client="10.0.0.3") == 401  # another address
    sprayer = _core()
    statuses = [
        _status(sprayer, f"ndk_{index:08d}0000_{'w' * 40}", client="10.0.0.9")  # a new key id prefix each
        for index in range(sprayer.MAX_ADDRESS_KEY_FAILURES + 1)
    ]
    assert statuses[:-1] == [401] * sprayer.MAX_ADDRESS_KEY_FAILURES and statuses[-1] == 429


# ---- login starts ------------------------------------------------------------------------------------------------


# MUP-ONA T-API-PROXY-2
@pytest.mark.parametrize("backend", ["nodestro"])
def test_login_starts_are_counted_per_forwarded_client(
    database: Any, identity: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the proxy's address in FORWARDED_ALLOW_IPS (uvicorn's proxy headers), client A exhausting its login start
    budget leaves client B usable; without it both share the proxy's address and its budget."""
    import stub_oidc
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    monkeypatch.setenv("NODESTRO_LOGIN_STARTS_PER_MINUTE", "3")
    provider = stub_oidc.StubProvider()

    def starts(trusted: str, name: str) -> tuple[list[int], int]:
        app = ProxyHeadersMiddleware(
            test_identity.tenant_app(identity, database, provider, tmp_path / name), trusted_hosts=trusted
        )
        served = test_identity.Server(app, identity, provider, [])
        try:
            client_a, client_b = served.browser(), served.browser()
            client_a.headers["X-Forwarded-For"] = "203.0.113.10"
            client_b.headers["X-Forwarded-For"] = "203.0.113.20"
            a = [client_a.get("/api/auth/login").status_code for _ in range(4)]
            return a, client_b.get("/api/auth/login").status_code
        finally:
            for client in served.clients:
                client.__exit__(None, None, None)

    # TestClient's peer address is "testclient": the proxy, trusted like FORWARDED_ALLOW_IPS would trust it.
    assert starts("testclient", "trusted") == ([303, 303, 303, 429], 303)
    assert starts("127.0.0.1", "untrusted") == ([303, 303, 303, 429], 429)  # the shared proxy address
