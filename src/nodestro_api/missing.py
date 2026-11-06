"""A record that another one refers to no longer exists (API-053).

Raised where a request or a stored record points to a workflow, run or publication by its id and that record is
gone (a test series whose workflow was deleted, a key for a withdrawn publication). The studio API answers it like
the not-found answer of a route's own record, ``404 <kind>.not_found`` with ``id``, ``name`` (the id, for older
clients) and ``kind``, plus ``display``: the name the reference recorded for the missing record, when it recorded
one, so the interface can name what is missing instead of showing an id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from nodestro_runtime.storage import DocumentStore

ReferenceKind = Literal["workflow", "run", "publication"]
#: The kinds a reference can name (``<kind>.not_found`` with ``display``; see :mod:`nodestro_api.error_codes`).
REFERENCE_KINDS: tuple[ReferenceKind, ...] = ("workflow", "run", "publication")


class MissingReference(KeyError):
    """``kind`` ``item_id`` is referenced but gone; ``display`` is its name as the reference knew it ("" unknown)."""

    def __init__(self, kind: ReferenceKind, item_id: str, display: str = "") -> None:
        super().__init__(item_id)
        self.kind = kind
        self.item_id = item_id
        self.display = display.strip()[:200]


class _NamedRun(BaseModel):
    workflow_name: str = ""


def workflow_display(store: DocumentStore, workflow_id: str) -> str:
    """The last name a deleted workflow had, as its newest run recorded it ("" without a run).

    One indexed page of one run summary (``list_page`` with ``equals``), never every run."""
    try:
        page, _more = store.list_page("runs", _NamedRun, limit=1, summary=True, equals={"workflow_id": workflow_id})
    except Exception:  # a name is a nicety: the 404 must not become a 500
        return ""
    return page[0].workflow_name if page else ""
