# Changelog

## Unreleased

The server starts (desktop/server plan, PR-12, 2026-10-31):

- New command `nodestro-server` (`nodestro_api.server`, extra `server`): `check`, `migrate` (with `pg_dump` backup
  or `--backup-taken`), `backup`, `api`, `worker`, `create-tenant`, `setup-link`. Settings come from the environment and are
  validated first (`ServerSettings`, `profile.invalid` listing every problem); API and worker start only with a
  current database schema and a reachable Docker (`sandbox.unavailable`).
- `TenantComposition.secrets` / `TenantFiles.secrets`: tenant provider keys are encrypted at rest; the server's
  environment keys are never used for tenants. `TenantComposition.static_dir` serves the built studio.
- `NODESTRO_MODEL_ENDPOINTS` (`TenantComposition.endpoints`): the model endpoints tenants may use; others are
  refused (`provider.endpoint_not_allowed`). `NODESTRO_WORKER_*_SECONDS` set the worker's lease and intervals.
- `create_tenant_app` accepts `http://localhost` origins for development (the cookie stays `Secure`).
- Pins: `nodestro-runtime` at `c6bc2e5` (server settings, tenant secrets, endpoint allowlist, runs stored as
  running; before `4104176`), `nodestro-examples` at `86c58a1` (before `43e02f6`).
- The desktop's `--profile server` is refused with `profile.invalid` (pointing to `nodestro-server`) instead of
  `profile.server_unavailable`.

Libraries in the Docker sandbox (desktop/server plan, PR-11, 2026-10-31):

- Pins `nodestro-runtime` 4104176 and `nodestro-examples` 43e02f6. With the Docker sandbox, the Python
  library endpoints no longer answer `deps.docker_unsupported`: an approved environment becomes a container image,
  and tools (also AI-generated ones) run with their libraries in a container without network. New error codes
  `deps.lock_without_hashes`, `deps.wheels_required`, `deps.hash_mismatch` and `deps.image_failed`; new approval
  reason `hashes` (an environment approved before this release needs a new approval for the Docker sandbox).
- Fix: the workflow architect writes a job's usage to the ledger before it marks the job finished, so the usage
  report never misses requests of a job that is already shown as finished (seen as a flaky test on Python 3.10).

Jobs and workers (desktop/server plan, PR-10, 2026-10-31). Nothing changes for the desktop:

- Pins `nodestro-runtime` 3a052d1 and `nodestro-examples` e591c48 (job queue, workers, run events).
- `TenantComposition.queue`: with a `JobQueue`, the multi-tenant API queues runs instead of executing them; run
  event streams and waits follow the worker through the database, and cancelling reaches the worker.
- `nodestro_api.worker.ServerWorker`: claims and executes run jobs, reaps jobs of dead workers (their runs become
  `run.interrupted` and are never executed again by themselves) and, while it holds the scheduler lock, fires the
  tenants' schedules as queued runs. `server_app.TenantFiles` opens a tenant's context for API and worker alike.
- `Schedules.claim_slot` and `record_slot`: on a server every schedule slot fires at most once
  (`nodestro_runtime.jobs.ScheduleFirings`).
- Like the rest of the server profile, the worker is not startable yet (PR-12). Batches, test series, assistant
  drafts and Python environment installations still run in the API process.

Server logins and tenants (desktop/server plan, PR-9, 2026-10-31). Nothing changes for the desktop:

- New permission `install` for the installation itself: backups and restore, diagnostics and the network listener
  (10 routes, before `admin`). The desktop's local owner holds every permission as before; on a server no tenant
  role holds `install`.
- `Principal` carries the tenant and role of a request. Routes reach the context of the principal's tenant only
  (`app_context`); on the desktop there is one context as before.
- `nodestro_api.identity`: OpenID Connect login (authorization code with PKCE, state, nonce, ID token checks with the
  provider's asymmetric keys), server-side sessions in a `__Host-` cookie, roles per tenant, one-time setup links for
  a tenant's first owner, `/api/auth/login`, `/callback`, `/me`, `/switch`, `/logout`, and a same-origin check for
  changing requests.
- `nodestro_api.server_app.create_tenant_app`: the studio API for many tenants on PostgreSQL. It is not startable
  yet (the server profile stays refused until PR-12) and is built by the tests only.
- Optional dependency `nodestro-api[server]` (psycopg, PyJWT, httpx). Generated HTML result pages also carry
  `sandbox` in their `Content-Security-Policy`.
- Pins: nodestro-runtime `8eea1ea` (PostgreSQL storage, tenant workspaces, identity records), nodestro-examples
  `9b018da`. The api adapts to the runtime's `DocumentStore` typing: backups and diagnostics use
  `Workspace.sqlite_store`.
- Tests: `tests/test_identity.py` with a stub OIDC provider and PostgreSQL in CI: anonymous `401`, invalid ID tokens
  (audience, issuer, expiry, nonce, forged signature, `none`), PKCE, replayed states, open redirects, cross-site
  requests, setup links, the role matrix and cross-tenant `404` for workflows, runs (also event streams, exports,
  files, inputs), knowledge content, input files and memory.

Desktop launch secret (desktop/server plan, PR-6, 2026-10-31):

- `NODESTRO_LAUNCH_SECRET` (`AppConfig.launch_secret`, module `nodestro_api.launch`): the desktop app's window
  exchanges it once at `GET /api/launch?token=…` for an `HttpOnly`, `SameSite=Strict` session cookie (a small page
  continues to `/` from the backend's origin, because a cross-site redirect would not carry a strict cookie); afterwards
  every request without the cookie answers `401 access.launch_required` (open: `/api/health`, `/api/published`).
  A second launch answers `403 access.launch_used`, a wrong token `403 access.launch_invalid`.
- The variable is removed from the process environment when it is read, and the token is redacted in uvicorn's
  access log. Secrets shorter than 32 characters are
  refused (`access.launch_secret_weak`), and so is `--reload` together with a secret.
- Without the variable nothing changes. OpenAPI is unchanged (the launch route is handled by the middleware).

Sandbox policy (desktop/server plan, M1 PR-4, ADR-0006, 2026-10-31): pins of `nodestro-runtime` at `d6e75b7` and
`nodestro-examples` at `31b5714`. **Changed behavior on purpose:**

- `ToolInfo.trust` (`trusted`, `user-authored`, `untrusted`) is part of every tool response (OpenAPI updated).
- Without Docker, `POST /api/tools/jobs` answers `400 sandbox.isolation_required` and starts no job; testing and
  approving tool drafts, running AI-generated tools and tools stored before this release report the same code and
  run nothing; the workflow check shows it on the agent's `tools` field before a run. With the `docker` setting they
  run in containers. AI-generated code with third-party libraries is refused (`sandbox.libraries_need_isolation`).
- A restore makes every restored tool `untrusted` (its provenance is unknown).
- Published releases record `tool_trust`; Nodestro's own API decides with the studio's provenance, not with that
  claim. Exported projects read it and take `TOOL_SANDBOX=subprocess|docker` (README and `.env.example` of the
  export): only user-authored tools run with `subprocess`.
- Tests use `nodestro_runtime.testing.ContainerDouble` (`tests/conftest.py: isolate`) where a flow needs Docker, and
  assert the refusal without it.

Storage seams (desktop/server plan, PR-3 follow-up, 2026-10-31): pins of `nodestro-runtime` at `e90c205`
(`DocumentStore`, schema version and migrations) and `nodestro-examples` at the same runtime pin.

- Backup and restore use the runtime's public storage methods (`DocumentStore.snapshot` and
  `DocumentStore.maintenance`) instead of private connection and lock attributes; a test checks that the API reads
  no private storage attribute.
- A backup whose database has a newer schema version (`PRAGMA user_version`) than this runtime supports is refused
  with `400 backup.schema_too_new` (`found`, `supported`) before anything is replaced.
- The export of a workflow as a Python project lists 27 runtime modules (new: `sqlite_tables`).

Operating profile and route permissions (desktop/server plan, PR-2, 2026-10-31): pins of `nodestro-runtime` at
`8ea9a5c` (operating profiles) and `nodestro-examples` at `f90d3a1` (the same runtime pin; before: `ad75859` and
`520fa72`). No change of routes, fields or the OpenAPI document.

- Profile: `AppConfig.profile`, `--profile desktop|server` and `NODESTRO_PROFILE`; `desktop` is the default and the
  previous behavior. `server` is refused with `profile.server_unavailable` and an unknown name with
  `profile.invalid`, before the data directory is created or locked. The profile is passed to the runtime's
  workspace explicitly.
- Permissions: every studio route declares `read`, `edit`, `run` or `admin`, checked on every request by a FastAPI
  dependency (`nodestro_api.permissions`). The desktop's local owner holds every permission. `/api/health` is
  declared public; `/api/published` keeps its API keys. `create_app` refuses to build an application with an
  undeclared route. The published prefix exempts only the endpoints listed in
  `serving.KEY_AUTHENTICATED_ROUTES` (route class `KeyAuthenticatedRoute`); a test calls each of them without a key
  and with an invalid one and expects `401`.
- **Compatibility, security:** the studio API is no longer offered to a network, because it has no login.
  `--host 0.0.0.0`, `::` or another non-loopback address and `NODESTRO_ALLOWED_HOSTS` with non-loopback names or `*`
  now stop the start with `access.network_needs_login` (before: allowed, protected by host and origin checks only).
  Requests with `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host` or `X-Real-IP` to the studio API answer `403
  request.forwarded_not_allowed`, so the studio API cannot be put behind a reverse proxy. Published workflow APIs
  are unchanged, including their network listener.
- **Compatibility:** an empty or blank `NODESTRO_DATA_DIR` now means `~/.nodestro` (before: the current directory);
  surrounding spaces are ignored. An invalid `NODESTRO_PROFILE` stops the start.

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
