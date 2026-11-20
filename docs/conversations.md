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
| `DELETE /api/conversations/{id}` | `edit` | Delete it with its messages and files; a running answer is cancelled; the runs stay |
| `GET /api/conversations/{id}/export` | `read` | Markdown, as a download |
| `POST /api/conversations/{id}/files` | `edit` | Upload files (multipart `files`); refused files come back with their problem |
| `DELETE /api/conversations/{id}/files/{file_id}` | `edit` | Remove a file |
| `GET /api/conversations/{id}/files/{file_id}/content` | `read` | The file as uploaded, always a download |
| `POST /api/conversations/{id}/messages` | `run` | Store a message (`content`, `file_ids`) and start its answer; returns both messages |
| `GET /api/conversations/{id}/messages/{message_id}` | `read` | One message; an answer as its run is now |
| `POST /api/conversations/{id}/messages/{message_id}/cancel` | `run` | Cancel a running answer (its run) |
| `GET /api/conversations/{id}/messages/{message_id}/events` | `read` | Server-Sent Events: `snapshot` (the message), `event` (each `RunEvent` of the answer's run), `end` (`status`) |

Rules:

- **One engine.** Every answer is a run (trigger `conversation`, `trigger_ref` = the conversation). It is in the run
  list (`GET /api/runs?trigger_ref=<id>`), its cost is in the usage views, and a tool call that waits for a person is
  decided with the existing approval routes (the answer lists it in `approval_ids`).
- **Models.** The API never picks another model. Without `model`, a conversation takes the agent's or the default
  model only when it is local, otherwise `422 conversation.model_required`. A local model that is not ready is refused
  before a message is stored (`409 model.local_unavailable`), without alternatives being chosen.
- **Owners and tenants.** On a server a conversation is its owner's: another member gets `404 conversation.not_found`
  on every route, its files included (also under `/api/inputs`, and as a run input). Every route works on the
  request's tenant only.
- **Codes.** `conversation.not_found` and `conversation.agent_not_found` answer `404`; `conversation.answer_running`
  and `conversation.budget_exhausted` `409`; `conversation.model_required` and `conversation.offline_cloud_model`
  `422`; the others `400`.

Open: the studio has no UI for it yet; an answer's run is a run of the tenant like any other (runtime RT-089).
