"""The API of connected MCP services (ADR-0007, work package WP-3).

Administrators connect remote MCP servers, test the connection (which lists the server's tools), and approve each
tool one by one with a classification (``read``, ``write``, ``destructive``). Editors then assign approved tools to
agents like any other tool (``GET /api/tools`` lists them with ``kind: "mcp"``). Everything lives in the tenant's
own store; tokens are write-only and never returned.

Permissions: ``read`` lists servers and tools; ``admin`` adds, changes, removes, tests, approves and reads the audit
events.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse
from nodestro_runtime.mcp.hub import sdk_available
from nodestro_runtime.mcp.models import McpEvent, McpServerIn, McpServerView, McpTool, ToolAccess, ToolDefinition
from nodestro_runtime.mcp_access import unattended_mcp_enabled
from nodestro_runtime.problems import NodestroError
from pydantic import BaseModel, Field

from .api import Ctx
from .permissions import ADMIN, READ

router = APIRouter(prefix="/api/mcp")


def _actor(request: Request) -> str | None:
    principal = getattr(request.state, "principal", None)
    return getattr(principal, "id", None)


def _server(ctx: Ctx, server_id: str) -> Any:
    try:
        return ctx.mcp.server(server_id)
    except NodestroError as exc:
        if exc.code == "mcp.server_not_found":
            raise KeyError(server_id) from None
        raise


class McpStatus(BaseModel):
    #: The MCP SDK is installed (Python 3.11+); without it servers can be configured but not reached.
    available: bool
    #: Tokens can be stored (encrypted); otherwise they are kept for the session only.
    token_vault: bool
    #: The operator's list of allowed endpoints applies (server profile: required, empty = no MCP).
    endpoint_list: list[str] | None
    #: Local development servers (loopback ``http``) can be allowed per server (desktop only).
    loopback_possible: bool
    #: Runs without a person may use MCP tools (operator switch after the security acceptance).
    unattended_enabled: bool
    #: Tools that change data can be used (per-call approval); until then only ``read`` tools are usable.
    write_tools_enabled: bool


class McpToolView(BaseModel):
    server_id: str
    name: str
    tool_name: str
    status: str
    access: ToolAccess
    suggested_access: ToolAccess
    hash: str
    definition: ToolDefinition
    approved_definition: ToolDefinition | None = None
    approved_hash: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    discovered_at: datetime
    last_seen_at: datetime
    #: Whether agents can use the tool right now; otherwise ``refusal`` names the reason (an ``mcp.*`` code).
    usable: bool
    refusal: str | None = None


class McpDiscovery(BaseModel):
    server: McpServerView
    protocol_version: str | None
    tools: list[McpToolView]
    skipped: list[dict[str, str]] = Field(default_factory=list)


class McpDecision(BaseModel):
    approve: bool
    access: ToolAccess | None = None
    #: The definition hash the administrator reviewed; a different current definition is refused.
    expected_hash: str | None = None


def _tool_view(ctx: Ctx, tool: McpTool) -> McpToolView:
    refusal: str | None = None
    try:
        ctx.mcp.check(tool.tool_name)
    except NodestroError as exc:
        refusal = exc.code
    return McpToolView(
        **tool.model_dump(
            include={
                "server_id",
                "name",
                "tool_name",
                "status",
                "access",
                "hash",
                "definition",
                "approved_definition",
                "approved_hash",
                "approved_by",
                "approved_at",
                "discovered_at",
                "last_seen_at",
            }
        ),
        suggested_access=tool.suggested_access(),
        usable=refusal is None,
        refusal=refusal,
    )


@router.get("/status", dependencies=READ)
def mcp_status(ctx: Ctx) -> McpStatus:
    policy = ctx.mcp.hub.policy
    return McpStatus(
        available=sdk_available(),
        token_vault=ctx.mcp.vault.persistent,
        endpoint_list=None if policy.allowlist is None else [f"{s}://{h}:{p}" for s, h, p in policy.allowlist],
        loopback_possible=policy.loopback_possible,
        unattended_enabled=unattended_mcp_enabled(),
        write_tools_enabled=bool(ctx.mcp.write_gate),
    )


@router.get("/servers", dependencies=READ)
def list_servers(ctx: Ctx) -> list[McpServerView]:
    return ctx.mcp.views()


@router.post("/servers", dependencies=ADMIN, status_code=201)
def add_server(ctx: Ctx, data: McpServerIn, request: Request) -> McpServerView:
    return ctx.mcp.view(ctx.mcp.add(data, actor=_actor(request)))


@router.get("/servers/{server_id}", dependencies=READ)
def get_server(ctx: Ctx, server_id: str) -> McpServerView:
    return ctx.mcp.view(_server(ctx, server_id))


@router.put("/servers/{server_id}", dependencies=ADMIN)
def update_server(ctx: Ctx, server_id: str, data: McpServerIn, request: Request) -> McpServerView:
    _server(ctx, server_id)
    return ctx.mcp.view(ctx.mcp.update(server_id, data, actor=_actor(request)))


@router.delete("/servers/{server_id}", dependencies=ADMIN, status_code=204)
def remove_server(ctx: Ctx, server_id: str, request: Request) -> None:
    _server(ctx, server_id)
    ctx.mcp.remove(server_id, actor=_actor(request))


@router.post("/servers/{server_id}/discover", dependencies=ADMIN)
async def discover_server(ctx: Ctx, server_id: str, request: Request) -> McpDiscovery:
    """Connect, negotiate the protocol version and list the tools (the "test connection" of the studio)."""
    _server(ctx, server_id)
    server, discovery = await ctx.mcp.adiscover(server_id, actor=_actor(request))
    return McpDiscovery(
        server=ctx.mcp.view(ctx.mcp.server(server.id)),
        protocol_version=discovery.protocol_version,
        tools=[_tool_view(ctx, tool) for tool in ctx.mcp.tools(server.id)],
        skipped=discovery.skipped,
    )


@router.get("/servers/{server_id}/tools", dependencies=READ)
def list_tools(ctx: Ctx, server_id: str) -> list[McpToolView]:
    _server(ctx, server_id)
    return [_tool_view(ctx, tool) for tool in ctx.mcp.tools(server_id)]


@router.post("/servers/{server_id}/tools/{name:path}/decision", dependencies=ADMIN)
def decide_tool(ctx: Ctx, server_id: str, name: str, decision: McpDecision, request: Request) -> McpToolView:
    _server(ctx, server_id)
    try:
        tool = ctx.mcp.decide(
            server_id,
            name,
            approve=decision.approve,
            access=decision.access,
            expected_hash=decision.expected_hash,
            actor=_actor(request),
        )
    except NodestroError as exc:
        if exc.code == "mcp.tool_not_found":
            raise KeyError(name) from None
        raise
    return _tool_view(ctx, tool)


@router.get("/events", dependencies=ADMIN)
def list_events(ctx: Ctx, limit: int = Query(200, ge=1, le=1000)) -> list[McpEvent]:
    """Administration and policy events, newest first (no secrets, no tool arguments)."""
    return ctx.mcp.events(limit=limit)


# ---- OAuth 2.1 (WP-6) -------------------------------------------------------------------------

_CALLBACK = "/api/mcp/oauth/callback"
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class McpOAuthStart(BaseModel):
    #: Open this URL in the browser to sign in at the server's authorization server.
    authorization_url: str


def _redirect_uri(request: Request) -> str:
    """Nodestro's own callback: the server's configured public origin, or the desktop's loopback address.

    Never taken from a ``Host`` header on a server: the callback must be the address registered with the
    authorization server, and a forged header must not redirect the code elsewhere."""
    origin = getattr(request.app.state, "public_origin", None)
    if origin:
        return f"{str(origin).rstrip('/')}{_CALLBACK}"
    url = request.base_url
    if url.hostname not in _LOOPBACK:
        raise NodestroError(
            "mcp.oauth_redirect_invalid", "Signing in to an MCP server needs the desktop's loopback address"
        )
    return f"{url.scheme}://{url.netloc}{_CALLBACK}"


@router.post("/servers/{server_id}/oauth/start", dependencies=ADMIN)
async def start_oauth(ctx: Ctx, server_id: str, request: Request) -> McpOAuthStart:
    """Discover the server's authorization server, register Nodestro and return the sign-in URL (PKCE S256)."""
    _server(ctx, server_id)
    url = await ctx.mcp.abegin_oauth(server_id, _redirect_uri(request), actor=_actor(request))
    return McpOAuthStart(authorization_url=url)


@router.post("/servers/{server_id}/oauth/sign-out", dependencies=ADMIN)
def sign_out_oauth(ctx: Ctx, server_id: str, request: Request) -> McpServerView:
    _server(ctx, server_id)
    return ctx.mcp.view(ctx.mcp.sign_out(server_id, actor=_actor(request)))


@router.get("/oauth/callback", dependencies=ADMIN, include_in_schema=False)
async def oauth_callback(
    ctx: Ctx,
    request: Request,
    state: str = Query("", max_length=512),
    code: str = Query("", max_length=4096),
    iss: str | None = Query(None, max_length=2000),
    error: str | None = Query(None, max_length=200),
) -> RedirectResponse:
    """Where the authorization server sends the browser back; continues on the studio's services page.

    The result is shown by the studio (``oauth=connected`` or ``oauth_error=<code>``); no token, code or state is
    put in the URL."""
    target = "/tools?tab=services"
    if error or not state or not code:
        # The started sign-in stays unused and expires (runtime: 10 minutes).
        reason = "mcp.oauth_denied" if error else "mcp.oauth_state_invalid"
        return _back(f"{target}&oauth_error={reason}")
    try:
        server = await ctx.mcp.acomplete_oauth(state, code, iss, actor=_actor(request))
    except NodestroError as exc:
        return _back(f"{target}&oauth_error={exc.code}")
    return _back(f"{target}&oauth=connected&server={server.id}")


def _back(url: str) -> RedirectResponse:
    return RedirectResponse(
        url, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    )
