# TODO: nodestro-api

> Open work for this repository: release blockers, known bugs, security follow-ups, technical debt and planned improvements.
> GitHub issues are the source of truth for details; this file is the entry point and summary.
>
> **Last full review:** 2026-11-06 against master at 984c61d (TODO abatement round)

## Summary

| Priority | Count |
|---|---|
| P0 Critical | 0 |
| P1 High | 1 |
| P2 Medium | 5 |
| P3 Low | 12 |
| P4 Optional | 6 |

**Release blockers:** None for this repository; every entry below is not a release blocker. The next desktop
preview is gated by nodestro-desktop DSK-001 (new candidate and Go/No-Go).

The desktop API is in solid shape. Most open work is in the multi-process server profile: worker observability,
key rotation, published APIs on the server. The portable export (#85, hardened in #87) still needs an independent
security review (API-054). The rest is follow-up from the internal backend audits and the UX rounds. Live Run Chat
(LRC-1) is merged (#49).

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

Tool approvals (`NODESTRO_TOOL_APPROVALS`, off by default and staying off) need the independent S-2 review tracked as
nodestro-runtime RT-001 (`Needs external review`); it covers the approval routes here. No separate entry in this file.

### API-054 · Independent security review of the portable export

- **Priority:** P2 Medium · **Status:** Needs external review · **Refs:** #85, #87, nodestro-studio #121,
  `docs/portable-export.md`; backend audit round 3 (internal review only)
- **What:** The portable export (a standalone package with its own web interface and launcher) and the hardening of
  #87 were reviewed internally only. An internal audit does not replace an independent security review.
- **Next step:** Have the export, the exported app and the launcher reviewed independently; record the result here.
- **Depends on:** None

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

### API-009 · Retention for runs, usage log, versions and safety backups

- **Priority:** P3 Low · **Status:** Needs decision · **Refs:** backend audit C-12 (residual)
- **What:** Jobs, events, sessions, login states and idempotency records are cleaned up. Runs, the usage log,
  workflow versions, `before-restore-*.zip`, `pre-migration-*.db` and exports with portable packages
  (`<data>/exports`, up to several GB each) grow without limit.
- **Next step:** Decide on a retention policy (defaults, per tenant on the server). Then add configurable cleanup
  to the existing leader task.
- **Depends on:** nodestro-runtime RT-014 (store deletes for runs and versions)

### API-048 · Portable export builds on the server profile: retention and job leases

- **Priority:** P3 Low · **Status:** Needs decision · **Refs:** backend audit round 3 (API3-07), `docs/portable-export.md`
  ("On a server")
- **What:** The server profile refuses export jobs (`export.server_unsupported`) unless the operator sets
  `NODESTRO_SERVER_EXPORT_JOBS=1`; then one build runs at a time per API process, `NODESTRO_SERVER_EXPORT_MAX_BYTES`
  (default 2 GiB) caps a package and, since the TODO abatement round 3, `NODESTRO_SERVER_EXPORT_TENANT_QUOTA_BYTES`
  (default 8 GiB) caps what one tenant keeps (`403 export.server_quota`; `DELETE /api/exports/{id}` frees space).
  Left: no automatic retention (packages stay until a member deletes them; the default retention is the open API-009
  owner decision), the build still runs in the API process (not as a server batch job with a lease), and the
  one-build slot is per API process, not per server (opening a tenant context also marks builds that another API
  process is running as failed).
- **Next step:** After the API-009 decision, add export retention to the worker's leader clean-up; move builds to a
  queued job with a lease (like the server's reindex jobs) and document the switch in the nodestro-server
  RUNBOOK.
- **Depends on:** API-009 (retention policy)

## Architecture and modularity

### API-010 · Server profile: published APIs, network listener and backups are not served yet

- **Priority:** P2 Medium · **Status:** Open · **Refs:** README "Server logins", `src/nodestro_api/server_app.py`
  docstring
- **What:** On the server, the published workflow APIs (with tenant-bound keys) are not served (the S-2 pilot saw
  405; `openapi/nodestro-api-server.json` has no `/api/published` route). The network listener and studio backups
  are not offered there either; operators use `pg_dump`. Batches, test series, assistant drafts and Python
  environment installs still run in the API process.
- **Next step:** (1) Tenant-bound API keys: the key record names its tenant, the key prefix carries no tenant id,
  and lookup goes through one platform table (key hash -> tenant), so a key never opens another tenant's context.
  (2) Serve `/api/published/{slug}` in `create_tenant_app` by resolving the key first, then the tenant context, and
  start runs through the worker queue (`QueuedRuns`), with per-tenant limits. (3) Leave the network listener off on
  the server (the proxy is the listener) and document that. (4) Document the server backup path (`nodestro-server
  backup`) in the studio. Each step needs its own security review before it is enabled.
- **Depends on:** None

### API-011 · Observability for the server profile: workers and tracing

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit D-10 (residual)
- **What:** The API process has `/api/health` (liveness with the database), `GET /api/ready` (readiness),
  `GET /api/metrics` (Prometheus, with `NODESTRO_METRICS_TOKEN`), request ids (`X-Request-ID`) and JSON logs
  (`NODESTRO_LOG_FORMAT=json`; since the TODO abatement round of 2026-11-06). Workers still have no liveness signal or
  metrics listener of their own, and there is no tracing (OpenTelemetry).
- **Next step:** A worker liveness signal (for example a heartbeat row the API's readiness or the metrics expose),
  then optional OpenTelemetry as an extra that is off by default. The nodestro-server RUNBOOK ("Metrics and status")
  documents `/api/ready`, `X-Request-ID` and `NODESTRO_LOG_FORMAT`.
- **Depends on:** nodestro-server (RUNBOOK monitoring section)

## Performance

None known.

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
  `src/nodestro_api/sandbox_status.py`; backend audit round 3 API3-08 (rest), UX round 3 apicodes handover B-1 (rest)
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

### API-042 · Approval nodes are not four-eyes

- **Priority:** P4 Optional · **Status:** Open · **Refs:** backend audit round 2 API2-15 (by design)
- **What:** Inside a tenant, a member with run permission may approve the approval node of their own run, and the
  edit permission can delete a run with its approval record. This is the documented role model, not a defect.
  (Assistant conversations have an owner on the server since the settings/providers round; older ones get one with
  `nodestro-server assistant backfill-owners`.)
- **Next step:** Decide whether approval nodes get a "needs another member" option.

### API-060 · Internal assistants on Anthropic use the default thinking display

- **Priority:** P4 Optional · **Status:** Needs decision · **Refs:** settings/providers round 2026-11-06 (thinking
  stream, THINK-3)
- **What:** Internal assistants do not request summarized thinking from Anthropic, so the studio shows "not
  transmitted". The thinking display is a preference of the browser only; the API does not know it, and requesting
  `display: summarized` means turning on adaptive thinking for every internal assistant call (cost and latency).
- **Next step:** Decide whether the preference becomes a workspace setting (or a per-request flag the studio sends);
  then request `display: summarized` only while it is on.

## Tests and quality assurance

### API-022 · Untested cases from the backend audit re-verification

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit final report, section 9 ("known test gaps")
- **What:** The following cases have not been run: schedules changed during a tick with several processes (C-09),
  atomicity of workflow deletion on a crash (C-16) and recovery of test series (C-15). (The MCP OAuth callback in
  the access log, D-02, is covered by `test_setup_tokens_and_sign_in_codes_never_reach_the_access_log`.)
- **Next step:** Add PostgreSQL-backed multi-process tests for C-09. Test C-15 and C-16 with a forced crash.

### API-040 · Run budget decisions: browser and server image checks

- **Priority:** P3 Low · **Status:** Open · **Refs:** BUDGET-1, `api_budget.py`, runtime `docs/BUDGET.md`
- **What:** The routes are tested with the desktop app, simulated principals and a PostgreSQL server (tenants,
  roles). The decision flow through a real worker process of the server image and a browser session is covered by
  the Studio and server suites, not here. Only simulated model calls are used.
- **Next step:** Keep the server image e2e of budget decisions green with every pin bump.

## Platform coverage

### API-047 · Portable packages: platforms tested only by the CI smoke test

- **Priority:** P2 Medium · **Status:** Open · **Refs:** `.github/workflows/portable.yml`, `docs/portable-export.md`,
  backend audit round 3 API3-11 (info, documentation open)
- **What:** Linux x86-64 is tested end to end (offline container without Python or network, a real local model
  run, restart, browser E2E). Linux ARM64, Windows x64/ARM64 and macOS Apple Silicon/Intel are covered only by the
  CI smoke test (build, self-test, start, a run without a model call, stop, restart): no local models, no browser,
  no Gatekeeper/SmartScreen. Windows ARM64 has no `sqlite-vec` wheel (semantic search falls back to full text). Releases using Gemini cannot be
  packaged for macOS Intel or Windows ARM64 (no `cryptography` wheel since 46.0.4).
  An offline package does not restrict the network of tools written by people in the workflow; this is not
  documented yet.
- **Next step:** Run a package with local models on real macOS and Windows machines, and a browser check there.
  Document the network behavior of person-written tools in offline packages and the other design notes of API3-11
  (what the offline mode and the default export do and do not restrict).

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

### API-064 · Portable "Open folder" untested on real desktops

- **Priority:** P3 Low · **Status:** Open · **Refs:** settings/providers round 2026-11-06 (result folder stream)
- **What:** Opening the result folder from the portable web interface was tested with a stub only.
- **Next step:** Exercise it on real Windows, macOS and Linux desktops in the portable smoke run; document a bind mount for `OUTPUT_DIR` in the standard export container.

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
  its request is open (agent tester, tool check and save, tool-draft tests, backups, OCR) is not canceled; it ends
  at its own timeout. A model request already in flight in a stopped tool job finishes and its answer is dropped.
  Until the next `suspension_check` (default 5 s) sees the suspension, a tool job keeps working and an install of
  libraries approved before the suspension may start; once stopped, no install starts (runtime R4-7 check under the
  install lock). An approval or rejection committed before the stop is still carried out, by design.
- **Next step:** None. New requests of the tenant are refused at once; cancelling a running worker thread would need
  cooperative cancel points in each of those paths.

## MUP-ONA

Packages of the security and database backend plan MUP-ONA ([plan][mup-ona]) that touch this repository. Each entry is
removed by the change that completes its package here.

### API-MUP-A1 · PDF extraction limits: map `document.too_complex`

- **Priority:** P1 High · **Status:** Open · **Refs:** MP-A1 (PS-1), [MUP-ONA][mup-ona]
- **What:** Uploads and knowledge routes read PDFs through the runtime, which gets page, character, time and memory
  limits in an isolated process (orkexa, runtime). The API must answer the new `document.too_complex` with 422 and keep
  its upload and knowledge paths on the runtime's limited path.
- **Next step:** After the runtime and orkexa parts merge: map the code, test the routes, bump the runtime pin through
  the pin cascade.

### API-MUP-B0 · Declare the orkexa dependency with the runtime's commit

- **Priority:** P3 Low · **Status:** Open · **Refs:** MP-B0, [MUP-ONA][mup-ona]
- **What:** The API imports orkexa but gets it only through the runtime.
- **Next step:** Declare orkexa in `pyproject.toml` at the same commit as the runtime pins it.

### API-MUP-B6 · Server start verifies the schema of the orkexa PostgreSQL backend

- **Priority:** P2 Medium · **Status:** Open · **Refs:** MP-B6, [MUP-ONA][mup-ona]
- **What:** When the runtime moves to `orkexa.backends.postgres`, the server profile must check the schema at start
  (`verify_schema`, read only) and follow the pin cascade.
- **Next step:** After B4/B5 and gate G1: call `verify_schema` in `server_app.py`, tests, pin bump.

[mup-ona]: https://github.com/peterkarlhuber/nodestro-studio/blob/master/docs/architecture/mup-ona/MUP-ONA.md

## Cross-repository dependencies

| This repo item | Other repo | Other item | Relation |
|---|---|---|---|
| (pointer) | nodestro-studio | ST-052 (Dependabot policy: weekly groups, cooldown, manual merge; auto-merge prerequisites) | Full entry in studio |
| (pointer) | nodestro-runtime | RT-001 (independent S-2 review, covers the approval routes here) | full entry in runtime |
| API-005 | nodestro-runtime | RT-010 | runtime adds old-key decryption; the operator command lives here |
| API-009 | nodestro-runtime | RT-014 | runtime provides deletes |
| API-011 | nodestro-server | SRV-007 (RUNBOOK monitoring section) | server documents what this adds |
| (pointer) | nodestro-sdk | SDK-005 | the server contract `openapi/nodestro-api-server.json` exists now (API-012 done); the SDK contract tests can use it |
| API-054 | nodestro-studio | portable export dialog (studio #121, ST-047) | one independent review covers both; full entry here |
| API-021 | nodestro-runtime | RT-015 | runtime change first |
| API-023 | nodestro-server | SRV-013 (PostgreSQL load test) | load test runs there |
| (pointer) | nodestro-local-docker | LD-010 (session hardening) | implementation would be here; tracked there |
| API-005, API-010 | nodestro-server | RUNBOOK limitations (pointer rows in its table) | server documents the current limits until these land |

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
