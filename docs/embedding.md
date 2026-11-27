# Embedding the API: the product host (N-01)

nodestro-api can be embedded by a product that serves only part of the studio API, next to routes of its own.
`nodestro_api.host.create_product_app` builds such an app from named **router groups**. It is the studio app with
fewer routes, never with fewer protections, and it never imports `nodestro_studio`.

Host interface version: **4** (`nodestro_api.host.HOST_API_VERSION`; 2 added the group `activity`, 3 the group
`conversations`, 4 the field `Branding.mcp_sign_in_page`).

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
- `branding`: `title`, `version`, `description` of the OpenAPI document and the API reference page (`/api/docs`);
  `mcp_sign_in_page`, the product's page after an MCP server sign-in (see the `mcp` group below).
- `switches`: `scheduler` (default on) runs due schedules in the background; off, schedules only run on explicit
  ticks (tests). No switch can weaken a guard.
- `config`: every other setting (listen address, allowed hosts, launch secret, access key, price sync, ...). By
  default it is read from the environment exactly as for the studio (`AppConfig.from_env`). `AppConfig.product_id`
  names the product whose data the workspace holds (default `nodestro`; lower-case letters, digits and `-`): the
  backups and exports of the `data` group carry it, and a backup of another product is refused
  ([backup.md](backup.md)).
- `integrations`, `client_factory`: passed to the workspace as by `nodestro_api.app.create_app`.

The module also exports `install_guards(app, config)` and `service_lifespan(workspace, scheduler=...)`, the building
blocks `create_product_app` and the studio app share; `GROUPS` and `REQUIRED_GROUPS` list the groups.

## Same protection as the studio

`create_app` (the studio) and `create_product_app` are built by the same function (`nodestro_api.app.compose_app`):

- The configuration is checked before anything is opened: the desktop answers only on this computer
  (`access.network_needs_login`), a local container needs its access key (`access.key_required`), a launch secret or
  access key must be long enough (`access.launch_secret_weak`).
- The same guards in the same order: body limits, development CORS only with `NODESTRO_DEV_CORS=1`, the launch secret
  or Local Docker's access key, the host and origin check, the contract header and the security headers. The session
  store, the loopback rules and the secret comparisons under them come from the shared library `nodestro-session`
  (N-02).
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
| `conversations` | `api_conversations` | `/api/conversations` (conversations with own agents, N-04; off by default: `NODESTRO_CONVERSATIONS`, [conversations.md](conversations.md)) |
| `knowledge` | `api_inputs`, `api.knowledge_router` | `/api/inputs`, `/api/knowledge` |
| `usage` | `api_pricing`, `api.usage_router` | `/api/pricing`, `/api/usage` |
| `local_models` | `api_local_models` | `/api/local-models`, `/api/knowledge/index` |
| `agents` | `api_agent_templates`, `api.agents_router` | `/api/agents`, `/api/library/agents` |
| `skills` | `api_skills` | `/api/skills`, `/api/skill-groups`, `/api/skill-stats`, `/api/library/skills` |
| `inbox` | `api_inbox` | `/api/notifications` |
| `connections` | `api_connections` | `/api/connections`, `/api/tools/builtin` |
| `mcp` | `api_mcp` | `/api/mcp` (connected MCP services) |
| `budget` | `api_budget` | `/api/budget-requests`, budget decisions of runs |
| `approvals` | `api_tool_approvals`, `api_access`, `api_folders` | `/api/tool-approvals`, `/api/tool-pre-grants`, tool decisions of runs, `/api/access` (grants and denies, [access-grants.md](access-grants.md)), `/api/folders` (granted folders, [folders.md](folders.md); never served on a server) |
| `activity` | `api_activity` | `/api/activity`, `/api/activity/export` (the activity log, [activity-log.md](activity-log.md)) |
| `runs` | `api_run_chat`, `api.runs_router` | `/api/runs`, `/api/approvals`, runs of a workflow |
| `core` | `error_codes`, `api.core_router` | `/api/health`, `/api/meta`, `/api/error-codes` |
| `settings` | `api.settings_router` | `/api/settings`, `/api/models`, `/api/sandbox` |
| `workflows` | `api.workflows_router` | `/api/workflows` and their versions |
| `library` | `api.library_router` | `/api/library` (example library) |
| `tools` | `api.tools_router` | `/api/tools` |
| `references` | `api.references_router` | `/api/used-by` |
| `memory` | `api.memory_router` | `/api/memory` |
| `data` | `api_data` | `/api/backup`, `/api/data/export`, `/api/data/delete-all` ([backup.md](backup.md)), `/api/diagnostics`, `/api/organize`, `/api/search` |

The order matters where paths overlap: `/api/library/agents` before `/api/library/{template_id}`, `/api/tools/drafts`
before `/api/tools/{name}`, `/api/published/_mcp` before `/api/published/{slug}`. A product that selects a subset
keeps that order.

### Agent templates in a product (N-25)

A product that selects the group `agents` serves the studio's agent template routes unchanged, with no product
content of their own: the person's templates, every changed save as an immutable version
(`/api/agents/templates/{id}/versions`), the origin of each template (`meta.origin`: `user`, `builtin:...` for a copy
of a shipped template from `nodestro-examples`, `import:<sha256>` for an imported file; a client can never set it),
the check of what a template needs in this workspace, the shipped library (`/api/library/agents`) and export and
import in the format `nodestro-agent/1`. The permissions are those of the studio (`read` lists, reads versions,
exports and checks; `edit` creates, changes, deletes, copies and imports), behind the host's guards: the launch
secret or access key, the host and origin check for every write, the body limits.

Export and import are a round trip: a template exported from one product workspace and imported into another is the
same agent with the same description (`category`, `summary`, `requirements`, `examples`, `tests`, `locale`), and
exporting the copy gives the same file apart from `meta.origin`. What belongs to one workspace does not travel:
service connections, knowledge files and approval rules (`tool_policies`) are left out of the export and dropped
from an import (`removed` names them); the import's answer names what the template still needs here. The version
history stays in its workspace: an export carries the latest version, and the import starts a new history at
version 1 (note `import`) with the origin `import:<sha256 of the file>`; a later change of it is its version 2 and
keeps that origin. The backup of a workspace keeps every version (`agent_template_versions`).
`tests/test_product_host_agents.py` holds these rules for a product app.

### Connected MCP services in a product (N-23)

A product that selects the group `mcp` serves the studio's MCP routes (`/api/mcp`) unchanged, on the same workspace
and with no MCP code of its own: the administrator's decision per tool (approved only with the reviewed definition
hash), the sign-in with OAuth, tokens that are write-only and sealed with the vault key (`NODESTRO_VAULT_KEY`), the
operator's endpoint list, the statistics and the events. With the groups `tools`, `workflows` and `runs` its agents
use the approved tools; with `approvals` a person decides the calls that are asked. What holds in a product as in the
studio:

- A tool that changes data stays behind the tool approval gate (G-2, `NODESTRO_TOOL_APPROVALS`, off by default and
  staying off): while the gate is off it is never usable (`mcp.write_not_supported`), not in the tool panel and not
  in a run (the run does not start, `run.agents_invalid`); with the gate on every changing call waits for a person.
  The tool panel never runs one.
- A local (stdio) server runs only in the Docker sandbox's containers (`sandbox.isolation_required` without it); a
  local development server over loopback `http` is possible only on the desktop, never in a local container.
- Every MCP tool reaches outside (`reach` `external`), so an MCP server counts as a recipient in the egress
  protection (N-08, runtime `docs/content-provenance.md`, off by default): once a run holds content of a granted
  source, a call of even a read-only MCP tool is asked (refused when nobody watches) until a grant on exactly that
  server (`/api/access/grants`, `mcp_server`) covers it.

Nodestro's own MCP server (`/api/published/_mcp`) is the separate group `mcp_server`. The sign-in's browser callback
(`/api/mcp/oauth/callback`) sends the browser back to `Branding.mcp_sign_in_page` (host interface 4), by default
the studio's page `/tools?tab=services`; a product with its own pages names its page there, a path of its own origin
(anything else is refused with `ValueError`, so the callback never redirects to another site). The result is added
to the page's query: `oauth=connected&server=<id>` or `oauth_error=<code>`. `tests/test_product_host_mcp.py` holds these rules for a product
app.

Error codes: every code is listed by `GET /api/error-codes` (group `core`) for the whole package; a group's routes
answer only with codes from that catalog.

## Versioning

`tests/test_product_host.py` freezes the signature of `create_product_app`, the fields of `Branding` and
`HostSwitches`, the group names and their order, and `REQUIRED_GROUPS`. Changing any of them is a change of the host
interface: raise `HOST_API_VERSION` and update this page in the same PR; adding a group raises it too (a new name
in the frozen list), although a product that selects groups by name keeps working. Adding a route to a group is not.
