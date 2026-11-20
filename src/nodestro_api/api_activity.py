"""The API of the shared activity log (N-13; runtime :mod:`nodestro_runtime.activity`, ``docs/activity-log.md``).

- ``GET /api/activity``: the workspace's activity events (on a server: the tenant's), newest first, filtered like
  :func:`nodestro_runtime.activity.query`, one page at a time. ``limit`` is at most :data:`MAX_LIMIT`; the answer's
  ``next_before`` is passed as ``before`` to read the next page (keyset paging; ``null``: no more events).
- ``GET /api/activity/export?format=jsonl|csv``: every event that matches the same filters (no page limit) as a
  download, JSON Lines or CSV (formula-like cells start with ``'``, see :func:`nodestro_runtime.activity.export`).

Both need ``admin``, like the other audit trails (``/api/access-audit``, ``/api/tool-approvals/audit``): the log
names who did what in the whole workspace or tenant. Both only read. An event holds identifiers, names, kinds and
outcomes only, never contents or secrets (the runtime refuses anything else when the event is written), and these
routes answer exactly the stored fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from nodestro_runtime import activity
from nodestro_runtime.activity import ActivityEvent, Outcome, Source
from nodestro_runtime.schemas import now
from pydantic import BaseModel

from .api import Ctx
from .permissions import ADMIN

router = APIRouter(prefix="/api")

#: The most events one page holds.
MAX_LIMIT = 1000
#: The media type of each export format.
MEDIA_TYPES: dict[str, str] = {"jsonl": "application/x-ndjson; charset=utf-8", "csv": "text/csv; charset=utf-8"}

ExportFormat = Literal["jsonl", "csv"]
#: Identifiers, names and keys in a filter are bounded like the event's own fields.
Token = Annotated[str | None, Query(min_length=1, max_length=200)]


class ActivityPage(BaseModel):
    """One page of activity events, newest first."""

    events: list[ActivityEvent]
    #: The ``before`` of the next page; ``null`` when this page holds the oldest matching event.
    next_before: str | None = None


class _Filters:
    """The query filters both routes take (``since`` inclusive, ``until`` exclusive; a time without a zone is UTC)."""

    def __init__(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        source: Annotated[list[Source] | None, Query()] = None,
        action: Annotated[list[str] | None, Query()] = None,
        actor: Token = None,
        agent: Token = None,
        resource_type: Token = None,
        resource_id: Token = None,
        outcome: Outcome | None = None,
        run_id: Token = None,
        conversation_id: Token = None,
    ) -> None:
        self.arguments: dict[str, Any] = {
            "since": _utc(since),
            "until": _utc(until),
            "sources": source,
            "actions": action,
            "actor": actor,
            "agent": agent,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "outcome": outcome,
            "run_id": run_id,
            "conversation_id": conversation_id,
        }


Filters = Annotated[_Filters, Depends()]


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


@router.get("/activity", dependencies=ADMIN)
def activity_events(
    ctx: Ctx,
    filters: Filters,
    before: Token = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 200,
) -> ActivityPage:
    """The activity log of this workspace (on a server: of the tenant), newest first (N-13): every file, tool,
    workflow and agent action, with who, which agent, the resource, the outcome and the run. Read only; it never holds
    contents or secrets. Filters combine (all must match); ``source`` and ``action`` may be repeated."""
    found = activity.query(ctx.store, **filters.arguments, before=before, limit=limit + 1)
    page = found[:limit]
    return ActivityPage(events=page, next_before=page[-1].key if len(found) > limit else None)


@router.get(
    "/activity/export",
    dependencies=ADMIN,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Every matching event, newest first, as JSON Lines or CSV (a download)",
            "content": {"application/x-ndjson": {}, "text/csv": {}},
        }
    },
)
def export_activity(
    ctx: Ctx,
    filters: Filters,
    format: Annotated[ExportFormat, Query()] = "jsonl",
) -> StreamingResponse:
    """The complete activity log that matches the filters (no page limit), newest first, as a download: JSON Lines
    (one object per event) or CSV (a header, then one row per event; a cell a spreadsheet would read as a formula
    starts with ``'``). The columns are the runtime's ``activity.FIELDS``."""
    # Read before the answer starts: the store is not used while the download streams.
    events = activity.query(ctx.store, **filters.arguments, limit=None)
    name = f"nodestro-activity-{now().strftime('%Y%m%dT%H%M%SZ')}.{format}"
    return StreamingResponse(
        activity.export(events, format),
        media_type=MEDIA_TYPES[format],
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
