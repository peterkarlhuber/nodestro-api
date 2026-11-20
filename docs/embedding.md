# Embedding the API: the product host (N-01)

nodestro-api can be embedded by a product that serves only part of the studio API, next to routes of its own.
`nodestro_api.host.create_product_app` builds such an app from named **router groups**. It is the studio app with
fewer routes, never with fewer protections, and it never imports `nodestro_studio`.

Host interface version: **2** (`nodestro_api.host.HOST_API_VERSION`; 2 added the group `activity`).

## Entry point

```python
from fastapi import APIRouter
from nodestro_api.host import Branding, HostSwitches, create_product_app
from nodestro_api.permissions import READ

own = APIRouter(prefix="/api/product")


@own.get("/status", dependencies=READ)  # every route declares who may call it
def status() -> dict[str, str]:
    return {"status": "ready"}


app = create_product_app(
    "desktop",  # operating profile: "desktop" or "local-container"
    "/path/to/data",  # the workspace's data directory
    ["settings", "knowledge", "assistant", own],
    Branding(title="Example Product", version="1.0.0"),
    HostSwitches(scheduler=True),
)
```

```python
def create_product_app(
    profile: ProfileKind,
    data_dir: Path | str,
    routers: Sequence[str | APIRouter],
    branding: Branding | None = None,
    switches: HostSwitches | None = None,
    *,
    config: AppConfig | None = None,
    integrations: Iterable[Integration] | None = None,
    client_factory: Callable[[str], Any] | None = None,
) -> FastAPI
```

- `profile`: `desktop` or `local-container`. A server (`server`) is refused with `profile.invalid`; a server is
  started with `nodestro-server`.
- `data_dir`: the workspace directory. It replaces the one in `config`.
- `routers`: group names (table below) and the product's own `APIRouter` objects. Groups are included in the
  registry's fixed order, the product's routers after them. An unknown group name raises `ValueError`.
- `branding`: `title`, `version`, `description` of the OpenAPI document and the API reference page (`/api/docs`).
- `switches`: `scheduler` (default on) runs due schedules in the background; off, schedules only run on explicit
  ticks (tests). No switch can weaken a guard.
- `config`: every other setting (listen address, allowed hosts, launch secret, access key, price sync, ...). By
  default it is read from the environment exactly as for the studio (`AppConfig.from_env`).
- `integrations`, `client_factory`: passed to the workspace as by `nodestro_api.app.create_app`.

The module also exports `install_guards(app, config)` and `service_lifespan(workspace, scheduler=...)`, the building
blocks `create_product_app` and the studio app share; `GROUPS` and `REQUIRED_GROUPS` list the groups.

## Same protection as the studio

`create_app` (the studio) and `create_product_app` are built by the same function (`nodestro_api.app.compose_app`):

- The configuration is checked before anything is opened: the desktop answers only on this computer
  (`access.network_needs_login`), a local container needs its access key (`access.key_required`), a launch secret or
  access key must be long enough (`access.launch_secret_weak`).
- The same guards in the same order: body limits, development CORS only with `NODESTRO_DEV_CORS=1`, the launch secret
  or Local Docker's access key, the host and origin check, the contract header and the security headers.
- The same error answers (`{"detail", "code", "params"}`) and the documented error schema on every operation.
- Fail closed: every route, the product's own included, must declare its access requirement
  (`nodestro_api.permissions`); otherwise the app is not built (`RouteAccessError`).
- The same background services (`service_lifespan`): schedules, the network listener when the user switched it on,
  the managed local models, the price sync, the sweep of paused runs (tool and budget decisions) and the retention of
  chat files.
- A database that only the user may repair gives the recovery app, as for the studio.

Routes of groups that are not selected answer `404 route.not_found` and are not in the OpenAPI document
(`/api/openapi.json`).

## Router groups

The registry (`nodestro_api.router_groups`) lists every router once, in the order every app includes them; the desktop
app serves all groups, the server app all but the desktop's own (`mcp_server`, `published`, `app_contract`) and adds
its inbox or notifications last. `core` is always served. OpenAPI tags come from the path as in the studio (API-016).

| Group | Module (router) | Paths |
|---|---|---|
| `mcp_server` | `mcp_server.build_mcp_router` | `/api/published/_mcp` (Nodestro's own MCP server, off by default) |
| `published` | `serving.build_router` | `/api/published/{slug}` (published workflow APIs, local channel) |
| `app_contract` | `app_discovery.build_app_router` | `/api/app` (app contract discovery, `docs/app-api/v1.md`) |
| `publishing` | `api_publish` | `/api/publications`, `/api/api-keys`, `/api/api-network`, `/api/exports`, `/api/export-jobs`, `/api/access-audit`, ... |
| `architect` | `api_architect` | `/api/architect`, `/api/tools/drafts` |
| `python` | `api_python` | `/api/python` |
| `automation` | `api_automation` | `/api/batches`, `/api/benchmarks`, `/api/benchmark-runs`, `/api/schedules` |
| `assistant` | `api_assistant` | `/api/assistant` (conversations, files, documentation search) |
| `knowledge` | `api_inputs`, `api.knowledge_router` | `/api/inputs`, `/api/knowledge` |
| `usage` | `api_pricing`, `api.usage_router` | `/api/pricing`, `/api/usage` |
| `local_models` | `api_local_models` | `/api/local-models`, `/api/knowledge/index` |
| `agents` | `api_agent_templates`, `api.agents_router` | `/api/agents`, `/api/library/agents` |
| `skills` | `api_skills` | `/api/skills`, `/api/skill-groups`, `/api/skill-stats`, `/api/library/skills` |
| `inbox` | `api_inbox` | `/api/notifications` |
| `connections` | `api_connections` | `/api/connections`, `/api/tools/builtin` |
| `mcp` | `api_mcp` | `/api/mcp` (connected MCP services) |
| `budget` | `api_budget` | `/api/budget-requests`, budget decisions of runs |
| `approvals` | `api_tool_approvals`, `api_access` | `/api/tool-approvals`, `/api/tool-pre-grants`, tool decisions of runs, `/api/access` (grants and denies, [access-grants.md](access-grants.md)) |
| `activity` | `api_activity` | `/api/activity`, `/api/activity/export` (the activity log, [activity-log.md](activity-log.md)) |
| `runs` | `api_run_chat`, `api.runs_router` | `/api/runs`, `/api/approvals`, runs of a workflow |
| `core` | `error_codes`, `api.core_router` | `/api/health`, `/api/meta`, `/api/error-codes` |
| `settings` | `api.settings_router` | `/api/settings`, `/api/models`, `/api/sandbox` |
| `workflows` | `api.workflows_router` | `/api/workflows` and their versions |
| `library` | `api.library_router` | `/api/library` (example library) |
| `tools` | `api.tools_router` | `/api/tools` |
| `references` | `api.references_router` | `/api/used-by` |
| `memory` | `api.memory_router` | `/api/memory` |
| `data` | `api_data` | `/api/backup`, `/api/diagnostics`, `/api/organize`, `/api/search` |

The order matters where paths overlap: `/api/library/agents` before `/api/library/{template_id}`, `/api/tools/drafts`
before `/api/tools/{name}`, `/api/published/_mcp` before `/api/published/{slug}`. A product that selects a subset
keeps that order.

Error codes: every code is listed by `GET /api/error-codes` (group `core`) for the whole package; a group's routes
answer only with codes from that catalog.

## Versioning

`tests/test_product_host.py` freezes the signature of `create_product_app`, the fields of `Branding` and
`HostSwitches`, the group names and their order, and `REQUIRED_GROUPS`. Changing any of them is a change of the host
interface: raise `HOST_API_VERSION` and update this page in the same PR; adding a group raises it too (a new name
in the frozen list), although a product that selects groups by name keeps working. Adding a route to a group is not.
