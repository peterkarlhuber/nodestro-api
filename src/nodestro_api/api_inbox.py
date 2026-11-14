"""The local inbox (NAL-A3): ``/api/notifications`` on the desktop, in Nodestro Local Docker, and on a server whose
Axentra inbox is off (:mod:`nodestro_runtime.notifications`).

Same routes and answers as the Axentra inbox of a server (:mod:`.api_notifications`), so an interface needs one
client. A member reads and marks only the messages of the session's tenant (the context's store) that are for
everyone or for them; the read state is their own. Cursors have the same shape and are checked the same way.
"""

from __future__ import annotations

import base64
from datetime import datetime
import json

from fastapi import APIRouter, Query, Request
from nodestro_runtime.notifications import Notification as Stored
from nodestro_runtime.notifications import ReadState

from .api import Ctx, actor_id
from .api_notifications import MarkReadIn, MarkReadOut, Notification, NotificationPage, check_cursor
from .permissions import READ

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _cursor(item: Stored) -> str:
    raw = json.dumps([item.created_at.isoformat(), item.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _after(cursor: str) -> tuple[datetime, str]:
    value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    return datetime.fromisoformat(value[0]), str(value[1])


def _out(item: Stored, state: ReadState) -> Notification:
    return Notification(
        id=item.id,
        kind=item.kind,
        subject=item.title or None,
        text=item.text or None,
        created_at=item.created_at,
        read_at=state.read_at(item),
        level=item.level,
        params=item.params,
        run_id=item.run_id,
        workflow_id=item.workflow_id,
        workflow_name=item.workflow_name or None,
    )


@router.get("", dependencies=READ)
def list_notifications(
    request: Request,
    ctx: Ctx,
    unread_only: bool = False,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
) -> NotificationPage:
    """The current member's notifications in the session's workspace, newest first."""
    check_cursor(cursor)
    inbox = ctx.inbox
    if inbox is None:
        return NotificationPage(items=[], unread=0)
    user = actor_id(request)
    state = inbox.read_state(user)
    items = inbox.for_user(user)  # the newest messages, newest first
    unread = sum(1 for item in items if state.read_at(item) is None)
    if unread_only:
        items = [item for item in items if state.read_at(item) is None]
    if cursor:
        moment, last = _after(cursor)
        items = [item for item in items if (item.created_at, item.id) < (moment, last)]
    page = items[:limit]
    more = len(items) > limit
    return NotificationPage(
        items=[_out(item, state) for item in page],
        unread=unread,
        next_cursor=_cursor(page[-1]) if more and page else None,
    )


@router.post("/read", dependencies=READ)
def mark_read(body: MarkReadIn, request: Request, ctx: Ctx) -> MarkReadOut:
    """Mark the current member's notifications as read (only their own; unknown ids change nothing)."""
    inbox = ctx.inbox
    if inbox is None:
        return MarkReadOut(marked=0)
    return MarkReadOut(marked=inbox.mark_read(actor_id(request), body.ids or None))


__all__ = ["router"]
