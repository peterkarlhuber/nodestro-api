# Conversations with own agents (N-04)

Status: **in development, off by default.** `NODESTRO_CONVERSATIONS=1` turns the routes on; otherwise every route
answers `404 conversation.disabled` and nothing is read or written. The object itself, its storage and its rules are
the runtime's (`nodestro_runtime.conversations`, runtime `docs/conversations.md`).

The routes are in the router group `conversations` (`nodestro_api.router_groups`), so a product app of the host
(`docs/embedding.md`) can serve them.

| Method and path | Permission | What |
|---|---|---|
| `GET /api/conversations` | `read` | The caller's conversations, most recently changed first |
| `POST /api/conversations` | `edit` | Create: `agent` (`template_id`, optional `version`), optional `model`, `title`, `offline`, `budget_usd`, `history`, `summary_model` |
| `GET /api/conversations/{id}` | `read` | The conversation with its messages, usage and the answer being written |
| `PATCH /api/conversations/{id}` | `edit` | Rename, switch the agent or model, the offline flag, the budget, `history` or `summary_model` (only the fields sent; `summary_model` `""` clears it) |
| `DELETE /api/conversations/{id}` | `edit` | Delete it with its messages, files and the answers' runs (a running answer is cancelled first); revokes its grants for "this conversation" |
| `GET /api/conversations/{id}/export` | `read` | Markdown, as a download |
| `POST /api/conversations/{id}/files` | `edit` | Upload files (multipart `files`); refused files come back with their problem |
| `DELETE /api/conversations/{id}/files/{file_id}` | `edit` | Remove a file |
| `GET /api/conversations/{id}/files/{file_id}/content` | `read` | The file as uploaded, always a download |
| `POST /api/conversations/{id}/messages` | `run` | Store a message (`content`, `file_ids`) and start its answer; returns both messages |
| `GET /api/conversations/{id}/messages/{message_id}` | `read` | One message; an answer as its run is now |
| `POST /api/conversations/{id}/messages/{message_id}/cancel` | `run` | Cancel a running answer (its run) |
| `GET /api/conversations/{id}/messages/{message_id}/events` | `read` | Server-Sent Events: `snapshot` (the message), `event` (each `RunEvent` of the answer's run), `end` (`status`, or `reason` `deleted` when the conversation is deleted meanwhile) |

Long conversations (N-05, nodestro-runtime `docs/conversations.md`, "Long conversations"): `history` is `trim`
(default) or `summarize`; `summary_model` is the helper model of `summarize` (`null`: the conversation's model; a
cloud model is refused while the conversation or its agent is offline, `conversation.offline_cloud_model`). Each
answer carries `summary` (`HistorySummary`: `method` `trimmed` or `summarized`, `first_seq`, `last_seq`, `text`,
`model`, `run_id`, `usage`, `sources`, `error`) when earlier messages did not fit the model's context window, and
`null` otherwise. A summary run is a run of the conversation like an answer's, so the run routes treat it the same.

Rules:

- **One engine.** Every answer is a run (trigger `conversation`, `trigger_ref` = the conversation). Its cost is in
  the usage views (under the workflow name `Conversation`, never the title), and a tool call that waits for a person
  is decided with the existing approval routes (the answer lists it in `approval_ids`).
- **The runs are the conversation's (H-1).** An answer's run holds the conversation, the answer and the files, so
  only who sees the conversation sees the run. `app_context` checks every route with `{run_id}` in its path (runs,
  export, files, cancel, resume, approvals, tool approvals, budget, inputs, run chat, events): another member gets
  `404 run.not_found`. The run list (`GET /api/runs`, also with `trigger_ref`), the pending approvals, the tool
  approval inbox, the budget requests and the assistant's run attachments leave such runs out; a page of the run list
  can then be shorter than `limit`. Deleting the conversation deletes its runs.
- **Search and preview (N-1).** A run hit of `GET /api/search` shows a snippet of the run's input, and the context
  preview (`POST /api/workflows/preview`) reads a run's input and outputs. Both leave out the runs of a conversation
  the caller does not see: search skips them, and the preview answers `404 run.not_found` for such a `run_id` and
  takes no latest run of another member's conversation.
- **Cost of the check (N-2).** A run summary already holds its trigger, so a top-level run that is not an answer is
  shown without a read. Only an answer's run costs a read of its conversation; a record of a run (an approval or
  budget request) costs one read of its run summary per run and request.
- **Aggregates still count the answers (N-3, by design).** The usage report by workflow (`GET /api/usage`, under the
  workflow name `Conversation`) and the skill usage statistics count the answers' runs of every member: numbers,
  cost and tokens only, never the messages, the answers or the files.
- **Models.** The API never picks another model. Without `model`, a conversation takes the agent's or the default
  model only when it is local, otherwise `422 conversation.model_required`. A local model that is not ready is refused
  before a message is stored (`409 model.local_unavailable`), without alternatives being chosen.
- **Owners and tenants.** On a server a conversation is its owner's: another member gets `404 conversation.not_found`
  on every route, its files included (also under `/api/inputs`, and as a run input). Every route works on the
  request's tenant only. A conversation without an owner (made on a desktop and restored into a server) is seen by
  nobody there (fail closed).
- **Conversations without an owner on a server (W3-S1, owner's decision).** An assistant conversation from before
  owners were recorded and a conversation with an own agent restored from a desktop (`owner` `None`) are nobody's on
  a server: no member sees them, administrators included, on any path (lists, detail, files and `/api/inputs`,
  search, preview, adoption, export, answer runs with their child runs, events and files). Before, an ownerless
  assistant conversation was visible, renamable and deletable for every member. Reading never assigns an owner, and
  nothing is deleted. The desktop's single person still sees them. The server's operator gives such a conversation
  an owner (`nodestro_api.conversation_owners`):
  - `nodestro-server assistant backfill-owners [--tenant TENANT]` lists them per tenant, with the person their
    files name as a suggestion; it writes nothing.
  - `nodestro-server assistant backfill-owners --tenant TENANT --user USER --conversation ID [...] [--dry-run]`
    assigns exactly these conversations to exactly this user, who must be a member of the tenant
    (`identity.not_a_member`). There is no default user and no wildcard. A conversation that has an owner keeps it
    (reported as `owned`). Each assignment is one entry of the tenant's access audit trail (kind
    `conversation_owner_assigned`, actor `operator`, target the new owner, details the conversation id and kind,
    the time; never a title or content), readable by the tenant's administrators (`GET /api/access-audit`).
  - Why a command and not a route: the operator already holds the database and the tenants' membership
    (`set-role`); an administrator route would let a tenant administrator assign another person's conversation to
    themselves and read it. Only who runs `nodestro-server` with the database settings can run it.
- **Who has an owner identity (W3-S1).** `owner_of(request)` decides whose conversations a request sees, per
  principal:

  | Principal | Owner | Sees |
  |---|---|---|
  | Tenant member on a server (any role: `owner`, `admin`, `editor`, `operator`, `viewer`) | its user id | its own conversations only |
  | The local owner of a single-user app (desktop, Local Docker) | `None` | every conversation of the workspace |
  | Any other principal without a role: a server principal that is no member (a service, a machine), a principal other than the local owner in a single-user app, a request without a principal | `NO_OWNER` | nothing: no conversation, message, file, answer run (nor a run below one), search hit or preview; it starts no conversation (`403 access.person_required`) |

  Before W3-S1, every principal without a role counted as the desktop's person and saw every conversation of its
  tenant. The server's own login (`SessionResolver`) always yields a role, so no such principal reached a server so
  far; a later one (service tokens) is closed by default. The same rule applies to the Nodestro Assistant's
  conversations and to the input files of both kinds of conversation.
- **Administrators.** The `admin` role grants no read of another member's conversation: there is no export path for
  administrators. What they read is the activity log (`/api/activity`, N-13: ids, names, kinds and outcomes, never
  contents) and the aggregates above. The desktop backup (`GET /api/backup`, permission `install`) holds every
  conversation, but `install` exists only for the desktop's single person and is never granted on a server; a server
  backup is the operator's `pg_dump` of the whole database, outside the API.
- **Runs and files by id in a body (W3-S1).** Knowledge adoption (`/api/knowledge/adopt/check` and
  `/api/knowledge/adopt`) reads a run's step output, proposals and result files: another person's answer run answers
  `404 run.not_found` there, like the preview and the assistant's run attachments. A conversation's file is no input
  of a run, batch, schedule, test case or form field by its id (the run file catalog leaves it out, like an assistant
  file; `form.file_missing`), also for its owner, who uploads it as an input file on purpose. An answer's run gets the
  files of its message without the catalog.
- **Memory (W3-S1 review).** An answer never writes into a shared memory space: the runtime turns a template's
  persistent memory into run memory for answers, because every member reads a space (`/api/memory`). The audit's
  template has persistent memory, and its answer writes a note with the marker.
- **Grants for "this conversation" (W3-S1 review).** `POST /api/access/grants` with a `conversation_id` names only a
  conversation the caller sees: another member's conversation with an own agent answers `404
  conversation.not_found` (as an unknown one), and a run the caller does not see `404 run.not_found`. An
  administrator does not change which tools another person's answers may use.
- **Audit tests.** `tests/test_conversation_access_audit.py` (desktop app, several principals) and
  `tests/test_conversation_access_audit_server.py` (PostgreSQL server: two members, a viewer, another tenant, a
  principal without a role) scan every route of the app (`check_routes`): no `GET` route answers anything of another
  person's conversation, and every route with its run, conversation, file, attachment or snapshot id in the path
  answers `404` (or refuses the body alike for an unknown id). Every path parameter gets the owner's real value where
  one exists (the answer's workflow id, the agent template, its memory space and note, the tools called, the
  assistant's attachment of the answer run and its snapshot); only resources of their own keep an unknown id.
- **Switched off.** The upload route reads its multipart body before it answers `404 conversation.disabled`, because
  FastAPI parses the body before the route runs (the server's request size limit applies); nothing is stored (L-3).
- **Codes.** `conversation.not_found` and `conversation.agent_not_found` answer `404`; `conversation.answer_running`
  and `conversation.budget_exhausted` `409`; `conversation.model_required`, `conversation.offline_cloud_model` and
  `conversation.cloud_model_not_chosen` (the answer would use a cloud model nobody chose, such as a cloud judge)
  `422`; the others `400`.

- **Offline (N-09).** With `offline`, every call of an answer and of the runs below it follows the runtime's
  offline rules (runtime `docs/offline.md`): local models only, no server tools, and a tool that is not available
  offline is refused (`offline.tool_unavailable` on the call). An agent template with `offline` answers with local
  models only in every conversation.
- **Offline content outside the run (N-09 review).** A run of an offline agent or conversation (in the run, above
  it or below it; runtime `RunManager.content_offline_scope`, fails closed) only reaches local models elsewhere too:
  - The Nodestro Assistant: attaching such a run is refused while the assistant model is a cloud model
    (`400 offline.cloud_model`, `scope` `agent` or `conversation`, nothing stored). Its snapshot records it
    (`Snapshot.offline`). Every message, retry and answer of an assistant conversation that has such a run attached,
    or sent one with an earlier message (the history repeats the answers about it, so detaching does not free the
    conversation), uses a local model on this computer only (`offline.cloud_model`, `offline.remote_endpoint`),
    also when a message asks for another model or the settings change later.
  - A test series' AI judge: the output of a case whose run has offline content goes to a local judge only; a cloud
    judge is not asked and the case's verdict has `error_code` `offline.cloud_model` (the case stays open). A run that
    cannot be read counts as offline.

Open: the studio has no UI for it yet (runtime RT-089).
