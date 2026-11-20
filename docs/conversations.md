# Conversations with own agents (N-04)

Status: **in development, off by default.** `NODESTRO_CONVERSATIONS=1` turns the routes on; otherwise every route
answers `404 conversation.disabled` and nothing is read or written. The object itself, its storage and its rules are
the runtime's (`nodestro_runtime.conversations`, runtime `docs/conversations.md`).

The routes are in the router group `conversations` (`nodestro_api.router_groups`), so a product app of the host
(`docs/embedding.md`) can serve them.

| Method and path | Permission | What |
|---|---|---|
| `GET /api/conversations` | `read` | The caller's conversations, most recently changed first |
| `POST /api/conversations` | `edit` | Create: `agent` (`template_id`, optional `version`), optional `model`, `title`, `offline`, `budget_usd` |
| `GET /api/conversations/{id}` | `read` | The conversation with its messages, usage and the answer being written |
| `PATCH /api/conversations/{id}` | `edit` | Rename, switch the agent or model, the offline flag or the budget (only the fields sent) |
| `DELETE /api/conversations/{id}` | `edit` | Delete it with its messages, files and the answers' runs (a running answer is cancelled first); revokes its grants for "this conversation" |
| `GET /api/conversations/{id}/export` | `read` | Markdown, as a download |
| `POST /api/conversations/{id}/files` | `edit` | Upload files (multipart `files`); refused files come back with their problem |
| `DELETE /api/conversations/{id}/files/{file_id}` | `edit` | Remove a file |
| `GET /api/conversations/{id}/files/{file_id}/content` | `read` | The file as uploaded, always a download |
| `POST /api/conversations/{id}/messages` | `run` | Store a message (`content`, `file_ids`) and start its answer; returns both messages |
| `GET /api/conversations/{id}/messages/{message_id}` | `read` | One message; an answer as its run is now |
| `POST /api/conversations/{id}/messages/{message_id}/cancel` | `run` | Cancel a running answer (its run) |
| `GET /api/conversations/{id}/messages/{message_id}/events` | `read` | Server-Sent Events: `snapshot` (the message), `event` (each `RunEvent` of the answer's run), `end` (`status`, or `reason` `deleted` when the conversation is deleted meanwhile) |

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
- **Switched off.** The upload route reads its multipart body before it answers `404 conversation.disabled`, because
  FastAPI parses the body before the route runs (the server's request size limit applies); nothing is stored (L-3).
- **Codes.** `conversation.not_found` and `conversation.agent_not_found` answer `404`; `conversation.answer_running`
  and `conversation.budget_exhausted` `409`; `conversation.model_required`, `conversation.offline_cloud_model` and
  `conversation.cloud_model_not_chosen` (the answer would use a cloud model nobody chose, such as a cloud judge)
  `422`; the others `400`.

Open: the studio has no UI for it yet (runtime RT-089).
