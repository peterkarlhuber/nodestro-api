"""The stored versions of a tool, with who wrote each (ST-036, ADR-0006 R-4).

The adoption dialog shows what the AI changed against the person's previous own version of a tool. A version is
the person's own when it was written in the tool editor (and is no generation job's), or when it is the version they
adopted with exactly the content they saw (its ``content_hash``, from the current adoption or, once a later AI version
ended it, from the ``tool_adopted`` entries of the audit trail); every other version was written by the AI. Read
only: the history comes from the tool library's registry and provenance records, so on a server it covers only the
tenant of the request.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from nodestro_runtime.tool_approvals import AUDIT, ToolApprovalEvent
from nodestro_runtime.tool_provenance import content_hash
from pydantic import BaseModel

if TYPE_CHECKING:
    from nodestro_runtime.storage import DocumentStore
    from nodestro_runtime.tool_library import ToolLibrary

#: ``editor``: written in the tool editor; ``adopted``: an AI version the person adopted as their own; ``ai``: any
#: other version (a generation job, a tool draft, the Architect).
VersionAuthor = Literal["editor", "adopted", "ai"]


class ToolVersion(BaseModel):
    version: int
    #: The registry's status of the version (``approved`` versions run; others are kept for the audit trail).
    status: str
    created_at: datetime | None = None
    author: VersionAuthor
    #: Whether this version is the person's own (``editor`` or ``adopted``).
    own: bool
    description: str = ""
    function_name: str | None = None
    code: str | None = None
    tests: str | None = None
    #: :func:`~nodestro_runtime.tool_provenance.content_hash` of code and tests (``None`` without code).
    content_hash: str | None = None


def _when(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _adopted(library: ToolLibrary, store: DocumentStore, name: str) -> set[tuple[int, str]]:
    """``(version, content_hash)`` of every adoption of ``name``: the current one and those in the audit trail."""
    found: set[tuple[int, str]] = set()
    current = library.provenance.adoption(name)
    if current is not None:
        found.add((current.version, current.content_hash))
    for _key, event in store.items(AUDIT, ToolApprovalEvent):
        if event.kind == "tool_adopted" and event.tool == name:
            version, digest = event.details.get("version"), event.details.get("content_hash")
            if isinstance(version, int) and isinstance(digest, str):
                found.add((version, digest))
    return found


def tool_versions(library: ToolLibrary, store: DocumentStore, name: str) -> list[ToolVersion]:
    """Every stored version of ``name``, newest first; empty for built-in and MCP tools (they have no stored code).

    Raises :class:`KeyError` for a name the library does not know."""
    library.get(name)  # unknown: KeyError (404)
    try:
        records = library.registry.versions(name)
    except Exception:  # a name without stored versions (an MCP tool)
        return []
    authored = library.provenance.authored(name)
    adopted = _adopted(library, store, name)
    found: list[ToolVersion] = []
    for record in records:
        if record.kind == "trusted":
            continue
        digest = content_hash(record.code, record.tests or "") if record.code is not None else None
        author: VersionAuthor = "ai"
        if record.version in authored and not record.requirement:
            author = "editor"
        elif digest is not None and (record.version, digest) in adopted:
            author = "adopted"
        found.append(
            ToolVersion(
                version=record.version,
                status=str(record.status),
                created_at=_when(record.created_at),
                author=author,
                own=author != "ai",
                description=record.description,
                function_name=record.function_name,
                code=record.code,
                tests=record.tests,
                content_hash=digest,
            )
        )
    return sorted(found, key=lambda item: item.version, reverse=True)
