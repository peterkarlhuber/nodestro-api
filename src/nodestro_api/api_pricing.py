"""The API of model prices and cost reconciliation (PRICING-1, phases N8 and N10).

Prices are resolved and costs calculated by Orkexa only; this module exposes the workspace's price catalog
(:class:`nodestro_runtime.pricing.PricingService`), its synchronization
(:class:`nodestro_runtime.pricing_sync.PricingSyncService`) and the optional comparison with the providers' bills
(:class:`nodestro_runtime.reconciliation.CostReconciliationService`).

Desktop and server:

- On the desktop the reference prices belong to the installation: the API process syncs them in the background,
  ``POST /api/pricing/sync`` syncs at once, and reviews are decided in the studio.
- On a server the reference prices belong to the server (:mod:`nodestro_api.pricing_setup`): one worker syncs them
  under a PostgreSQL advisory lock, operators decide reviews with ``nodestro-server pricing``. Tenants read them and
  keep their own overrides; ``POST /api/pricing/sync`` and review decisions answer 403 there.
- Offline nothing breaks: a failed sync keeps the last valid prices and records the error; prices age into
  ``stale`` and the studio says so.

Admin keys of the providers' billing APIs are never the model keys. A server stores them encrypted with its secret
box (bound to tenant and field); the desktop has no encrypted store, so it never stores them: it reads them from
``NODESTRO_OPENAI_ADMIN_KEY`` / ``NODESTRO_ANTHROPIC_ADMIN_KEY`` or takes them with a single reconciliation request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from nodestro_runtime.pricing import PriceEntry, PricingEvent, PricingStatus, StoredSnapshot, SubmitResult
from nodestro_runtime.pricing_sync import SyncReport
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.reconciliation import (
    AnthropicCostAdapter,
    CostAdapter,
    CostReconciliationService,
    OpenAICostsAdapter,
    ReconciliationReport,
    ReconciliationSettings,
    nodestro_estimates,
)
from nodestro_runtime.schemas import now
from orkexa.pricing import ModelPrice, TokenRates
from pydantic import BaseModel, Field

from .api import Ctx
from .permissions import ADMIN, READ
from .serving import ApiError

if TYPE_CHECKING:
    from .context import Nodestro

router = APIRouter(prefix="/api/pricing")

BillingProvider = Literal["openai", "anthropic"]
BILLING_PROVIDERS: tuple[BillingProvider, ...] = ("openai", "anthropic")
_BILLING = "pricing_billing"
_ENV_KEYS = {"openai": "NODESTRO_OPENAI_ADMIN_KEY", "anthropic": "NODESTRO_ANTHROPIC_ADMIN_KEY"}
_MAX_PERIOD = timedelta(days=92)


# ---- prices ----------------------------------------------------------------------------------------------


class PricingOverview(PricingStatus):
    """The catalog's state plus what the studio needs to explain it."""

    #: ``True`` on a server: reference prices are managed by the server's operator.
    reference_read_only: bool
    #: Whether this process syncs reference prices in the background.
    sync_enabled: bool
    price_mode: str
    warn_after_hours: float
    critical_after_days: float
    review_change_percent: float
    refresh_interval_hours: float


@router.get("", dependencies=READ)
def list_prices(ctx: Ctx) -> list[PriceEntry]:
    """Every effective price (reference prices and overrides) with status, quality, source and check date."""
    return ctx.pricing.entries()


@router.get("/status", dependencies=READ)
def pricing_status(ctx: Ctx) -> PricingOverview:
    status = ctx.pricing.status()
    settings = ctx.pricing.settings
    return PricingOverview(
        **status.model_dump(),
        reference_read_only=not ctx.pricing.reference_writable,
        sync_enabled=ctx.price_sync is not None and ctx.config.price_sync.enabled,
        price_mode=ctx.price_mode(),
        warn_after_hours=settings.warn_after.total_seconds() / 3600,
        critical_after_days=settings.critical_after.total_seconds() / 86400,
        review_change_percent=float(settings.review_change_ratio * 100),
        refresh_interval_hours=settings.refresh_interval.total_seconds() / 3600,
    )


@router.get("/history", dependencies=READ)
def pricing_history(ctx: Ctx, limit: int = Query(200, ge=1, le=1000)) -> list[PricingEvent]:
    """Price changes, syncs, reviews and overrides, newest first (a server shows its reference events too)."""
    return ctx.pricing.history(limit=limit)


@router.post("/sync", dependencies=ADMIN)
async def sync_prices(ctx: Ctx) -> SyncReport:
    """Check the prices against their sources now (desktop; a server syncs in its worker)."""
    sync = ctx.price_sync
    if sync is None:
        raise ApiError(403, "pricing.reference_read_only", "Reference prices are synchronized by the server's worker")
    return await asyncio.to_thread(sync.sync, force=True)


class ReviewOut(BaseModel):
    """A snapshot held back because some of its changes need a person's decision."""

    version: str
    source: str
    created_at: datetime | None = None
    changes: list[Any]
    issues: list[Any]


def _review(record: StoredSnapshot) -> ReviewOut:
    return ReviewOut(
        version=record.version,
        source=record.source or "",
        created_at=record.created_at,
        changes=[change.model_dump(mode="json") for change in record.changes],
        issues=[issue.model_dump(mode="json") for issue in record.issues],
    )


@router.get("/reviews", dependencies=READ)
def list_reviews(ctx: Ctx) -> list[ReviewOut]:
    return [_review(record) for record in ctx.pricing.snapshots() if record.state == "pending_review"]


class ReviewDecision(BaseModel):
    note: str | None = Field(default=None, max_length=500)


def _writable(ctx: Nodestro) -> None:
    if not ctx.pricing.reference_writable:
        raise ApiError(403, "pricing.reference_read_only", "Reference prices are managed by the server's operator")


@router.post("/reviews/{version}/approve", dependencies=ADMIN)
def approve_review(version: str, body: ReviewDecision, ctx: Ctx) -> SubmitResult:
    _writable(ctx)
    return ctx.pricing.approve(version, note=body.note)


@router.post("/reviews/{version}/reject", status_code=204, dependencies=ADMIN)
def reject_review(version: str, body: ReviewDecision, ctx: Ctx) -> Response:
    _writable(ctx)
    ctx.pricing.reject(version, note=body.note)
    return Response(status_code=204)


class OverrideIn(BaseModel):
    """An individual price (USD per million tokens, as decimal strings): a contract price or a correction."""

    input_usd_per_mtok: Decimal = Field(ge=0)
    output_usd_per_mtok: Decimal = Field(ge=0)
    cache_read_usd_per_mtok: Decimal | None = Field(default=None, ge=0)
    cache_write_5m_usd_per_mtok: Decimal | None = Field(default=None, ge=0)
    cache_write_1h_usd_per_mtok: Decimal | None = Field(default=None, ge=0)
    #: ``contract`` (an agreed price, default), ``verified`` (checked against the official price list),
    #: ``estimated`` or ``unverified``.
    quality: Literal["contract", "verified", "estimated", "unverified"] = "contract"
    source: str = Field(default="custom", min_length=1, max_length=80)
    source_url: str | None = Field(default=None, max_length=500)
    version: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)


@router.put("/overrides/{provider}/{model:path}", dependencies=ADMIN)
def set_override(provider: str, model: str, body: OverrideIn, ctx: Ctx) -> PriceEntry:
    """Store an individual price for one model; it wins over the reference price (history is kept)."""
    try:
        price = ModelPrice(
            provider=provider,
            model=model,
            rates=TokenRates.of(
                body.input_usd_per_mtok,
                body.output_usd_per_mtok,
                body.cache_read_usd_per_mtok,
                body.cache_write_5m_usd_per_mtok,
                body.cache_write_1h_usd_per_mtok,
            ),
            source=body.source,
            source_url=body.source_url,
            version=body.version or now().date().isoformat(),
            checked_at=now(),
            quality=body.quality,
        )
    except (ValueError, TypeError) as exc:  # Orkexa's PricingError is a ValueError
        raise NodestroError("pricing.invalid_price", str(exc)) from None
    ctx.pricing.set_override(price, note=body.note)
    entry = ctx.pricing.entry(provider, model)
    assert entry is not None
    return entry


@router.delete("/overrides/{provider}/{model:path}", status_code=204, dependencies=ADMIN)
def remove_override(provider: str, model: str, ctx: Ctx) -> Response:
    """Go back to the reference price."""
    if not ctx.pricing.remove_override(provider, model):
        raise KeyError(f"{provider}:{model}")
    return Response(status_code=204)


# ---- billing keys and reconciliation ---------------------------------------------------------------------


class BillingKeyIn(BaseModel):
    admin_key: str = Field(min_length=8, max_length=500, repr=False)
    #: OpenAI project ids (``proj_...``) or Anthropic workspace ids (``wrkspc_...``) to restrict the reports to;
    #: empty: the whole organization.
    scope_ids: list[str] = Field(default_factory=list, max_length=50)


class BillingKeyOut(BaseModel):
    provider: BillingProvider
    configured: bool
    #: ``stored`` (encrypted, server), ``environment`` (``NODESTRO_*_ADMIN_KEY``) or ``none``.
    origin: Literal["stored", "environment", "none"]
    scope_ids: list[str] = Field(default_factory=list)
    #: Whether this installation can store admin keys (only with encryption at rest).
    storable: bool


def _secrets(ctx: Nodestro) -> Any:
    return getattr(ctx, "_secrets", None)


def _field(provider: str) -> str:
    return f"pricing.billing.{provider}.admin_key"


def _stored_key(ctx: Nodestro, provider: str) -> tuple[str | None, list[str]]:
    raw = ctx.store.get_raw(_BILLING, provider) or {}
    scope = [str(item) for item in raw.get("scope_ids") or []]
    secrets = _secrets(ctx)
    token = raw.get("admin_key")
    if not token or secrets is None:
        return None, scope
    try:
        return str(secrets.decrypt(ctx.tenant_id, _field(provider), token)), scope
    except NodestroError:
        return None, scope  # unreadable (changed server key): enter it again


def _key(ctx: Nodestro, provider: str) -> tuple[str | None, list[str], Literal["stored", "environment", "none"]]:
    stored, scope = _stored_key(ctx, provider)
    if stored:
        return stored, scope, "stored"
    env = os.environ.get(_ENV_KEYS[provider], "").strip() if ctx.config.profile_kind == "desktop" else ""
    if env:
        return env, scope, "environment"
    return None, scope, "none"


@router.get("/billing-keys", dependencies=ADMIN)
def list_billing_keys(ctx: Ctx) -> list[BillingKeyOut]:
    """Which providers' billing APIs can be asked (never the keys themselves)."""
    found = []
    for provider in BILLING_PROVIDERS:
        key, scope, origin = _key(ctx, provider)
        found.append(
            BillingKeyOut(
                provider=provider,
                configured=key is not None,
                origin=origin,
                scope_ids=scope,
                storable=_secrets(ctx) is not None,
            )
        )
    return found


@router.put("/billing-keys/{provider}", dependencies=ADMIN)
def set_billing_key(provider: BillingProvider, body: BillingKeyIn, ctx: Ctx) -> BillingKeyOut:
    """Store an admin key of a provider's billing API, encrypted (servers only)."""
    secrets = _secrets(ctx)
    if secrets is None:
        raise ApiError(
            409,
            "pricing.billing_key_not_storable",
            "This installation has no encrypted store for admin keys; set "
            f"{_ENV_KEYS[provider]} or enter the key for a single reconciliation",
            provider=provider,
        )
    ctx.store.put_raw(
        _BILLING,
        provider,
        {
            "admin_key": secrets.encrypt(ctx.tenant_id, _field(provider), body.admin_key.strip()),
            "scope_ids": [item.strip() for item in body.scope_ids if item.strip()],
            "updated_at": now().isoformat(),
        },
    )
    return BillingKeyOut(provider=provider, configured=True, origin="stored", scope_ids=body.scope_ids, storable=True)


@router.delete("/billing-keys/{provider}", status_code=204, dependencies=ADMIN)
def remove_billing_key(provider: BillingProvider, ctx: Ctx) -> Response:
    if not ctx.store.delete(_BILLING, provider):
        raise KeyError(provider)
    return Response(status_code=204)


class ReconciliationIn(BaseModel):
    start: date
    end: date
    #: Admin keys for this one request (never stored), by provider; they win over stored keys.
    keys: dict[BillingProvider, BillingKeyIn] = Field(default_factory=dict)
    relative_threshold_percent: Decimal = Field(default=Decimal("5"), gt=0, le=100)
    absolute_threshold_usd: Decimal = Field(default=Decimal("1"), ge=0)


def _adapters(ctx: Nodestro, given: Mapping[BillingProvider, BillingKeyIn]) -> list[CostAdapter]:
    adapters: list[CostAdapter] = []
    for provider in BILLING_PROVIDERS:
        if provider in given:
            key: str | None = given[provider].admin_key.strip()
            scope = given[provider].scope_ids
        else:
            key, scope, _ = _key(ctx, provider)
        if not key:
            continue
        if provider == "openai":
            adapters.append(OpenAICostsAdapter(key, project_ids=tuple(scope)))
        else:
            adapters.append(AnthropicCostAdapter(key, workspace_ids=tuple(scope)))
    return adapters


def _reconcile(ctx: Nodestro, body: ReconciliationIn) -> ReconciliationReport:
    if body.end <= body.start or body.end - body.start > _MAX_PERIOD:
        raise NodestroError(
            "pricing.reconciliation_period",
            "Choose a period of 1 to 92 days",
            start=body.start.isoformat(),
            end=body.end.isoformat(),
        )
    adapters = _adapters(ctx, body.keys)
    if not adapters:
        raise NodestroError("pricing.billing_key_missing", "No admin key of a provider's billing API is configured")
    service = CostReconciliationService(
        adapters,
        lambda start, end: nodestro_estimates(ctx, start, end),
        settings=ReconciliationSettings(
            relative_threshold=body.relative_threshold_percent / 100,
            absolute_threshold_usd=body.absolute_threshold_usd,
        ),
    )
    return service.reconcile(body.start, body.end)


def _default_period() -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    return today - timedelta(days=30), today + timedelta(days=1)


@router.get("/reconciliation", dependencies=ADMIN)
async def get_reconciliation(
    ctx: Ctx, start: Annotated[date | None, Query()] = None, end: Annotated[date | None, Query()] = None
) -> ReconciliationReport:
    """Compare Nodestro's estimates with the providers' bills (stored or environment keys; default: 30 days)."""
    first, last = _default_period()
    body = ReconciliationIn(start=start or first, end=end or last)
    return await asyncio.to_thread(_reconcile, ctx, body)


@router.post("/reconciliation", dependencies=ADMIN)
async def post_reconciliation(body: ReconciliationIn, ctx: Ctx) -> ReconciliationReport:
    """Like ``GET``, with thresholds and, optionally, admin keys for this one request (never stored)."""
    return await asyncio.to_thread(_reconcile, ctx, body)


__all__ = ["BILLING_PROVIDERS", "router"]
