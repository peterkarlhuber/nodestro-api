"""Service connections and the built-in tool catalog (NAL-A1, nodestro-runtime ADR-0001).

A service connection is what a built-in tool needs to reach an outside service (web search, mail, webhooks; databases
with CR-DB): its settings, the hosts it may reach and one secret. Secrets are write-only: no route returns them,
``secret_set`` only says whether one is stored. Everything lives in the tenant's own store.

Exchange (N-14, ``nodestro-connection/1``, :mod:`nodestro_runtime.exchange`): ``GET /api/connections/export`` writes
the connections' configuration without secrets (checked for secrets first); ``POST /api/connections/import`` previews
(``dry_run``) or creates the connections of such a file without secrets, by the rules of creating one by hand.

Permissions: ``read`` lists connections, types and the tool catalog; ``admin`` creates, changes, deletes, sets the
secret, checks, sends a test (NAL-A4), exports, imports and reads the audit events (on the desktop the owner has
``admin``).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from nodestro_runtime import exchange
from nodestro_runtime.connections import (
    ConnectionEvent,
    ConnectionIn,
    ConnectionPatch,
    ConnectionView,
    connection_types,
)
from nodestro_runtime.problems import NodestroError
from pydantic import BaseModel, Field

from .api import Ctx
from .permissions import ADMIN, READ

router = APIRouter(prefix="/api")


def _actor(request: Request) -> str | None:
    principal = getattr(request.state, "principal", None)
    return getattr(principal, "id", None)


def _connection(ctx: Ctx, connection_id: str) -> Any:
    try:
        return ctx.connections.get(connection_id)
    except NodestroError as exc:
        if exc.code == "connection.not_found":
            raise KeyError(connection_id) from None
        raise


class ConnectionStatus(BaseModel):
    #: Secrets can be stored encrypted; otherwise they are kept for this session only.
    secret_vault: bool
    mode: Literal["desktop", "local-container", "server"]
    contract: int


class ConnectionTypeInfo(BaseModel):
    name: str
    providers: list[str]
    secret_required: bool
    secret_label: str
    settings_schema: dict[str, Any]
    modes: list[str]
    checkable: bool


class SecretIn(BaseModel):
    """The new secret; ``""`` removes it. Never returned by any route."""

    secret: str = Field(max_length=8192)


class BuiltinToolInfo(BaseModel):
    """A built-in tool's catalog entry (its spec) with the description the model reads."""

    name: str
    description: str
    #: ``nodestro`` for Nodestro's own tools, ``integration`` for tools an installed integration added.
    origin: Literal["nodestro", "integration"]
    spec: dict[str, Any] | None = None


@router.get("/connections/status", dependencies=READ)
def connection_status(ctx: Ctx) -> ConnectionStatus:
    from nodestro_runtime.connections import CONTRACT_VERSION

    return ConnectionStatus(
        secret_vault=ctx.connections.vault.persistent, mode=ctx.connections.mode, contract=CONTRACT_VERSION
    )


@router.get("/connections/types", dependencies=READ)
def list_connection_types() -> list[ConnectionTypeInfo]:
    """The connection types this installation offers (``database`` appears once CR-DB registers it)."""
    return [ConnectionTypeInfo.model_validate(item.describe()) for _, item in sorted(connection_types().items())]


@router.get("/connections", dependencies=READ)
def list_connections(ctx: Ctx, type: str | None = Query(None, max_length=32)) -> list[ConnectionView]:
    return ctx.connections.views(type=type)


@router.post("/connections", dependencies=ADMIN, status_code=201)
def create_connection(ctx: Ctx, data: ConnectionIn, request: Request) -> ConnectionView:
    return ctx.connections.view(ctx.connections.create(data, actor=_actor(request)))


@router.get("/connections/export", dependencies=ADMIN, responses={200: {"model": exchange.ConnectionPackage}})
def export_connections(
    ctx: Ctx, ids: Annotated[list[str] | None, Query(max_length=exchange.MAX_ITEMS)] = None
) -> JSONResponse:
    """The connections (``ids``: only these) as ``nodestro-connection/1``: type, provider, name, description and
    settings, never a secret, an id, the reachable hosts or a check. An export that would hold a secret is refused
    (``exchange.secret_detected``)."""
    document = exchange.export_connections(ctx.connections, ids)
    return JSONResponse(
        document, headers={"Content-Disposition": 'attachment; filename="connections.nodestro-connection.json"'}
    )


@router.post("/connections/import", dependencies=ADMIN)
def import_connections(
    ctx: Ctx, request: Request, body: dict[str, Any], dry_run: bool = False
) -> exchange.ImportReport:
    """Preview (``dry_run``) or apply a ``nodestro-connection/1`` file. Each connection is checked like creating it by
    hand (type and mode, provider, settings, address rules, a unique name); applying creates all of them or none,
    without secrets (``secrets_to_reenter`` names those to enter). A newer format version is refused
    (``exchange.version_newer``)."""
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    return exchange.import_connections(ctx.connections, raw, dry_run=dry_run, actor=_actor(request))


@router.get("/connections/events", dependencies=ADMIN)
def list_connection_events(ctx: Ctx, limit: int = Query(200, ge=1, le=1000)) -> list[ConnectionEvent]:
    """Administration events, newest first (who changed which connection; never secrets or settings values)."""
    return ctx.connections.events(limit=limit)


@router.get("/connections/{connection_id}", dependencies=READ)
def get_connection(ctx: Ctx, connection_id: str) -> ConnectionView:
    return ctx.connections.view(_connection(ctx, connection_id))


@router.patch("/connections/{connection_id}", dependencies=ADMIN)
def update_connection(ctx: Ctx, connection_id: str, data: ConnectionPatch, request: Request) -> ConnectionView:
    _connection(ctx, connection_id)
    return ctx.connections.view(ctx.connections.update(connection_id, data, actor=_actor(request)))


@router.delete("/connections/{connection_id}", dependencies=ADMIN, status_code=204)
def delete_connection(ctx: Ctx, connection_id: str, request: Request, force: bool = False) -> None:
    """Delete a connection; one that agents still bind is refused (``connection.in_use``) unless ``force``."""
    _connection(ctx, connection_id)
    ctx.connections.delete(connection_id, actor=_actor(request), force=force)


@router.put("/connections/{connection_id}/secret", dependencies=ADMIN)
def set_connection_secret(ctx: Ctx, connection_id: str, data: SecretIn, request: Request) -> ConnectionView:
    """Store or replace the secret (``""`` removes it). The answer only says ``secret_set``."""
    _connection(ctx, connection_id)
    return ctx.connections.view(ctx.connections.set_secret(connection_id, data.secret, actor=_actor(request)))


@router.post("/connections/{connection_id}/check", dependencies=ADMIN)
def check_connection(ctx: Ctx, connection_id: str, request: Request) -> ConnectionView:
    """Check the connection; the result is stored as status and error code (never the service's own message)."""
    _connection(ctx, connection_id)
    return ctx.connections.view(ctx.connections.check(connection_id, actor=_actor(request)))


class SendTestIn(BaseModel):
    """A person's explicit confirmation: the studio sends ``SEND`` only after its confirmation dialog."""

    confirm: Literal["SEND"]


class SendTestOut(BaseModel):
    ok: bool
    #: The webhook's HTTP status (``webhook``); ``None`` for mail.
    status: int | None = None
    #: The size of the test message in bytes (``smtp``); ``None`` for webhooks.
    size: int | None = None


@router.post("/connections/{connection_id}/test-send", dependencies=ADMIN)
def send_connection_test(ctx: Ctx, connection_id: str, data: SendTestIn, request: Request) -> SendTestOut:
    """Send one test (NAL-A4): ``smtp`` mails a fixed text to the connection's own ``test_address``, ``webhook``
    posts a payload marked ``test: true``. Only a person with ``admin`` can do this; agents cannot. At most 5 per
    connection and hour (``connection.test_send_limit``); every attempt is an audit event (``test_sent``). A failure
    answers with its code only (``connection.*``), never the service's message."""
    _connection(ctx, connection_id)
    return SendTestOut.model_validate(ctx.connections.test_send(connection_id, actor=_actor(request)))


@router.get("/tools/builtin", dependencies=READ)
def list_builtin_tools(ctx: Ctx) -> list[BuiltinToolInfo]:
    """The built-in tools with their specs: network, offline, connection type, limits, cost unit (NAL-A1)."""
    from nodestro_runtime.tool_spec import BUILTIN_SPECS

    found: list[BuiltinToolInfo] = []
    for info in ctx.tools.all():
        if info.kind != "builtin":
            continue
        spec = ctx.tools.specs.get(info.name)
        found.append(
            BuiltinToolInfo(
                name=info.name,
                description=info.description,
                origin="nodestro" if info.name in BUILTIN_SPECS else "integration",
                spec=spec.catalog() if spec is not None else None,
            )
        )
    return sorted(found, key=lambda item: (item.origin != "nodestro", item.name))
