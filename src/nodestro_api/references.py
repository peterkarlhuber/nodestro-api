"""Where a knowledge file, a tool or a memory space is used ("used in", UX-7).

Read only: the answer lists the users and changes nothing; deleting an object stays exactly as it was. Everything
comes from the context of the request, so on a server the answer covers only the tenant of the request's principal.

Users are

- the agents (and start nodes, for knowledge files) of the saved workflows, in their current version;
- the agents saved in the agent library;
- the active release of a published API, for knowledge files and memory spaces. A release reads them live, so
  deleting one changes what the published API does. Tools are frozen into a release and are not listed for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from nodestro_runtime.engine import node_label
from nodestro_runtime.schemas import AgentConfig, AgentTemplate, Workflow
from pydantic import BaseModel, Field

from .publishing import Publication

if TYPE_CHECKING:
    from .context import Nodestro

ReferenceKind = Literal["knowledge", "tool", "memory"]


class ReferenceUser(BaseModel):
    """One place that uses the object."""

    kind: Literal["workflow", "template", "publication"]
    #: The workflow, agent library entry or publication.
    id: str
    name: str
    #: The node in the workflow (``None`` for a library agent or a publication).
    node_id: str | None = None
    node_label: str | None = None
    #: ``start``: the start node gives the file to every run (knowledge files only).
    role: Literal["agent", "start"] = "agent"
    #: The number of the active release (publications only).
    release: int | None = None
    #: The published workflow (publications only), to open it in the editor.
    workflow_id: str | None = None


class Reference(BaseModel):
    """The users of one object."""

    ref: str
    users: list[ReferenceUser] = Field(default_factory=list)


def _uses(kind: ReferenceKind, agent: AgentConfig) -> list[str]:
    if kind == "knowledge":
        return list(agent.knowledge.file_ids)
    if kind == "tool":
        return list(agent.tools)
    return [agent.memory.space] if agent.memory.mode == "persistent" and agent.memory.space else []


def references(ctx: Nodestro, kind: ReferenceKind, ref: str | None = None) -> list[Reference]:
    """The objects of ``kind`` that are used somewhere, each with its users (only ``ref`` when given)."""
    found: dict[str, list[ReferenceUser]] = {}

    def add(item: str, user: ReferenceUser) -> None:
        if ref is None or item == ref:
            users = found.setdefault(item, [])
            if user not in users:
                users.append(user)

    for workflow in sorted(ctx.store.list("workflows", Workflow), key=lambda item: item.name.lower()):
        for node in workflow.nodes:
            if node.agent is not None:
                for item in _uses(kind, node.agent):
                    add(
                        item,
                        ReferenceUser(
                            kind="workflow",
                            id=workflow.id,
                            name=workflow.name,
                            node_id=node.id,
                            node_label=node_label(node),
                        ),
                    )
            if kind == "knowledge" and node.type == "start" and node.start is not None:
                for item in node.start.file_ids:
                    add(
                        item,
                        ReferenceUser(
                            kind="workflow",
                            id=workflow.id,
                            name=workflow.name,
                            node_id=node.id,
                            node_label=node_label(node),
                            role="start",
                        ),
                    )
    for template in sorted(ctx.store.list("templates", AgentTemplate), key=lambda item: item.agent.name.lower()):
        for item in _uses(kind, template.agent):
            add(item, ReferenceUser(kind="template", id=template.id, name=template.agent.name))
    if kind != "tool":
        for publication in sorted(ctx.publications.all(), key=lambda item: item.name.lower()):
            for item in _release_uses(ctx, kind, publication):
                add(
                    item,
                    ReferenceUser(
                        kind="publication",
                        id=publication.id,
                        name=publication.name,
                        release=publication.active_release,
                        workflow_id=publication.workflow_id,
                    ),
                )
    return [Reference(ref=item, users=users) for item, users in sorted(found.items())]


def _release_uses(ctx: Nodestro, kind: ReferenceKind, publication: Publication) -> list[str]:
    if publication.active_release is None:
        return []
    try:
        bundle = ctx.publications.release(publication.id, publication.active_release).bundle
    except KeyError:
        return []
    if kind == "knowledge":
        return [item.id for item in bundle.knowledge]
    return [item.space for item in bundle.memory]
