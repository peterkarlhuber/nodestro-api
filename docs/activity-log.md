# Activity log API (N-13)

The activity log answers "who did what, with which agent, to what, and how did it end" for a workspace (on a server:
for a tenant). The runtime writes the events (`nodestro_runtime.activity`, see nodestro-runtime
`docs/activity-log.md` for the event fields, sources and actions); this API reads and exports them. The module is
`nodestro_api.api_activity`, served by the router group `activity` (see [embedding.md](embedding.md)).

## Endpoints

Both endpoints only read, need the permission `admin` (like `GET /api/access-audit` and
`GET /api/tool-approvals/audit`), and work on the request's workspace or tenant only. Without a login they answer
`401 access.login_required`, without `admin` `403 access.forbidden` (`params.permission: "admin"`).

### `GET /api/activity`

One page of events, newest first:

```json
{"events": [{"id": "act_...", "at": "...", "source": "tool_call", "action": "tool.called", "...": "..."}],
 "next_before": "2026-11-20T10:15:00.123456+00:00_act_..."}
```

| Query parameter | Meaning |
| --- | --- |
| `since`, `until` | ISO 8601 times; `since` inclusive, `until` exclusive. A time without a zone is UTC |
| `source` | `tool_call`, `tool_approval`, `mcp`, `connection`, `workflow`, `agent` or `file`; may be repeated (any of them) |
| `action` | e.g. `tool.called`, `file.uploaded`; may be repeated (any of them) |
| `actor`, `agent`, `resource_type`, `resource_id`, `run_id`, `conversation_id` | exact match (1 to 200 characters) |
| `outcome` | `success`, `failure` or `denied` |
| `before` | keyset paging: the `next_before` of the previous page |
| `limit` | events per page, 1 to 1000 (default 200) |

Filters combine: an event must match all of them. `next_before` is `null` when the page holds the oldest matching
event; otherwise pass it as `before`, with the same filters, for the next page. Paging by key never skips or repeats
an event, also when new events arrive in between (they are newer than the first page).

### `GET /api/activity/export`

Every event that matches the same filters (no page limit), newest first, as a download:

| `format` | Content type | File name |
| --- | --- | --- |
| `jsonl` (default) | `application/x-ndjson; charset=utf-8` | `nodestro-activity-<UTC time>.jsonl` |
| `csv` | `text/csv; charset=utf-8` | `nodestro-activity-<UTC time>.csv` |

JSON Lines holds one object per event, CSV a header and one row per event, both with the columns of
`nodestro_runtime.activity.FIELDS` in that order. A CSV cell that a spreadsheet would read as a formula (starting with
`=`, `+`, `-` or `@`) gets a leading `'`. The answer is `Cache-Control: no-store`. The events are read before the
download starts.

## What the answers hold

Exactly the stored event fields: identifiers, names, kinds, outcomes and codes. No tool arguments, prompts, model
answers, outputs, file contents, file names or paths, tokens or secrets: the runtime refuses them when the event is
written, and these routes add nothing. `tests/test_activity_api.py` puts one canary value into a prompt, a run's
input, tool arguments, a model answer, an uploaded file's name and contents, a knowledge file and a provider key and
checks that neither the list nor either export contains it.

## Retention

Events older than `NODESTRO_ACTIVITY_RETENTION_DAYS` days (default 90; `0` keeps them without limit) are deleted:

- Desktop and Nodestro Local Docker: when the workspace opens (the runtime).
- Server: the worker that holds the scheduler lock, hourly (`WorkerSettings.activity_every`), for every active tenant
  (`ServerWorker.prune_activity`); a suspended tenant's events stay until it is resumed. Set the variable on the
  worker processes.

## Not a source (yet)

- The API's own access audit trail (`access_audit`: logins, tenant switches, API key and member changes) stays its own
  record (`GET /api/access-audit`). It is not fed into the activity log: the runtime's event sources are a closed list
  without an access source, and its events carry `details` (a key's name and prefix) that the activity log does not
  take. Adding it needs a new source in the runtime first (`docs/TODO.md` API-072).
- Assistant conversations do not write events, and nothing sets `conversation_id` yet (nodestro-runtime RT-084).
