# TODO: nodestro-api

> Open work for this repository: release blockers, known bugs, security follow-ups, technical debt and planned improvements.
> GitHub issues are the source of truth for details; this file is the entry point and summary.
>
> **Last full review:** 2026-11-04 against `master` at `b55793a` (internal backend audit round 3).

## Summary

| Priority | Count |
|---|---|
| P0 Critical | 0 |
| P1 High | 0 |
| P2 Medium | 5 |
| P3 Low | 21 |
| P4 Optional | 8 |

**Release blockers:** None for this repository.

The desktop API is in solid shape. Most open work is in the multi-process server profile: batch leases, observability,
key rotation, published APIs on the server. The rest is follow-up from the internal backend audit and the UX
initiative of 2026-11-01. Live Run Chat (LRC-1) is merged (#49).

## Priorities and statuses

- **P0 Critical**: data loss, security exposure, or broken core function; blocks any release.
- **P1 High**: blocks a planned release or a promised capability, or a significant risk.
- **P2 Medium**: real defect or gap with a workaround; schedule soon.
- **P3 Low**: minor defect, polish, or small debt.
- **P4 Optional**: improvement idea; not a defect.

Statuses: `Open`, `In progress` (PR linked), `Blocked` (names the blocker), `Needs decision` (owner decision pending),
`Needs external review`, `Accepted risk` (deliberately not fixed; reason stated).

## Release blockers and critical items

None known.

## Security

### API-001 · Cleanup of idle tenant contexts on the server

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit A-09 (residual); request size limits done in
  backend audit round 2 (API2-03)
- **What:** Hardening follow-up: resource cleanup for idle tenant contexts on the server. Details are kept privately.
- **Next step:** Add the cleanup, as described in the internal audit.

### API-002 · Rate limiting of the anonymous login start (server)

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit A-02 (residual)
- **What:** Expired login states are now cleaned up. Hardening of the login start (an application-level rate limit
  and tighter input bounds) is still open; today the nodestro-server RUNBOOK recommends a proxy rate limit.
- **Next step:** Add the application-level limits as described in the internal audit.

### API-004 · Audit trail for logins, keys, tenants and roles (server)

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit D-11
- **What:** Tool approvals have an audit trail. Logins, API key changes, tenant creation and role changes appear
  only in the process log. With `NODESTRO_IDENTITY_BACKEND=mirror|axentra` (off by default) Axentra's audit log also
  records tenant and membership changes and refused logins of its login flow, but nothing reads it back.
- **Next step:** Add an `audit_events` store (actor, action, target, time) and record these events. Expose the
  events to tenant admins read-only.

### API-005 · Rotation of the server secret key

- **Priority:** P2 Medium · **Status:** In progress · **Refs:** #52, nodestro-runtime `docs/AXENTRA.md`
- **What:** `NODESTRO_SECRET_KEY` encrypts all tenant secrets, and it cannot be rotated.
- **Done (2026-11-02):** with `NODESTRO_AXENTRA=on` and `NODESTRO_SECRETS_FORMAT=axs1`, `nodestro-server secrets
  reencrypt [--dry-run] [--tenant]` moves every tenant secret to the active key of Axentra's keyring (several keys,
  rotation; unreadable values are reported, never overwritten; exit code 2).
- **Next step:** Rotation without Axentra (v1 format) stays impossible; decide whether new servers default to the
  keyring, and rehearse the procedure on a staging copy (nodestro-server SRV-019).
- **Depends on:** nodestro-runtime RT-010 (`SecretBox` support for an old key)

## Bugs and reliability

### API-006 · Batches and test series on the server have no owner or lease

- **Priority:** P2 Medium · **Status:** Open · **Refs:** #51, backend audit C-07 (residual)
- **What:** After an API process crashes, its batches and test series stay `running` until someone cancels them.
- **Next step:** Give them an owner and a lease with a reaper, as run jobs have (or move them to the worker queue).

### API-007 · Billing reconciliation hides `not_reconciled` without a billing key

- **Priority:** P3 Low · **Status:** Open · **Refs:** #55, Bedrock final report item 4
- **What:** A workspace that uses only Bedrock gets `pricing.billing_key_missing` instead of a report that names
  its unreconciled providers.
- **Next step:** Return a report with the `not_reconciled` list when no adapter exists.

### API-008 · `nodestro-server check` does not warn about unknown tenant IDs in the Bedrock role list

- **Priority:** P3 Low · **Status:** Open · **Refs:** #56, Bedrock final report item 5
- **What:** A typo in `NODESTRO_BEDROCK_TENANT_ROLES` silently leaves a tenant without Bedrock.
- **Next step:** Warn in `check` about role entries for tenants that do not exist.

### API-009 · Retention for runs, usage log, versions and safety backups

- **Priority:** P3 Low · **Status:** Needs decision · **Refs:** backend audit C-12 (residual)
- **What:** Jobs, events, sessions, login states and idempotency records are cleaned up. Runs, the usage log,
  workflow versions, `before-restore-*.zip`, `pre-migration-*.db` and exports with portable packages
  (`<data>/exports`, up to several GB each) grow without limit.
- **Next step:** Decide on a retention policy (defaults, per tenant on the server). Then add configurable cleanup
  to the existing leader task.
- **Depends on:** nodestro-runtime RT-014 (store deletes for runs and versions)

### API-048 · Portable export builds on the server profile: quota and retention

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit round 3 (API3-07), `docs/portable-export.md`
  ("On a server")
- **What:** Since round 3 the server profile refuses export jobs (`export.server_unsupported`) unless the operator
  sets `NODESTRO_SERVER_EXPORT_JOBS=1`; then one build runs at a time on the whole server and
  `NODESTRO_SERVER_EXPORT_MAX_BYTES` (default 2 GiB) caps a package. Left: no per-tenant quota and no retention, so
  packages stay until a member deletes them, and the build still runs in the API process.
- **Next step:** Before anyone enables export jobs on a shared server: per-tenant quota plus retention (see
  API-009), and run builds as server batch jobs with a lease (like API-044). Document the switch in the
  nodestro-server RUNBOOK.
- **Depends on:** None

### API-043 · Idempotent run starts are serialised per API process only

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit round 2 re-verification R2V-03, API2-08
- **What:** Run starts with an `Idempotency-Key` (studio and published API) check and record the key under a lock
  held by one process. With several API processes behind a load balancer, two concurrent retries with the same key
  on different processes can each start a run.
- **Next step:** An atomic insert-if-absent for raw store records (SQLite and PostgreSQL) in nodestro-runtime; reserve
  the key with it before the run starts.
- **Depends on:** nodestro-runtime RT-045

## Architecture and modularity

### API-010 · Server profile: published APIs, network listener and backups are not served yet

- **Priority:** P2 Medium · **Status:** Open · **Refs:** README "Server logins", `src/nodestro_api/server_app.py` docstring
- **What:** On the server, the published workflow APIs (with tenant-bound keys) are not served (the S-2 pilot saw
  405). The network listener and studio backups are not offered there either; operators use `pg_dump`. Batches,
  test series, assistant drafts and Python environment installs still run in the API process.
- **Next step:** Design tenant-bound API keys and serve `/api/published/{slug}` per tenant through the worker
  queue. Document the server backup path in the studio.

### API-011 · Observability for the server profile

- **Priority:** P2 Medium · **Status:** Open · **Refs:** backend audit D-10 (residual)
- **What:** `/api/health` reports database loss (503), and `GET /api/metrics` (Prometheus, only with
  `NODESTRO_METRICS_TOKEN`; nodestro-runtime `docs/AXENTRA.md`, "Observability") exists. The server still has no
  JSON log, request or correlation IDs or readiness endpoint separate from liveness, and workers have no liveness
  signal or metrics listener of their own.
- **Next step:** Add request IDs and structured logging first, then readiness. The nodestro-server RUNBOOK ("Metrics
  and status") then documents them.
- **Depends on:** nodestro-server (RUNBOOK monitoring section)

### API-012 · No OpenAPI contract for the server routes

- **Priority:** P4 Optional · **Status:** Open · **Refs:** backend audit D-18
- **What:** `openapi/nodestro-api.json` describes the desktop app. The identity and tenant routes of
  `create_tenant_app` have no contract, so the SDKs are not contract-tested against the server profile.
- **Next step:** Generate a second OpenAPI document from `create_tenant_app`, with a test like the existing one.
- **Depends on:** nodestro-sdk (contract tests, SDK-005)

## Performance

### API-013 · `/api/usage` reads every run of the period

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit C-05 (residual)
- **What:** The totals are now correct, but at 5,000 runs a request took about 0.8 s on SQLite and 1.4 s on
  PostgreSQL. The time grows linearly with the number of runs.
- **Next step:** Aggregate in the store (SQL), or read only the usage fields with a `since` filter.
- **Depends on:** nodestro-runtime RT-009 (store-level time filter)

## Features and enhancements

### API-045 · Offline container image of a published workflow

- **Priority:** P4 Optional · **Status:** Open · **Refs:** portable export work order (2026-11-04),
  `docs/portable-export.md` ("Container image")
- **What:** The source export's `Dockerfile` is the container route; it needs a network to install. A real offline
  image (pinned glibc base image, the Linux portable package as a layer, OCI layout written without a Docker daemon)
  was evaluated and deferred. The Linux portable package already runs unchanged in a container.
- **Next step:** Assemble an OCI layout from a pinned base image digest plus the package, sign it, and test `docker
  load` + `docker run --network none` in CI.

### API-046 · Portable packages are not code-signed

- **Priority:** P3 Low · **Status:** Open · **Refs:** portable export work order (2026-11-04)
- **What:** macOS Gatekeeper and Windows SmartScreen ask once before the first start (the README explains it). The
  bundled Python and Ollama binaries keep their upstream signatures where they have one.
- **Next step:** Needs signing identities (Apple Developer ID, Windows code-signing certificate) owned by the
  project; then sign and notarize in the export job or in a separate release step.

### API-044 · Reindex jobs run inside the API process

- **Priority:** P3 Low · **Status:** Open · **Refs:** local models work order (2026-11-03)
- **What:** `POST /api/knowledge/index/rebuild` runs the embedding job in a thread of the API process. A restart
  stops it; the next rebuild continues where it stopped. On a multi-process server two replicas could each start
  one for the same tenant (the runtime's final switch stays atomic, but the work is done twice).
- **Next step:** Run reindex jobs as server batch jobs with a lease once the server profile gets them.
- **Depends on:** nodestro-runtime RT-046

### API-016 · Machine-readable list of all error codes (X-03)

- **Priority:** P3 Low · **Status:** Open · **Refs:** #53, backend audit X-03
- **What:** No generated list of error codes exists, so new codes reach the studio without a text. The OpenAPI
  document also declares only 422/429 for the published run routes (the real answers include 401, 403, 404, 409,
  413 and 503 with `ErrorResponse`), and most studio routes have no tag, so `/api/docs` is one flat list (UX
  round 3 handover B13).
- **Next step:** Generate one list of API and runtime codes, published next to the OpenAPI document. Add a test
  for codes that are missing from it. Declare the error responses of the published routes and tag the studio
  routers (workflows, runs, publications, local models, MCP, pricing).
- **Depends on:** nodestro-runtime RT-011

### API-017 · Budget information in the usage report (AD-20)

- **Priority:** P3 Low · **Status:** Open · **Refs:** #54, UX audit AD-20
- **What:** The usage report has no budget fields: daily budget use per publication, runs stopped by budget.
- **Next step:** Add a `budgets` section to `UsageReport`, then regenerate the contract and the SDK types.
- **Depends on:** nodestro-studio ST-014 (Usage page "Budgets" card)

### API-018 · Expose the decision result and the run-level failure cause (X-01, AD-02)

- **Priority:** P3 Low · **Status:** Blocked · **Refs:** backend audit X-01, UX audit AD-02
- **What:** The approval decision still answers with the run in `paused` (`decide_approval`), and failed runs carry
  no run-level cause. Both shapes come from the runtime.
- **Next step:** Once runtime RT-005 and RT-012 land, update the response models and docstrings, regenerate the
  OpenAPI document, and add API tests.
- **Depends on:** nodestro-runtime RT-005, RT-012; consumed by nodestro-studio ST-003 (studio #81)

### API-019 · Member and role management for tenant admins (server)

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit D-03 (residual)
- **What:** Members, roles and sessions can be managed only with the operator CLI (`set-role`, `remove-member`,
  `revoke-sessions`). Tenant owners have no API for it, and invitations do not exist.
- **Next step:** Add admin routes for members, roles and invitations (permission `admin`), with audit events
  (API-004).
- **Depends on:** nodestro-studio (access UI)

### API-020 · Tool approval phase for published workflow APIs

- **Priority:** P3 Low · **Status:** Blocked · **Refs:** D10 tool approvals plan ("later")
- **What:** Published APIs warn about tool calls that would need approval, but they have no approval phase of
  their own, so such calls are refused at run time.
- **Next step:** Design the approval flow for the `approve` key scope after the S-2 review.
- **Depends on:** nodestro-runtime RT-001 (independent S-2 review)

### API-021 · Region in usage data (Bedrock)

- **Priority:** P4 Optional · **Status:** Open · **Refs:** Bedrock final report, section 7
- **What:** Usage records carry no provider region, so the usage views cannot show costs by Bedrock region.
- **Next step:** Record the region in the runtime usage data, then add it to `UsageBucket` or a `by_region` view.
- **Depends on:** nodestro-runtime RT-015 (region in usage records)

### API-027 · Docker sandbox status routes: follow-ups

- **Priority:** P3 Low · **Status:** Open · **Refs:** Docker-first plan A1, `GET /api/sandbox/status`,
  `POST /api/sandbox/self-test`, nodestro-runtime RT-033/RT-034
- **What:** Both routes answer the runtime's background check (`Workspace.docker_status`, a `SandboxBackendStatus`);
  `GET /api/settings` never waits for Docker. A forced check (`refresh=1` by an admin, the self-test) starts at most
  every 10 s per path and Docker monitor, which one server's tenants share (see API-028). The studio shows the
  status and the diagnostics codes `docker.checking` and `docker.unavailable` (nodestro-studio ST-033). Open: the
  export (`TOOL_SANDBOX=docker`) still uses Orkexa's `orkexa-sandbox` image, which the operator builds by hand from
  Orkexa's digest-pinned `docker/sandbox.Dockerfile` (the export's README gives the commands since 2026-11-02);
  Nodestro's runtime image cannot be used, because building it needs the runtime's Python environment manager,
  which an export does not carry, and an image id cannot be pinned in the export because it is local to one daemon.
  The self-test resolves the tag to an id and every container runs that id, so a tag moved later is not run
  unchecked. The routes were tested with stand-ins only (no Docker daemon in the development container).
- **Next step:** Decide whether the export should build its sandbox image itself (for example a pinned
  `docker/sandbox.Dockerfile` shipped in the export); pin the export's own `Dockerfile` (`FROM python:<v>-slim`,
  no digest yet) by digest, e.g. from nodestro-runtime's `container_images.BASE_IMAGES`; a real-daemon test of both
  routes and of the server worker's Docker watch (`DockerWatch`, which keeps the proof warm; tested with stand-ins
  and by hand against a daemon that refused or could not be reached only) in the CI `docker` job.
- **Depends on:** None

### API-028 · Docker sandbox status: residual risks after the security review (DS-6, DS-8, DS-12)

- **Priority:** P3 Low · **Status:** Open · **Refs:** security review DS-6, DS-8, DS-12, DS-13;
  `src/nodestro_api/sandbox_status.py`
- **What:** Readers no longer start checks, see host details or block more than 4 worker threads. Left: on a server
  "admin" is a tenant admin, so every tenant's admins still see the shared host's Docker detail and share one 10 s
  window per path (one tenant admin polling `refresh=1` throttles another's status refresh, not the self-test); the
  diagnosis (`GET /api/diagnostics`, permission `install`) still waits up to 5 s outside the waiter cap; the runtime's
  periodic preflight (TTL) is still started by any status read, as before. Related (round 3, API3-08): server
  tenants without the `install` permission now get `GET /api/local-models` without hardware, paths, the local model
  server address, or advice and recommendation reasons that describe the machine; the catalog's `fits` flag per model
  and the recommended models still hint at its size, and `GET /api/local-models/jobs` still shows job target paths.
- **Next step:** Decide whether Docker host detail on a server goes to the operator only (a server-operator
  permission instead of tenant `admin`), and whether the windows should be per tenant. The studio shows
  `throttled`/`retry_after`, `refresh_ignored` and `redacted` (nodestro-studio ST-033) and the SDK returns them
  (`SandboxStatus`, nodestro-sdk, pin `dde77f5`).
- **Depends on:** None

### API-042 · Approval nodes are not four-eyes; conversations have no owner

- **Priority:** P4 Optional · **Status:** Open · **Refs:** backend audit round 2 API2-15 (by design)
- **What:** Inside a tenant, a member with run permission may approve the approval node of their own run, any editor
  can read, rename or delete assistant conversations, and the edit permission can delete a run with its approval
  record. This is the documented role model, not a defect.
- **Next step:** Decide whether approval nodes get a "needs another member" option and conversations get an owner.

### API-052 · Run search in `GET /api/search`

- **Priority:** P4 Optional · **Status:** Open · **Refs:** UX round 3 handover H1, studio ST-008 (#83)
- **What:** The studio's global search filters only the newest 100 runs on the client; older runs are not found,
  and the server profile's search has no runs.
- **Next step:** Add a `SearchResult` kind `run` (id = run id, title = workflow name, subtitle = status, snippet =
  input excerpt, `workflow_id`), matched on workflow name, input text and run id, newest first, with the same read
  permission as `/api/runs`. Regenerate the contract and the SDK types.
- **Depends on:** None

### API-053 · Developer messages: export check and jobs, not-found names, exported client, server check

- **Priority:** P4 Optional · **Status:** Open · **Refs:** UX round 3 DX handovers B15-B18, apicodes handovers B-2 to B-4
- **What:** The export check does not report `export.server_unsupported` or the server's size limit up front, so the
  refusal appears only on "Build package"; export jobs carry no `error_params`, so a failed job's message loses its
  details; the `*.not_found` codes send no display name, so the studio cannot name the missing item (UX round 3
  apicodes handovers B-2 to B-4). The exported `examples/client.py` has a fixed base URL and prints a traceback on HTTP errors; the export
  README shows an approval section and "-" when the workflow has no approval steps, and the exported approval test
  returns instead of skipping; `local.*` task errors do not list the allowed tasks; `nodestro-server check` prints
  all profile problems in one line.
- **Next step:** Report the server refusal and limit in the export check; add `error_params` to export jobs; add
  `params.display` where the name is known. `NODESTRO_BASE_URL` with the current default and the error code from the
  JSON body in the client;
  leave out the empty approval section, "none" instead of "-", `pytest.skip`; `params.allowed`; one problem per line.
- **Depends on:** None

## Tests and quality assurance

### API-022 · Untested cases from the backend audit re-verification

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit final report, section 9 ("known test gaps")
- **What:** The following cases have not been run: schedules changed during a tick with several processes (C-09),
  atomicity of workflow deletion on a crash (C-16), recovery of test series (C-15), and redaction of the MCP OAuth
  callback in the access log (D-02).
- **Next step:** Add PostgreSQL-backed multi-process tests for C-09 and an access-log test for the MCP OAuth
  callback. Test C-15 and C-16 with a forced crash.

### API-040 · Run budget decisions: browser and server image checks

- **Priority:** P3 Low · **Status:** Open · **Refs:** BUDGET-1, `api_budget.py`, runtime `docs/BUDGET.md`
- **What:** The routes are tested with the desktop app, simulated principals and a PostgreSQL server (tenants,
  roles). The decision flow through a real worker process of the server image and a browser session is covered by
  the Studio and server suites, not here. Only simulated model calls are used.
- **Next step:** Keep the server image e2e of budget decisions green with every pin bump.

## Platform coverage

### API-047 · Portable packages: platforms tested only by the CI smoke test

- **Priority:** P2 Medium · **Status:** Open · **Refs:** `.github/workflows/portable.yml`, `docs/portable-export.md`
- **What:** Linux x86-64 is tested end to end (offline container without Python or network, a real local model
  run, restart, browser E2E). Linux ARM64, Windows x64/ARM64 and macOS Apple Silicon/Intel are covered only by the
  CI smoke test (build, self-test, start, a run without a model call, stop, restart): no local models, no browser,
  no Gatekeeper/SmartScreen. Windows ARM64 has no `sqlite-vec` wheel (semantic search falls back to full text). Releases using Gemini cannot be
  packaged for macOS Intel or Windows ARM64 (no `cryptography` wheel since 46.0.4).
  An offline package does not restrict the network of tools written by people in the workflow; this is not
  documented yet.
- **Next step:** Run a package with local models on real macOS and Windows machines, and a browser check there.
  Document the network behavior of person-written tools in offline packages.

### API-023 · Platforms and environments not verified

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit sections 3 and 10; Bedrock final report
- **What:** Not verified: the desktop backend on macOS and Windows, PostgreSQL throughput on server hardware under
  real load (C-14 measured only in a shared container), real model providers (including a live Amazon Bedrock
  smoke test, which needs the owner's approval), and container sandbox paths outside CI: the Docker sandbox
  routes and the export's `TOOL_SANDBOX=docker` never ran against a real daemon in this repository's CI; Orkexa's
  checks behind them ran in Orkexa's CI on Docker Engine (rootful, rootless, arm64), rootful Podman and Colima on
  macOS x86-64 only. Docker Desktop (macOS Apple silicon and Intel, Windows x64 and ARM64), OrbStack and rootless
  Podman never ran in any CI (orkexa ORK-024).
- **Next step:** Run the load test on target hardware (nodestro-server SRV-013) and a live provider smoke test once
  approved. Add macOS and
  Windows runners when the desktop release needs them.

## Documentation

None known.

## Technical debt

None known.

## Later optimizations

None known.

## Accepted risks

### API-026 · Event streams continue up to about 15 seconds after access is revoked

- **Priority:** P4 Optional · **Status:** Accepted risk · **Refs:** backend audit A-04
- **What:** Open SSE streams check access in the keep-alive cycle (`STREAM_RECHECK_SECONDS`, 15 s) and end with
  `access_ended`, so events within that window are still delivered.
- **Next step:** None. If needed, shorten the window. Reason: a check on every event would load each stream with
  database queries.

### API-041 · Request-bound work of a suspended tenant runs to its own timeout

- **Priority:** P4 Optional · **Status:** Accepted risk · **Refs:** security review NF-1 (#69), R4-7;
  `server_app.stop_tenant_work`
- **What:** `stop_tenant_work` stops all background work of a suspended tenant (batches, benchmarks, assistant,
  architect, tool generation jobs, run chat, Python installs). Work that a route runs with `asyncio.to_thread` while
  its request is open (agent tester, tool check and save, tool-draft tests, backups, OCR) is not cancelled; it ends
  at its own timeout. A model request already in flight in a stopped tool job finishes and its answer is dropped.
  Until the next `suspension_check` (default 5 s) sees the suspension, a tool job keeps working and an install of
  libraries approved before the suspension may start; once stopped, no install starts (runtime R4-7 check under the
  install lock). An approval or rejection committed before the stop is still carried out, by design.
- **Next step:** None. New requests of the tenant are refused at once; cancelling a running worker thread would need
  cooperative cancel points in each of those paths.

## Cross-repository dependencies

| This repo item | Other repo | Other item | Relation |
|---|---|---|---|
| (pointer) | nodestro-runtime | RT-001 (independent S-2 review, covers the approval routes here) | full entry in runtime |
| API-005 | nodestro-runtime | RT-010 | runtime adds old-key decryption; the operator command lives here |
| API-009 | nodestro-runtime | RT-014 | runtime provides deletes |
| API-011 | nodestro-server | SRV-007 (RUNBOOK monitoring section) | server documents what this adds |
| API-012 | nodestro-sdk | SDK-005 | SDK contract tests use the server contract |
| API-013 | nodestro-runtime | RT-009 (store time filter) | runtime change first |
| API-016 | nodestro-runtime | RT-011 | combined code list |
| API-043 | nodestro-runtime | RT-045 (atomic insert-if-absent) | runtime change first |
| API-044 | nodestro-runtime | RT-046 (reindex jobs in the API process) | runtime change first |
| API-017 | nodestro-studio | ST-014 (AD-20 Usage page card) | studio consumes the field |
| API-018 | nodestro-runtime, nodestro-studio | RT-005, RT-012; ST-003 (studio #81) | runtime changes the run shape first; studio consumes it |
| API-021 | nodestro-runtime | RT-015 | runtime change first |
| API-023 | nodestro-server | SRV-013 (PostgreSQL load test) | load test runs there |
| (pointer) | nodestro-local-docker | LD-010 (session hardening) | implementation would be here; tracked there |
| API-001, API-002, API-005, API-006, API-010, API-019 | nodestro-server | RUNBOOK limitations (pointer rows in its table) | server documents the current limits until these land |
| (pointer) | nodestro-runtime | X-01 / RT-005 | full entry in runtime |

## CI access to private repositories

Keep the repository secret `NODESTRO_READ`; do not delete it. CI currently installs the sibling repositories anonymously
because they are public, and does not need the secret. When the repositories become private, CI authenticates again
with this read-only secret, passed only in the environment of the install steps (never in a global Git configuration,
a log or an image layer).

## Maintaining this file

- Add an entry when work is identified but not done in the current PR. Use the next free ID; IDs are never reused.
- Link a GitHub issue for anything concrete enough to be picked up; keep details in the issue, not here.
- When an item is finished, delete its entry in the PR that completes it and mention the ID in the PR description.
  Git history is the record; do not keep ticked checklists.
- Accepted risks stay listed with their reason until the risk is removed or re-evaluated.
- Re-review the whole file before each release and update the "Last full review" line.
- Security issues that are not yet fixed are described only in general terms here. Report details privately
  (GitHub private vulnerability reporting), never in this file or in public issues.
