# Access grants and denies (N-06)

The routes of the access model v2 (`nodestro_api.api_access`, router group `approvals`). The model itself (records,
call sites, safety limits, audit) is described in nodestro-runtime's `docs/access-grants.md`.

**Off by default.** Only while `NODESTRO_ACCESS_GRANTS=1` together with `NODESTRO_TOOL_APPROVALS=1`. While it is off,
every route but `GET /api/access/state` answers `404 access.grants_disabled`, and nothing is read or written.

| Route | Permission | What it does |
|---|---|---|
| `GET /api/access/state` | `read` | `enabled` and the global safety limits (`safety_limits`: name -> text). Always answers, so the studio knows whether to show scope and duration on an approval card |
| `GET /api/access/grants`, `GET /api/access/denies` | `admin` | Every record of the tenant, newest first, with revoked, expired and used ones |
| `POST /api/access/grants`, `POST /api/access/denies` | `admin` | Store a record (`201`) |
| `DELETE /api/access/grants/{id}`, `DELETE /api/access/denies/{id}` | `admin` | Revoke a record. Revoking twice changes nothing. An unknown id answers `404 access.not_found` |
| `GET /api/access/who-may` | `admin` | "Who may do what and why": for every resource, action, agent and mode an active record names, what the call site does now and which record or safety limit decides it. Narrow it with `resource_type` and `resource_id` (always both), `agent` and `mode` |
| `GET /api/access/explain` | `admin` | The same for one access: `resource_type`, `resource_id`, `action` (default: the action the runtime checks there, `read` for folders), `agent`, `mode` |

## Who writes

- The writer (`granted_by`, `denied_by`, `revoked_by`) is always the request's signed-in person, never a field of the
  body.
- On the desktop, that is `local`, the owner. On a server, it is the member's user id.
- A principal that is not a person, such as an API key (`api:<id>`), `system` or anything with a `:`, is refused with
  `403 access.person_required`.
- No other route writes records. That includes the published APIs, the app contract, Nodestro's MCP server, the
  assistants and tool calls. `tests/test_access_api.py` checks that only `api_access` builds a `Person` or uses the
  rule store.
- A tool grant names the tool as it is now: its version, classification and MCP definition come from the tool library,
  never from the body. The grant ends when any of them changes.
- A grant can name the tool as the person saw it: `expected` holds the `version` and `definition_hash` of the
  approval request. If the library holds another version or MCP definition now, the grant is refused with
  `409 access.tool_changed`, and nothing is stored. The stored facts still come from the library.
- Deleting for good is never granted (`422 access.grant_destructive`).

## Connections in `who-may` and `explain`

A tool's MCP server always counts. A connection belongs to one agent: it is the one that agent's tool settings name
in its saved workflow, as at the gate.

- With `agent` (`<workflow id>/<node id>`), a deny on that connection shows in the tool's row, and the row says
  `connections_considered: true`.
- Without `agent`, connections are not considered, and tool rows say `connections_considered: false`. A deny on a
  connection still has its own row.
- Rows of other resources carry `connections_considered: null`.

Every route works on the request's tenant only. On a server, ids of another tenant are not found.

## Refusals

Each `access.*` problem code is answered with a fixed HTTP status, in the studio API and in the published APIs
(`nodestro_api.serving.ACCESS_ERROR_STATUS`).

| Code | Status | Raised for |
|---|---|---|
| `access.denied`, `access.safety_limit`, `access.missing_grant` | `403` | A run start, a sub-workflow or a cloud model request that is refused |
| `access.person_required`, `access.in_tool_call` | `403` | A write by something other than a signed-in person |
| `access.not_found` | `404` | An unknown record id |
| Validation codes (`access.grant_destructive`, `access.unattended_*`, `access.expiry`, `access.invalid`, ...) | `422` | A record that cannot be stored as asked |

**What the refusal names.** It names the resource (`resource_type`, `resource_id`). Only a person also gets the
deciding record (`source`, for example `deny:<id>`). The runtime leaves `source` out for runs that no person started.
The published APIs also strip it from every error, run status and run event they serve.

**Who is checked for a model.** Every caller of `connect_model` passes the signed-in person as the actor. These are
the assistants, the workflow architect, the API guide, tool generation, memory proposals and the live run chat.

Two callers are exceptions:

- The benchmark judge connects unattended, because nobody waits for it.
- Model choice only probes, so a refusal there is not audited.
