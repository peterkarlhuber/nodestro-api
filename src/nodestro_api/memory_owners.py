"""Memory spaces without an owner on a server, and the operator's controlled assignment of one (RT-089c).

On a server a memory space belongs to its first writer (:class:`~nodestro_runtime.memory_spaces.MemorySpaces`): only
its owner's runs, agent tests and requests list, read and change it. A space that already had files before owners
were recorded has no owner and is used by nobody, administrators included: no list, file, run, preview, attachment
or export shows it. Nothing is deleted, and nothing claims it by itself.

It becomes usable again only when the server's operator assigns it an owner with
``nodestro-server memory backfill-owners`` (:func:`nodestro_api.server.backfill_memory_owners`):

- :func:`report` only reads: the spaces of a tenant that have files but no owner (names, file counts and sizes).
- :func:`assign` gives exactly the named spaces exactly the named user, who must be a member of the tenant (the
  caller checks it). Only a space with files and without an owner gets one; an existing owner is never replaced.
  Each assignment is one entry of the tenant's access audit trail (``memory_space_owner_assigned``: the operator,
  the space, the new owner and the time; never a file's content), written in the same transaction.

Why a command and not a route: as for conversations without an owner (:mod:`nodestro_api.conversation_owners`), a
route would let a tenant administrator give another person's space to themselves and read it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nodestro_runtime.memory_spaces import MemorySpaceInfo, MemorySpaces, check_space_name
from pydantic import BaseModel, Field

from .access_audit import OPERATOR, AccessEvent, record


class MemoryAssignment(BaseModel):
    """What :func:`assign` did (or would do, in a dry run)."""

    owner: str
    dry_run: bool = False
    #: Got the owner.
    assigned: list[str] = Field(default_factory=list)
    #: Already had an owner: left as it was (never overwritten).
    owned: list[str] = Field(default_factory=list)
    #: No space with files of this name in the tenant.
    unknown: list[str] = Field(default_factory=list)


def report(spaces: MemorySpaces) -> list[MemorySpaceInfo]:
    """The tenant's spaces with files and without an owner; reads only."""
    return spaces.ownerless()


def assign(
    spaces: MemorySpaces, owner: str, names: Iterable[str], *, dry_run: bool = False, actor: str = OPERATOR
) -> MemoryAssignment:
    """Give each named space with files and without an owner the owner ``owner`` (a member of the tenant: the caller
    checks it); a space with an owner keeps it. Each assignment is recorded in the access audit trail in the same
    transaction. ``dry_run`` reports only."""
    store: Any = spaces.owners
    if store is None:
        raise ValueError("Memory spaces have owners only on a server")
    done = MemoryAssignment(owner=owner, dry_run=dry_run)
    ownerless = {item.name for item in spaces.ownerless()}
    for name in dict.fromkeys(check_space_name(name) for name in names):
        if spaces.owner(name) is not None:
            done.owned.append(name)
        elif name not in ownerless:
            done.unknown.append(name)
        elif dry_run:
            done.assigned.append(name)
        else:
            with store.transaction():
                if not spaces.assign(name, owner):
                    done.owned.append(name)  # another assignment came first
                    continue
                record(
                    store,
                    AccessEvent(
                        kind="memory_space_owner_assigned",
                        actor=actor,
                        target_type="user",
                        target_id=owner,
                        details={"space": name},
                    ),
                )
            done.assigned.append(name)
    return done


__all__ = ["MemoryAssignment", "assign", "report"]
