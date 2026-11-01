"""Logins, sessions, roles and tenant isolation of the multi-tenant studio API (needs PostgreSQL).

The app is built by ``create_tenant_app`` against a stub
OpenID Connect provider (``stub_oidc``). The PostgreSQL cases need ``NODESTRO_TEST_POSTGRES``; with
``NODESTRO_REQUIRE_POSTGRES=1`` (CI) a missing database fails instead of skipping.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Any
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import ScriptedClient, text

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from cryptography.hazmat.primitives.asymmetric import rsa
from nodestro_runtime.identity_store import IdentityStore
from nodestro_runtime.storage_postgres import PostgresDatabase
import stub_oidc

from nodestro_api.identity import LOGIN_COOKIE, SESSION_COOKIE, OidcClient, OidcSettings, safe_return_path
from nodestro_api.server_app import TenantComposition, create_tenant_app

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


@pytest.fixture
def database() -> Iterator[PostgresDatabase]:
    if not DSN:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    import psycopg
    from psycopg import sql

    schema = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    opened = PostgresDatabase(DSN, schema=schema)
    opened.migrate()
    yield opened
    opened.close()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@dataclass
class Server:
    app: Any
    identity: IdentityStore
    provider: stub_oidc.StubProvider
    clients: list[TestClient]

    def browser(self) -> TestClient:
        """A browser of its own (own cookies) on the studio's origin."""
        client = TestClient(self.app, base_url=stub_oidc.ORIGIN, follow_redirects=False)
        client.headers["Origin"] = stub_oidc.ORIGIN
        client.__enter__()
        self.clients.append(client)
        return client

    def log_in(self, browser: TestClient, subject: str, query: str = "", **claims: Any) -> httpx.Response:
        started = browser.get(f"/api/auth/login{query}")
        assert started.status_code == 303, started.text
        return browser.get(self.provider.authorize(started.headers["location"], subject, **claims))

    def member(self, tenant: str, subject: str, role: str) -> TestClient:
        """A browser logged in as ``subject`` with ``role`` in ``tenant`` (created when missing)."""
        if self.identity.tenant(tenant) is None:
            self.identity.create_tenant(tenant, tenant.title())
        user = self.identity.login(stub_oidc.ISSUER, subject)
        owners = [m for m in self.identity.members(tenant) if m.role == "owner"]
        if role != "owner" and not owners:
            self.identity.set_role(tenant, self.identity.login(stub_oidc.ISSUER, f"{tenant}-owner").id, "owner")
        self.identity.set_role(tenant, user.id, role)  # type: ignore[arg-type]
        browser = self.browser()
        answer = self.log_in(browser, subject, f"?tenant={tenant}")
        assert answer.status_code == 303, answer.text
        return browser


@pytest.fixture
def server(database: PostgresDatabase, tmp_path: Path) -> Iterator[Server]:
    provider = stub_oidc.StubProvider()
    oidc = OidcClient(
        OidcSettings(
            issuer=stub_oidc.ISSUER,
            client_id=stub_oidc.CLIENT_ID,
            client_secret=stub_oidc.CLIENT_SECRET,
            redirect_uri=stub_oidc.REDIRECT_URI,
        ),
        http=httpx.Client(transport=provider.transport()),
    )
    identity = IdentityStore(database)
    scripted = ScriptedClient({"ALPHA": [text("Alpha result")] * 5})
    app = create_tenant_app(
        TenantComposition(
            database=database,
            identity=identity,
            oidc=oidc,
            origin=stub_oidc.ORIGIN,
            data_root=tmp_path / "server",
            client_factory=lambda _provider: scripted,
            integrations=[],
        )
    )
    built = Server(app, identity, provider, [])
    yield built
    for client in built.clients:
        client.__exit__(None, None, None)


# ---- logging in ---------------------------------------------------------------------------------


def test_anonymous_requests_are_refused(server: Server) -> None:
    browser = server.browser()
    for path in ("/api/workflows", "/api/runs", "/api/knowledge", "/api/settings", "/api/auth/me"):
        answer = browser.get(path)
        assert answer.status_code == 401, path
        assert answer.json()["code"] == "access.login_required"
    assert browser.post("/api/workflows", json={"name": "x", "nodes": [], "edges": []}).status_code == 401
    assert browser.get("/api/runs/run_x/events").status_code == 401
    assert browser.get("/api/health").status_code == 200


def test_a_member_logs_in_and_gets_a_secure_session_cookie(server: Server) -> None:
    server.identity.create_tenant("acme", "ACME")
    user = server.identity.login(stub_oidc.ISSUER, "alice")
    server.identity.set_role("acme", user.id, "owner")
    browser = server.browser()
    answer = server.log_in(browser, "alice", "?return_to=/runs", email="alice@example.com", email_verified=True)
    assert answer.status_code == 303 and answer.headers["location"] == "/runs"
    cookie = answer.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
        assert attribute.lower() in cookie.lower(), attribute
    assert "domain" not in cookie.lower()
    me = browser.get("/api/auth/me").json()
    assert (me["user_id"], me["tenant_id"], me["role"], me["email"]) == (user.id, "acme", "owner", "alice@example.com")
    assert me["permissions"] == ["admin", "edit", "read", "run"]
    assert browser.get("/api/workflows").status_code == 200


def test_a_login_alone_grants_nothing(server: Server) -> None:
    browser = server.browser()
    answer = server.log_in(browser, "stranger")
    assert answer.status_code == 403 and answer.json()["code"] == "identity.no_tenant"
    assert SESSION_COOKIE not in answer.headers.get("set-cookie", "")
    assert browser.get("/api/workflows").status_code == 401


def test_logout_ends_the_session(server: Server) -> None:
    browser = server.member("acme", "alice", "editor")
    token = browser.cookies.get(SESSION_COOKIE)
    assert browser.post("/api/auth/logout").status_code == 204
    assert browser.get("/api/workflows").status_code == 401
    # The old token is worthless even if someone kept it.
    thief = server.browser()
    thief.cookies.set(SESSION_COOKIE, token or "", domain="studio.test")
    assert thief.get("/api/workflows").status_code == 401


def test_removing_a_member_ends_access_at_once(server: Server) -> None:
    browser = server.member("acme", "alice", "editor")
    assert browser.get("/api/workflows").status_code == 200
    user = server.identity.login(stub_oidc.ISSUER, "alice")
    server.identity.remove_member("acme", user.id)
    assert browser.get("/api/workflows").status_code == 401


def test_login_states_are_used_once(server: Server) -> None:
    server.member("acme", "alice", "viewer")
    browser = server.browser()
    started = browser.get("/api/auth/login")
    callback = server.provider.authorize(started.headers["location"], "alice")
    assert browser.get(callback).status_code == 303
    replay = server.browser()
    answer = replay.get(callback)
    assert answer.status_code == 400 and answer.json()["code"] == "identity.login_expired"
    assert replay.get("/api/auth/callback?code=x&state=forged").json()["code"] == "identity.login_expired"


@pytest.mark.parametrize(
    "tamper",
    [
        {"aud": "another-client"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 3600},
        {"nonce": "another-login"},
        {"sub": None},
    ],
    ids=["audience", "issuer", "expired", "nonce", "no-subject"],
)
def test_invalid_id_tokens_are_refused(server: Server, tamper: dict[str, Any]) -> None:
    server.member("acme", "alice", "owner")
    browser = server.browser()
    server.provider.tamper = tamper
    answer = server.log_in(browser, "alice")
    assert answer.status_code == 400 and answer.json()["code"] == "identity.login_failed"
    assert browser.get("/api/workflows").status_code == 401


def test_forged_and_unsigned_tokens_are_refused(server: Server) -> None:
    server.member("acme", "alice", "owner")
    browser = server.browser()
    server.provider.forge_with = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert server.log_in(browser, "alice").json()["code"] == "identity.login_failed"
    import jwt

    unsigned = jwt.encode({"iss": stub_oidc.ISSUER, "sub": "alice"}, key=None, algorithm="none")
    client = OidcClient(
        OidcSettings(stub_oidc.ISSUER, stub_oidc.CLIENT_ID, stub_oidc.CLIENT_SECRET, stub_oidc.REDIRECT_URI),
        http=httpx.Client(transport=server.provider.transport()),
    )
    from nodestro_api.identity import LoginError

    with pytest.raises(LoginError):
        client.verify(unsigned, nonce="x")


def test_a_wrong_pkce_verifier_is_refused_by_the_provider(server: Server) -> None:
    server.member("acme", "alice", "owner")
    browser = server.browser()
    started = browser.get("/api/auth/login")
    callback = server.provider.authorize(started.headers["location"], "alice")
    code = callback.split("code=")[1].split("&")[0]
    server.provider.grants[code].challenge = "not-the-challenge"
    assert browser.get(callback).json()["code"] == "identity.login_failed"


def _settings(issuer: str) -> OidcSettings:
    return OidcSettings(issuer=issuer, client_id="c", client_secret="s", redirect_uri="https://studio.test/cb")


@pytest.mark.parametrize("issuer", ["http://login.example", "ftp://login.example", "login.example"])
def test_the_identity_provider_is_reached_over_https(issuer: str) -> None:
    with pytest.raises(ValueError, match="https"):
        OidcClient(_settings(issuer))


def test_only_a_loopback_development_provider_may_use_http() -> None:
    def metadata(issuer: str, endpoint: str) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{endpoint}/auth",
                    "token_endpoint": f"{endpoint}/token",
                    "jwks_uri": f"{endpoint}/keys",
                },
            )

        return httpx.MockTransport(handle)

    local = "http://localhost:5556/dex"
    client = OidcClient(_settings(local), http=httpx.Client(transport=metadata(local, local)))
    assert client.metadata()["token_endpoint"] == f"{local}/token"
    remote = "https://login.example"
    downgraded = OidcClient(_settings(remote), http=httpx.Client(transport=metadata(remote, "http://localhost:5556")))
    with pytest.raises(Exception, match="not an https URL"):
        downgraded.metadata()


@pytest.mark.parametrize(
    "target", ["https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)", "/ok\r\nX: y"]
)
def test_the_return_path_never_leaves_the_studio(target: str) -> None:
    assert safe_return_path(target) == "/"
    assert safe_return_path("/runs?tab=1") == "/runs?tab=1"


def test_cross_site_changes_are_refused(server: Server) -> None:
    browser = server.member("acme", "alice", "owner")
    answer = browser.post(
        "/api/workflows", json={"name": "x", "nodes": [], "edges": []}, headers={"Origin": "https://evil.example"}
    )
    assert answer.status_code == 403 and answer.json()["code"] == "access.cross_site"
    del browser.headers["Origin"]
    assert browser.post("/api/workflows", json={"name": "x", "nodes": [], "edges": []}).status_code == 403
    assert browser.get("/api/workflows").json() == []


# ---- bootstrap -------------------------------------------------------------------------------------


def test_a_setup_link_makes_the_first_owner_once(server: Server) -> None:
    server.identity.create_tenant("acme", "ACME")
    token = server.identity.create_bootstrap_token("acme")
    first = server.browser()
    assert server.log_in(first, "founder", f"?setup={token}").status_code == 303
    assert first.get("/api/auth/me").json()["role"] == "owner"
    second = server.browser()
    answer = server.log_in(second, "latecomer", f"?setup={token}")
    assert answer.status_code == 403 and answer.json()["code"] == "identity.bootstrap_invalid"
    assert second.get("/api/workflows").status_code == 401


# ---- roles -------------------------------------------------------------------------------------------


WORKFLOW = {"name": "Roles", "nodes": [], "edges": []}
ROLE_MATRIX = {
    # role: (read, edit, run, admin, install)
    "viewer": (True, False, False, False, False),
    "operator": (True, False, True, False, False),
    "editor": (True, True, True, False, False),
    "admin": (True, True, True, True, False),
    "owner": (True, True, True, True, False),
}


@pytest.mark.parametrize("role", list(ROLE_MATRIX))
def test_roles_grant_their_permissions(server: Server, role: str) -> None:
    browser = server.member("acme", f"user-{role}", role)
    can_read, can_edit, can_run, can_admin, can_install = ROLE_MATRIX[role]
    assert (browser.get("/api/workflows").status_code == 200) is can_read
    created = browser.post("/api/workflows", json=WORKFLOW)
    assert (created.status_code == 201) is can_edit, created.text
    run = browser.post("/api/workflows/wf_missing/runs", json={"input": "x"})
    assert (run.status_code != 403) is can_run, run.text
    settings = browser.put("/api/settings", json={})
    assert (settings.status_code != 403) is can_admin, settings.text
    for method, path in (("GET", "/api/diagnostics"), ("GET", "/api/backup/local"), ("GET", "/api/api-network")):
        answer = browser.request(method, path)
        assert (answer.status_code != 403) is can_install, path
        if not can_install:
            assert answer.json()["params"] == {"permission": "install"}


# ---- tenant isolation ----------------------------------------------------------------------------------


def _wait(browser: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = browser.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _resources(alice: TestClient) -> dict[str, str]:
    """Create one resource of every kind in Alice's tenant; returns the ids."""
    flow = {
        "name": "Alpha flow",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "ALPHA"}},
            {"id": "out", "type": "output"},
        ],
        "edges": [{"id": "e1", "source": "start", "target": "a"}, {"id": "e2", "source": "a", "target": "out"}],
    }
    workflow = alice.post("/api/workflows", json=flow)
    assert workflow.status_code == 201, workflow.text
    workflow_id = workflow.json()["id"]
    run = alice.post(f"/api/workflows/{workflow_id}/runs", json={"input": "go"})
    assert run.status_code == 201, run.text
    finished = _wait(alice, run.json()["id"])
    assert finished["status"] == "succeeded", finished
    knowledge = alice.post("/api/knowledge", files={"files": ("secret.txt", b"Alpha secret contract", "text/plain")})
    assert knowledge.status_code in (200, 201), knowledge.text
    upload = alice.post("/api/inputs", files={"files": ("input.txt", b"Alpha input", "text/plain")})
    assert upload.status_code in (200, 201), upload.text
    memory = alice.put("/api/memory/Team/file", json={"path": "/memories/a.md", "content": "alpha"})
    assert memory.status_code in (200, 204), memory.text
    return {
        "workflow": workflow_id,
        "run": run.json()["id"],
        "knowledge": knowledge.json()[0]["id"],
        "input": upload.json()[0]["file"]["id"],
    }


def test_no_tenant_sees_another_tenants_resources(server: Server) -> None:
    alice = server.member("acme", "alice", "owner")
    bob = server.member("globex", "bob", "owner")
    ids = _resources(alice)
    # Alice sees her resources...
    for path in (f"/api/workflows/{ids['workflow']}", f"/api/runs/{ids['run']}", f"/api/inputs/{ids['input']}"):
        assert alice.get(path).status_code == 200, path
    # ...Bob gets 404 for every one of them, also streams, file content, exports and changes.
    hidden = [
        ("GET", f"/api/workflows/{ids['workflow']}"),
        ("GET", f"/api/workflows/{ids['workflow']}/versions"),
        ("GET", f"/api/workflows/{ids['workflow']}/export"),
        ("PUT", f"/api/workflows/{ids['workflow']}"),
        ("DELETE", f"/api/workflows/{ids['workflow']}"),
        ("POST", f"/api/workflows/{ids['workflow']}/runs"),
        ("GET", f"/api/runs/{ids['run']}"),
        ("GET", f"/api/runs/{ids['run']}/events"),
        ("GET", f"/api/runs/{ids['run']}/export"),
        ("GET", f"/api/runs/{ids['run']}/files"),
        ("GET", f"/api/runs/{ids['run']}/inputs"),
        ("GET", f"/api/knowledge/{ids['knowledge']}/content"),
        ("DELETE", f"/api/knowledge/{ids['knowledge']}"),
        ("GET", f"/api/inputs/{ids['input']}"),
        ("GET", f"/api/inputs/{ids['input']}/content"),
        ("GET", f"/api/inputs/{ids['input']}/text"),
        ("GET", "/api/memory/Team/file?path=/memories/a.md"),
    ]
    for method, path in hidden:
        body = (
            {"name": "x", "nodes": [], "edges": []}
            if method == "PUT"
            else ({"input": "x"} if method == "POST" else None)
        )
        answer = bob.request(method, path, json=body)
        assert answer.status_code == 404, (method, path, answer.status_code, answer.text[:200])
    # Lists show nothing of Alice's tenant.
    assert bob.get("/api/workflows").json() == []
    for path in ("/api/runs", "/api/inputs", "/api/knowledge"):
        listed = bob.get(path)
        assert listed.status_code == 200, path
        assert not any(value in listed.text for value in ids.values()), path
    assert bob.get("/api/knowledge").json() == []
    assert bob.get("/api/memory").json() == []
    # "Used in" (UX-7) answers from the request's tenant only.
    template = {"agent": {"name": "Alpha", "knowledge": {"file_ids": [ids["knowledge"]]}, "tools": ["calculator"]}}
    assert alice.post("/api/agents/templates", json=template).status_code == 201
    for kind in ("knowledge", "tool", "memory"):
        assert bob.get("/api/used-by", params={"kind": kind}).json() == [], kind
    assert bob.get("/api/used-by", params={"kind": "knowledge", "ref": ids["knowledge"]}).json() == []
    used = alice.get("/api/used-by", params={"kind": "knowledge"}).json()
    assert [(item["ref"], [user["name"] for user in item["users"]]) for item in used] == [(ids["knowledge"], ["Alpha"])]
    # And Alice's data is untouched by Bob's attempts.
    assert alice.get(f"/api/workflows/{ids['workflow']}").status_code == 200
    assert alice.get(f"/api/knowledge/{ids['knowledge']}/content").content == b"Alpha secret contract"


def test_switching_tenants_needs_a_membership(server: Server) -> None:
    alice = server.member("acme", "alice", "owner")
    server.member("globex", "bob", "owner")
    answer = alice.post("/api/auth/switch", json={"tenant_id": "globex"})
    assert answer.status_code == 404 and answer.json()["code"] == "identity.not_a_member"
    assert alice.get("/api/auth/me").json()["tenant_id"] == "acme"
    user = server.identity.login(stub_oidc.ISSUER, "alice")
    server.identity.set_role("globex", user.id, "viewer")
    old = alice.cookies.get(SESSION_COOKIE)
    assert alice.post("/api/auth/switch", json={"tenant_id": "globex"}).status_code == 204
    me = alice.get("/api/auth/me").json()
    assert (me["tenant_id"], me["role"]) == ("globex", "viewer")
    assert server.identity.session(old or "") is None  # the old session ended


# ---- audit hardening: login binding, logs, health, API reference, attribution --------------------


def test_a_callback_url_from_another_browser_logs_nobody_in(server: Server) -> None:
    """A-01: the callback URL of a login that Mallory started is worthless in Victor's browser (login CSRF)."""
    server.identity.create_tenant("evil", "Evil")
    server.identity.set_role("evil", server.identity.login(stub_oidc.ISSUER, "mallory").id, "owner")
    attacker = server.browser()
    started = attacker.get("/api/auth/login?tenant=evil")
    assert started.status_code == 303
    cookie = started.headers["set-cookie"]
    assert cookie.startswith(f"{LOGIN_COOKIE}=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=600"):
        assert attribute.lower() in cookie.lower(), attribute
    callback = server.provider.authorize(started.headers["location"], "mallory")
    victim = server.browser()
    answer = victim.get(callback)
    assert answer.status_code == 400 and answer.json()["code"] == "identity.login_expired"
    assert f"{SESSION_COOKIE}=" not in answer.headers.get("set-cookie", "")
    assert victim.get("/api/auth/me").status_code == 401
    # The state was taken: the attacker cannot use the same callback any more either.
    assert attacker.get(callback).status_code == 400


def test_the_login_cookie_must_match_and_is_cleared_after_the_callback(server: Server) -> None:
    server.member("acme", "alice", "viewer")
    browser = server.browser()
    started = browser.get("/api/auth/login")
    callback = server.provider.authorize(started.headers["location"], "alice")
    browser.cookies.set(LOGIN_COOKIE, "another-state", domain="studio.test")
    refused = browser.get(callback)
    assert refused.status_code == 400 and refused.json()["code"] == "identity.login_expired"
    assert f'{LOGIN_COOKIE}=""' in refused.headers["set-cookie"]  # cleared on failure
    again = browser.get("/api/auth/login")
    answer = browser.get(server.provider.authorize(again.headers["location"], "alice"))
    assert answer.status_code == 303
    cookies = answer.headers.get_list("set-cookie")
    assert any(item.startswith(f"{SESSION_COOKIE}=") for item in cookies)
    assert any(item.startswith(f'{LOGIN_COOKIE}=""') for item in cookies)  # cleared on success
    assert browser.get("/api/auth/me").status_code == 200


def test_setup_tokens_and_sign_in_codes_never_reach_the_access_log(server: Server) -> None:
    """D-02: the server's access log does not keep the bootstrap token, nor the provider's code and state."""
    import io
    import logging

    from uvicorn.logging import AccessFormatter

    secret = "BOOTSTRAP-TOKEN-EXAMPLE-0123456789"
    access = logging.getLogger("uvicorn.access")
    written = io.StringIO()
    handler = logging.StreamHandler(written)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    level = access.level
    access.setLevel(logging.INFO)
    access.addHandler(handler)
    try:
        for path in (
            f"/api/auth/login?setup={secret}",
            f"/api/auth/login?return_to=/runs&setup={secret}",
            f"/api/auth/callback?code={secret}&state={secret}",
            f"/api/mcp/oauth/callback?state={secret}&code={secret}",
        ):
            access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", path, "1.1", 303)
    finally:
        access.removeHandler(handler)
        access.setLevel(level)
    text_written = written.getvalue()
    assert secret not in text_written
    assert "/api/auth/login?setup=[redacted]" in text_written
    assert "return_to=/runs&setup=[redacted]" in text_written
    assert "/api/auth/callback?code=[redacted]&state=[redacted]" in text_written


def test_the_server_health_check_reports_the_database(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-10: ``/api/health`` answers 503 when this process cannot reach its database."""
    browser = server.browser()
    answer = browser.get("/api/health")
    assert answer.status_code == 200 and answer.json() == {"status": "ok", "database": "ok"}
    check = server.app.state.health_check
    check.timeout = 0.3

    def unreachable(**_kwargs: Any) -> bool:
        return False  # ``ping`` reports a lost connection as False; it never raises

    monkeypatch.setattr(check.database, "ping", unreachable)
    answer = browser.get("/api/health")
    assert answer.status_code == 503 and answer.json() == {"status": "unavailable", "database": "unreachable"}

    def hanging(**_kwargs: Any) -> bool:
        time.sleep(2)
        return True

    monkeypatch.setattr(check.database, "ping", hanging)
    began = time.monotonic()
    assert browser.get("/api/health").status_code == 503
    assert browser.get("/api/health").status_code == 503  # the hanging check is not started twice
    assert time.monotonic() - began < 1.8
    monkeypatch.undo()
    time.sleep(2.1)
    assert browser.get("/api/health").status_code == 200


def test_the_api_reference_needs_a_login_on_a_server(server: Server) -> None:
    """A-09: the OpenAPI document and the reference page are not anonymous on a server."""
    anonymous = server.browser()
    for path in ("/api/openapi.json", "/api/docs"):
        answer = anonymous.get(path)
        assert answer.status_code == 401 and answer.json()["code"] == "access.login_required", path
    viewer = server.member("acme", "victor", "viewer")
    document = viewer.get("/api/openapi.json")
    assert document.status_code == 200 and "/api/workflows" in document.json()["paths"]
    page = viewer.get("/api/docs")
    assert page.status_code == 200 and "/api/openapi.json" in page.text


APPROVAL_FLOW = {
    "name": "Needs approval",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "check", "type": "approval", "label": "Release payment"},
        {"id": "a", "type": "agent", "label": "A", "agent": {"name": "A", "system_prompt": "ALPHA"}},
        {"id": "out", "type": "output"},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "check"},
        {"id": "e2", "source": "check", "target": "a", "source_handle": "approved"},
        {"id": "e3", "source": "a", "target": "out"},
    ],
}


def test_approvals_and_uploads_name_the_member_who_made_them(server: Server) -> None:
    """A-06: the approval record and the uploaded input name the user, not ``local``."""
    owner = server.member("acme", "alice", "owner")
    operator = server.member("acme", "oscar", "operator")
    oscar = server.identity.login(stub_oidc.ISSUER, "oscar").id
    alice = server.identity.login(stub_oidc.ISSUER, "alice").id
    workflow = owner.post("/api/workflows", json=APPROVAL_FLOW)
    assert workflow.status_code == 201, workflow.text
    run_id = owner.post(f"/api/workflows/{workflow.json()['id']}/runs", json={"input": "pay"}).json()["id"]
    deadline = time.monotonic() + 15
    while owner.get(f"/api/runs/{run_id}").json()["status"] != "paused":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    decided = operator.post(f"/api/runs/{run_id}/approvals/check", json={"decision": "approved", "comment": "ok"})
    assert decided.status_code == 200, decided.text
    record = owner.get(f"/api/runs/{run_id}").json()["nodes"]["check"]["approval"]
    assert record["decided_by"] == oscar
    upload = owner.post("/api/inputs", files={"files": ("input.txt", b"Alpha input", "text/plain")})
    assert upload.status_code == 201, upload.text
    file_id = upload.json()[0]["file"]["id"]
    assert server.app.state.tenants.get("acme").inputs.get(file_id).uploaded_by == alice
