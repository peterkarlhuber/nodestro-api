"""The price API (PRICING-1 N6, N8, N10): catalog, overrides, sync, reviews, usage, billing keys, reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.pricing_sync import PricingSyncService, SnapshotFileSource
from orkexa import UsageRecord
from orkexa.pricing import BUILTIN_VERSION, ModelPrice, PriceSnapshot, TokenRates
import pytest

from nodestro_api.app import create_app
from nodestro_api.context import AppConfig
from nodestro_api.pricing_setup import PRICE_REFERENCE_TENANT, PriceSyncConfig

MakeApp = Callable[..., TestClient]


def _ctx(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


def _snapshot_file(tmp_path: Path, version: str, *prices: ModelPrice) -> Path:
    path = tmp_path / f"{version}.json"
    path.write_text(json.dumps(PriceSnapshot(version, prices).to_dict()), encoding="utf-8")
    return path


def _price(model: str, input_rate: str, output_rate: str, **extra: Any) -> ModelPrice:
    return ModelPrice(
        provider="openai", model=model, rates=TokenRates.of(input_rate, output_rate), source="file", **extra
    )


def test_the_catalog_lists_prices_with_status_quality_and_source(make_app: MakeApp) -> None:
    client = make_app()
    entries = client.get("/api/pricing").json()
    sonnet = next(item for item in entries if item["model"] == "claude-sonnet-5-5")
    assert sonnet["provider"] == "anthropic" and sonnet["quality"] == "verified" and sonnet["source"] == "builtin"
    assert sonnet["status"] in {"current", "stale"} and sonnet["version"] == BUILTIN_VERSION
    status = client.get("/api/pricing/status").json()
    assert status["active_version"] == BUILTIN_VERSION
    assert status["reference_read_only"] is False and status["sync_enabled"] is False
    assert status["price_mode"] == "standard"
    assert status["warn_after_hours"] == 48 and status["critical_after_days"] == 7
    assert status["review_change_percent"] == 20


def test_overrides_win_keep_history_and_can_be_removed(make_app: MakeApp) -> None:
    client = make_app()
    answer = client.put(
        "/api/pricing/overrides/openai/gpt-contract",
        json={"input_usd_per_mtok": "1.5", "output_usd_per_mtok": "6", "note": "ACME contract"},
    )
    assert answer.status_code == 200, answer.text
    entry = answer.json()
    assert entry["overridden"] is True and entry["status"] == "custom" and entry["quality"] == "contract"
    assert entry["input_usd_per_mtok"] == "1.5"
    kinds = [event["kind"] for event in client.get("/api/pricing/history").json()]
    assert "override_set" in kinds
    assert client.delete("/api/pricing/overrides/openai/gpt-contract").status_code == 204
    assert client.delete("/api/pricing/overrides/openai/gpt-contract").status_code == 404
    assert "override_removed" in [event["kind"] for event in client.get("/api/pricing/history").json()]


def test_invalid_overrides_are_refused(make_app: MakeApp) -> None:
    client = make_app()
    negative = client.put(
        "/api/pricing/overrides/openai/x", json={"input_usd_per_mtok": "-1", "output_usd_per_mtok": "1"}
    )
    assert negative.status_code == 422
    unknown = client.put(
        "/api/pricing/overrides/nobody/x", json={"input_usd_per_mtok": "1", "output_usd_per_mtok": "1"}
    )
    assert unknown.status_code == 400 and unknown.json()["code"] == "pricing.invalid_price"


def test_sync_activates_new_prices_and_holds_big_changes_for_review(make_app: MakeApp, tmp_path: Path) -> None:
    client = make_app()
    ctx = _ctx(client)
    path = _snapshot_file(tmp_path, "f1", _price("gpt-test", "2", "8"))
    ctx.price_sync = PricingSyncService(ctx.pricing, [SnapshotFileSource(path)])
    report = client.post("/api/pricing/sync").json()
    assert report["ran"] is True and report["sources"][0]["outcome"] == "activated"
    assert any(item["model"] == "gpt-test" for item in client.get("/api/pricing").json())

    path.write_text(json.dumps(PriceSnapshot("f2", (_price("gpt-test", "4", "8"),)).to_dict()), encoding="utf-8")
    report = client.post("/api/pricing/sync").json()
    assert report["sources"][0]["outcome"] == "pending_review"
    [review] = client.get("/api/pricing/reviews").json()
    assert review["changes"] and client.get("/api/pricing/status").json()["pending_reviews"] == [review["version"]]
    entry = next(item for item in client.get("/api/pricing").json() if item["model"] == "gpt-test")
    assert entry["input_usd_per_mtok"] == "2" and entry["status"] == "pending_review"

    approved = client.post(f"/api/pricing/reviews/{review['version']}/approve", json={"note": "checked"})
    assert approved.status_code == 200, approved.text
    entry = next(item for item in client.get("/api/pricing").json() if item["model"] == "gpt-test")
    assert entry["input_usd_per_mtok"] == "4"
    assert client.post("/api/pricing/reviews/nothing/reject", json={}).json()["code"] == "pricing.review_not_found"


def test_the_desktop_syncs_in_the_background_without_network_when_litellm_is_off(tmp_path: Path) -> None:
    config = AppConfig(data_dir=tmp_path / "data", price_sync=PriceSyncConfig(enabled=True, litellm=False))
    app = create_app(config, client_factory=lambda _p: None, integrations=[], scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for _ in range(100):
            status = client.get("/api/pricing/status").json()
            if status["last_success_at"]:
                break
            import time

            time.sleep(0.05)
        assert status["last_success_at"] is not None and status["last_error"] is None
        assert status["sync_enabled"] is True


def test_the_price_sync_settings_come_from_the_environment(tmp_path: Path) -> None:
    config = PriceSyncConfig.from_env(
        {"NODESTRO_PRICE_SYNC": "off", "NODESTRO_PRICE_LITELLM": "on", "NODESTRO_PRICE_SNAPSHOT_FILE": str(tmp_path)}
    )
    assert config.enabled is False and config.litellm is True and config.snapshot_file == tmp_path
    assert PriceSyncConfig.from_env({}).enabled is True
    with pytest.raises(Exception, match="on or off"):
        PriceSyncConfig.from_env({"NODESTRO_PRICE_SYNC": "sometimes"})


def _record(
    model: str, *, cost: str | None, version: str | None = "v1", quality: str | None = "verified"
) -> UsageRecord:
    from decimal import Decimal

    return UsageRecord(
        agent="assistant",
        model=model,
        input_tokens=10,
        output_tokens=10,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        thinking_tokens=0,
        web_search_requests=0,
        web_fetch_requests=0,
        estimated_cost_usd=Decimal(cost) if cost is not None else None,
        provider="anthropic",
        pricing_version=version,
        pricing_source="builtin" if version else None,
        pricing_quality=quality,
    )


def test_usage_separates_known_partial_and_unknown_costs(make_app: MakeApp) -> None:
    client = make_app()
    ctx = _ctx(client)
    ctx.usage_log.record("assistant", [_record("claude-sonnet-5-5", cost="0.25")])
    report = client.get("/api/usage").json()
    assert report["total"]["cost_state"] == "known" and report["total"]["known_cost_usd"] == pytest.approx(0.25)
    assert report["total"]["pricing_versions"] == ["v1"] and report["total"]["pricing_quality"] == "verified"

    ctx.usage_log.record("assistant", [_record("claude-unknown-9", cost=None, version=None, quality=None)])
    report = client.get("/api/usage").json()
    total = report["total"]
    assert total["cost_state"] == "partial" and total["unpriced"] == 1
    assert total["estimated_cost_usd"] == total["known_cost_usd"] == pytest.approx(0.25)
    unknown = next(bucket for bucket in report["by_model"] if bucket["key"] == "anthropic:claude-unknown-9")
    assert unknown["cost_state"] == "unknown"


def test_billing_keys_are_never_stored_without_encryption(make_app: MakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_app()
    refused = client.put("/api/pricing/billing-keys/openai", json={"admin_key": "sk-admin-12345678"})
    assert refused.status_code == 409 and refused.json()["code"] == "pricing.billing_key_not_storable"
    keys = {item["provider"]: item for item in client.get("/api/pricing/billing-keys").json()}
    assert keys["openai"]["configured"] is False and keys["openai"]["storable"] is False
    monkeypatch.setenv("NODESTRO_ANTHROPIC_ADMIN_KEY", "sk-ant-admin-12345678")
    keys = {item["provider"]: item for item in client.get("/api/pricing/billing-keys").json()}
    assert keys["anthropic"]["configured"] is True and keys["anthropic"]["origin"] == "environment"
    assert "sk-ant" not in json.dumps(keys)


def test_reconciliation_compares_estimates_with_a_providers_bill(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app()
    ctx = _ctx(client)
    ctx.usage_log.record("assistant", [_record("claude-sonnet-5-5", cost="10")])
    today = ctx.usage_log.entries()[0].at.date()
    seen: list[dict[str, str]] = []

    def fake_get(url: str, headers: Any, params: Any, **_kwargs: Any) -> Any:
        seen.append(dict(headers))
        return {
            "data": [
                {
                    "starting_at": f"{today.isoformat()}T00:00:00Z",
                    "cost": {"amount": "1200", "currency": "USD"},  # 12.00 USD
                    "group": {"model": "claude-sonnet-5-5"},
                }
            ],
            "has_more": False,
            "data_refreshed_at": "2099-01-01T00:00:00Z",
        }

    monkeypatch.setattr("nodestro_runtime.reconciliation.get_json", fake_get)
    answer = client.post(
        "/api/pricing/reconciliation",
        json={
            "start": today.isoformat(),
            "end": (today + timedelta(days=1)).isoformat(),
            "keys": {"anthropic": {"admin_key": "sk-ant-admin-once-123"}},
        },
    )
    assert answer.status_code == 200, answer.text
    report = answer.json()
    assert seen[0]["x-api-key"] == "sk-ant-admin-once-123"
    assert [p["provider"] for p in report["providers"]] == ["anthropic"]
    model_row = next(row for row in report["rows"] if row["model"] == "claude-sonnet-5-5")
    assert model_row["status"] == "warning" and float(model_row["billed_usd"]) == pytest.approx(12)
    assert report["warnings"] == 2
    assert ctx.store.get_raw("pricing_billing", "anthropic") is None  # the one-off key was not stored


def test_reconciliation_needs_a_key_and_a_sensible_period(make_app: MakeApp) -> None:
    client = make_app()
    assert client.get("/api/pricing/reconciliation").json()["code"] == "pricing.billing_key_missing"
    start = date(2026, 1, 1)
    answer = client.post(
        "/api/pricing/reconciliation",
        json={"start": start.isoformat(), "end": (start + timedelta(days=200)).isoformat()},
    )
    assert answer.json()["code"] == "pricing.reconciliation_period"


# ---- server -----------------------------------------------------------------------------------------------


@pytest.fixture
def database() -> Iterator[Any]:
    import os
    import uuid

    from nodestro_runtime.storage_postgres import PostgresDatabase

    dsn = os.environ.get("NODESTRO_TEST_POSTGRES", "")
    if not dsn:
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    import psycopg
    from psycopg import sql

    schema = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    opened = PostgresDatabase(dsn, schema=schema)
    opened.migrate()
    yield opened
    opened.close()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_servers_share_reference_prices_but_never_overrides(database: Any, tmp_path: Path) -> None:
    from nodestro_runtime.problems import NodestroError

    from nodestro_api.pricing_setup import server_sync
    from nodestro_api.server_app import TenantFiles

    files = TenantFiles(database=database, data_root=tmp_path / "server", integrations=[])
    path = _snapshot_file(tmp_path, "srv1", _price("gpt-server", "3", "9"))
    report = server_sync(database, PriceSyncConfig(litellm=False, snapshot_file=path)).sync(force=True)
    assert report.ran and report.active_version != BUILTIN_VERSION

    alpha, beta = files.open("alpha"), files.open("beta")
    try:
        for tenant in (alpha, beta):
            assert tenant.pricing.entry("openai", "gpt-server") is not None
            assert tenant.price_sync is None and tenant.pricing.reference_writable is False
            assert tenant.pricing.status().active_version == report.active_version
        alpha.pricing.set_override(_price("gpt-server", "1", "1"))
        alpha_entry, beta_entry = (
            alpha.pricing.entry("openai", "gpt-server"),
            beta.pricing.entry("openai", "gpt-server"),
        )
        assert alpha_entry is not None and alpha_entry.overridden
        assert beta_entry is not None and not beta_entry.overridden
        with pytest.raises(NodestroError):
            alpha.pricing.submit(PriceSnapshot("tenant-attempt", (_price("gpt-x", "1", "1"),)))
        # The tenants' own stores hold no reference prices.
        assert alpha.store.get_raw("price_state", "reference") is None
    finally:
        alpha.close()
        beta.close()


def test_one_server_process_syncs_at_a_time(database: Any, tmp_path: Path) -> None:
    import os

    from nodestro_runtime.storage_postgres import PostgresDatabase

    from nodestro_api.pricing_setup import server_sync, sync_lock

    other = PostgresDatabase(os.environ["NODESTRO_TEST_POSTGRES"], schema=database.schema)
    try:
        with sync_lock(other)() as held:
            assert held
            report = server_sync(database, PriceSyncConfig(litellm=False)).sync(force=True)
            assert not report.ran and report.skipped_reason == "already_running"
        assert server_sync(database, PriceSyncConfig(litellm=False)).sync(force=True).ran
    finally:
        other.close()


def test_the_reference_tenant_id_is_reserved() -> None:
    from nodestro_runtime.problems import NodestroError

    from nodestro_api.server import create_tenant

    with pytest.raises(NodestroError, match="reserved"):
        create_tenant(None, PRICE_REFERENCE_TENANT, "x")  # type: ignore[arg-type]


def test_the_worker_syncs_reference_prices_when_due(database: Any, tmp_path: Path) -> None:
    import asyncio

    from nodestro_runtime.identity_store import IdentityStore

    from nodestro_api.server_app import TenantFiles
    from nodestro_api.worker import ServerWorker, WorkerSettings

    files = TenantFiles(database=database, data_root=tmp_path / "server", integrations=[])
    worker = ServerWorker(files, IdentityStore(database), WorkerSettings(price_sync=PriceSyncConfig(litellm=False)))
    assert worker.price_sync.due()
    asyncio.run(worker.sync_prices())
    assert worker.price_sync.pricing.state().last_success_at is not None
    assert not worker.price_sync.due()
    asyncio.run(worker.aclose())
