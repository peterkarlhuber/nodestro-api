# Changelog

## Unreleased

Release check (2026-10-30): no change of routes, fields or the OpenAPI document.

- Assistant: an answer was stored as finished a moment before it was released, so a retry or a new message sent right
  after it finished could be refused with `assistant.busy`, and a conversation could be read without the finished
  answer's usage. Storing and releasing are now one step, and reading a conversation is consistent. This was the
  cause of the intermittently failing assistant usage test.
- Backups: an archive whose entry does not match its header (wrong CRC, a size that differs from the data, encryption,
  an unknown compression method) answered `500`; it is now refused with `400 backup.checksum` or
  `backup.unexpected_file`. Checking a backup while a restore runs waits for the restore instead of deleting the
  backup the restore reads from, and a restore never copies from a database without Nodestro documents.

Platform integration (2026-10-30): pins of `nodestro-runtime` at `ad75859` and `nodestro-examples` at `520fa72` (before:
`b1bae2a` and `0e7e6fa`). No change of routes, fields or the OpenAPI document.

- With that runtime, a published run's result and its result files no longer contain an approval's comment, an
  unreadable knowledge PDF answers `400 document.unreadable` or `document.encrypted` instead of `500`, and the event
  ids of a paused run continue after a restart, so `Last-Event-ID` reconnects miss nothing.
- Published event streams: `approval_decided` carries the decision but no longer the reviewer's comment, which is
  internal; it stays in the run's approval record in the studio. The run's result and files never contained it.
- Knowledge uploads are all or nothing: when one file of an upload is refused, the files before it are removed again.
- `nodestro-api` and `nodestro` stop with one sentence, before they announce a URL, when another Nodestro process already uses the data directory
  (the runtime now refuses a second workspace on it; two processes marked each other's runs as interrupted and ran
  every schedule twice).
- The README of an exported API says "1 approval step" ("1 Freigabeschritt") instead of "1 approval steps".
- The interactive reference under `/api/docs` loads Swagger UI from a CDN; offline, `/api/openapi.json` and
  `openapi/nodestro-api.json` remain available.

Quality round of 2026-10-30: security, data integrity, streaming and reliability. No data migration, no change of
routes, fields, error codes or the OpenAPI document; existing clients and SDKs keep working.

- Security: uploaded knowledge files were served inline with the type the uploader chose, so an HTML file ran its
  scripts in the studio's origin (stored cross-site scripting against the key-less studio API). HTML, SVG and XML
  files are now downloaded, every uploaded file is served with `X-Content-Type-Options: nosniff`, and everything
  except PDF carries a restrictive `Content-Security-Policy`. Result files carry `nosniff` as well.
- Security: published APIs limited the request size (80 MB) only when the client sent a truthful
  `Content-Length`; a chunked or mislabeled request was read without limit. The size is now counted while reading,
  for JSON and `multipart/form-data` alike (`413 request.too_large`).
- Security: a key verification that read a key just before it was revoked could store it back unrevoked (the
  update of `last_used_at`). Key changes and the usage update now happen under one lock and re-read the key; two
  rotations of the same key at the same time give one new key instead of two.
- Limits: the daily cost limit, the limit of waiting runs, the usage report, the run list of an API, the run counts
  of its versions, "switch off and cancel" and the check before deleting a publication looked only at the 5,000
  most recently updated runs of the whole installation. With more runs (a large batch, for example) older API runs
  were ignored: the daily budget could be exceeded, waiting approvals stayed uncancelled and a publication with
  waiting runs could be deleted. Every run of the publication is now counted (filtered in SQLite with
  nodestro-runtime's `Store.find`, when available). The retention cleanup of exported projects likewise missed the
  oldest runs beyond 100,000.
- Idempotency: a retried request that is answered from its `Idempotency-Key` no longer counts against
  `runs_per_minute` (a retry could get `429` instead of its run). When storing the idempotency record fails after the
  run started, the client still gets the started run instead of `500`, so it does not start it again.
- Server-Sent Events: after a reconnect with `Last-Event-ID`, the published event stream sent the status with the
  newest id before replaying the missed events with lower ids. A client that dropped during the replay then
  reconnected with the newest id and lost the events in between. The status now carries an id only when nothing is
  replayed, so ids increase strictly.
- Assistant and Workflow Architect streams: a subscriber's snapshot could already contain a text delta, step,
  source or handover whose event it then received again (duplicated text). Changes and their events are now recorded
  in one step. Architect events beyond the 400 kept log entries got numbers that repeated; they are numbered by a
  counter now.
- Network listener: a configuration change stopped the running listener first; when the new one could not start
  (port taken, unusable certificate) published APIs stayed unreachable in the network. The previous listener now
  keeps running and the status reports why the change failed. Changes are serialized, and a listener that fails to
  start releases its port.
- Backups: a backup whose entries would not fit on the disk when unpacked (a "ZIP bomb") is refused before
  unpacking (`backup.too_large`). A restore copies the tool and input files before it replaces the database, so a
  full disk no longer leaves restored data next to the old files; only one restore runs at a time
  (`backup.restore_running`).
- Published APIs: a non-numeric `limit` or `days` query parameter answered `500`; it answers `422 request.invalid`.
- Shutdown: when stopping one service failed, the remaining services and the database stayed open; every service is
  now stopped and the database closed. A failed start stops what had already started.
- English-only technical content: README (rewritten: security models, published APIs, export, backups,
  limitations), CHANGELOG, PROVENANCE and CI messages. Localized content (the German export README, multilingual
  word lists, user data in tests) is unchanged.
- CI: the package job starts the installed wheel with `nodestro-api` and calls it over HTTP, including the Host check.
- Security: with `--host 0.0.0.0` (or `::`) the studio API answered every host name; a web page could read and use
  it through DNS rebinding, and the origin check did not help (same origin). It now answers IP addresses, loopback,
  this computer's name (`name`, `name.local`) and `NODESTRO_ALLOWED_HOSTS` there; other host names get
  `403 request.host_not_allowed`. `NODESTRO_ALLOWED_HOSTS=*` still accepts every host name explicitly.
- Security: the studio API (without keys of its own) answers only requests for `localhost`, loopback addresses and
  the address chosen with `--host`, and accepts changes only from its own pages. A web page in the browser can
  therefore neither read and use it through DNS rebinding nor create workflows, start runs or upload files with a
  cross-site request (`403` with `request.host_not_allowed` or `request.origin_not_allowed`).
  `NODESTRO_ALLOWED_HOSTS` names further host names (reverse proxy).
- Privacy: deleting a run also deletes the runs of its sub-workflows (with their result files) and releases their
  input files. Before, these copies stayed invisible in the database and kept the uploaded files.
- The Nodestro Assistant releases the live events of a finished answer (before, they stayed in memory for every
  answer).
- Pins of `nodestro-runtime` and `nodestro-examples` at the current state.
- CI: current actions (Node 24), checkout without stored credentials.

## 0.2.0 (2026-10-29)

- First version as a repository of its own: extracted from `nodestro` 0.1.0 (commit 15e2076, now
  `nodestro-legacy`) without new features and without behavior changes. The origin of every file: PROVENANCE.md.
- Dependencies pinned to exact commits (orkexa `cbf6df3d6f727b96924f1ae66e60b469eeef58e6`); the tested combination
  is recorded in `nodestro-studio/platform/versions.json`.
- CI with the repository secret `NODESTRO_READ`.
- `Nodestro` now extends the runtime's `Workspace`. The API no longer serves an interface (that is
  `nodestro-studio`'s job); new are the command `nodestro-api` and the OpenAPI contract `openapi/nodestro-api.json`.
- Export: the runtime is part of the exported project as `nodestro_runtime/` and `nodestro_api/`; `RUNTIME.json` names
  the versions and commits.
