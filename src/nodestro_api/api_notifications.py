"""The in-app inbox of a server (``NODESTRO_AXENTRA_NOTIFY=1``): ``/api/notifications``.

Only mounted when the switch is on (otherwise the routes do not exist: ``404``). The messages live in Axentra's
notify module (channel ``inbox``, :mod:`nodestro_runtime.axentra_notify`); a logged-in member reads and marks only
their own messages of the session's tenant. The member's Axentra user and tenant come from the link table
(``nodestro_axentra_ids``); a member without a linked Axentra user (identity backfill not run) has an empty inbox.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
import json
import re
from typing import Any

from fastapi import APIRouter, Query, Request
from nodestro_runtime.problems import NodestroError
from pydantic import BaseModel, Field

from .permissions import READ, Principal

#: The longest cursor accepted (a cursor holds a timestamp and a message id).
MAX_CURSOR = 512
#: The timestamp of a cursor as Axentra writes it (``str(datetime)``, or ISO 8601 with ``T``), in the subset that both
#: Python and PostgreSQL read the same way: no week dates, no comma fractions, no fractional or other exotic offsets,
#: ASCII digits only (security re-review SR-06a).
# The exact form Axentra writes (``datetime.isoformat()`` of an aware value, or ``Z``) and accepts back since its
# re-review fix AX-R1; any other shape is refused here before Axentra sees it.
_CURSOR_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
#: A message id as Axentra makes them (``msg_<base32>``), ASCII only: no NUL byte, no lone surrogate.
_CURSOR_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


def check_cursor(cursor: str | None) -> None:
    """Refuse a cursor that is not one this API handed out (``400 notifications.invalid_cursor``, security review
    SR-06, SR-06a): base64url JSON of ``[timestamp, message id]``. Axentra only checks that it is a JSON list, and a
    malformed one failed in the database as ``500``; so does a value Python reads but PostgreSQL does not (a NUL byte,
    an ISO week date, an offset with a fraction, a comma fraction) or one that cannot be encoded (a lone surrogate).
    Only the shapes both read alike pass. The cursor is never echoed into the answer or the log."""
    if not cursor:
        return
    problem = NodestroError("notifications.invalid_cursor", "The cursor is not valid; start again without it")
    if len(cursor) > MAX_CURSOR:
        raise problem
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, binascii.Error):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise problem from None
    if not (isinstance(value, list) and len(value) == 2 and all(isinstance(part, str) for part in value)):
        raise problem
    at, message_id = value
    if not _CURSOR_TIME.fullmatch(at) or not _CURSOR_ID.fullmatch(message_id):
        raise problem
    try:
        parsed = datetime.fromisoformat(at)
    except ValueError:  # a day or an hour out of range
        raise problem from None
    if parsed.tzinfo is None:
        raise problem


class Notification(BaseModel):
    id: str
    kind: str
    subject: str | None
    text: str | None
    created_at: datetime
    read_at: datetime | None


class NotificationPage(BaseModel):
    items: list[Notification]
    unread: int
    next_cursor: str | None = None


class MarkReadIn(BaseModel):
    #: The messages to mark as read; empty or absent: all of them.
    ids: list[str] = Field(default_factory=list, max_length=500)


class MarkReadOut(BaseModel):
    marked: int


def notifications_router(notifications: Any) -> APIRouter:
    """The inbox routes over :class:`nodestro_runtime.axentra_notify.AxentraNotifications`."""
    router = APIRouter(prefix="/api/notifications", tags=["notifications"])
    link = notifications.link

    def scope(request: Request) -> tuple[Any, str, str | None] | None:
        from axentra.core.context import Actor, ActorKind
        from axentra.core.context import Principal as AxentraPrincipal

        principal: Principal = request.state.principal
        user = link.lookup("user", principal.id)
        tenant = link.tenant(principal.tenant_id)
        if user is None or tenant is None:
            return None
        # The ignore: mypy checks for Python 3.10, where Axentra's StrEnum (3.11+) is not known.
        actor = Actor(ActorKind.USER, user, "nodestro")  # type: ignore[arg-type,unused-ignore]
        return AxentraPrincipal(actor=actor), link.services.product_id, tenant

    @router.get("", dependencies=READ)
    def list_notifications(
        request: Request,
        unread_only: bool = False,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = None,
    ) -> NotificationPage:
        """The current member's notifications in the session's tenant, newest first."""
        check_cursor(cursor)
        found = scope(request)
        if found is None:
            return NotificationPage(items=[], unread=0)
        from axentra.core.errors import Forbidden

        who, product, tenant = found
        notify = link.services.platform.notify
        from axentra.core.errors import InvalidCursor

        try:
            page = notify.list_inbox(who, product, tenant, unread_only=unread_only, limit=limit, cursor=cursor)
        except InvalidCursor:  # Axentra's own check (AX-R1) as a second line: same answer as check_cursor
            raise NodestroError(
                "notifications.invalid_cursor", "The cursor is not valid; start again without it"
            ) from None
        except Forbidden:  # a role without an inbox (viewer): nothing is ever sent to it
            return NotificationPage(items=[], unread=0)
        return NotificationPage(
            items=[
                Notification(
                    id=item.id,
                    kind=item.template_key.removeprefix("nodestro."),
                    subject=item.subject,
                    text=item.text,
                    created_at=item.created_at,
                    read_at=item.read_at,
                )
                for item in page.items
            ],
            unread=notify.unread_count(who, product, tenant),
            next_cursor=page.next_cursor,
        )

    @router.post("/read", dependencies=READ)
    def mark_read(body: MarkReadIn, request: Request) -> MarkReadOut:
        """Mark the current member's notifications as read (only their own; unknown ids change nothing)."""
        found = scope(request)
        if found is None:
            return MarkReadOut(marked=0)
        from axentra.core.errors import Forbidden

        who, product, tenant = found
        try:
            marked = link.services.platform.notify.mark_read(who, product, tenant, message_ids=body.ids or None)
        except Forbidden:
            marked = 0
        return MarkReadOut(marked=marked)

    return router


__all__ = ["check_cursor", "notifications_router"]
