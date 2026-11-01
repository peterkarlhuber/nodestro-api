"""Nodestro as an MCP server for published workflows (ADR-0007 decision D-8, work package WP-11).

**Off by default.** Two deliberate switches are needed, and both are off unless someone turns them on:

1. the operator starts Nodestro with ``NODESTRO_MCP_SERVER=1``;
2. an administrator offers a publication as an MCP tool (``Publication.mcp_tool``, ``PATCH /api/publications/{id}``).

Then ``/api/published/_mcp`` answers the MCP protocol (Streamable HTTP, official SDK, stateless, JSON responses).
Each offered publication with an active release is one tool: its name is the API's slug, its input schema the
release's request schema. A call is an ordinary run of the published API: it goes through
:meth:`~nodestro_api.serving.ServingCore.launch` with the caller's API key, so the key's publications and scopes
(``run``), rate limits, run limits, budgets, approvals, the release's frozen tools and the run history apply exactly
as for ``POST /api/published/{slug}/runs``. There is no second engine and no second approval path. A run that stops
at an approval step answers with its id and status; it is decided in Nodestro or through the published API.

Like the other published endpoints here, it answers only clients on this computer (the network listener does not
serve it). Without the MCP SDK (Python 3.10) it answers ``mcp.unavailable``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import contextvars
import json
import os
from typing import Any

from fastapi import APIRouter, Request, Response

from . import __version__
from .serving import (
    _LOOPBACK,
    PREFIX,
    ApiError,
    KeyAuthenticatedRoute,
    KeyIdentity,
    ServedRelease,
    ServingCore,
    input_model,
)

#: The operator's switch; anything but ``1`` leaves the MCP server off.
ENV = "NODESTRO_MCP_SERVER"
#: The endpoint (``_`` cannot start a publication's slug, so it never collides with ``/{slug}``).
PATH = "/_mcp"

_IDENTITY: contextvars.ContextVar[KeyIdentity | None] = contextvars.ContextVar("nodestro_mcp_identity", default=None)


def mcp_server_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return (os.environ if environ is None else environ).get(ENV, "").strip() == "1"


class McpServerEndpoint:
    """Lists and calls the offered publications for the API key of the current request."""

    def __init__(self, core: ServingCore, catalog: Callable[[], list[ServedRelease]]) -> None:
        self.core = core
        #: The active releases of the publications offered as MCP tools (enabled or not).
        self.catalog = catalog

    def tools_for(self, identity: KeyIdentity) -> dict[str, ServedRelease]:
        if "run" not in identity.scopes:
            return {}
        return {
            release.slug: release
            for release in self.catalog()
            if release.publication_id in identity.publications and release.enabled
        }

    async def list_tools(self, _context: Any, _params: Any) -> Any:
        import mcp_types as types

        identity = _IDENTITY.get()
        assert identity is not None
        tools = []
        for slug, release in sorted(self.tools_for(identity).items()):
            description = release.description or f"Runs the published Nodestro workflow {release.name}."
            if release.approvals:
                description += " Runs can stop at an approval step; the answer then names the run to decide on."
            tools.append(
                types.Tool(
                    name=slug,
                    title=release.name,
                    description=description[:2000],
                    input_schema=input_model(release.workflow).model_json_schema(),
                    annotations=types.ToolAnnotations(
                        read_only_hint=False, open_world_hint=True, idempotent_hint=False
                    ),
                )
            )
        return types.ListToolsResult(tools=tools)

    async def call_tool(self, _context: Any, params: Any) -> Any:
        import mcp_types as types

        identity = _IDENTITY.get()
        assert identity is not None

        def failure(code: str, message: str) -> Any:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"{code}: {message}")],
                structured_content={"error": {"code": code, "message": message}},
                is_error=True,
            )

        release = self.tools_for(identity).get(params.name)
        if release is None:
            return failure("tool.not_found", f"No tool called {params.name}")
        try:
            self.core.authorize(identity, release, "run")
            wait = float(release.limits.sync_timeout_seconds) if release.limits.sync_enabled else 0.0
            response = await self.core.launch(identity, release, dict(params.arguments or {}), wait=wait)
        except ApiError as exc:
            return failure(exc.code, exc.message)
        body = json.loads(bytes(response.body or b"{}"))
        status = str(body.get("status") or "")
        if status == "succeeded":
            text = json.dumps(body.get("output"), ensure_ascii=False, default=str)
        elif status in ("failed", "cancelled"):
            error = body.get("error") or {}
            text = f"The run {body.get('run_id')} {status}: {error.get('message') or error.get('code') or ''}".strip()
        else:
            text = (
                f"The run {body.get('run_id')} is {status or 'running'}; ask for its result through the published API "
                f"({PREFIX}/{release.slug}/runs/{body.get('run_id')})."
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=body,
            is_error=status in ("failed", "cancelled"),
        )

    async def handle(self, request: Request) -> Response:
        if not mcp_server_enabled():
            raise ApiError(404, "mcp_server.disabled", "Nodestro's MCP server is switched off")
        client = request.client.host if request.client else ""
        if client not in _LOOPBACK:
            raise ApiError(403, "lan.not_enabled", "Nodestro's MCP server answers only this computer")
        identity = self.core.authenticate(request)
        from nodestro_runtime.mcp.hub import sdk_available

        if not sdk_available():
            raise ApiError(503, "mcp.unavailable", "The MCP server needs Python 3.11 or later")
        token = _IDENTITY.set(identity)
        try:
            return await self._serve(request)
        finally:
            _IDENTITY.reset(token)

    async def _serve(self, request: Request) -> Response:
        from mcp.server.lowlevel import Server
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        server: Any = Server(
            "nodestro",
            version=__version__,
            instructions="Published Nodestro workflows. Each tool starts a run with the caller's API key.",
            on_list_tools=self.list_tools,
            on_call_tool=self.call_tool,
        )
        # Stateless with JSON answers: nothing is kept between requests, every request is authenticated again.
        manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
        start: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def send(message: Mapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(bytes(message.get("body", b"")))

        async with manager.run():
            await manager.handle_request(request.scope, request.receive, send)
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in start.get("headers", [])
            if key.lower() not in (b"content-length", b"transfer-encoding")
        }
        headers["Cache-Control"] = "no-store"
        return Response(content=b"".join(chunks), status_code=int(start.get("status", 500)), headers=headers)


def build_mcp_router(endpoint: McpServerEndpoint) -> APIRouter:
    """``/api/published/_mcp``; include it before the published routes (``/{slug}`` would match ``_mcp``)."""
    router = APIRouter(prefix=PREFIX, route_class=KeyAuthenticatedRoute, include_in_schema=False)

    @router.api_route(PATH, methods=["POST", "GET", "DELETE"])
    async def mcp_endpoint(request: Request) -> Response:
        return await endpoint.handle(request)

    return router
