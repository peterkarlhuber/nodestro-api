"""The router registry: every router of the studio API in one fixed order, each in a named group.

The desktop and Nodestro Local Docker app (:func:`nodestro_api.app.create_app`), the server app
(:func:`nodestro_api.server_app.create_tenant_app`) and product apps (:func:`nodestro_api.host.create_product_app`)
include their routers from this one list, so the order rules between routers whose paths overlap are kept in one
place: a router that serves a more specific path comes before the one with the path parameter that would also match
it (``/api/library/agents`` before ``/api/library/{template_id}``, ``/api/tools/drafts`` before
``/api/tools/{name}``, Nodestro's MCP server ``/api/published/_mcp`` before ``/api/published/{slug}``).

A group is a building block of the API: an app serves a group's routes completely or not at all. Group names are
part of the host interface (:data:`nodestro_api.host.HOST_API_VERSION`); renaming or removing one is a breaking
change. ``docs/embedding.md`` lists what each group serves.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI

from . import api
from .api_agent_templates import router as agent_templates_router
from .api_architect import router as architect_router
from .api_assistant import router as assistant_router
from .api_automation import router as automation_router
from .api_budget import router as budget_router
from .api_connections import router as connections_router
from .api_data import router as data_router
from .api_inbox import router as inbox_router
from .api_inputs import router as inputs_router
from .api_local_models import router as local_models_router
from .api_mcp import router as mcp_router
from .api_pricing import router as pricing_router
from .api_publish import router as publish_router
from .api_python import router as python_router
from .api_run_chat import router as run_chat_router
from .api_skills import router as skills_router
from .api_tool_approvals import router as tool_approvals_router
from .app_discovery import build_app_router
from .error_codes import router as error_codes_router
from .mcp_server import build_mcp_router
from .serving import build_router

if TYPE_CHECKING:
    from .context import Nodestro


@dataclass(frozen=True)
class RouterSlot:
    """One router in the registry: its group and how it is built (``workspace``: the app's one context; ``None``
    for apps without one, such as the server, which only include groups whose routers need none)."""

    group: str
    build: Callable[[Nodestro | None], APIRouter]
    needs_workspace: bool = False


def _static(group: str, router: APIRouter) -> RouterSlot:
    return RouterSlot(group, lambda _: router)


def _workspace(group: str, build: Callable[[Nodestro], APIRouter]) -> RouterSlot:
    def built(workspace: Nodestro | None) -> APIRouter:
        if workspace is None:
            raise ValueError(f"The router group {group!r} needs the app's workspace")
        return build(workspace)

    return RouterSlot(group, built, needs_workspace=True)


#: Every router, in the order an app includes them.
SLOTS: tuple[RouterSlot, ...] = (
    # Nodestro's MCP server (WP-11, off by default) before the published APIs' ``/{slug}``, which would match ``_mcp``.
    _workspace("mcp_server", lambda workspace: build_mcp_router(workspace.mcp_server)),
    # Published workflows (``/api/published``): key-protected, answered only on this computer here; the network
    # listener (``nodestro_api.network``) serves them separately when switched on. Before the workflow routes:
    # /api/tools/drafts must not be taken for a tool named "drafts".
    _workspace("published", lambda workspace: build_router(workspace.serving, channel="local")),
    # The app contract's discovery (NAL-C1, docs/app-api/v1.md): public, more with a published API's key.
    _workspace("app_contract", lambda workspace: build_app_router(workspace.serving, channel="local")),
    _static("publishing", publish_router),
    _static("architect", architect_router),  # before the tool library (/api/tools/drafts)
    _static("python", python_router),
    _static("automation", automation_router),
    _static("assistant", assistant_router),
    _static("knowledge", inputs_router),  # before the knowledge files (/api/knowledge/adopt, /collections)
    _static("usage", pricing_router),
    _static("local_models", local_models_router),  # before the knowledge files (/api/knowledge/index)
    _static("agents", agent_templates_router),  # before the example library (/api/library/agents)
    _static("skills", skills_router),  # before the example library (/api/library/skills)
    _static("inbox", inbox_router),
    _static("connections", connections_router),
    _static("mcp", mcp_router),
    _static("budget", budget_router),
    _static("approvals", tool_approvals_router),
    _static("runs", run_chat_router),
    _static("core", error_codes_router),
    *(_static(name, part) for name, part in api.ROUTERS.items()),
    _static("data", data_router),
)

#: The names of all groups, in the order of their first router.
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(slot.group for slot in SLOTS))

#: The groups every app includes: health, ``/api/meta`` and the error-code catalog.
REQUIRED_GROUPS: frozenset[str] = frozenset({"core"})


def check_groups(names: Iterable[str]) -> frozenset[str]:
    """The named groups; an unknown name is refused (``ValueError``)."""
    found = frozenset(names)
    unknown = sorted(found - set(GROUPS))
    if unknown:
        raise ValueError(f"Unknown router groups: {', '.join(unknown)} (known: {', '.join(GROUPS)})")
    return found


def include_groups(app: FastAPI, groups: Collection[str], workspace: Nodestro | None) -> None:
    """Include the routers of ``groups`` in the registry's order."""
    selected = check_groups(groups)
    for slot in SLOTS:
        if slot.group in selected:
            app.include_router(slot.build(workspace))
