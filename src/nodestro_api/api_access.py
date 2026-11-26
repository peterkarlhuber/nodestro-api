"""The API of the access model v2 (N-06; runtime ``docs/access-grants.md``): grants, denies and "who may do what and
why" (BER-1). Off by default, like the runtime: only while ``NODESTRO_ACCESS_GRANTS=1`` together with
``NODESTRO_TOOL_APPROVALS=1``.

- ``GET /api/access/state`` (``read``): whether the access model is on, and the global safety limits. Always answers,
  so the studio knows whether to show scope and duration on an approval card.
- ``GET /api/access/grants`` and ``GET /api/access/denies`` (``admin``): every record of the tenant, newest first,
  revoked, expired and used ones included.
- ``POST /api/access/grants`` and ``POST /api/access/denies`` (``admin``): store a record. The writer is always the
  request's signed-in person (:class:`~nodestro_runtime.tool_approvals.Person`), never a field of the body; a
  principal that is not a person (an API key, a machine) is refused (``403 access.person_required``). A tool grant
  names the tool as it is now (version, classification, MCP definition), read from the tool library, never from the
  body.
- ``DELETE /api/access/grants/{grant_id}`` and ``DELETE /api/access/denies/{deny_id}`` (``admin``): revoke a record
  (revoking twice changes nothing; ``404 access.not_found`` for an unknown id).
- ``GET /api/access/who-may`` (``admin``): "who may do what and why" for every resource, action, agent and mode an
  active record names (:func:`~nodestro_runtime.tool_policy.who_may`), narrowed by ``resource_type`` and
  ``resource_id``, ``agent`` and ``mode``. Tools are explained as they are now, with their MCP server.
- ``GET /api/access/explain`` (``admin``): the same for one access (``resource_type``, ``resource_id``, ``action``,
  ``agent``, ``mode``).

While the switch is off, every route but ``state`` answers ``404 access.grants_disabled`` (like other switched-off
parts of the API), and nothing is read or written. Every route works on the request's tenant only (its workspace and
store); ids of other tenants are not found. No route here is reachable with an API key: the published APIs, the app
contract and Nodestro's MCP server have their own routes, and none of them writes grants or denies. Every change is
audited by the runtime (``access_*`` in the tool approval audit and the activity log). The HTTP status of each
``access.*`` problem code is in :data:`nodestro_api.serving.ACCESS_ERROR_STATUS`.

One more writer, outside these routes: deleting a conversation with an own agent (N-04 L-2) revokes its grants for
"this conversation" through :func:`revoke_conversation_grants`, in the name of the person who deleted it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from nodestro_runtime.conversations import ConversationNotFound
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.tool_approvals import AccessRules, Person, access_grants_enabled, tool_facts
from nodestro_runtime.tool_policy import (
    SAFETY_LIMITS,
    SITE_ACTIONS,
    AccessAction,
    AccessExplanation,
    AccessMode,
    AccessRequest,
    AccessResource,
    AccessScope,
    Deny,
    DenyDuration,
    Grant,
    GrantDuration,
    ResourceType,
    ToolFacts,
    ToolNow,
    effect_for,
    explain,
    who_may,
)
from pydantic import AwareDatetime, BaseModel, Field

from .api import Ctx, owner_of
from .permissions import ADMIN, READ, Principal
from .serving import ApiError

router = APIRouter(prefix="/api/access")


class AccessState(BaseModel):
    #: Whether grants and denies take part in the decisions (``NODESTRO_ACCESS_GRANTS=1`` with per-call approvals).
    enabled: bool
    #: The global safety limits (code, not data): name -> what it means. No grant lifts them.
    safety_limits: dict[str, str]


class ToolExpected(BaseModel):
    """The tool as the person saw it (an approval request's ``version`` and ``definition_hash``)."""

    version: int | None = None
    definition_hash: str | None = Field(default=None, max_length=200)


class GrantIn(BaseModel):
    """A grant as a person gives it. The writer, the time and (for a tool) the tool's facts come from the server."""

    resource_type: ResourceType
    resource_id: str = Field(min_length=1, max_length=4096)
    actions: list[AccessAction] = Field(min_length=1, max_length=8)
    duration: GrantDuration
    scope: AccessScope = "all"
    agents: list[str] = Field(default_factory=list, max_length=200)
    mode: AccessMode = "supervised"
    expires_at: AwareDatetime | None = None
    conversation_id: str | None = Field(default=None, max_length=200)
    #: For a tool: the version and MCP definition the person saw (L-1). When the library holds another one now, the
    #: grant is refused (``409 access.tool_changed``) instead of covering a tool the person never saw. The stored
    #: facts still come from the library.
    expected: ToolExpected | None = None


class AccessRow(AccessExplanation):
    """An explanation as this API serves it. ``connections_considered`` (tools only): whether the connection the
    agent's tool settings name was counted. It is only known for one agent (``agent``); without it a deny on a
    connection does not show in a tool's row (L-3)."""

    connections_considered: bool | None = None


class DenyIn(BaseModel):
    resource_type: ResourceType
    resource_id: str = Field(min_length=1, max_length=4096)
    actions: list[AccessAction] = Field(min_length=1, max_length=8)
    scope: AccessScope = "all"
    agents: list[str] = Field(default_factory=list, max_length=200)
    #: ``None``: both modes.
    mode: AccessMode | None = None
    duration: DenyDuration = "until_revoked"
    expires_at: AwareDatetime | None = None


def _require_enabled() -> None:
    if not access_grants_enabled():
        raise ApiError(404, "access.grants_disabled", "Grants and denies are not switched on for this application")


def _person(request: Request) -> Person:
    """The signed-in person of the request; anything else is refused (``access.person_required``)."""
    principal: Principal = request.state.principal
    return Person(principal.id)


def revoke_conversation_grants(ctx: Ctx, request: Request, conversation_id: str, owner: str | None) -> None:
    """Revoke the grants for "this conversation" of a deleted conversation (N-04 L-2), whether or not the access model
    is switched on now. The writer is the request's person; for a principal that is not one (an API key on a desktop)
    the conversation's owner, or ``local`` (the desktop's only person)."""
    principal: Principal | None = getattr(request.state, "principal", None)
    try:
        by = Person(principal.id if principal is not None else "local")
    except NodestroError:
        by = Person(owner or "local")
    AccessRules(ctx.store).revoke_conversation(conversation_id, by=by)


def _rules(ctx: Ctx) -> AccessRules:
    return AccessRules(ctx.store)


def _agent_connection(ctx: Ctx, agent: str | None, tool: str) -> str | None:
    """The connection the agent ``<workflow id>/<node id>`` uses for ``tool`` in its saved workflow (its tool
    settings), as the gate names it (``ToolGate.related``); ``None`` without one or for an unknown agent."""
    if not agent or "/" not in agent:
        return None
    workflow_id, node_id = agent.rsplit("/", 1)
    try:
        workflow = ctx.workflow(workflow_id)
    except (KeyError, ValueError, NodestroError):
        return None
    node = next((item for item in workflow.nodes if item.id == node_id), None)
    settings = node.agent.tool_settings.get(tool) if node is not None and node.agent is not None else None
    return settings.connection_id if settings is not None else None


def _tool_now(ctx: Ctx, name: str, agent: str | None = None) -> ToolNow:
    """A tool as the gate sees it now: facts, kind, MCP server and, for one agent, the connection its tool settings
    name (unknown tools: no facts, so no grant matches)."""
    try:
        info = ctx.tools.get(name)
    except KeyError:
        return ToolNow()
    capability, version, kind, definition = tool_facts(ctx.tools, name)
    related: list[AccessResource] = []
    if kind == "mcp" and info.server_id:
        related.append(AccessResource(type="mcp_server", id=info.server_id))
    connection = _agent_connection(ctx, agent, name)
    if connection:
        related.append(AccessResource(type="connection", id=connection))
    return ToolNow(
        facts=ToolFacts.of(capability, version=version, definition_hash=definition), kind=kind, related=tuple(related)
    )


def _row(found: AccessExplanation, agent: str | None) -> AccessRow:
    considered = (agent is not None) if found.resource.type == "tool" else None
    return AccessRow(**dict(found), connections_considered=considered)


def _local_servers(ctx: Ctx) -> set[str]:
    return {server.id for server in ctx.mcp.servers() if server.transport == "stdio"}


def _protected(ctx: Ctx) -> tuple[str, ...]:
    return (str(ctx.data_dir.resolve()),)


@router.get("/state", dependencies=READ)
def access_state() -> AccessState:
    return AccessState(enabled=access_grants_enabled(), safety_limits=dict(SAFETY_LIMITS))


@router.get("/grants", dependencies=ADMIN)
def list_grants(ctx: Ctx) -> list[Grant]:
    _require_enabled()
    return _rules(ctx).grants()


@router.get("/denies", dependencies=ADMIN)
def list_denies(ctx: Ctx) -> list[Deny]:
    _require_enabled()
    return _rules(ctx).denies()


def _conversation_seen(ctx: Ctx, request: Request, conversation_id: str) -> None:
    """The conversation a ``conversation`` grant names is one the caller sees: a conversation with an own agent
    (``conv_…``) only its owner (``404 conversation.not_found``, also for an unknown one), and the root run of a
    workflow run (the conversation of a run chat) only who sees that run (``404 run.not_found``). An administrator
    changes no other member's conversation this way."""
    owner = owner_of(request)
    if conversation_id.startswith("conv_"):
        try:
            ctx.conversations.get(conversation_id, owner)
        except ConversationNotFound:
            raise ApiError(
                404, "conversation.not_found", "The conversation or its part does not exist", id=conversation_id
            ) from None
    elif not ctx.conversations.run_visible(conversation_id, owner):
        raise ApiError(404, "run.not_found", f"Run {conversation_id!r} does not exist", id=conversation_id, kind="run")


@router.post("/grants", status_code=201, dependencies=ADMIN)
def create_grant(body: GrantIn, ctx: Ctx, request: Request) -> Grant:
    """Grant an access. A tool grant holds for the tool as it is now and ends with a new version, another
    classification or another MCP definition; deleting for good is never granted (``access.grant_destructive``)."""
    _require_enabled()
    by = _person(request)
    if body.conversation_id:  # only a conversation the caller sees (W3-S1 review)
        _conversation_seen(ctx, request, body.conversation_id)
    tool: ToolFacts | None = None
    if body.resource_type == "tool":
        tool = _tool_now(ctx, body.resource_id).facts
        if tool is None:
            raise KeyError(body.resource_id)
        seen = body.expected
        if seen is not None and (seen.version != tool.version or seen.definition_hash != tool.definition_hash):
            raise ApiError(
                409,
                "access.tool_changed",
                "The tool changed since it was shown; look at it again before granting",
                tool=body.resource_id,
            )
    elif body.expected is not None:
        raise NodestroError("access.invalid", "Only a tool grant names the tool as it was seen")
    return _rules(ctx).grant(
        resource=AccessResource(type=body.resource_type, id=body.resource_id),
        actions=body.actions,
        duration=body.duration,
        by=by,
        scope=body.scope,
        agents=body.agents,
        mode=body.mode,
        expires_at=body.expires_at,
        conversation_id=body.conversation_id,
        tool=tool,
    )


@router.post("/denies", status_code=201, dependencies=ADMIN)
def create_deny(body: DenyIn, ctx: Ctx, request: Request) -> Deny:
    """Block an access, whatever is granted (a deny for a folder also blocks every folder below it)."""
    _require_enabled()
    found = _rules(ctx).deny(
        resource=AccessResource(type=body.resource_type, id=body.resource_id),
        actions=body.actions,
        by=_person(request),
        scope=body.scope,
        agents=body.agents,
        mode=body.mode,
        duration=body.duration,
        expires_at=body.expires_at,
    )
    ctx.folder_access_changed()  # N-12: a denied folder leaves the folder indexes at their next sync
    return found


@router.delete("/grants/{grant_id}", dependencies=ADMIN)
def revoke_grant(grant_id: str, ctx: Ctx, request: Request) -> Grant:
    """Revoke a grant; it no longer applies from the next access on, also for running tasks."""
    _require_enabled()
    found = _rules(ctx).revoke_grant(grant_id, by=_person(request))
    ctx.folder_access_changed()  # N-12: a revoked folder grant's index is dropped now
    return found


@router.delete("/denies/{deny_id}", dependencies=ADMIN)
def revoke_deny(deny_id: str, ctx: Ctx, request: Request) -> Deny:
    _require_enabled()
    found = _rules(ctx).revoke_deny(deny_id, by=_person(request))
    ctx.folder_access_changed()  # N-12: a folder no longer denied is indexed again
    return found


def _who_may(
    ctx: Ctx,
    *,
    resource: AccessResource | None,
    agent: str | None,
    mode: AccessMode | None,
    at: datetime,
) -> list[AccessRow]:
    grants, denies = _rules(ctx).current()
    names = {item.resource.id for item in [*grants, *denies] if item.resource.type == "tool"}
    found = who_may(
        grants,
        denies,
        at=at,
        resource=resource,
        agent=agent,
        mode=mode,
        offline=ctx.strict_offline(),
        protected_folders=_protected(ctx),
        tools={name: _tool_now(ctx, name, agent) for name in names},
        local_servers=_local_servers(ctx),
    )
    return [_row(item, agent) for item in found]


def _resource(resource_type: ResourceType | None, resource_id: str | None) -> AccessResource | None:
    if resource_type is None and resource_id is None:
        return None
    if resource_type is None or not resource_id:
        raise NodestroError("access.invalid", "A resource is named by its type and its id")
    return AccessResource(type=resource_type, id=resource_id)


@router.get("/who-may", dependencies=ADMIN)
def who_may_route(
    ctx: Ctx,
    resource_type: ResourceType | None = None,
    resource_id: Annotated[str | None, Query(max_length=4096)] = None,
    agent: Annotated[str | None, Query(max_length=400)] = None,
    mode: AccessMode | None = None,
) -> list[AccessRow]:
    """ "Who may do what and why": what decides each access an active grant or deny names, and its source. A tool's
    MCP server always counts; the connection of its agent only when ``agent`` names one (``connections_considered``)."""
    _require_enabled()
    from nodestro_runtime.schemas import now

    return _who_may(ctx, resource=_resource(resource_type, resource_id), agent=agent, mode=mode, at=now())


@router.get("/explain", dependencies=ADMIN)
def explain_route(
    ctx: Ctx,
    resource_type: ResourceType,
    resource_id: Annotated[str, Query(min_length=1, max_length=4096)],
    action: AccessAction | None = None,
    agent: Annotated[str | None, Query(max_length=400)] = None,
    mode: AccessMode = "supervised",
) -> AccessRow:
    """Why one access is allowed or refused at its call site now (default action: the one the runtime checks there,
    ``read`` for folders). A tool is taken as it is now, with its MCP server."""
    _require_enabled()
    from nodestro_runtime.schemas import now

    resource = AccessResource(type=resource_type, id=resource_id)
    chosen: AccessAction = action or SITE_ACTIONS.get(resource_type, "read")
    tool = _tool_now(ctx, resource_id, agent) if resource_type == "tool" else ToolNow()
    grants, denies = _rules(ctx).current()
    try:
        request = AccessRequest(
            resource=resource,
            action=chosen,
            effect=effect_for(resource_type, chosen, tool=tool.facts),
            agent=agent,
            mode=mode,
            offline=ctx.strict_offline(),
            protected_folders=_protected(ctx) if resource_type == "folder" else (),
            tool=tool.facts,
        )
    except ValueError:
        raise NodestroError("access.invalid", "The access is not valid") from None
    found = explain(
        request,
        grants=grants,
        denies=denies,
        at=now(),
        tool_kind=tool.kind if resource_type == "tool" else None,
        related=tool.related,
        local_servers=_local_servers(ctx),
    )
    return _row(found, agent)


__all__ = ["AccessRow", "AccessState", "DenyIn", "GrantIn", "ToolExpected", "revoke_conversation_grants", "router"]
