# Changelog

## Unreleased

- MUP-ONA B2 cascade (2026-11-07): pins nodestro-runtime d9c3d6e (runtime #136 B2, #135 Axentra 60d83b3 with the
  A8 fixes, #134 PostgreSQL reindex switch under load), orkexa 028faa1 (orkexa #64: SQLite backends in
  `orkexa.backends.sqlite`, old import paths unchanged) and nodestro-examples a0edd20. No API change.
- MUP-ONA B1/B3 cascade (2026-11-07): pins nodestro-runtime 099f4d4 (runtime #130 B1 orkexa conformance suite,
  #131 B3 PostgreSQL ranking drift fixes and bounded reindex switch, #132 sandbox gateway fix), orkexa b3d6fae
  (orkexa #63) and nodestro-examples b8998b2. New catalog codes `agent.VectorError` and `run.agent.VectorError`
  (orkexa's vector check, `vector.invalid`/`vector.dimension`): a refused vector answers 422 like the other retrieval
  errors, not as a generic bad request although it is also a `ValueError` (test). Searches over 1000 characters or
  64 distinct words now get 422 `agent.RetrievalError` (`query.too_long`); the global search allows 200 characters.
- TODO abatement round 5 (2026-11-07):
  - Usage by region (API-021): `GET /api/usage` gains `by_region`, the usage per provider region (Amazon Bedrock),
    the most expensive first, and every `UsageBucket` lists its `regions`. Runs are split step by step (agents,
    orchestrator decisions, sub-workflows and orchestrator teams, from the runtime's `Usage.regions` of each step);
    usage log entries go to their `region`. Usage without a known region (other providers, usage recorded before the
    runtime recorded regions) is in no region bucket. Additive OpenAPI fields; the SDK types are regenerated.
  - `nodestro-server check` warns when pgvector does not rank the knowledge vectors of the server's schema
    (RT-048): `warning: knowledge.pgvector_<state>` (`installable`, `other_schema`, `unavailable`) with the
    runtime's message and the SQL that fixes it. It is a warning, never a reason for "not ready"; a state that
    cannot be read gives `knowledge.pgvector_unknown` without the error text.
- MUP-ONA A8 (shared security test vectors, VB-7):
  - Security: the OIDC client checks ID tokens as Axentra does. A token issued more than
    `OidcSettings.id_token_max_age` (default 600 s, plus 120 s clock skew) ago is refused (`iat`), as is a token
    whose `azp` names another client (also with a single audience) and one whose `sub` is empty, not a string or
    longer than 255 characters (OpenID Connect Core 1.0, section 2). A `kid` whose key does not fit the header's
    algorithm ends the login with `identity.login_failed` instead of an unhandled error.
  - Tests: `tests/test_security_vectors_jwt.py` runs the ID token vectors of the shared security test vectors
    against the OIDC client, with keys generated in the test (T-VEC-3). It reads a pinned copy of nodestro-runtime's
    canonical corpus (`tests/vectors/security-vectors.json`, version 1; origin and SHA-256 in
    `tests/vectors/SOURCE.md`, checked by the test).
- TODO abatement round 4 (2026-11-06):
  - Pins nodestro-runtime f26dac4 (runtime #126: a busy sandbox image build stays `checking` (LD-020), Bedrock
    region in usage, registry facts before a download, `pgvector_status()`; includes the price-table date fix of
    runtime #125, runtime #128: the legacy fixture restore test skips without psycopg, and MUP-ONA A11 runtime #123)
    and nodestro-examples 570ef4f (examples #81: the same runtime pin). The OpenAPI documents gain the
    runtime's new optional fields: `value_warnings` (agent test, RT-054), `from_registry`, `regions` and `region`.
  - Server exports: a portable export job is queued (`export` job of the job queue, `ExportJob.queued`) and a worker
    builds it with a lease, one package at a time on the whole server (session advisory lock); a workspace has one
    active export job (`export.busy`). Cancelling works for queued and running jobs from any API process, a job
    whose worker died ends as `export.interrupted` (the reaper, or the next read), and a suspended tenant's job ends
    as `identity.tenant_suspended`. Workers need `NODESTRO_SERVER_EXPORT_JOBS=1` and the shared data volume as the
    API processes do. The package is written to a work folder and moved into place (API-048).
  - Server worker: with `NODESTRO_WORKER_HEARTBEAT_FILE` the worker writes a heartbeat file every 10 s (state,
    database reachability, duties); `nodestro-server worker-health [--file] [--max-age 60]` exits 0 when it is fresh,
    the database was reachable and no duty ended, 1 otherwise and 2 when there is nothing to check (API-011).
  - Tests: crash and multi-process tests with real killed processes on PostgreSQL: a workflow delete killed inside
    its transaction (desktop and server), a test series whose API process was killed, and schedules changed while
    two worker processes tick and the leader is killed (API-022).
  - The export's `Dockerfile` is pinned by digest from nodestro-runtime's `container_images.BASE_IMAGES` (API-027);
    the standard export README (six languages) shows the container with bind mounts for `DATA_DIR` and
    `OUTPUT_DIR` (API-064); the portable README and `docs/portable-export.md` say what offline mode restricts and
    that it is not a firewall for person-written tools (API-047).
- TODO abatement round 3 (2026-11-06):
  - Server profile: `GET /api/members`, `PUT /api/members/{user_id}/role` and `DELETE /api/members/{user_id}`
    (admin): a tenant's administrators list members and change or remove them. Only an owner gives, takes or
    changes the owner role (`403 identity.owner_required`), a tenant keeps one owner (`409 identity.last_owner`),
    a user of another tenant is `404 identity.not_a_member`, and each change is in the access audit trail
    (`role_changed`, `member_removed`) (API-019).
  - Server profile: tenant contexts that nobody uses and that have no background work are closed after
    `NODESTRO_TENANT_IDLE_MINUTES` (default 30, `0` keeps them open); requests hold their tenant's context while
    they run (API-001).
  - Server worker: semantic reindex runs as a `reindex` job of the job queue with a lease, so it survives a restart
    of the API process and every process sees its progress (runtime RT-046) (API-044).
  - Server worker: the leader removes assistant chat files older than each tenant's retention every hour
    (`WorkerSettings.chat_file_every`) (API-066).
  - Server exports: `NODESTRO_SERVER_EXPORT_TENANT_QUOTA_BYTES` (default 8 GiB) caps the packages one tenant keeps
    (`403 export.server_quota` with `size`, `used`, `limit`, also as an export check issue);
    `DELETE /api/exports/{export_id}` (edit) removes an export and its package (API-048).
  - Pins nodestro-runtime b29b738 (runtime #121: `jobs.ReindexWorker`, `Workspace.background_work`) and
    nodestro-examples 4b947c4 (examples #79: the same runtime pin).
- MUP-ONA A1 (PS-1), with the runtime's PDF limits (pins nodestro-runtime de56875, runtime #127, orkexa #59, and
  nodestro-examples cda07c4, examples #82: the same runtime pin):
  - Security: the runtime reads PDF text in a separate, limited process (`NODESTRO_PDF_TIMEOUT_SECONDS`,
    `NODESTRO_PDF_MAX_PAGES`, `NODESTRO_PDF_MAX_CHARS`, `NODESTRO_PDF_MAX_MEMORY_MB`). A knowledge upload with a PDF
    over a limit answers `422 document.too_complex` (`name`, `reason`: `pages`, `chars`, `timeout`, `memory`,
    `limit`, `crash`) and keeps none of the upload's files; an input file over a limit is reported with that code in
    its upload result. The error code list has `document.too_complex` and `document.limits_invalid` (an invalid
    `NODESTRO_PDF_*` value, `variable`). Knowledge and input uploads read PDFs off the event loop, so the API keeps
    answering (`/api/health` included) while a PDF is read; a published API's run start now also stores and reads
    its uploaded files off the event loop (it blocked every request of the process before), and answers a PDF over a
    limit with `422 document.too_complex`.
- MUP-ONA B0: `pyproject.toml` declares `orkexa`, which the API imports, at the commit the runtime pins
  (`f89102c`). The Axentra login flow builds its provider settings with `model_validate`, so type checking passes
  with the Axentra commit the runtime pins next (`273e675`).
- MUP-ONA security hardening (A2, A3, A5):
  - Security (A2, PS-2): error answers name no file system path, input value, credential or library text. An
    exception without an error code of its own (`bad_request`, a `not_found` for a key the request did not name,
    `provider_api`, `agent.<error>` except `agent.RetrievalError`) answers with a fixed `detail` and
    `params.request_id`; its original text is logged with the same id (logger `nodestro_api.errors`, credentials
    redacted). Coded texts keep their wording with `[path]` for absolute paths; validation messages show `[input]`
    instead of the input. Statuses and codes are unchanged; clients that read `detail` of these answers get the
    fixed text.
  - Security (A3, PS-3): `nodestro-server check` warns and `api` logs `proxy.client_address_unknown` when
    `NODESTRO_BEHIND_PROXY=1` is set without `FORWARDED_ALLOW_IPS`; `NODESTRO_REQUIRE_CLIENT_ADDRESS=1` makes both
    refuse. Failed published API keys are counted per key id prefix (8 characters, never the secret), with a ten
    times higher limit per address, so one client's wrong key does not throttle other keys behind the same proxy.
  - Security (A5, PS-5): the network listener and exported projects check every route's protection when they are
    built, like the studio and the server (`check_routes`; deny by default): `/health` and an export's API reference
    are declared `PUBLIC`, the export's web interface routes are listed in `WEB_UI_ROUTES`. Exports now contain
    `nodestro_api.permissions`.
- MUP-ONA A11: CI job `tenant-sql` checks every PostgreSQL statement of the API with the runtime's tenant SQL check
  twice, with the pinned runtime and with the current runtime `master` (installed over the same public `git+https`
  route as the pin, no secret), on every change and daily. `NODESTRO_REQUIRE_TENANT_SCAN=1`: a checker that cannot
  run, a missing `pglast`, a failed install, a scan of no file, a planted leak that is not reported or a runtime that
  is not `master` fails the job (`scripts/tenant_sql_scan.py`); none of its tests may skip.
- MUP-ONA A0 (security and test baseline, no code change):
  - CI compares the skipped tests of every pytest job with an expectation file (`tests/ci/skipped-<job>.txt`, each
    entry with its reason; `scripts/check_skips.py`): a test that starts to skip, or an expected skip that runs, fails
    the job. The dependency audit (`pip-audit`, blocking, `PIP_AUDIT_IGNORE` with reasons) stays as it was.
  - `docs/TODO.md` lists the open MUP-ONA packages of this repository (`API-MUP-*`).
- TODO abatement round 2 (2026-11-06):
  - Idempotency-Keys of studio and published-API run starts are reserved with one atomic insert in the store
    (runtime `put_raw_if_absent`) before the run starts, so concurrent retries on several API processes start one
    run; a retry while the first start is in progress answers `409 idempotency.in_progress` with `Retry-After: 1`
    (API-043).
  - `GET /api/usage` reads only the runs of its period (keyset pages of the store's creation index with `since`);
    the totals are unchanged (API-013).
  - `GET`/`PUT /api/assistant/files/retention` (read / admin): how long assistant chat files are kept (none by
    default, as before). The input file clean-up removes older ones and reports `assistant_files_expired`; the
    desktop app also removes them at start and hourly (API-056).
  - A workflow, run or publication that a request or a stored record refers to and that is gone answers
    `404 <kind>.not_found` with `id`, `kind` and, where the reference knew it, `display` (its name) (API-053).
  - `POST /api/library/{id}/copy` takes `memory_space`: a memory space of the copy's own for its agents with
    persistent memory, checked with the runtime's space name rule (`memory.space_name`) (examples EX-007).
  - `GET /api/tools/{name}/versions` (read): every stored version of a tool with code, tests, content hash and its
    author (`editor`, `adopted` or `ai`), for the adoption dialog's diff (studio ST-036).
  - The published routes declare their error answers (401, 403, 404, 422, 429 and their own 400, 409, 413, 503)
    with the `PublishedError` schema, and every studio operation has an OpenAPI tag (workflows, runs,
    publications, local models, MCP, pricing, ...) (API-016).
- TODO abatement round (2026-11-06):
  - `GET /api/error-codes` (read): every problem code of the API and the runtime with its parameters and where it
    is raised (`ErrorCodeCatalog`), kept in `error_codes.json`; a test fails while it is out of date
    (`python -m nodestro_api.error_codes --write`) (API-016).
  - Runs and run summaries carry `cause` (`RunCause`: the step that failed first, its code and params); the approval
    decision answers the run as it goes on (`queued`) (API-018).
  - `GET /api/usage` has `budgets`: every publication's daily limit with the cost of the last 24 hours, its run
    limit and whether the limit is reached, and the runs of the period a budget stopped or holds (API-017).
  - Copies of examples carry `origin` (example id, library version, language, offline and connected connections,
    copy time); it is read-only and kept when the workflow is saved (runtime RT-055).
  - The Workflow Architect streams the reasoning of its sub-requests too: the Agent Architect (one per agent) and
    the Coding Agent each get a reasoning entry named by `step` and `subject` (`ArchitectReasoning.subject`), and
    their `thinking` events carry `step`, `subject` and `index` (runtime RT-063).
  - Pins nodestro-runtime 3d69702 (runtime #120) and nodestro-examples 82d894b (examples #77).
  - `local.job_running` answers 409, like `/stop` does for the same conflict (API-062).
  - A reconciliation without a billing key returns a report naming the unreconciled providers instead of
    `pricing.billing_key_missing`; the code stays for workspaces with nothing to reconcile (API-007).
  - `nodestro-server check` warns (`bedrock.tenant_unknown`) about Bedrock role entries for tenants that do not
    exist (API-008).
  - `GET /api/settings/defaults` (read) names the default of every setting (API-061).
  - The assistant chat maps Orkexa's `InvalidRequestError.reason`: `assistant.context_too_large`, and the new
    `assistant.provider_rejected_messages` (params `provider`, `status`) (API-065).
  - Published API errors of a missing local model carry `params.hint` for the operator (API-058).
  - `GET /api/search` finds runs (kind `run`) by workflow name, input text and run id, newest first (API-052).
  - Assistant chat files are refused as run inputs by id (`400 input.assistant_file`; another user's file is 404)
    (API-055).
  - `nodestro-server assistant backfill-owners [--dry-run] [--tenant]` gives older conversations the owner of
    their files (API-057).
  - Developer messages (API-053): the export check reports `export.server_unsupported`, `export.server_too_large`
    and the limit (`export.server_limit`, info) up front, and export jobs carry `error_params`; an export README
    without approval steps has no approval part and says "none" instead of "-" (all six languages), and its own
    approval test is skipped; the exported Python client reads `NODESTRO_BASE_URL` and prints HTTP errors as one
    line; `local.task_unknown` has `params.allowed`; `nodestro-server` prints `profile.invalid` with one problem
    per line; `GET /api/workflows` adds `contract_fields` (name, type, description, required); the library model
    plan adds `part_name`.
  - `GET /api/publication-overview` adds `export_jobs` (running portable builds) and no longer validates the
    active release's bundle (API-063).
  - `openapi/nodestro-api-server.json`: the contract of the server profile, written with
    `python -m nodestro_api.openapi --server` and checked by a test (API-012).
  - Server logins: one address starts at most `NODESTRO_LOGIN_STARTS_PER_MINUTE` logins a minute per API process
    (default 60, `0` off; `429 identity.rate_limited`), and oversized login parameters are refused (API-002).
  - Server batches and test series have an owner and a 60-second lease, renewed by the API process that drives
    them; after a stopped process's lease ran out, the next read marks them `interrupted` (`batch.interrupted`,
    `benchmark.interrupted`) so a person continues them. Running batches from before this change are interrupted
    at their next read. A process whose batch was taken over stops (`409 lease.lost`) (API-006).
  - Access audit trail per tenant: logins, tenant switches (refused ones too), logouts, API key changes and the
    operator's tenant and member commands, read by admins at `GET /api/access-audit`; mirrored into Axentra's audit
    log with `NODESTRO_AXENTRA_AUDIT=1` (API-004).
  - Live run chat answers written by one server API process show live in streams of the others, relayed through
    the store (`run_chat_live`) (API-059).
  - Server observability: `X-Request-ID` on every answer and in every log line, `NODESTRO_LOG_FORMAT=json`, and
    `GET /api/ready` (API-011, in part).

- Settings, attachments and providers (2026-11-06; contract additions only):
  - Assistant files: `/api/assistant/files/support` and `/api/assistant/conversations/{id}/files[/{file_id}]`
    (upload, remove, download as attachment, text), `SendRequest.file_ids`; in server mode a conversation belongs
    to the user who created it (others get 404, also on `/api/inputs/*` and knowledge adoption).
  - `GET /api/models/profile`; the Workflow Architect plan defaults to 32,000 tokens (was 16,000), every step uses
    its configurable limit, and a cut-off answer is retried once with a higher limit within the cap and the job
    budget (`architect.truncated`, `architect.truncated_over_budget`).
  - Live reasoning in the assistant, the Workflow Architect and the run chat (`thinking`, `thinking_status`, SSE
    `answering`); `GET /api/models/capabilities` declares `reasoning`.
  - `POST /api/local-models/setup`; `/stop` refuses Local Docker and external servers (409 `local.not_managed`);
    `LocalJob` carries steps, layers, rate, ETA and resume information.
  - 409 `model.local_unavailable` before the Workflow Architect, the assistant, agent design and test, runs and the
    knowledge index use a local model that is not ready; `POST /api/local-models/readiness`; `attach` on pull; an
    optional per-request `model` for the architect and the assistant (`model.choice_invalid`).
  - `GET /api/publication-overview` (read) for the studio's Publications page.
  - The inline download and the Architect plan use the runtime's result format table; Word and Excel results are
    tested end to end (dev extras `python-docx`, `openpyxl`).
  - Portable web interface: Settings → Result folder shows the folder clearly and lets the operator choose another
    one with a folder browser (folder names only), create it after confirming, open it on this computer and restore
    the default; stored in `data/settings/output-folder.json`, used from the next run, `OUTPUT_DIR` still wins.

- Pin nodestro-runtime 57b40d2 (runtime #116: strict offline mode, RT-004, expired budget decisions, missing
  sqlite-vec) and nodestro-examples f015a4d (examples #74: MCP demo setup texts name the computer where Nodestro runs).
- In the strict offline mode the background price check makes no request, also when the mode is switched on while the
  app runs; it resumes when the mode is switched off. `POST /api/pricing/sync` is refused with
  `pricing.offline_sync` (400). Assistant, architect, agent design and tool generation requests with a cloud model
  are refused with `check.offline_cloud_model`.
- On a server, batches and test series now finish when their runs end: the API process that started them reads the
  runs that the workers execute from the database every two seconds. Only a stopped API process still leaves a batch
  or series `running` (API-006).
- OpenAPI regenerated for the nodestro-examples 64e534a pin: `TemplateTeam.contract_fields` (`ContractField`: `name`,
  `description`).
- A server's tenants no longer learn the server's memory, CPU or GPU through the local model advice
  (`local.cpu_only`, `local.memory_low`, `local.model_slow`, `local.model_too_large`) or the recommendation reasons
  `no_gpu` and `hardware_limit` (`GET /api/local-models`, `GET /api/local-models/recommend`; follow-up of API3-08,
  UX round 3 handover B-1). The desktop and Local Docker are unchanged.
- Pin nodestro-runtime 40c8c06 (runtime #113, backend audit round 3; orkexa e9c2147, axentra 0a1d1cc) and
  nodestro-examples 64e534a (examples #70 team `contract_fields`, #71 runtime pin). The contract
  gains the local catalog's `license_source`.

- Export check speed (UX handover B2): the module closure of an export and the dependency closure are kept per
  installed package version and file change time instead of being computed on every `POST .../export/check`
  (about 1 s per call before, 0.1 s after the first one here; 2.7-10 s were measured on a slower machine).
- Importing a folder that is no offline model package (no `orkexa-models.json`, unknown format, a bad entry) answers
  `400 local.invalid_package` with `params.reason` (orkexa's code) and `params.path` instead of `agent.OllamaError`
  (UX handover B3).
- Portable export check (UX handover B5): no `export.distribution_unconfirmed` ("or choose the Git reference") for
  `kind: "portable"`, which always bundles orkexa (`export.portable_distribution` asks instead); `export.downloads` and
  `export.download_needed` carry `params.downloads` as codes (`python`, `libraries`, `ollama`) for translation
  (`params.items` stays).
- Unknown ids (DX handover B12, error-code change): a route's own unknown workflow, run or publication answers 404 with
  `workflow.not_found`, `run.not_found` or `publication.not_found` (params `id`, `kind`, and `name` as before) and
  "Workflow 'wf_x' does not exist"; other unknown names keep `not_found`.
- `nodestro-api` (DX handover B2): a taken port stops with exit code 2 and one sentence ("Port 8000 on 127.0.0.1 is
  already in use ... Start with --port <number> or stop the other program") before anything says "running"; "running
  at" is printed once the server listens. `cli.serve` takes an optional `ready` callback (used by the studio's
  `nodestro` to open the browser only then) and `cli.port_problem`/`cli.require_free_port` are new.
- Export with the Git reference to orkexa (DX handover B1): the check no longer warns `export.private_repository`
  (orkexa's repository is public), the README in six languages says installing needs access to github.com and no
  longer advises a token in the global git configuration, the Dockerfile comment says the same, and the manifest
  key `orkexa.private_repository_access_needed` is now `orkexa.network_access_needed`.
- Checks that may ask a local model server (RT3-04 pattern) no longer run on the event loop: the agent test's
  check and providers, the provider check before a published run starts (studio and exported applications), the
  assistant model check of the architect, the assistant and the run chat, and the model check of a test series.
  `Benchmarks.start` is now a coroutine. No API change.
- Portable packages (API3-04): on POSIX the launcher creates `data/` and `logs/` owner-only (`0700`, `application.log`
  `0600`) and narrows them when they are inside the package, so other users of a shared computer cannot read runs,
  results, the knowledge or the log.
- Local models on a server (API3-08, contract change): for a server's tenants `GET /api/local-models` returns
  `hardware: null` (the field is now nullable) and a status without `base_url`, `runtime_path`, `models_dir`,
  `free_bytes` and `detected_external` (null); catalog, installed models and recommendations are unchanged.
- Portable export jobs on a server (API3-07): on the server profile `POST /publications/{id}/export-jobs` answers
  `403 export.server_unsupported` unless the operator sets `NODESTRO_SERVER_EXPORT_JOBS=1`; then one build at a time
  on the whole server (`export.busy`) and a size limit per package (`NODESTRO_SERVER_EXPORT_MAX_BYTES`, default 2 GiB,
  `403 export.server_too_large`). Desktop and Nodestro Local Docker are unchanged. See `docs/portable-export.md`.
- Knowledge search bounds (API3-10, contract change): `GET /api/knowledge/search` takes `k` from 1 to 50 and `q`
  of at most 2000 characters; other values answer 422 (before: any `k`, `k <= 0` answered 400 from the runtime).
- Exported applications (backend audit round 3): `/docs` serves a vendored, pinned Swagger UI (exports carry
  `nodestro_api/swagger_ui`) instead of loading it from a CDN into the web interface's origin; request bodies are
  limited before the key check (`BodyLimitMiddleware` in exported applications and the network listener; the
  published `approve`/`resume` bodies get the JSON limit, answered as `413 request.too_large` in the published
  APIs' error format; web sign-in at most 4 KiB, provider setup 16 KiB); the generated `app/main.py` writes the
  workflow name as an escaped string. Portable packages: `start --host` obeys the HTTPS rule for serving the
  network, the one-time sign-in link is no longer written to the log file, the bundled Python's build
  configuration no longer names the exporting computer's data folder, and `output_dir` values with characters
  outside the Basic Multilingual Plane give a valid `config/settings.toml`.
- Pin nodestro-runtime c6f0240 (runtime #111, #112: orkexa 22f2910, the Ollama runtime of another platform) and
  nodestro-examples ed350b2 (examples #68, #69: the same runtime pin). No API change from the pins.
- Portable export and web interface: `ExportOptions` gains `kind` (`standard`, `portable`), `web_ui`, `platform`,
  `local_models`, `offline`, `confirm_models`, `allow_downloads` and `output_dir`; `ExportCheck.portable` lists every
  model with its role, installation state, size, memory, license and the network needs. A ready-to-run package is
  built as a background job (`POST /publications/{id}/export-jobs`, `GET /publications/{id}/export-jobs`,
  `GET /export-jobs/{id}`, `POST /export-jobs/{id}/cancel`): its own Python, every library for the target platform
  (hash-locked), a launcher (`start`, `stop`, `status`, `open`, `configure`, `keys`, `check`), optionally the local
  models with Ollama (SHA-256 verified) and a web interface in six languages. No key is ever in a package. Both
  export kinds now carry `openapi.json` and a README in the export language (de, en, es, fr, it, pt-BR), and include
  `python-multipart` so `multipart/form-data` uploads work in standard exports too. Thinking models served by the
  exported Ollama report `reasoning` and use the CPU saving mode without a GPU. See `docs/portable-export.md`.
- Pin nodestro-runtime 2f1de71 (runtime #108 to #110: checked key figures, the built-in `table_totals` tool, orkexa
  6e61862 so local Ollama agents with a structured result call their tools) and nodestro-examples c05ae45 (examples
  #67: production-downtime and four more templates take their key figures from a tool). OpenAPI: `OutputField.source`
  (`model` or `tool`). The export carries the new runtime module `value_checks`.
- Pin nodestro-runtime 7cb9b4b (runtime #107: a local quality judge answers without thinking in the CPU saving
  mode, longer request timeout for local model servers) and nodestro-examples 36488a6 (examples #65). No API change.
- Pin nodestro-runtime 3006ce0 (runtime #106) and nodestro-examples be55542 (examples #64: task profiles per agent, `agent_plan`, `ModelChoice.agents`).
- Models per agent for example copies (EX-011): `GET /api/library/{template_id}/models` lists every agent of the
  template with its task profile and the model a copy gives it. In a workspace with local models that is the installed
  model the runtime recommends for the task on this computer (hardware, reported capabilities, the agent's tools and
  images, the copy's language, the European preference), never a download (`suggested` names a better one); without a
  suitable installed model the default model stays. `POST /api/library/{template_id}/copy` takes `models` (agent key
  -> model): the person's choice always wins (`library.agent_unknown` for an unknown key). Cloud workspaces keep the
  curated tiers.
- Local model hints and measured speed (RT-050/RT-051): `GET /api/local-models` returns `advice` and the status'
  context window and GPU use; `POST /api/local-models/benchmark` measures an installed chat model (permission
  `install`, refused on server tenants like the other changes of the local model server). Catalog entries carry
  `fits` (this computer) and `build_inputs` (the pinned files of an offline build).
- Pin nodestro-runtime 90ba97b and nodestro-examples 7b79fde (examples #62, the same runtime pin) (runtime #104: 32K context window for the managed Ollama, mDenseOn as optional European
  embedding model built from pinned weights, orkexa 71f8aaf). `POST /api/local-models/pull` builds a catalog model
  with `source="build"`; `POST /api/local-models/import` with `kind: "build"` builds it from local weights and template
  files (air gap). The OpenAPI document gains the job kind `build`, `ImportIn.template` and `LocalModelInfo.source`.
- Pin nodestro-runtime df725c5 and nodestro-examples c03f132 (examples #61, the same runtime pin) (runtime #103: European local models as catalog alternatives, the
  `prefer_european_models` setting, no thinking at effort `low` for local thinking models, a refused Ollama address
  no longer breaks the agent check). `GET /api/local-models` recommends with the stored preferences. The OpenAPI
  document gains `Settings.prefer_european_models` and the catalog fields `tasks` and `alternative`.
- Pin nodestro-runtime 7b9f4f7 (runtime #102: local agents are checked with what their server reports; checks
  `check.local_model_missing`, `check.local_model_not_chat`, `check.local_model_no_tools`; a thinking local model gets
  the effort as `reasoning_effort`) and nodestro-examples d389e91 (examples #60: the same runtime pin). The API
  contract is unchanged.
### Local models, embeddings and semantic knowledge search (2026-11-03)

- Pin nodestro-runtime a60be7d (nodestro-runtime #99 and #101: local models, embeddings, semantic knowledge search,
  selection preferences, a provider configured by an injected client; it pins orkexa 9b06a5e) and nodestro-examples
  da9f1c9 (examples #59: the same runtime pin).
- New routes for local models (Ollama, `api_local_models.py`): `GET /api/local-models` (server state, catalog with
  what is installed and verified, hardware, a recommendation per task), `GET /api/local-models/recommend`,
  `POST .../start|stop`, `PUT .../autostart`, and jobs with progress and cancel for installing the pinned runtime
  (`POST .../runtime`, from a local archive or by download), downloading a model (`POST .../pull`), importing an
  offline model package or a GGUF file (`POST .../import`), deleting a model and moving the model folder
  (`PUT .../storage`, use or copy). On a server every change answers 403 `local.server_managed`; downloads answer 400
  `local.offline_download` in the strict offline mode.
- The semantic knowledge index: `GET /api/knowledge/index`, `POST /api/knowledge/index/rebuild|cancel`,
  `DELETE /api/knowledge/index`. Knowledge search takes `mode` (`fts`, `vector`, `hybrid`); hits carry `ranks`.
- The application starts the managed local model server in the background when it starts (never blocking start-up)
  and skips the online price sync in the strict offline mode. Models are resolved per model, so a local model gets
  the capabilities the server reports.
- Usage buckets count `local_requests` (local inference: no external API billing, operating costs not recorded).
- Backups keep the semantic index (`index_spaces`, `index_vectors`) with its chunks.
- Contract (`openapi/nodestro-api.json`), additions only: the routes, `LocalOverview`, `LocalStatus`, `LocalJob`,
  `KnowledgeIndexStatus` and the new optional settings and usage fields.
- Tests: `tests/conftest.py` keeps the cloud default models for scripted tests (`make_app(local_defaults=True)`
  shows the defaults of a new installation).

### Backend audit round 2 (2026-11-03)

- Export without test tools: an installation without pytest (the server and desktop images) answered the export
  check and the export with 500. The exported project's test dependencies are pinned to the installed version where
  there is one, otherwise to the floor its generated tests need (`pytest>=8`, `httpx>=0.27`). Found in the joint
  integration run.
- API2-01/DX-23: the API reference is served at `/api/docs` from a vendored Swagger UI 5.34.0 (no CDN, no inline
  script; file hashes in `VENDORED.md`), with READ access.
- API2-02: security headers on every answer: `Content-Security-Policy` (`script-src 'self'`, no inline script),
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, HSTS only over HTTPS.
  File answers keep their own policy and `SAMEORIGIN` so the studio previews work.
- API2-03: request bodies are limited while they are read (2 MiB default, 16 MiB large requests, 256 MiB uploads,
  2 GiB backups; settable by environment). New code `request.too_large` with `limit_mb`.
- API2-06/DX-22: every error answer has the `{detail, code, params}` shape without echoing input; new codes
  `request.invalid` (`errors[]`), `request.malformed`, `route.not_found`, `method.not_allowed`, `http.<status>`.
  The OpenAPI document declares `4XX` as `ErrorResponse`.
- API2-07: unknown ids answer 404 and list limits must be at least 1. DX-21: a taken explicit publication slug
  answers 409 `publication.slug_taken`.
- API2-08: `POST /api/workflows/{id}/runs` honours `Idempotency-Key` (`idempotency.conflict`).
- API2-04: `GET /api/runs` reads one keyset page of summary fields (`DocumentStore.list_page`), no 2,000-run window.
- API2-05: published-API code examples quote values safely (Python `repr`, curl `shlex.quote`).
- API2-10: CORS only with `NODESTRO_DEV_CORS=1`, with a strict origin check.
- API2-11: backup restore copies only known tables and columns, with quoted identifiers.
- DX-31: `nodestro-server check` checks the OIDC issuer (`identity.provider`) and warns about a superuser or
  `BYPASSRLS` database role (`storage.role_unsafe`).
  `check --skip-identity-provider` leaves the provider out, for a worker's periodic health check, which must not
  poll the identity provider or fail while it is down.
- CI installs the public sibling repositories anonymously (OPS2-05).
- Pins nodestro-runtime 140d479 (runtime #96: backend audit round 2; orkexa bc8c4ed; Axentra a872242) and
  nodestro-examples 8499edf (examples #56: anonymous CI installs, same runtime pin).

### Platform pins: security review round 4 (2026-11-02)

- Pins nodestro-runtime bcfea7f (runtime #95: no tool-job install starts after a stop, R4-7; static tenant SQL check
  R4-3 to R4-6; #94: orkexa 0861c1b; Axentra 2767a2e) and nodestro-examples 9dd0c1a (examples #54).
- `docs/TODO.md` API-041 now also records the `suspension_check` window and approvals committed before a stop.

### Server worker keeps the Docker sandbox's proof warm (2026-11-02)

- A server worker (`nodestro-server worker`) now reads the process's Docker monitor from its start and then every
  `WorkerSettings.docker_every` seconds (default 120, at most half of the runtime's `PROOF_TTL` of 600 s), so the
  monitor's background check starts when the worker starts and its proof stays fresh while the worker runs. A job
  after an idle spell is no longer refused with `docker-checking` only because the proof expired.
- No extra load and no change to the security model: the watch only reads the existing monitor's status (one monitor
  per process; it checks in its own thread, at most one check at a time, `docker info` at most every 5 s, the
  self-test again only for a changed daemon or after a failure). Tool code still runs only after the preflight and
  the self-test passed; an unavailable daemon or a refusing self-test still refuses every tool run.
- The worker logs each change of the Docker sandbox's state once (check running, proven, unavailable with its reason
  code), with fixed texts and no host details (no socket paths or `DOCKER_HOST` addresses).
- Only workspaces whose tool code can run in Docker are watched (on a server: every tenant; on a desktop the `docker`
  and `auto` modes); the watch ends with `stop`.

### Stopping a suspended tenant's tool generation jobs (security review round 3, NF-1, 2026-11-02)

- `server_app.stop_tenant_work` also stops the tenant's tool generation jobs (`POST /api/tools/jobs`), before its
  Python environment installations: the job is `failed` at once with `message_code` `identity.tenant_suspended`
  (no new status), sends no further model request, has a running sandbox test killed, stops waiting for a decision
  (a late decision gets `400 job.not_awaiting`) and never registers its tool. Other tenants' jobs are untouched.
- Pins nodestro-runtime 96f14ef (runtime #93: `ToolLibrary.cancel_jobs`, static tenant SQL check round-3 gaps #89,
  Axentra f3ed8d2) and nodestro-examples cacf6b5 (examples #53). The API's own tenant SQL test passes the stricter
  check. Request-bound work that is not cancelled is recorded as accepted risk API-041 in `docs/TODO.md`.

### Platform pins: Docker-first follow-up (2026-11-02)

- Pins: nodestro-runtime `958be1d` (#90 R-1 source builds only on the desktop, R-5 container shape checks; #91 MCP
  container deadline 15 minutes; #92 R-4 tool adoption) and nodestro-examples `8c338d3` (#52).

### Adopting AI-generated tool code as one's own, desktop only (R-4, 2026-11-02)

- `POST /api/tools/{name}/adopt` (`admin`; body `content_hash` from `GET /api/tools/{name}`) adopts an AI-generated
  tool as the caller's own code (ADR-0006 amendment): the desktop's local owner is recorded with the role `owner`,
  a tenant member with their role (only `admin` passes). Refusals: `403 tool.adoption_desktop_only` (server,
  Nodestro Local Docker), `403 tool.adoption_forbidden`, `409 tool.adoption_stale`, `409 tool.adoption_not_needed`,
  `400 tool.adoption_not_applicable`, `404` for an unknown tool. Adoptions, refusals and revocations appear in
  `GET /api/tool-approvals/audit`.
- Contract (`openapi/nodestro-api.json`), additions only: the route, `ToolAdoptRequest`, `ToolAdoption`, and the
  optional `ToolInfo.content_hash` and `ToolInfo.adoption`.
- Needs a runtime pin with R-4 (developed against the local runtime worktree; the pin is bumped when the runtime PR
  is merged).

### Python 3.11 minimum (2026-11-02)

- Pin nodestro-runtime 51262a2 (runtime #88: Python 3.11 minimum, orkexa dec66eb) and nodestro-examples 3f91300
  (examples #51).
- Python 3.11 is now the minimum (3.10 reached end of life in October 2026): `requires-python`, ruff `py311`, mypy 3.11,
  CI matrix 3.11/3.13.
- The exported project's vendored `orkexa` now declares `requires-python = ">=3.11"` (orkexa's new minimum).
- The MCP tests no longer skip without the SDK (it is always installed); `mcp.unavailable` stays.
- `ruff --select UP` for 3.11: `datetime.UTC`, `TimeoutError`, `Permission` is a `StrEnum` (every use reads
  `.value`; `str()` now gives the value too).

### Platform pins: Axentra migration completion

- Runtime pin `50edcf7` (nodestro-runtime #86: security review fixes, performance fixes, M12 cleanup, Axentra
  `a8ea5d5`); examples pin `c3ef412` (examples #49). The runtime's `AxentraServices.start` now requires the
  process's database pool.

### Security re-review fixes (SR-01a, SR-01b, SR-03a, SR-04a, SR-06a)

Runtime pin `0369132` (nodestro-runtime #87: the same fixes with `RunChat.cancel_answers`,
`PythonEnvironments.cancel_installs` and `AxentraServices.key_samples`; Axentra `024da57`). Examples pin `b7a1d65` (examples #50).

- `tests/test_tenant_filters.py` runs the runtime's stricter tenant check (names resolved where they are read, texts
  that start with an unknown part reported) over the API package; it still reports nothing.
- A suspended tenant's live run chat answers and Python environment installations stop too
  (`server_app.stop_tenant_work`, `identity.tenant_suspended`): the question fails at once and a late answer is not
  stored; an installation stops before its next step.
- The start's key-bytes check also decrypts samples of Axentra's own keyring columns, so other bytes under a key id
  that only those columns use are refused (`profile.invalid`).
- `/api/notifications` accepts only cursors whose timestamp and message id PostgreSQL reads the way Python does: a NUL
  byte, an ISO week date, an offset with a fraction, a comma fraction, non-ASCII digits or a lone surrogate is a 400
  `notifications.invalid_cursor` (they were 500s), and the input is never echoed. The timestamp must have the exact
  form Axentra writes and accepts since AX-R1 (`YYYY-MM-DDTHH:MM:SS[.ffffff]` plus `Z` or `±HH:MM`); Axentra's own
  `InvalidCursor` maps to the same 400.

### Security review fixes (SR-01, SR-03 to SR-06, SR-09, SR-10)

- `tests/test_tenant_filters.py` runs the runtime's static tenant check over the API package.
- A suspended tenant's batches, benchmarks, assistant answers and architect drafts stop
  (`server_app.stop_tenant_work`, checked every 5 s), with `stop_code` `identity.tenant_suspended`.
- `/api/notifications` refuses a malformed cursor with 400 `notifications.invalid_cursor`.
- `secrets reencrypt` without `--tenant` also re-encrypts Axentra's columns (`axentra` in the report); the start
  checks the keys of stored values.
- `nodestro-server files cleanup [--dry-run] [--tenant]`; `files` exits with 2 when a report is not `ok`.
- `set-role`, `remove-member`, `revoke-sessions`: an e-mail address needs the operator to type the user id on a
  terminal (`identity.user_confirm` otherwise); an ambiguous address lists every identity.
- `backup`: an empty storage archive before the first upload, the gzip header names the archive, and a clear error
  when `pg_dump` runs without the owner role.
### Pins: Docker sandbox security review fixes (2026-11-02)

- Runtime pin `bf9af7d` (nodestro-runtime #85: review fixes DS-1..DS-5, DS-7, DS-9, DS-11; Orkexa `3364fb8`, DS-10),
  examples pin `9669439` (nodestro-examples #48, the same runtime). The settings accept the new sandbox mode `linux`
  (the built-in Linux sandbox, only when chosen); in `auto` AI-created code runs only in Docker. A reinstall without
  code takes its trust from the stored tool, so an unsaved or AI-created tool's libraries are never installed on the
  host.
- Fix: `api`, `worker` and `check` of a server refused to start on a fresh Docker daemon (no sandbox image yet): since
  Orkexa #31 the start-up check ran the full probe (image and self-test). It now asks Orkexa's preflight only (daemon
  reachable, Linux containers, enforced limits, seccomp, cgroup v2 when rootless; `sandbox.unavailable` with the
  reason `docker-<reason>` otherwise); the image and the in-container self-test stay with the runtime's Docker
  monitor, which refuses every container run until they passed.

### Run budget decisions (BUDGET-1, 2026-11-02)

- New `api_budget.py`: `GET /api/runs/{run_id}/budget` (read; the run's budget, its open decision, earlier ones and
  `policy.can_decide`), `POST /api/runs/{run_id}/budget/decisions` (`raise` with `limit_usd`, or `end`; needs `run`
  and that the caller started the run or is an admin; 409 `budget.not_pending`, 422 `budget.limit_not_higher` /
  `budget.limit_invalid`, 403 `budget.over_policy` / `access.forbidden`; idempotent with `idempotency_key`) and
  `GET /api/budget-requests` (read; the tenant's open requests).
- Runs started and continued from the Studio record `started_by` (the principal). Run summaries carry `budget`.
- The worker sweeps budget decisions next to the tool-approval sweep (overdue requests expire, decided runs
  continue); the desktop sweeps both. Settings `max_run_budget_usd`, `clear_max_run_budget`,
  `budget_decision_hours` and the read-only `operator_max_run_budget_usd` (`NODESTRO_MAX_RUN_BUDGET_USD`).
- Published API runs, schedules, batches and test series never wait for a budget decision: they fail as before.
- New `tests/test_budget_api.py`; the OpenAPI contract is regenerated.
- Pins nodestro-runtime `64895e6` (runtime #84) and nodestro-examples `59fc96b` (examples #47).

### Docker sandbox status: admin-only checks, redaction, bounded waits (security review DS-6, DS-8, DS-12)

- `GET /api/sandbox/status?refresh=1` starts a check only for callers with `admin`; a reader's `refresh` is ignored
  (`refresh_ignored: true`) and starts no container or image build (DS-6). The status refresh and `POST
  /api/sandbox/self-test` have separate 10 s windows per Docker monitor, so neither starves the other.
- Both routes answer the new `SandboxStatus` (a superset of `SandboxBackendStatus`; additive OpenAPI change) with
  `check_started`, `throttled` and `retry_after` (seconds; when a forced check was asked for but none started),
  `refresh_ignored`, `wait_skipped` and `redacted`. A self-test that did not run now says so (`throttled: true`,
  `check_started: false`) instead of looking like a fresh result (DS-13).
- Callers without `admin` no longer see the Docker host's details in `GET /api/sandbox/status` and `GET
  /api/settings` (DS-8): `detail` is a fixed English text per `reason`, the checks keep `name` and `ok` with an empty
  `detail` (no CLI path, image ID, daemon error output with socket paths or `tcp://` addresses), `engine` is empty,
  and on a server `rootless` and `cgroup_version` are `null`. The settings' `sandbox_selection.detail` no longer
  quotes the Docker detail either. Administrators keep the full detail.
- `wait` and the self-test's `timeout` block a worker thread: at most 4 status requests and 2 self-tests wait at
  once per process; the others answer at once with the current status (`wait_skipped: true`) (DS-12).
- Tests: `tests/test_sandbox_status_api.py`.

### Pins (Axentra completion cascade, 2026-11-02)

- Runtime pin `95114ae` (nodestro-runtime #82: Axentra `f6ae01d` across the version chain), examples pin `9cdc1f5`
  (nodestro-examples #45).

### Performance benches (Axentra migration, requirement 15)

- `tests/bench_identity.py`, `bench_api.py`, `bench_pool_load.py`, `bench_mirrors.py`, `bench_migrations.py`,
  `bench_workers.py` (shared set-up in `tests/perf_support.py`): before/after measurements on a throwaway PostgreSQL
  schema (session resolution, API latency, pool and connections under API plus worker load, mirror write cost and
  run usage settlement, migration commands with a SIGKILL and resume, worker throughput and a killed worker, process
  memory). Run with `python tests/bench_x.py`; not collected by pytest. Results: nodestro-runtime `docs/AXENTRA.md`,
  "Performance".

### Identity completion (Axentra migration M5; off by default)

- `NODESTRO_IDENTITY_BACKEND=axentra`: Axentra runs the OIDC login (`AxentraLoginFlow`, Axentra's `OidcService` behind
  the same routes, cookies and error codes; new codes `identity.account_disabled` 403 and `identity.rate_limited` 429).
  `nodestro`/`mirror` keep Nodestro's own client. Permissions of `axentra` mode come from Axentra's role definitions.
- Suspended tenants are closed: `403 identity.tenant_suspended` for sessions, logins, switches and setup links;
  `/api/auth/me` lists active tenants only; workers neither start nor continue their runs and fire none of their
  schedules. New commands `suspend-tenant` and `resume-tenant`.
- Session rotation on login: the session the browser held before ends.
- Tests: `test_identity.py` runs every server case in `nodestro`, `mirror` and `axentra` mode, plus a full
  `nodestro -> mirror -> axentra -> mirror -> nodestro` walk with real OIDC callbacks.

### Database roles and one connection pool (Axentra migration, work package dbinfra)

- `NODESTRO_DATABASE_MIGRATION_URL[_FILE]` (optional): `migrate`, `backup` and the pre-migration `pg_dump` run as
  that owner role; `api`, `worker`, `check` and the operator commands as the runtime role of
  `NODESTRO_DATABASE_URL`. `migrate` refuses a runtime role that bypasses row-level security or owns tables before
  changing anything, and grants it its privileges afterwards. New commands `db grant-runtime-role [ROLE]` and
  `db roles` (exit code 3 when RLS does not fully apply to the runtime role). `check`, `api` and `worker` refuse such a
  role (`storage.role_unsafe`) with `NODESTRO_AXENTRA=on` (unless `NODESTRO_AXENTRA_ALLOW_SUPERUSER=1`) or a separate
  migration role. Without the new variable nothing changes.
- Axentra borrows the process's Nodestro pool (one connection budget, `NODESTRO_DATABASE_POOL_SIZE`); its schema gate
  reads through it, so serving processes need no owner credentials.
- With `NODESTRO_DATABASE_SCHEMA`, `backup` and the pre-migration dump contain that schema only.
- Tests: `tests/test_server_roles.py`.

### Docker sandbox status and self-test routes (Docker-first plan A1, 2026-11-02)

- New `GET /api/sandbox/status` (permission `read`): the Docker sandbox as the runtime's background check last found
  it (`SandboxBackendStatus`, the `docker` row of the settings' `sandbox_backends`, with `state`, `reason`, `checks`,
  `engine`, `rootless`, `cgroup_version`, `tested_at`). It never waits for the daemon; `refresh=1` starts a new check
  (the self-test included) in the background and answers at once; `wait` (seconds, at most 5) waits that long at most.
- New `POST /api/sandbox/self-test` (permission `admin`): runs the checks again now, the in-container self-test
  included, and waits at most `timeout` seconds (default 30, at most 60); a longer check (the first image build) goes
  on in the background and the answer says `checking`. A forced check starts at most every 10 s per Docker monitor
  (one serves every tenant of a server); within that the current status is returned.
- `GET /api/settings` reads the Docker rows from that check and never runs `docker info` (the first settings read on
  Windows used to wait up to 10 s for it).
- Diagnostics: the `docker` check comes from the same status (no blocking `docker info`) with `params.state`,
  `params.reason` and `params.checks`; new codes `checking` and `unavailable` besides `available`, `not_installed`
  and `unreachable`.
- Export (`TOOL_SANDBOX=docker`): untrusted tools run only after the daemon passed Orkexa's preflight and the
  in-container self-test, checked in the background at the start (with the runtime of plan R2 they were refused for
  good, because nothing proved the daemon); the containers carry the installation's own owner label and only expired
  ones are reaped. The README says what the container sandbox needs and enforces.
- Contract (`openapi/nodestro-api.json`), all additions: the two routes, `SandboxCheck`, the new optional fields of
  `SandboxBackendStatus` and its state `checking`.
- Tests: `tests/test_sandbox_status_api.py`; workspaces in tests get a static "Docker missing" monitor unless marked
  `real_docker_status`. Runtime pin `b608f90` (nodestro-runtime #83: Docker-first plan R1-R4, Orkexa `e220f70`); examples pin
  `50552a0` (nodestro-examples #46, the same runtime).

### Axentra platform services in `nodestro-server` (2026-11-02; everything off by default)

- Observability and rollback safety: `GET /api/metrics` (Prometheus text, only with `NODESTRO_METRICS_TOKEN`, bearer
  token, no tenant labels); `axentra status` shows switches, both schemas, keyring key ids, stored secrets by format
  and readability, identity readiness and the reconcile backlog; `check`, `api` and `worker` refuse a configuration
  that cannot read the stored secrets (`profile.invalid`); `secrets reencrypt` with `NODESTRO_SECRETS_FORMAT=v1`
  moves `axs1` values back to v1. With `NODESTRO_AXENTRA_NOTIFY=1`: `GET /api/notifications` and
  `POST /api/notifications/read` (the member's own inbox) and the worker's inbox sweep; with
  `NODESTRO_AXENTRA_RUN_USAGE=1` the worker settles finished runs' usage into Axentra (both under the scheduler lock).

- `migrate` applies Axentra's migrations after Nodestro's with one shared backup; `check`, `api` and `worker` refuse
  a schema that is behind. New commands: `secrets reencrypt [--dry-run] [--tenant]`, `axentra status`,
  `axentra reconcile [--dry-run] [--tenant]` (audit/usage mirrors), `axentra backfill-identity [--dry-run]` and
  `axentra reconcile-identity`. The API and worker attach the audit/usage mirrors (`NODESTRO_AXENTRA_AUDIT`,
  `_USAGE`) and use the identity backend `NODESTRO_IDENTITY_BACKEND` (`nodestro` default, `mirror`, `axentra`;
  `axentra` refuses to start before the backfill). Routes, cookies, error codes and the OpenAPI document are unchanged.
- Input files in Axentra's storage (M8, `NODESTRO_FILES_BACKEND=axentra`, default `volume`): API and worker open
  the tenants' input files through it; new commands `files migrate|verify|remove-source|export-volume [--dry-run]
  [--tenant]` (exit code 2 when a file failed); `backup` also archives Axentra's local storage directory; `check`
  refuses a relative `storage.local_path`; `GET /api/inputs/{id}/content` serves a file kept inert as
  `application/octet-stream`. Tests: `tests/test_server_files.py` (commands, restore drill, API/worker contexts).
- Runtime pin `95dbdfa` (nodestro-runtime #79), examples pin `0ae5ad4` (nodestro-examples #44). The `server` and `dev` extras install `nodestro-runtime[postgres]`,
  which brings Axentra on Python 3.11+ only.
- Tests: `tests/test_server_axentra.py`; `tests/test_identity.py` runs every server case with identity on Axentra too.

### Desktop secrets through a pipe, not the environment (review MS-1, 2026-11-02)

- New option `--secrets-fd FD` of `nodestro` and `nodestro-api` (module `nodestro_api.handoff`): the launch secret
  and the MCP token vault key arrive as one JSON object `{"launch_secret": ..., "vault_key": ... | null}` on an
  inherited descriptor (the desktop app uses 0, standard input). It is read before anything else (at most 64 KiB),
  made non-inheritable and closed (descriptor 0 becomes the null device); the values stay in memory and never enter
  `os.environ`, so neither `KERN_PROCARGS2` (macOS) nor `/proc/<pid>/environ` (Linux) of the backend or of any child
  shows them. Malformed input, an unknown key or an unusable descriptor stops the start with `access.handoff_invalid`;
  `--reload` is refused after a handoff.
- `AppConfig.vault_key` (not in `repr`): the workspace seals MCP tokens with it instead of reading
  `NODESTRO_VAULT_KEY` from the environment. The operator's `NODESTRO_VAULT_KEY` still works and is now taken out of
  `os.environ` on the first read (as `NODESTRO_LAUNCH_SECRET` already was), so no child process inherits it; a
  malformed key stops `serve` with one sentence. After a handoff both variables are removed and ignored.

### Automatic sandbox selection (2026-11-02, ADR-0009)

- Pins: nodestro-runtime 7fd8761 (runtime #78, `auto` sandbox mode) and nodestro-examples f1c6403. `GET/PUT /api/settings` accept and return
  `sandbox: "auto"` and return `sandbox_selection` (the runtime's OS and CPU, where untrusted and user-authored code
  runs, or why it is blocked) and `sandbox_backends` (every backend with its state); OpenAPI regenerated.
- Release tool libraries use the studio's `auto` container fallback, so an untrusted tool of a published release
  runs in Docker on a desktop without the built-in sandbox, as in the studio, and is refused otherwise.

### Live run chat and steering (LRC-1, 2026-11-01)

- New routes (`api_run_chat.py`) for talking to an orchestrator of a running run: `GET /api/runs/{id}/chat`
  (`read`: conversation, the orchestrators of the run and its nested teams with what they do now, limits),
  `POST /api/runs/{id}/chat/messages` (`run`: `kind` `question` or `instruction`, `target_run_id` + `node_id`,
  idempotent with `client_id`: 201 new, 200 existing), `GET /api/runs/{id}/chat/messages/{message_id}` and the event
  stream `GET /api/runs/{id}/chat/events` (`snapshot`, `message`, `targets`, `end`; re-checks access, ends with the
  run). Refusals have stable codes: 404 `run_chat.unknown_target`, 409 `run_chat.run_not_active`,
  `run_chat.orchestrator_finished`, `run_chat.steering_unattended`, `run_chat.steering_off`, ..., 429
  `run_chat.busy`, `run_chat.rate_limited`, `run_chat.budget_exhausted`, `run_chat.too_many_*`. Questions are
  answered in a thread of the API process (at most 4 at once) with the assistant model, recorded in the usage log
  as `run_chat`; instructions reach the orchestrator through the store, also in a worker process.
- The full trace export of a top-level run carries its chat (`chat`; the sanitized export masks or removes it),
  deleting a run deletes its chat, run retention too, and backups validate `run_messages`.
- Pins `nodestro-runtime` `a42b672` (runtime #67 and its fix #68; before `ac96c6f`): `nodestro_runtime.run_chat`, and
  `nodestro-examples` `ebe899a` (before `dcad0d1`, the same runtime pin).

### Amazon Bedrock (2026-11-01)

- Pins `nodestro-runtime` `ac96c6f` (before `d141b8f`; the runtime's Bedrock provider and its review fixes, Orkexa
  `3467940` with boto3) and `nodestro-examples` `dcad0d1` (the same runtime pin, branch only).
- Contract (`openapi/nodestro-api.json`), all additions:
  - `POST /api/settings/providers/bedrock/verify` (permission `admin`, like changing the settings; optional body
    `ProviderVerifyIn {model}`, `bedrock:` in front is accepted, model ids keep every colon): checks the Bedrock
    settings with free AWS calls only (STS `GetCallerIdentity` and one control-plane read; no model is invoked) and
    answers the provider's `ProviderStatus` with `verification {state, checked_at, code}`. A failed check is a 200 with
    `state: "failed"` and a stable `provider.aws_*` code; nothing about the AWS identity is returned. A second check
    within 10 seconds answers 429 `provider.verify_rate_limited` with `Retry-After`; a model of another provider 400
    `provider.model_invalid`.
  - From the runtime: `ProviderInfo.auth` (`api_key`/`endpoint`/`aws`) and `region_env_vars`; `ProviderSettingsIn`
    `region`, `auth`, `profile_name`, `data_residency`; `ProviderStatus` `auth`, `region`, `profile_name`,
    `data_residency(_forced)`, `auth_methods`, `allowed_regions`, `operator_role`, `verification`
    (`ProviderVerification`); `ModelInfo.price_region(s)`; `ReconciliationReport.not_reconciled`
    (`UnreconciledProvider`). `/api/meta` lists `bedrock` and EU inference-profile suggestions.
  - `OverrideIn.aws_regions`: the AWS regions an individual Bedrock price applies to (the first is the default);
    required for `bedrock` (`pricing.invalid_price` with `pricing.region_required`), refused for other providers.
    The override routes already take model ids with colons and slashes (`{model:path}`, ARNs).
- `GET /api/models/capabilities?model=` (permission `read`, `ModelCapabilities {model, capabilities}`): Orkexa's
  capability values of a typed Bedrock model id (the same values the catalog entries of `/api/meta` now carry in
  `ModelInfo.capabilities`); `null` for other providers and invalid names. A new route because no existing response
  fits one typed id (`/api/meta` lists only the catalog, `/agents/validate` answers a list, `/workflows/check` a
  schema the Workflow Architect shares). From the runtime also `PriceEntry.regions`.
- The connection diagnosis of Bedrock stores the verification result as a side effect (documented on both routes).
- Errors of the studio API now carry the headers of an `ApiError` (here `Retry-After`).
- Server: `nodestro-server` reads `NODESTRO_BEDROCK_TENANT_ROLES[_FILE]`, `NODESTRO_BEDROCK_DATA_RESIDENCY` and
  `NODESTRO_BEDROCK_ALLOWED_REGIONS` (`profile.invalid` names the variable, never a role ARN or an external id, so
  `nodestro-server check` validates them); API and worker processes hand each tenant's workspace only its own role
  (`ServerEnvironment.bedrock` → `TenantComposition.bedrock`/`TenantFiles.bedrock` → `tenant_storage(bedrock=)`).
- Export: `workflow/manifest.json` gains `settings` (Bedrock: region and data residency only); `.env.example` and
  the README name `AWS_REGION`/`AWS_DEFAULT_REGION` and the default credential chain instead of a key or base URL;
  the export check has `export.region_missing` (error), `export.aws_profile_not_exported` and
  `export.aws_role_not_exported` (warnings). The vendored orkexa `pyproject.toml` takes its extras from orkexa's
  metadata (`bedrock` with boto3, `all` with boto3), matching `requirements.lock`. The exported API reads the region
  from the environment or the manifest (`provider.region_missing` → 503).
- A published API whose Bedrock settings are missing, have no operator role or a refused region answers 503
  `model.unavailable`; the connection diagnosis of Bedrock runs the verification (codes `ok`, `key_rejected`,
  `unreachable`, `not_configured`, `failed`, with the `provider.*` code in `params.reason`).
- The AWS SDK's loggers (`botocore`, `boto3`, `s3transfer`) start at WARNING: at DEBUG botocore logs signed request
  headers and temporary session tokens, which must not reach logs through a process-wide DEBUG setting.

### Run deletion removes orchestrator team runs (2026-11-01)

- `DELETE /api/runs/{id}` also deletes the team runs an orchestrator node started (`child_run_ids`), at any depth,
  with their files and tool waits. Before, only sub-workflow runs (`child_run_id`) went with the deleted run, and team
  runs holding copies of its inputs and results stayed behind (backend audit, reported by the Live Run Chat thread).

### Pins: PostgreSQL read performance (2026-11-01)

- Pins `nodestro-runtime` `d141b8f` (before `6eacec1`) and `nodestro-examples` `660e9a9` (before `1ead26c`). The
  runtime's PostgreSQL store checks pooled connections only after they sat idle, retries reads on a lost connection,
  writes single statements in autocommit, and migration 0006 makes the `status`/`trigger_ref` indexes partial. A
  server applies migration 0006 at its next start (`nodestro-server migrate`, backup first as for every migration).

### Example guide terminology (2026-11-01)

- Pin of `nodestro-examples` at `1ead26c` (before: `e370567`): the localized example guides use the studio's
  terms (de "Arbeitsbereich", it "spazio di lavoro", pt-BR "fluxo de trabalho", fr "vous"). Texts only; same runtime.

### Backend audit (2026-11-01)

- Security and operations (server profile):
  - A-01: logins are bound to the browser that started them. A `__Host-nodestro-login` cookie must match the OIDC
    `state` at `/api/auth/callback`; otherwise the callback answers 400 `identity.login_expired`.
  - D-02: the access log no longer contains setup-link tokens or OIDC/MCP sign-in codes and states.
  - D-10: `/api/health` checks the database and answers 503 `{"status": "unavailable", "database": "unreachable"}`
    when it is unreachable.
  - A-09: `/api/docs` and `/api/openapi.json` need a login on a server.
  - A-04: open run, Workflow Architect and assistant event streams end within about 15 seconds once the viewer's
    session or membership ends (`event: end` with `{"reason": "access_ended"}`).
  - A-06: approval decisions and input uploads record the deciding or uploading user instead of `local`.
  - A-02: workers delete expired sessions and abandoned login starts, finished jobs after 7 days and run events after
    2 days (`NODESTRO_WORKER_CLEANUP_SECONDS`, default 600).
  - A-09: published APIs prune idle rate-limit counters and Idempotency-Key records older than 24 hours.
  - A-08, D-03: new operator commands manage tenant membership: `nodestro-server members`, `set-role`,
    `remove-member` and `revoke-sessions`.
- Reliability:
  - C-04, C-15: the run list filtered by workflow or trigger, the usage report, batches, test series and the cost
    estimate see every stored item, not only the newest 2000 or 500.
  - C-07: on a server, opening a tenant no longer marks batches and test series running in another API process as
    interrupted.
  - C-09: the scheduler no longer brings back deleted schedules, switches disabled ones on again or starts them.
  - C-10: a damaged workflow document is skipped and logged instead of breaking the workflow list and search.
  - C-13: backup inspection checks every document for valid JSON and validates the restored collections against
    their models.
  - C-16: deleting a workflow is refused (409 `workflow.runs_active`, params `runs`) while its runs are queued or
    running, and runs as one transaction. Saving or restoring a version of a workflow deleted meanwhile answers 404
    instead of recreating it.
- AX-21: on a server, `create_tenant_app` serves the built studio gzip-compressed, with
  `public, max-age=31536000, immutable` for content-hashed `/assets` and `no-cache` for `index.html` and the
  single-page fallback. API responses and event streams are unchanged. `mount_frontend` moved to
  `nodestro_api.frontend` and is still importable from `nodestro_api.server_app`.
- UX follow-ups:
  - AD-07: `PUT /api/workflows/{id}` accepts `If-Match: <updated_at>` and refuses a save based on an older revision
    with 409 `workflow.conflict` (`params.updated_at`, `params.name`); without the header, saves work as before. The
    workflow and its version are written in one transaction under `store.lock`.
  - AD-08: imports without a `nodes` list are refused (`workflow.import_invalid`); creating, saving or importing a
    workflow with a dangling connection or a duplicate node id is refused with the codes `/workflows/check` uses.
    Workflows created without a name are named in the interface language.
  - AD-05: `/api/meta` reports `profile` (`desktop`, `local-container`, `server`). Server tenants are opened with the
    server profile, so they no longer fall back to the operator's billing keys from the environment.
  - AD-15: validation errors of published APIs name each field once (`{field, code, message}`, `number_parsing` for a
    value that is not a number).
  - AX-25: `GET /api/runs` pages with `before` and the `X-Has-More` / `X-Next-Before` response headers.
  - Run and run summaries carry `warnings` from the runtime.
  - `/api/health` on a server uses `PostgresDatabase.ping`; `architect.py` uses the runtime's public names (D-14).
- CI: the read token is scoped per step, dependencies are audited, and PostgreSQL 17 is tested.
- Pins: `nodestro-runtime` at `6eacec1` (before: `58cec5c`) with the runtime audit fixes, and `nodestro-examples`
  at `e370567` (before: `5e2d532`), which pins the same runtime.

### Example library format 2: teams, knowledge files, integrations, model tiers (2026-11-01)

- `POST /api/library/{id}/copy` builds the copy with `nodestro_examples.instantiate`: parts used as sub-workflows or
  orchestrator teams are saved first and every reference is pinned to their first version; the template's knowledge
  files are stored in the knowledge base (collection "Examples: <name>" in the copy's language) and given to the
  agents (a later copy reuses an identical file, recognised by the metadata `library_sha256`); agents get the model
  tiers mapped to the workspace's default and assistant models (`model_choice`).
- New field `integrations` (integration id -> MCP server id): only tools the server offers as approved (current hash)
  and classified `read` are bound to the agents (`mcp__<slug>__<tool>`); otherwise 409
  `library.integration_unavailable` (params `integration`, `server`, `missing`, `not_read_only`). An unknown
  integration is 422 `library.integration_unknown`, an unknown server 404. Integrations not named run in their
  labelled offline mode.
- `tests` together with `integrations` is refused (422 `library.tests_need_offline`): a test series runs without a
  person, and MCP tools are refused in such runs.
- Pin of `nodestro-examples` at `5e2d532` (before: `3429f5b`): template format 2 and the modernized examples.
- Everything is checked before anything is stored; a copy that fails half-way removes the workflows, knowledge files
  and input files it created.
- The OpenAPI document follows the library's new response models (teams, knowledge, integrations, budget).

### Security fixes from the independent MCP re-review (R-1) (2026-11-01)

- Pin of `nodestro-runtime` at `58cec5c` (before: `f71086a`; re-review fixes R-1 and R-2) and `nodestro-examples` at
  `3429f5b` (before: `69e5e65`, same examples on the new runtime).
- `POST /api/runs/{run_id}/tool-approvals/{request_id}` checks both classifications of an MCP tool call: the one it
  was asked with and the tool's classification now. If either is `destructive`, deciding needs `admin` (403
  `access.forbidden`, `permission: admin`), so a tool reclassified as destructive while its call waited is never
  decided with `run`. The current classification is passed to the runtime: approving a call whose tool was
  classified differently since it was asked is refused (409 `tool.approval_stale`) and closes the request; the
  runtime also closes such requests itself when the tool is reclassified, and refuses stale approvals before the call.
- New tests `tests/test_rereview_reclassify_destructive.py`: the independent reviewer's regression test plus both
  directions, rejections, administrator rights, parallel decisions and a reclassification racing an approval.

### Security fixes from the independent MCP review (H-1, M-3, L-3) (2026-11-01)

- Pin of `nodestro-runtime` at `f71086a` (before: `c86781b`; independent MCP review fixes, offline worker fix F5) and
  `nodestro-examples` at `69e5e65` (before: `cd8b0af`, same examples on the new runtime).
- `POST /api/agents/test` keeps `run` and additionally needs `edit` unless the tested configuration is exactly a
  saved one (the agent of a node of `workflow_id`, or an agent template): a run-only user (`operator`) can no longer
  give an agent arbitrary tools or prompts through a test (403 `access.forbidden`, `permission: edit`). Every tool call
  of a test follows the same per-call rules as a workflow run (runtime): a call the rules would ask about is refused
  with `tool.approval_not_in_test`, MCP tools that change data never run in a test.
- `POST /api/tools/{name}/run` (the tool editor's test panel) now needs `edit` and `run` (before: `run`); it never runs
  an MCP tool that changes data (`mcp.write_not_supported`).
- `POST /api/runs/{run_id}/tool-approvals/{request_id}`: deciding about a call of a destructive MCP tool needs `admin`
  (403 `access.forbidden`, `permission: admin`); every other call keeps `run`. The check reads the stored request.
- `POST /api/mcp/servers/{id}/tools/{name}/decision`: approving needs `expected_hash` (400 `mcp.hash_required`).
- The MCP tool list reports a write tool as usable when a gated workflow run could use it (`check(..., gated=True)`).

### Runs paused at a tool approval (AO M6b, behind a switch), pin: nodestro-runtime c86781b (2026-11-01)

- Pin of `nodestro-runtime` at `c86781b` (before: `6ac4ae3`) and `nodestro-examples` at `cd8b0af` (before:
  `1da29b7`, same examples on the new runtime). With `NODESTRO_TOOL_APPROVALS=1` and the new switch
  `NODESTRO_TOOL_APPROVAL_CHECKPOINTS=1` (default off), a tool call that waits for a person pauses the run and
  frees its worker; nothing changes while the switch is off.
- `POST /api/runs/{run_id}/tool-approvals/{request_id}` (decide) continues a run paused at that call once none of
  its calls waits any more (`RunManager.decide_tool_call`); the rules and the at-most-once check run again when the
  call resumes. When the route and the sweeps of several workers continue the same run at once, it is queued
  exactly once (runtime c86781b). Deleting a run closes its open questions and deletes its checkpoints.
- A background task (API process every 15 s, server worker once before serving and then periodically) runs the
  runtime's deadline sweep: overdue questions expire, decided runs continue, checkpoints of runs that are gone are
  dropped.
- OpenAPI: `ToolApprovalRequest` gains `checkpointed`, `use` and `executed_at`; audit kinds `suspended`, `resumed`,
  `executed`, `stale`, `result_lost` and `switch_off`.

### Security fix: stale tools in server workers (S-2 pilot finding F1), pin: nodestro-runtime 6ac4ae3 (2026-11-01)

- Pin of `nodestro-runtime` at `6ac4ae3` (before: `081705d`) and `nodestro-examples` at `1da29b7` (before:
  `b5aa4db`, same examples on the new runtime). The API process and every server worker now see tool changes and
  deletions made by another process at their next lookup; a running step refuses a tool whose version changed or
  was deleted since it started, also after an approval or advance approval for the old version. Publishing freezes
  the current tool version. No route or OpenAPI change.

### Dynamic teams and approvals inside teams (AO M6b), pin: nodestro-runtime 081705d (2026-11-01)

- Pin of `nodestro-runtime` at `081705d` (before: `1bc3924`) and `nodestro-examples` at `b5aa4db` (before:
  `6658599`, same examples on the new runtime). Orchestrators wait for approvals inside their teams instead of
  cancelling them; the existing approval routes decide them and the run continues where it waited. The pending
  approvals of a run include those of its orchestrators' team runs.
- OpenAPI: `OrchestratorConfig` gains `max_parallel`, `staffing`, `max_team` and `allow_values` (all off by
  default); `NodeRun.orchestration` carries protocol version 2 records (`outcomes`, `pending`). No route changes.

### MCP resources (WP-10), pin: nodestro-runtime 1bc3924 (2026-11-01)

- Pin of `nodestro-runtime` at `1bc3924` (before: `2d55242`) and `nodestro-examples` at `6658599` (before:
  `63be887`, same examples on the new runtime). A server's listed resources show up as one more tool,
  `resources/read`, in discovery and the tool list; it is approved, classified and test-called through the existing
  routes (the decision route takes the percent-encoded name). No route or schema changes; the OpenAPI document is
  unchanged.

### Tool approvals: warnings before unattended starts (S-2, R1, 2026-11-01)

- Schedules (`POST /api/schedules/preview`, new optional query `schedule_id`, and switching on), batches
  (`BatchPlan.tool_warnings`), test series (`BenchmarkPlan.tool_warnings`) and the publication check
  (`ApiCheck.problems`, level `warning`) name every tool call the per-call approval rules would refuse in those
  unattended runs: codes `tool.unattended_*` with the tool, agent, workflow and the reason. It uses the runtime's own
  policy (`tool_preflight.unattended_tool_issues`), grants nothing and blocks nothing; the runtime still refuses at
  every call. Empty while `NODESTRO_TOOL_APPROVALS` is off. Published APIs are always warned about because their own
  approval phase does not exist yet.
- Pins: `nodestro-runtime` `2d55242` (before `a13f894`), `nodestro-examples` `63be887` (before `2488c93`).

Nodestro as an MCP server for published workflows (ADR-0007 D-8, WP-11; off by default):

- `/api/published/_mcp` (Streamable HTTP, official MCP SDK, stateless): lists the publications an API key may run as
  tools and runs them through `ServingCore.launch` (same limits, budgets, approvals and run history as
  `POST /api/published/{slug}/runs`). Answers only clients on this computer.
- Needs both the operator's `NODESTRO_MCP_SERVER=1` and the administrator's per-publication offer
  (`Publication.mcp_tool`, `PATCH /api/publications/{id}`; history events `mcp_tool_on` / `mcp_tool_off`).
  Otherwise `404 mcp_server.disabled` or no tools.

`GET /api/mcp/stats` (`admin`, WP-7): MCP calls in flight, the operator's limits and each server's health (calls,
failures, circuit breaker, last error code; no secrets, no arguments).

Local MCP servers and MCP tools in published APIs (ADR-0007, WP-8 and WP-9):

- `GET /api/mcp/servers/{id}/environment` and `POST /api/mcp/servers/{id}/environment/plan` (admin): the packages of
  a local stdio server, planned hash-pinned for review and approved with `POST /api/python/plans/{id}/approve`; it
  runs only in the Docker sandbox's containers. `GET /api/mcp/status` reports `stdio_possible`.
- Publishing freezes the MCP tools a workflow uses in the release (`bundle.mcp_tools`: server id and slug, approved
  hash, classification; no address or token). The check reports `api.mcp_tools`, `api.mcp_unattended_off` (API runs
  need the operator's `NODESTRO_MCP_UNATTENDED`) and blocks unusable tools (`api.mcp_tool_unusable`). A release's
  runs use only the frozen tools and stop when an approval changed (`mcp.release_tool_changed`).
- Exports refuse releases with MCP tools (`export.mcp_tools`): the standalone project has no connection to the
  tenant's MCP servers.

### Tool approvals: one-time activation step (S-2 security acceptance, 2026-10-31)

- **Fix:** `POST /api/tool-approvals/legacy` could be repeated, and every repeat stamped bindings created since then
  (new tool assignments, workflows, versions and releases) as legacy exemptions. The step now runs once: the
  workspace part is guarded by the runtime (`stamped_at`), the releases part by `publications_stamped_at`, and it
  stamps only releases created up to the workspace's `stamped_at` (so an interrupted first call can be finished
  without exempting later releases). A repeat returns the stored report and is audited as repeated.
- `ToolApprovalsState.legacy` carries `stamped_at`, `stamped_by` and `publications_stamped_at` (OpenAPI regenerated).
- Pins `nodestro-runtime` `a13f894` (before `40e04b4`) and `nodestro-examples` `2488c93` (before `df458e8`).
- README: the activation order (stamp, review, then switch on) and the one-time digest change of published releases.

Model choice from measured test series results (Adaptive Orchestration M6a):

- `GET /api/benchmarks/{suite_id}/model-choice?node_id=…` (`read`) recommends a model for one agent from the
  series' finished test runs, by a policy given in the request: `objective` (`cost`, `duration` or `quality`),
  `min_pass_rate` (default: the configured model's measured pass rate, so no regression) and `no_new_failures`
  (default on). Only measurements count: runs of the same workflow apart from its models, the same cases and judge,
  the same models for every other agent, and a result for every case. An unknown cost is never treated as zero, so
  such a model cannot win on cost. Without a measurement the answer keeps the configured model. Nothing changes on
  its own; the answer names the policy, the test runs used and why other runs or models were left out.
- Test runs record `agent_models`, `basis` (the workflow without models, names and positions) and `suite_basis`
  (cases and judge) at the start. Test runs from before this change have neither and are never used.
- OpenAPI regenerated (`ModelChoice`, `ModelPolicy`, `Measurement`, new `BenchmarkRun` fields).

Teams, orchestrator and pool selection (Adaptive Orchestration M3–M5):

- Pins `nodestro-runtime` at `40e04b4` (output contracts, capabilities, orchestrator node, pool selection by
  capability, stdio MCP servers) and `nodestro-examples` at `df458e8`.
- `GET /api/workflows` lists each workflow's `capabilities` and `contract` (the field names of its output contract).
- Publishing pins and snapshots every workflow a node calls: sub-workflows and orchestrator teams, including teams
  selected by capability (the selection is fixed in the release). The published schema of an orchestrator's result
  is text; the assistant's workflow context names the teams.
- OpenAPI regenerated (`OrchestratorConfig`, `OrchestratorMember`, `OrchestratorSelect`, `NodeRun.orchestration`,
  `child_run_ids`, `decision_usage`, output contracts, capabilities, workflow limits).

MCP OAuth 2.1 sign-in (ADR-0007, WP-6; needs runtime `82f2669` or later):

- `POST /api/mcp/servers/{id}/oauth/start` (`admin`) returns the authorization URL of the server's authorization
  server (PKCE `S256`, `state`, `resource`); `GET /api/mcp/oauth/callback` (`admin`, the browser's return) exchanges
  the code and continues at `/tools?tab=services&oauth=connected` or `&oauth_error=<code>` (no code or state in that
  URL, `Referrer-Policy: no-referrer`); `POST /api/mcp/servers/{id}/oauth/sign-out` forgets the credentials.
- The callback address is the server's configured public origin, or the desktop's loopback address; it is never
  taken from a request's `Host` header (`mcp.oauth_redirect_invalid`).

Per-call tool approvals (Evolution S-2b, decision D10; off unless `NODESTRO_TOOL_APPROVALS=1`):

- New routes (`api_tool_approvals`): the approval inbox `GET /api/tool-approvals` and `GET
  /api/runs/{run_id}/tool-approvals` (`read`); `POST /api/runs/{run_id}/tool-approvals/{request_id}` (`run`) approves or
  rejects one call. The decision names the `arguments_hash` that was shown; other arguments or another tool definition
  answer 409 `tool.approval_stale`, a late or repeated decision 409 `tool.approval_expired` / `tool.approval_decided`.
  The decider is the request's principal.
- Pre-grants for one schedule, batch or test series: `GET /api/tool-pre-grants` (`read`), `POST` and `DELETE
  /api/tool-pre-grants/{scope}/{scope_id}/{grant_id}` (`admin`; refused for MCP and destructive tools, unknown scopes
  answer 404).
- `GET /api/tool-approvals/audit` (`admin`): decisions, refused decisions, pre-grants, revocations, activation.
- `GET /api/tool-approvals/legacy` (state and activation report) and `POST /api/tool-approvals/legacy` (`admin`): the
  activation step stamps existing bindings of workflows, workflow versions **and published releases** as visible
  legacy exemptions, so activating approvals never changes a published API silently. A stamped release gets a new
  digest and its publication records `legacy_stamped` with the old and new digest. Idempotent; it does not switch
  approvals on.
- Server workers: `NODESTRO_WORKER_MAX_RUNS` (default 2) and `NODESTRO_WORKER_MAX_WAITING` (default 1) set the runs a
  worker holds and how many of them may wait for an approval; invalid combinations stop the server at start
  (`profile.invalid`).
- Every route is tenant-scoped (ids of other tenants answer 404); tests cover the role matrix, stale, late and
  repeated decisions, pre-grant rules, release stamping and tenant isolation on PostgreSQL.
- Pin of `nodestro-runtime` at `0aee6a9` (before: `8597544`) and `nodestro-examples` at `9597421` (before: `cf151e6`):
  per-call tool approvals with audit and inbox (S-2), MCP write gate (WP-5) and OAuth (WP-6), budget reservations
  (Orkexa `b85133d`), run, time and step limits, and the customer-service approval loop. The OpenAPI contract is
  regenerated (new `Run`/`Usage`/`WorkflowLimits` fields included); the template test now treats an agent that a
  rejection loops back to as a normal step.

Nodestro Local Docker browser sessions (D1, D3 of the internal cross-check):

- Browser sessions exist only under the installation's random host name `nodestro-<10 letters and digits>.localhost`,
  which `nodestro-local serve` creates next to the access key (`browser-host`, mode 0600). Cookies are scoped by host
  name, not port, so a session cookie for `localhost` or `127.0.0.1` also reached every other local web server.
  `/api/launch` on any other name is refused with 403 `access.launch_host`, and a session cookie sent there is not
  accepted. The bearer key still works under every loopback name. `NODESTRO_BROWSER_HOST` can replace the name with
  another loopback name; `nodestro-local link` then warns. A local container without a browser host name is
  refused (`access.browser_host_required`).
- Sessions end after 8 hours without a request (`NODESTRO_SESSION_IDLE_HOURS`) and 24 hours after they were opened
  (`NODESTRO_SESSION_MAX_HOURS`); the cookie carries that `Max-Age`.
- Logout: `GET /api/logout` shows a sign-out form, `POST /api/logout` ends this browser's session and deletes the
  cookie, and `POST /api/logout?all=1` with the bearer key (`nodestro-local logout`) ends every session.
- Every answer carries `Referrer-Policy: no-referrer`, so the host name does not leave with a link.
- D3: only the published port (`NODESTRO_HOST_PORT`, default 8765) and the container's port 8080 are answered in
  `Host`; any other port gets 403 `request.port_not_allowed`.
- `nodestro-local rotate-key` also replaces the host name; `link` uses it by default. The desktop app is unchanged.

Connected MCP services (ADR-0007, WP-3):

- New routes under `/api/mcp` (`api_mcp`): status, servers (add, change, remove; tokens write-only), discovery
  (`POST /servers/{id}/discover`), tools with definitions, hashes and the reason a tool is unusable, decisions
  (approve with classification and `expected_hash`, reject) and audit events. Listing needs `read`, everything else
  `admin`; every route is tenant-scoped (other tenants get 404). Approved MCP tools appear in `GET /api/tools` with
  `kind: "mcp"`. Tests cover the role matrix, tenant isolation, token sealing per tenant and the server's endpoint list.

Sandbox gateway security fixes (runtime pin `8597544`):

- Pin of `nodestro-runtime` at `8597544` (before: `b6b757b`) and `nodestro-examples` at `cf151e6` (before:
  `8a5862e`). The sandbox gateway of Nodestro Local Docker parses request heads strictly (request smuggling fix,
  runtime PR #33), and it refuses to start sandboxes when the Docker daemon cannot enforce resource limits (runtime
  PR #37). The new runtime also brings the tool approval groundwork of runtime PRs #30 to #36, which is off by
  default.

Flow kernel, loop connections and MCP provenance (runtime pin `b6b757b`):

- Pin of `nodestro-runtime` at `b6b757b` (before: `78d2899`): the engine runs on the `orkexa.flow` kernel (Orkexa
  `456b92f`), workflows may contain loop connections with mandatory limits (Adaptive Orchestration M1-4), and MCP
  tools carry their provenance. Pin of `nodestro-examples` at `8a5862e` (before: `6c88aa7`; same runtime pin). The
  export ships the two new runtime modules `flow_driver` and `mcp_access`.
- OpenAPI (additive): `Edge.loop` (`EdgeLoop`: `max_iterations`, `on_exhausted`), `NodeRun.iteration`,
  `NodeRun.earlier`, `Run.flow_state`, the MCP fields of `ToolInfo` and `ToolCallRecord.provenance`.
- Published APIs stream the new `loop_iteration` event (`iteration`, `max_iterations`) and the `iteration` of
  `node_started`/`node_finished`; the loop's internal id stays inside. A run that reaches a loop's limit fails with
  `run.loop_exhausted`.
- The workflow assistant's cycle repair keeps loop connections; it only removes connections that close a cycle
  without `loop`.

Nodestro Local Docker (the `local-container` profile):

- `create_app` and `nodestro-api`/`nodestro` accept `NODESTRO_PROFILE=local-container`: the studio of one person on
  SQLite that runs in a container and is used from the host's browser. It may listen on the container's interfaces
  (`0.0.0.0`, published by Docker on the host's loopback only), but only with an access key
  (`NODESTRO_ACCESS_KEY_FILE`, `access.key_required` without it) and only loopback names in
  `NODESTRO_ALLOWED_HOSTS` (`access.network_needs_login`). The desktop profile still refuses every interface.
- The access key reuses the desktop's launch middleware (`LaunchSecretMiddleware(reusable=True, bearer=True)`): the
  link `/api/launch?token=<key>` opens any number of browser sessions, each with its own random `HttpOnly`,
  `SameSite=Strict` cookie `nodestro_local_session` (not the desktop's name: cookies are shared across the ports of
  `localhost`); scripts send `Authorization: Bearer <key>`. A browser without a session gets a short page that says
  how to open Nodestro; the health check and the key-protected published APIs stay open.
- Strict host and origin checks (`LocalAccessMiddleware(strict_origin=True)`): only loopback host names are answered
  (not the container's addresses or name), and changing requests and WebSockets only from the exact origin they were
  sent to, so a page of another program on `localhost` cannot use the studio. No CORS for development servers, and
  network access for published APIs cannot be switched on (`lan.unavailable`).
- One person holds every permission (`resolver_for`), and provider keys may come from the environment as on the
  desktop.
- New command `nodestro-local` (`nodestro_api.local_container`), run inside the container: `serve` (creates the
  access key with mode 0600 on first start), `link`, `rotate-key`, `backup` and `restore` (the studio's own complete
  backups, through the running API), `status`, `check` (health check) and `gateway` (the runtime's sandbox gateway).
- No change to the OpenAPI document.
- Pins `nodestro-runtime` at `78d2899` (before: `d2cb987`) and `nodestro-examples` at `6c88aa7` (before: `1c2f1e4`).

Test cases from failed runs (evolution plan Q-4):

- `POST /api/runs/{id}/test-case` (read) proposes a test case from the input of a failed run: form values, free text
  and files as the run had them. Credentials are always replaced (`nodestro_runtime.sanitize`), personal data too
  unless `personal_data` is `false`. Files are referenced, not copied; files that no longer exist are left out and
  listed in `missing_files`, and values that no longer fit the current form are listed in `problems`.
- Nothing is stored: the case is added only when it is saved into a test series with the existing
  `POST`/`PUT /api/benchmarks` routes. `TestCase.origin` (`CaseOrigin`: run, status, version, error code, what was
  replaced) records where it came from; it is `None` for cases written by hand, so existing series are unchanged.
- Imported runs, sub-workflow runs and runs that did not fail are refused (`testcase.run_unsupported`,
  `testcase.run_not_failed`).

Pins (2026-10-31): `nodestro-runtime` at `d2cb987` (before: `45e7774`; strict price-mode refusals get their own error
codes) and `nodestro-examples` at `1c2f1e4` (before: `786c453`; same runtime). No API changes.

Opt-in release gate per publication (evolution plan Q-3, decisions D6 and D6a):

- `Publication.gate` (`ReleaseGate`: `suite_ids`, `min_pass_rate`, `judge_min_score`), default `None`: without a gate
  publishing is unchanged. `PUT /api/publications/{id}/gate` switches it on, changes it or switches it off (admin);
  `POST /api/workflows/{id}/api/publish` accepts `gate` and `override`.
- `POST /api/workflows/{id}/api/gate` (read) reports the gate for a saved version, evaluated on the server from the
  stored test series, with the rules of the studio's test status: the latest finished run with the saved models must
  have tested this exact version, and the suite must not have changed since.
- Items come in three kinds. `critical`: the evidence is missing or does not belong to this version
  (`gate.suite_missing`, `gate.suite_untested`, `gate.suite_variant_only`, `gate.suite_outdated`,
  `gate.suite_changed`); it blocks the release (`gate.blocked`), and nothing in the UI or the API overrides it.
  `deterministic`: the share of cases whose run succeeded and whose checks all passed (`gate.pass_rate`). `judge`:
  the AI judge's mean score (`gate.judge_score`, `gate.judge_missing`), always marked `probabilistic` and never mixed
  into the deterministic numbers.
- A failed non-critical item needs a written reason (`override.reason`, 10 to 500 characters;
  `gate.override_required` without one). Publishing needs the `admin` permission, so on a server only the roles
  `owner` and `admin` can give an override. The reason, who gave it and the failed items are kept on the release
  (`Release.gate`) and in the history (`gate_override`). `ReleaseSummary.gate` is `passed`, `overridden` or `None`.
- Fix: saving an existing test series (`PUT /api/benchmarks/{id}`) failed because the updated cases were not
  validated again.

Assistant live events carry their payload unwrapped (fix, found by the studio's assistant e2e suite):

- `Assistant._emit` took the payload as `**data`, and every caller passed `data=...`, so each live event of an answer
  arrived as `{"data": {"data": {...}}}`. The interface reads `event.data.target`, `event.data.kind` and
  `event.data.message`: a streamed handover card had no target (the panel crashed on its missing icon), steps had no
  kind and `finished` never replaced the message. It went unnoticed while answers finished before the stream was
  opened. `_emit` now takes `data` as a mapping; the handover test checks the payload of every event kind.

Comparing models in a test series (evolution plan Q-2, 2026-10-31):

- `POST /api/benchmarks/{id}/runs` and `/plan` accept `models` (agent node id -> model, e.g.
  `{"worker": "anthropic:claude-haiku-4-5"}`). The run uses the same cases and the same judge with these models;
  the saved workflow is not changed. The run records them in `BenchmarkRun.models` (empty for a run of the saved
  version), and the batch in `Batch.model_overrides`. Two runs compare with the existing case comparison.
- Such a run needs `max_cost_usd` (`benchmark.model_cost_cap`): earlier runs used other models, so the plan gives no
  average (`based_on` 0) and bounds the cost by the limit. An unknown agent (`benchmark.model_node_unknown`), an
  empty model (`config.model_missing`) or a variant the pre-check rejects (`run.agents_invalid`, e.g. reasoning
  effort on a model without it) is refused before anything starts. Without `models` nothing changes.
- Pins: nodestro-runtime `45e7774` (built-in Linux sandbox; before `272e2f6`) and nodestro-examples `786c453`
  (before `e6d9619`). The OpenAPI snapshot gains `Settings.builtin_sandbox`; exports carry the two sandbox modules.

Model prices and cost reconciliation (PRICING-1, phases N5, N6, N8 and N10, 2026-10-31):

- Pins: `nodestro-runtime` 272e2f6 (price catalog, sync, costs priced with the catalog, strict price mode,
  reconciliation) and `nodestro-examples` e6d9619 (the same runtime).
- New routes under `/api/pricing` (module `api_pricing`): the catalog, status and history, `POST /sync`, reviews
  with approve and reject, overrides per provider and model (`PUT`/`DELETE`), billing admin keys and the
  reconciliation (`GET`/`POST`). Reads need `read`, changes `admin`. OpenAPI: the new paths and schemas.
- Every AI operation of the API is priced with the workspace's catalog and price mode (`Workspace.tracker`): agent
  design, memory proposals, tool generation, the Workflow Architect, the Nodestro Assistant, API guides and
  benchmark judges. Their usage carries price references (versions, sources, quality, stale prices).
- `GET /api/usage`: each bucket has `known_cost_usd`, `cost_state` (`known`, `partial`, `unknown`, `none`),
  `stale_price_requests`, `missing_cost_components`, `pricing_versions`, `pricing_sources` and `pricing_quality`.
  `estimated_cost_usd` stays (the known sum) and `unpriced` now also counts requests with incomplete costs.
- Desktop: the API process syncs the reference prices in the background (`NODESTRO_PRICE_SYNC`,
  `NODESTRO_PRICE_LITELLM`, `NODESTRO_PRICE_SNAPSHOT_FILE`; off in `AppConfig()` built by code, so tests never go
  online). Server: the reference prices live under the reserved document tenant `nodestro-price-reference`;
  workers sync them under an advisory lock (`WorkerSettings.price_sync`), tenants read them and keep their own
  overrides; new command `nodestro-server pricing status|sync|approve|reject`.
- Billing admin keys are stored only with encryption at rest (servers, bound to tenant and field). The desktop
  refuses to store them (`pricing.billing_key_not_storable`) and reads `NODESTRO_OPENAI_ADMIN_KEY` /
  `NODESTRO_ANTHROPIC_ADMIN_KEY` or a key sent with one reconciliation request.
- The standalone export carries the runtime's `pricing` and `cost_tracking` modules (32 runtime modules).
- `GET /api/meta`: the suggested models carry the workspace's prices with `price_status`, `price_quality`,
  `price_source` and `price_version` (overrides and synced prices included; before: Orkexa's built-in prices).

"Used in" for knowledge files, tools and memory spaces (evolution plan UX-7, 2026-10-31):

- New read-only route `GET /api/used-by?kind=knowledge|tool|memory[&ref=…]` (permission `read`). It lists each
  object that is used somewhere, with its users: the agents and start nodes of saved workflows, the agents in the
  agent library, and the active release of a published API (knowledge files and memory spaces only, because a
  release reads them live; tools are frozen into a release). With `ref`, only that object; an unused object gives
  an empty list.
- The answer comes from the request's context, so on a server it covers only the principal's tenant (tested on
  SQLite and PostgreSQL). Deleting knowledge files, tools or memory spaces is unchanged.
- OpenAPI: new path `/api/used-by` and the schemas `Reference` and `ReferenceUser`.

Designed agents get the default model as their judge (evolution plan Q-0, 2026-10-31):

- Agents designed by the Agent Architect (`POST /api/agents/design`) and by the Workflow Architect, including its
  fallback agent, now carry `quality.judge_model` set to the configured default model (`settings.default_model`).
  Before, they got the runtime's global default judge (`anthropic:claude-opus-5-5`), so a workspace on Ollama or
  another provider had a hidden Anthropic dependency once the check was switched on. The check stays off. The
  runtime's global default is unchanged.
- When the default model cannot return the structured verdict a quality check needs, the draft carries the note
  `draft.judge_unsupported` (with `model`). The judge is not swapped for another model.
- Unchanged: existing agents, stored workflows, templates, agents the Workflow Architect keeps or revises, and
  explicitly configured judges. No schema or OpenAPI change.

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
