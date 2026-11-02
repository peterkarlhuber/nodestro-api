"""The in-app inbox of a server (``NODESTRO_AXENTRA_NOTIFY=1``): ``/api/notifications``.

Only mounted when the switch is on (otherwise the routes do not exist: ``404``). The messages live in Axentra's
notify module (channel ``inbox``, :mod:`nodestro_runtime.axentra_notify`); a logged-in member reads and marks only
their own messages of the session's tenant. The member's Axentra user and tenant come from the link table
(``nodestro_axentra_ids``); a member without a linked Axentra user (identity backfill not run) has an empty inbox.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from .permissions import READ, Principal


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
        return AxentraPrincipal(actor=Actor(ActorKind.USER, user, "nodestro")), link.services.product_id, tenant

    @router.get("", dependencies=READ)
    def list_notifications(
        request: Request,
        unread_only: bool = False,
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = None,
    ) -> NotificationPage:
        """The current member's notifications in the session's tenant, newest first."""
        found = scope(request)
        if found is None:
            return NotificationPage(items=[], unread=0)
        from axentra.core.errors import Forbidden

        who, product, tenant = found
        notify = link.services.platform.notify
        try:
            page = notify.list_inbox(who, product, tenant, unread_only=unread_only, limit=limit, cursor=cursor)
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


__all__ = ["notifications_router"]
