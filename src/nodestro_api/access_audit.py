"""The access audit trail of a tenant (API-004): logins, API keys, the tenant itself and its members' roles.

Every event is stored in the tenant's own store (collection ``access_audit``; on a server the tenant's rows in
PostgreSQL), newest first for ``GET /api/access-audit`` (permission ``admin``, read only). It names who did what to
which target, when and with which outcome (``success`` or ``denied``: a refused tenant switch). Refused logins
without a tenant stay in the process log (and in Axentra's audit log with its login flow). It never holds a secret
(no API key, token, setup link or code, only ids and the key's prefix).

As the tool approval trail does, an event is mirrored into Axentra's audit log in the same transaction when the store
has a platform sink (``NODESTRO_AXENTRA=on`` with ``NODESTRO_AXENTRA_AUDIT=1``; action ``nodestro.access.<kind>``).
With ``NODESTRO_IDENTITY_BACKEND=mirror|axentra`` Axentra's own audit log also records its login flow and membership
changes; this trail is Nodestro's record and the one tenant admins read.

On a server the operator's commands (``create-tenant``, ``set-role``, ``remove-member``, ``revoke-sessions``,
``tenant suspend|resume``) are recorded with the actor ``operator``.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Literal

from nodestro_runtime.platform_events import platform_events
from nodestro_runtime.schemas import new_id, now
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

COLLECTION = "access_audit"
#: The newest events kept per tenant (older ones are dropped when the trail is read), as the approval trail.
MAX_EVENTS = 5000
#: The actor of the server operator's commands.
OPERATOR = "operator"

AccessKind = Literal[
    "login",
    "logout",
    "tenant_switched",
    "api_key_created",
    "api_key_revoked",
    "api_key_rotated",
    "api_key_deleted",
    "tenant_created",
    "tenant_status_changed",
    "role_changed",
    "member_removed",
    "sessions_revoked",
]


class AccessEvent(BaseModel):
    """One entry of a tenant's access audit trail."""

    id: str = Field(default_factory=lambda: new_id("aae"))
    at: datetime = Field(default_factory=now)
    kind: AccessKind
    #: The user id that acted (``operator`` for the server's commands, ``local`` on the desktop).
    actor: str
    target_type: Literal["user", "api_key", "tenant", "session"]
    target_id: str
    outcome: Literal["success", "denied"] = "success"
    #: Ids and codes only: a role before and after, a refusal code, a key's name and prefix; never a secret.
    details: dict[str, Any] = Field(default_factory=dict)


def record(store: Any, event: AccessEvent) -> AccessEvent:
    """Store ``event``; with a platform sink its Axentra mirror commits in the same transaction."""
    key = f"{event.at.isoformat()}_{event.id}"
    sink = platform_events(store)
    if sink is None:
        store.put(COLLECTION, key, event)
        return event
    with store.transaction():
        store.put(COLLECTION, key, event)
        sink.audit(
            str(getattr(store, "tenant_id", "")),
            event_id=event.id,
            at=event.at,
            action=f"nodestro.access.{event.kind}",
            actor=event.actor,
            target_type=event.target_type,
            target_id=event.target_id,
            outcome=event.outcome,
            details={"nodestro_event": event.id, **event.details},
        )
    return event


def record_quietly(store: Any, event: AccessEvent) -> None:
    """:func:`record` for a request that must not fail because its audit entry could not be written (a login); the
    failure is logged."""
    try:
        record(store, event)
    except Exception:
        logger.exception("The access audit event %s (%s) could not be stored", event.id, event.kind)


def events(store: Any, *, limit: int = 200, kind: str | None = None) -> list[AccessEvent]:
    """The newest events first (only of ``kind`` when given)."""
    items = store.items(COLLECTION, AccessEvent)
    items.sort(key=lambda pair: pair[0], reverse=True)
    for key, _ in items[MAX_EVENTS:]:
        store.delete(COLLECTION, key)
    found = [event for _, event in items[:MAX_EVENTS] if kind is None or event.kind == kind]
    return found[:limit]
