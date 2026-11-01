"""Amazon Bedrock in the API: settings, the verification route, server tenants, prices, exports, diagnostics.

Offline only: botocore's HTTP send fails the test, AWS clients are stubbed with ``botocore.stub.Stubber`` and fake
credentials, and the server's own (ambient) ``AWS_*`` variables are set to canaries that must never be used.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import os
from pathlib import Path
from typing import Any
import uuid
import zipfile

from fastapi.testclient import TestClient
from nodestro_runtime import bedrock as bedrock_auth
from nodestro_runtime.bedrock import BedrockOperatorConfig, TenantRole
from nodestro_runtime.problems import NodestroError
import pytest
from test_published_api import publish

from nodestro_api.context import AWS_LOGGERS

from conftest import ScriptedClient
from test_flow import edge, save
from test_runs import agent_node

ACCOUNT = "123456789012"
ROLE_A = TenantRole(f"arn:aws:iam::{ACCOUNT}:role/nodestro-tenant-alpha", "ext-alpha-0001")
#: The server's own credentials: present in its environment, never to be used for (or shown to) a tenant.
AMBIENT = {
    "AWS_ACCESS_KEY_ID": "AKIAAMBIENTSERVER000",
    "AWS_SECRET_ACCESS_KEY": "ambient-server-secret-key",
    "AWS_SESSION_TOKEN": "ambient-server-session-token",
    "AWS_PROFILE": "operator-profile",
}
FAKE = {"aws_access_key_id": "AKIAIOSFODNN7EXAMPLE", "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/EXAMPLEKEY"}
MODEL = "bedrock:eu.anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.fixture(autouse=True)
def no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any HTTP request of botocore fails the test; no AWS configuration of this machine is read."""
    from botocore.httpsession import URLLib3Session

    def refuse(self: Any, request: Any) -> Any:
        raise AssertionError(f"a test tried to reach AWS: {request.method} {request.url}")

    monkeypatch.setattr(URLLib3Session, "send", refuse)
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/nodestro-test-aws-config")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent/nodestro-test-aws-credentials")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def _stubbed(service: str, region: str = "eu-central-1") -> tuple[Any, Any]:
    import boto3
    import botocore.session
    from botocore.stub import Stubber

    profile = os.environ.pop("AWS_PROFILE", None)  # the ambient canary of some tests names no real profile
    try:
        client = boto3.Session(botocore_session=botocore.session.Session()).client(service, region_name=region, **FAKE)
    finally:
        if profile is not None:
            os.environ["AWS_PROFILE"] = profile
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


class FakeSession:
    def __init__(self, clients: dict[str, Any]) -> None:
        self.clients = clients

    def client(self, service: str, **_: Any) -> Any:
        return self.clients[service]


def _identity() -> dict[str, Any]:
    return {"UserId": "AIDAEXAMPLE", "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/alice"}


def _desktop_session(monkeypatch: pytest.MonkeyPatch, *, denied: bool = False) -> list[Any]:
    """Replace the user's AWS chain with stubbed STS and Bedrock clients; returns the stubbers."""
    sts, sts_stub = _stubbed("sts")
    control, control_stub = _stubbed("bedrock")
    sts_stub.add_response("get_caller_identity", _identity(), {})
    if denied:
        control_stub.add_client_error(
            "list_foundation_models",
            service_error_code="AccessDeniedException",
            service_message=f"User: arn:aws:iam::{ACCOUNT}:user/alice is not authorized",
        )
    else:
        control_stub.add_response("list_foundation_models", {"modelSummaries": []}, {"byProvider": "anthropic"})
    session = FakeSession({"sts": sts, "bedrock": control})
    monkeypatch.setattr(bedrock_auth, "desktop_session", lambda settings: session)
    return [sts_stub, control_stub]


def _configure(client: TestClient, **bedrock: Any) -> dict[str, Any]:
    response = client.put("/api/settings", json={"providers": {"bedrock": bedrock}})
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["providers"] if item["id"] == "bedrock")


# ---- desktop: settings and verification ---------------------------------------------------------


def test_bedrock_settings_are_saved_without_credentials(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    status = next(item for item in client.get("/api/settings").json()["providers"] if item["id"] == "bedrock")
    assert status["configured"] is False and status["verification"]["state"] == "not_configured"
    assert status["auth_methods"] == ["default_chain", "profile"] and status["operator_role"] is None
    status = _configure(client, region="eu-central-1", auth="profile", profile_name="work", data_residency="eu_only")
    assert status["configured"] is True and status["verification"]["state"] == "configured"
    assert (status["region"], status["auth"], status["profile_name"]) == ("eu-central-1", "profile", "work")
    assert status["data_residency"] == "eu_only"
    # No key, no base URL, no declared capabilities for Bedrock; Bedrock fields for no other provider.
    for change, field in (({"api_key": "AKIAX"}, "api_key"), ({"base_url": "https://bedrock.example"}, "base_url")):
        refused = client.put("/api/settings", json={"providers": {"bedrock": change}})
        assert refused.status_code == 400 and refused.json()["code"] == "provider.field_not_supported"
        assert refused.json()["params"]["field"] == field
    refused = client.put("/api/settings", json={"providers": {"openai": {"region": "eu-central-1"}}})
    assert refused.json()["code"] == "provider.field_not_supported"
    refused = client.put("/api/settings", json={"providers": {"bedrock": {"region": "Frankfurt"}}})
    assert refused.json()["code"] == "provider.region_invalid"
    refused = client.put("/api/settings", json={"providers": {"bedrock": {"region": "us-east-1"}}})
    assert refused.json()["code"] == "provider.data_residency_region"  # eu_only allows only EU regions
    refused = client.put("/api/settings", json={"providers": {"bedrock": {"auth": "operator_role"}}})
    assert refused.json()["code"] == "provider.auth_not_allowed"


def test_verify_checks_free_calls_only_and_never_returns_the_aws_identity(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app()
    route = "/api/settings/providers/bedrock/verify"
    unconfigured = client.post(route)
    assert unconfigured.status_code == 200 and unconfigured.json()["verification"]["state"] == "not_configured"
    _configure(client, region="eu-central-1")
    stubbers = _desktop_session(monkeypatch)
    monkeypatch.setattr("nodestro_runtime.workspace.VERIFY_INTERVAL_SECONDS", 0.0)
    answer = client.post(route)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["id"] == "bedrock" and body["verification"]["state"] == "verified"
    assert body["verification"]["checked_at"] and body["verification"]["code"] is None
    for stubber in stubbers:
        stubber.assert_no_pending_responses()
    assert ACCOUNT not in answer.text and "alice" not in answer.text and "AKIA" not in answer.text
    # The result is kept in the status.
    status = next(item for item in client.get("/api/settings").json()["providers"] if item["id"] == "bedrock")
    assert status["verification"]["state"] == "verified"
    # A refusal by AWS becomes a stable code; its message (with the caller's ARN) is not returned.
    _desktop_session(monkeypatch, denied=True)
    failed = client.post(route, json={})
    assert failed.status_code == 200 and failed.json()["verification"] == {
        "state": "failed",
        "checked_at": failed.json()["verification"]["checked_at"],
        "code": "provider.aws_access_denied",
    }
    assert ACCOUNT not in failed.text and "arn:" not in failed.text
    # A model of another provider is refused before anything is called.
    other = client.post(route, json={"model": "anthropic:claude-opus-5-5"})
    assert other.status_code == 400 and other.json()["code"] == "provider.model_invalid"


def test_verify_with_a_model_keeps_its_colons_and_is_rate_limited(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app()
    _configure(client, region="eu-central-1")
    sts, sts_stub = _stubbed("sts")
    control, control_stub = _stubbed("bedrock")
    sts_stub.add_response("get_caller_identity", _identity(), {})
    model_id = MODEL.partition(":")[2]
    control_stub.add_response(
        "get_inference_profile",
        {
            "inferenceProfileName": "EU Claude Haiku",
            "inferenceProfileArn": f"arn:aws:bedrock:eu-central-1:{ACCOUNT}:inference-profile/{model_id}",
            "inferenceProfileId": model_id,
            "models": [{"modelArn": "arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-haiku-4-5"}],
            "status": "ACTIVE",
            "type": "SYSTEM_DEFINED",
        },
        {"inferenceProfileIdentifier": model_id},
    )
    monkeypatch.setattr(bedrock_auth, "desktop_session", lambda settings: FakeSession({"sts": sts, "bedrock": control}))
    answer = client.post("/api/settings/providers/bedrock/verify", json={"model": MODEL})
    assert answer.json()["verification"]["state"] == "verified", answer.text
    control_stub.assert_no_pending_responses()
    again = client.post("/api/settings/providers/bedrock/verify")
    assert again.status_code == 429 and again.json()["code"] == "provider.verify_rate_limited"
    assert 1 <= int(again.headers["Retry-After"]) <= 11


def test_bedrock_prices_need_their_regions_and_keep_colons(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    model = MODEL.partition(":")[2]
    rates = {"input_usd_per_mtok": "1", "output_usd_per_mtok": "5"}
    missing = client.put(f"/api/pricing/overrides/bedrock/{model}", json=rates)
    assert missing.status_code == 400 and "pricing.region_required" in missing.json()["params"]["issues"]
    stored = client.put(
        f"/api/pricing/overrides/bedrock/{model}", json={**rates, "aws_regions": ["eu-central-1", "eu-west-1"]}
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["model"] == model and stored.json()["tariffs"] == ["aws_region"]
    priced = next(item for item in client.get("/api/meta").json()["models"] if item["id"] == MODEL)
    assert priced["price_region"] == "eu-central-1" and priced["price_regions"] == ["eu-central-1", "eu-west-1"]
    other = client.put("/api/pricing/overrides/openai/gpt-5.5", json={**rates, "aws_regions": ["eu-central-1"]})
    assert other.status_code == 400 and other.json()["code"] == "pricing.invalid_price"
    arn = f"arn:aws:bedrock:eu-central-1:{ACCOUNT}:application-inference-profile/abc"
    arn_price = client.put(f"/api/pricing/overrides/bedrock/{arn}", json={**rates, "aws_regions": ["eu-central-1"]})
    assert arn_price.status_code == 200 and arn_price.json()["model"] == arn, arn_price.text
    assert client.delete(f"/api/pricing/overrides/bedrock/{arn}").status_code == 204
    assert client.delete(f"/api/pricing/overrides/bedrock/{model}").status_code == 204
    assert client.delete(f"/api/pricing/overrides/bedrock/{model}").status_code == 404


def test_the_connection_diagnosis_of_bedrock_is_its_verification(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app()
    unconfigured = client.post("/api/diagnostics/connection?provider=bedrock").json()
    assert (unconfigured["status"], unconfigured["code"]) == ("warning", "not_configured")
    _configure(client, region="eu-central-1")
    _desktop_session(monkeypatch, denied=True)
    denied = client.post("/api/diagnostics/connection?provider=bedrock").json()
    assert (denied["status"], denied["code"], denied["params"]["reason"]) == (
        "error",
        "key_rejected",
        "provider.aws_access_denied",
    )
    assert ACCOUNT not in json.dumps(denied)


def test_the_aws_sdk_never_logs_below_warning() -> None:
    for name in AWS_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_published_api_without_bedrock_settings_answers_503(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    _configure(client, region="eu-central-1")
    publication = publish(client, _bedrock_workflow(client))["publication"]
    _configure(client, region="")  # the settings are removed after publishing
    from test_published_api import PUBLISHED, auth, new_key

    token = new_key(client, publication["id"])
    answer = client.post(f"{PUBLISHED}/{publication['slug']}/runs", json={}, headers=auth(token))
    assert answer.status_code == 503 and answer.json()["error"]["code"] == "model.unavailable"
    assert answer.json()["error"]["params"]["provider"] == "bedrock"


# ---- export ------------------------------------------------------------------------------------


def _bedrock_workflow(client: TestClient) -> str:
    return save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER", model=MODEL, effort=None),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "writer"), edge("writer", "out")],
    )


def test_the_export_carries_the_region_and_never_aws_credentials(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in AMBIENT.items():
        monkeypatch.setenv(name, value)
    client = make_app()
    status = _configure(
        client, region="eu-central-1", auth="profile", profile_name="secret-profile-name", data_residency="eu_only"
    )
    assert status["configured"]
    publication = publish(client, _bedrock_workflow(client))["publication"]
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}}).json()
    codes = {item["code"]: item["level"] for item in check["problems"]}
    assert codes["export.aws_profile_not_exported"] == "warning" and "export.region_missing" not in codes
    assert "secret-profile-name" not in json.dumps(check)
    created = client.post(f"/api/publications/{publication['id']}/exports", json={"options": {}})
    assert created.status_code == 201, created.text
    data = client.get(f"/api/exports/{created.json()['id']}/download").content
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    root = next(iter(members)).split("/")[0]
    manifest = json.loads(members[f"{root}/workflow/manifest.json"])
    assert manifest["settings"] == {"bedrock": {"region": "eu-central-1", "data_residency": "eu_only"}}
    env = members[f"{root}/.env.example"].decode()
    assert "AWS_REGION=eu-central-1" in env and "BEDROCK_API_KEY" not in env and "BEDROCK_BASE_URL" not in env
    assert "`AWS_REGION` / `AWS_DEFAULT_REGION`" in members[f"{root}/README.md"].decode()
    values = [*AMBIENT.values(), "secret-profile-name", f"arn:aws:iam::{ACCOUNT}"]
    # Field names appear in the exported runtime's source code, never in its data and configuration files.
    fields = ["profile_name", "role_arn", "external_id", "aws_access_key_id", "aws_secret_access_key", "AKIA", "ASIA"]
    for name, content in members.items():
        text = content.decode("utf-8", errors="replace")
        for item in [*values, *([] if name.endswith(".py") else fields)]:
            assert item not in text, f"{item!r} in {name}"


def test_an_export_without_a_bedrock_region_is_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    _configure(client, region="eu-central-1")
    publication = publish(client, _bedrock_workflow(client))["publication"]
    _configure(client, region="")
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}}).json()
    assert {item["code"]: item["level"] for item in check["problems"]}["export.region_missing"] == "error"


def test_the_vendored_library_offers_bedrock() -> None:
    from nodestro_api.exporter import _vendor_pyproject

    text = _vendor_pyproject()
    assert 'bedrock = ["boto3' in text and "boto3" in text.split("all = ", 1)[1].split("\n", 1)[0]


def test_the_standalone_host_reads_the_region_from_the_manifest_or_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nodestro_api.standalone import Manifest, StandaloneHost

    host = StandaloneHost.__new__(StandaloneHost)
    host._client_factory = None
    host.manifest = Manifest.model_validate(
        {
            "publication_id": "p",
            "slug": "s",
            "name": "n",
            "release_id": "r",
            "release_number": 1,
            "release_digest": "d",
            "published_at": "2026-11-01T00:00:00+00:00",
            "settings": {"bedrock": {"region": "eu-central-1", "data_residency": "eu_only"}},
        }
    )
    connection = host.connection("bedrock")
    assert connection.bedrock is not None and connection.bedrock.region == "eu-central-1"
    assert connection.bedrock.auth == "default_chain" and connection.bedrock.data_residency == "eu_only"
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    assert host.connection("bedrock").bedrock.region == "eu-west-1"  # type: ignore[union-attr]
    monkeypatch.delenv("AWS_REGION")
    host.manifest.settings = {}
    with pytest.raises(NodestroError) as error:
        host.connection("bedrock")
    assert error.value.code == "provider.region_missing"


# ---- server: operator configuration and tenant isolation -----------------------------------------


def _server_env(tmp_path: Path, **changes: str) -> dict[str, str]:
    import base64

    environ = {
        "NODESTRO_PROFILE": "server",
        "NODESTRO_DATA_DIR": str(tmp_path / "server"),
        "NODESTRO_DATABASE_URL": "host=127.0.0.1 dbname=none",
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.test",
        "NODESTRO_OIDC_ISSUER": "https://login.example",
        "NODESTRO_OIDC_CLIENT_ID": "nodestro-studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
        "NODESTRO_SECRET_KEY": base64.b64encode(os.urandom(32)).decode(),
    }
    environ.update(changes)
    return environ


def test_the_server_reads_and_checks_the_operators_bedrock_settings(tmp_path: Path) -> None:
    pytest.importorskip("psycopg")
    from nodestro_api import server as command

    assert command.load(_server_env(tmp_path)).bedrock == BedrockOperatorConfig()
    roles = json.dumps({"alpha": {"role_arn": ROLE_A.role_arn, "external_id": ROLE_A.external_id}})
    loaded = command.load(
        _server_env(tmp_path, NODESTRO_BEDROCK_TENANT_ROLES=roles, NODESTRO_BEDROCK_DATA_RESIDENCY="eu_only")
    )
    assert loaded.bedrock is not None and set(loaded.bedrock.roles) == {"alpha"}
    assert loaded.bedrock.data_residency == "eu_only"
    assert ROLE_A.role_arn not in repr(loaded)
    file = tmp_path / "roles.json"
    file.write_text(roles, encoding="utf-8")
    from_file = command.load(_server_env(tmp_path, NODESTRO_BEDROCK_TENANT_ROLES_FILE=str(file)))
    assert from_file.bedrock is not None and from_file.bedrock.roles["alpha"] == ROLE_A
    bad = json.dumps({"alpha": {"role_arn": "arn:aws:iam::1:user/x", "external_id": "secret-external"}})
    for changes in (
        {"NODESTRO_BEDROCK_TENANT_ROLES": bad},
        {"NODESTRO_BEDROCK_TENANT_ROLES": roles, "NODESTRO_BEDROCK_TENANT_ROLES_FILE": str(file)},
        {"NODESTRO_BEDROCK_DATA_RESIDENCY": "everywhere"},
        {"NODESTRO_BEDROCK_ALLOWED_REGIONS": "us-east-1", "NODESTRO_BEDROCK_DATA_RESIDENCY": "eu_only"},
    ):
        with pytest.raises(NodestroError) as error:
            command.load(_server_env(tmp_path, **changes))
        assert error.value.code == "profile.invalid"
        (problem,) = error.value.params["problems"]
        assert problem["field"].startswith("NODESTRO_BEDROCK_")
        assert "secret-external" not in json.dumps(error.value.params) and "secret-external" not in str(error.value)


DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


@pytest.fixture
def database() -> Iterator[Any]:
    if not DSN:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    pytest.importorskip("psycopg")
    from nodestro_runtime.storage_postgres import PostgresDatabase
    import psycopg
    from psycopg import sql

    schema = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    opened = PostgresDatabase(DSN, schema=schema)
    opened.migrate()
    yield opened
    opened.close()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


class StsFactory:
    """The operator's STS client (stubbed): records which role each AssumeRole call asked for."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, region: str) -> Any:
        client, stubber = _stubbed("sts", region)
        expected = {
            "RoleArn": ROLE_A.role_arn,
            "RoleSessionName": "nodestro-alpha",
            "ExternalId": ROLE_A.external_id,
            "DurationSeconds": 1800,
        }
        stubber.add_response(
            "assume_role",
            {
                "Credentials": {
                    "AccessKeyId": "ASIASTSFAKEALPHA0001",
                    "SecretAccessKey": "sts-secret-alpha",
                    "SessionToken": "sts-token-alpha-0000000000000000",
                    "Expiration": datetime.now(timezone.utc) + timedelta(minutes=30),
                },
                "AssumedRoleUser": {
                    "AssumedRoleId": "AROAEXAMPLE:nodestro-alpha",
                    "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/nodestro-tenant-alpha/nodestro-alpha",
                },
            },
            expected,
        )
        self.calls.append({"region": region, **expected})
        return client


@dataclass
class Tenants:
    app: Any
    identity: Any
    provider: Any
    clients: list[TestClient]
    sts: StsFactory

    def member(self, tenant: str, subject: str, role: str) -> TestClient:
        import stub_oidc

        if self.identity.tenant(tenant) is None:
            self.identity.create_tenant(tenant, tenant.title())
        user = self.identity.login(stub_oidc.ISSUER, subject)
        if role != "owner" and not [m for m in self.identity.members(tenant) if m.role == "owner"]:
            self.identity.set_role(tenant, self.identity.login(stub_oidc.ISSUER, f"{tenant}-owner").id, "owner")
        self.identity.set_role(tenant, user.id, role)
        browser = TestClient(self.app, base_url=stub_oidc.ORIGIN, follow_redirects=False)
        browser.headers["Origin"] = stub_oidc.ORIGIN
        browser.__enter__()
        self.clients.append(browser)
        started = browser.get(f"/api/auth/login?tenant={tenant}")
        answer = browser.get(self.provider.authorize(started.headers["location"], subject))
        assert answer.status_code == 303, answer.text
        return browser


@pytest.fixture
def tenants(database: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tenants]:
    pytest.importorskip("jwt")
    import httpx
    from nodestro_runtime.identity_store import IdentityStore
    import stub_oidc

    from nodestro_api.identity import OidcClient, OidcSettings
    from nodestro_api.server_app import TenantComposition, create_tenant_app

    for name, value in AMBIENT.items():  # the server's own AWS chain, which no tenant may use
        monkeypatch.setenv(name, value)
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
    sts = StsFactory()
    config = BedrockOperatorConfig(roles={"alpha": ROLE_A}, sts_client_factory=sts)
    scripted = ScriptedClient({})
    app = create_tenant_app(
        TenantComposition(
            database=database,
            identity=identity,
            oidc=oidc,
            origin=stub_oidc.ORIGIN,
            data_root=tmp_path / "server",
            client_factory=lambda _provider: scripted,
            integrations=[],
            bedrock=config,
        )
    )
    built = Tenants(app, identity, provider, [], sts)
    yield built
    for client in built.clients:
        client.__exit__(None, None, None)


def _status(client: TestClient) -> dict[str, Any]:
    return next(item for item in client.get("/api/settings").json()["providers"] if item["id"] == "bedrock")


def test_each_tenant_uses_only_its_own_operator_role(tenants: Tenants, monkeypatch: pytest.MonkeyPatch) -> None:
    alpha = tenants.member("alpha", "alice", "admin")
    beta = tenants.member("beta", "bob", "admin")
    # beta has no role: Bedrock is not available, and its settings cannot be changed.
    status = _status(beta)
    assert status["operator_role"] is False and status["verification"]["code"] == "provider.bedrock_not_available"
    refused = beta.put("/api/settings", json={"providers": {"bedrock": {"region": "eu-central-1"}}})
    assert refused.status_code == 400 and refused.json()["code"] == "provider.bedrock_not_available"
    failed = beta.post("/api/settings/providers/bedrock/verify").json()
    assert failed["verification"]["code"] == "provider.bedrock_not_available"
    assert tenants.sts.calls == []
    # alpha signs in with its role only (operator_role is the one method); profiles are a desktop setting.
    status = _status(alpha)
    assert status["operator_role"] is True and status["auth_methods"] == ["operator_role"]
    assert alpha.put("/api/settings", json={"providers": {"bedrock": {"profile_name": "x"}}}).status_code == 400
    status = _configure(alpha, region="eu-central-1")
    assert status["configured"] and status["auth"] == "operator_role"
    # Verification: AssumeRole with alpha's role (never the server's credentials), then STS and Bedrock with the
    # assumed role's credentials.
    seen: list[str] = []
    sts, sts_stub = _stubbed("sts")
    control, control_stub = _stubbed("bedrock")
    sts_stub.add_response("get_caller_identity", _identity(), {})
    control_stub.add_response("list_foundation_models", {"modelSummaries": []}, {"byProvider": "anthropic"})

    def session(self: Any) -> Any:
        credentials = self.credentials.get_frozen_credentials()
        seen.append(credentials.access_key)
        return FakeSession({"sts": sts, "bedrock": control})

    monkeypatch.setattr(bedrock_auth.TenantCredentials, "session", session)
    verified = alpha.post("/api/settings/providers/bedrock/verify")
    assert verified.status_code == 200 and verified.json()["verification"]["state"] == "verified", verified.text
    assert seen == ["ASIASTSFAKEALPHA0001"]
    assert [call["RoleArn"] for call in tenants.sts.calls] == [ROLE_A.role_arn]
    assert tenants.sts.calls[0]["region"] == "eu-central-1"
    for answer in (verified, alpha.get("/api/settings"), alpha.get("/api/meta")):
        for secret in (ROLE_A.role_arn, ROLE_A.external_id, ACCOUNT, *AMBIENT.values()):
            assert secret not in answer.text
    # Only admins may check (a settings action); members without admin rights are refused.
    viewer = tenants.member("alpha", "victor", "viewer")
    assert viewer.post("/api/settings/providers/bedrock/verify").status_code == 403
    # beta still cannot use alpha's role.
    assert _status(beta)["operator_role"] is False
