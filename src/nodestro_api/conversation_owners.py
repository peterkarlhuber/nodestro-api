"""Conversations without an owner on a server, and the operator's controlled assignment of one (W3-S1, API-073).

A conversation of the Nodestro Assistant from before owners were recorded, or a conversation with an own agent (N-04)
made on a desktop and restored into a server, has no owner (``owner`` ``None``). On a server such a conversation is
seen by nobody, administrators included: no list, detail, file, search hit, preview, adoption, export or run shows
it (:func:`nodestro_api.api.owner_of`, :meth:`nodestro_api.assistant.Assistant.check_owner`, the runtime's
``conversations.visible``). Only the desktop's single person sees it. Nothing is deleted.

It becomes visible again only when the server's operator assigns it an owner with
``nodestro-server assistant backfill-owners`` (:func:`nodestro_api.server.backfill_conversation_owners`):

- :func:`report` only reads: the conversations without an owner per kind, with the person their files name as a
  suggestion (an assistant conversation whose every file was uploaded by one server user). Nothing is assigned from a
  suggestion.
- :func:`assign` gives exactly the named conversations exactly the named user, who must be a member of the tenant
  (the caller checks it). It assigns only where there is no owner; an existing owner is never replaced. Each
  assignment is one entry of the tenant's access audit trail (``conversation_owner_assigned``: the operator, the
  conversation, the new owner and the time; never a title or content).

Why a command and not a route: the operator already holds the database and the tenants' membership (``set-role``,
``remove-member``), and a route would let a tenant administrator assign another person's ownerless conversation to
themselves and read it. There is no default user, no wildcard and no "the logged-in user".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from nodestro_runtime import conversations as own
from nodestro_runtime.input_files import COLLECTION as INPUT_FILES
from nodestro_runtime.input_files import InputFile
from pydantic import BaseModel, Field

from .access_audit import OPERATOR, AccessEvent, record
from .assistant import Conversation as AssistantConversation

#: The collection of assistant conversations (``nodestro_api.assistant``).
ASSISTANT = "assistant_conversations"

ConversationKind = Literal["assistant", "agent"]


class Ownerless(BaseModel):
    """The conversations of one tenant without an owner (:func:`report`)."""

    #: Conversation id -> its kind (``assistant`` or ``agent``: with an own agent, N-04).
    conversations: dict[str, ConversationKind] = Field(default_factory=dict)
    #: Conversation id -> the one server user all its files were uploaded by: a suggestion, never applied by itself.
    suggested: dict[str, str] = Field(default_factory=dict)


class Assignment(BaseModel):
    """What :func:`assign` did (or would do, in a dry run)."""

    owner: str
    dry_run: bool = False
    #: Conversation id -> its kind: got the owner.
    assigned: dict[str, ConversationKind] = Field(default_factory=dict)
    #: Already had an owner: left as it was (never overwritten).
    owned: list[str] = Field(default_factory=list)
    #: No conversation with this id in the tenant.
    unknown: list[str] = Field(default_factory=list)


def _own_conversation(store: Any, conversation_id: str) -> own.Conversation | None:
    found = store.get(own.COLLECTION, conversation_id, own.Conversation)
    tenant = getattr(store, "tenant_id", None)
    if found is None or (tenant is not None and found.tenant_id != tenant):
        return None
    return found  # type: ignore[no-any-return]


def report(store: Any) -> Ownerless:
    """The tenant's conversations without an owner, with suggestions; reads only."""
    found = Ownerless()
    for conversation in store.list(ASSISTANT, AssistantConversation):
        if conversation.owner is not None:
            continue
        found.conversations[conversation.id] = "assistant"
        uploaders = set()
        for item in conversation.files:
            stored = store.get(INPUT_FILES, item.id, InputFile)
            if stored is not None and stored.origin == "assistant":
                uploaders.add(stored.uploaded_by)
        if len(uploaders) == 1 and next(iter(uploaders)) not in ("", "local"):
            found.suggested[conversation.id] = next(iter(uploaders))
    tenant = getattr(store, "tenant_id", None)
    for item in store.list(own.COLLECTION, own.Conversation):
        if item.owner is None and (tenant is None or item.tenant_id == tenant):
            found.conversations[item.id] = "agent"
    return found


def assign(
    store: Any, owner: str, conversation_ids: Iterable[str], *, dry_run: bool = False, actor: str = OPERATOR
) -> Assignment:
    """Give each named conversation without an owner the owner ``owner`` (a member of the store's tenant: the caller
    checks it), under the conversation's lock; a conversation with an owner keeps it. Each assignment is recorded in
    the access audit trail in the same transaction. ``dry_run`` reports only."""
    if not owner or owner == own.NO_OWNER:
        raise ValueError("An owner is a user id")
    done = Assignment(owner=owner, dry_run=dry_run)
    for conversation_id in dict.fromkeys(conversation_ids):
        with store.transaction():
            kind: ConversationKind
            if conversation_id.startswith("conv_"):
                kind, collection = "agent", own.COLLECTION
            else:
                kind, collection = "assistant", ASSISTANT
            store.lock(collection, conversation_id)
            current: Any = (
                _own_conversation(store, conversation_id)
                if kind == "agent"
                else store.get(ASSISTANT, conversation_id, AssistantConversation)
            )
            if current is None:
                done.unknown.append(conversation_id)
                continue
            if current.owner is not None:
                done.owned.append(conversation_id)
                continue
            done.assigned[conversation_id] = kind
            if dry_run:
                continue
            store.put(collection, conversation_id, current.model_copy(update={"owner": owner}))
            record(
                store,
                AccessEvent(
                    kind="conversation_owner_assigned",
                    actor=actor,
                    target_type="user",
                    target_id=owner,
                    details={"conversation_id": conversation_id, "conversation_kind": kind},
                ),
            )
    return done


def chat_file_visible(store: Any, item: InputFile, owner: str | None) -> bool:
    """Whether a file of a conversation (origin ``assistant`` or ``conversation``) belongs to a conversation that
    ``owner`` sees. ``None`` (the desktop's single person) sees every one. Anyone else sees a file only in a
    conversation they own: never in one without an owner, never in a file no conversation holds (fail closed)."""
    if owner is None:
        return True
    if owner == own.NO_OWNER:
        return False
    if item.origin == "assistant":
        holders: list[Any] = [
            conversation
            for conversation in store.list(ASSISTANT, AssistantConversation)
            if any(entry.id == item.id for entry in conversation.files)
        ]
    else:
        tenant = getattr(store, "tenant_id", None)
        holders = [
            conversation
            for conversation in store.list(own.COLLECTION, own.Conversation)
            if (tenant is None or conversation.tenant_id == tenant)
            and any(entry.id == item.id for entry in conversation.files)
        ]
    return bool(holders) and all(conversation.owner == owner for conversation in holders)


__all__ = ["Assignment", "Ownerless", "assign", "chat_file_visible", "report"]
