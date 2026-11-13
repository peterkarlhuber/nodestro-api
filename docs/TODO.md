# TODO: nodestro-api

> Open work for this repository: release blockers, known bugs, security follow-ups, technical debt and planned improvements.
> GitHub issues are the source of truth for details; this file is the entry point and summary.
>
> **Last full review:** 2026-11-06 against master at 1f99df7 (TODO abatement round)

## Summary

| Priority | Count |
|---|---|
| P0 Critical | 0 |
| P1 High | 0 |
| P2 Medium | 5 |
| P3 Low | 10 |
| P4 Optional | 5 |

**Release blockers:** None for this repository; every entry below is not a release blocker. The next desktop
preview is gated by nodestro-desktop DSK-001 (new candidate and Go/No-Go).

The desktop API is in solid shape. Most open work is in the multi-process server profile: key rotation, published
APIs on the server, worker metrics and tracing. The portable export (#85, hardened in #87) still needs an independent
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
  (`<data>/exports`, up to several GB each) grow without limit. On the server, `NODESTRO_SERVER_EXPORT_TENANT_QUOTA_BYTES`
  (default 8 GiB) caps what one tenant keeps (`403 export.server_quota`), but packages stay until a member deletes
  them (the former API-048 rest; export builds themselves run as queued worker jobs with a lease since 2026-11-06).
- **Next step:** Decide on a retention policy (defaults, per tenant on the server). Then add configurable cleanup
  to the existing leader task of the worker, including export packages.
- **Depends on:** nodestro-runtime RT-014 (store deletes for runs and versions)

## Architecture and modularity

### API-010 · Server profile: published APIs, network listener and backups are not served yet

- **Priority:** P2 Medium · **Status:** Needs decision · **Refs:** README "Server logins", `src/nodestro_api/server_app.py`
  docstring
- **What:** On the server, the published workflow APIs are not served (`openapi/nodestro-api-server.json` has no
  `/api/published` route; the S-2 pilot saw 405). The network listener and studio backups are not offered there
  either (operators use `nodestro-server backup`, a `pg_dump`); the README says so. Batches and test series run in the
  API process that started them, with leases that hand them to another process when it dies (API-006); assistant
  drafts and Python environment installs run in the API process.
- **Why not started (re-verified 2026-11-06):** there is no self-contained first step that leaves the security
  boundary unchanged. Serving published APIs needs tenant-bound API keys first (the key record names its tenant, a
  platform table maps key hash to tenant, so a key never opens another tenant's context): that changes key format
  and lookup for every published API and is the security-relevant part. Serving `/api/published/{slug}` through the
  worker queue (`QueuedRuns`) with per-tenant limits builds on it. Keeping the listener off on the server is the
  current state; offering backups in the studio is studio work.
- **Next step:** Owner decision whether published APIs are served by the server profile at all; if yes, design the
  tenant-bound keys and have the design reviewed before it is built, then each later step reviewed before it is
  enabled.
- **Depends on:** Owner decision; independent security review of the key design

### API-011 · Observability for the server profile: worker metrics and tracing

- **Priority:** P3 Low · **Status:** Open · **Refs:** backend audit D-10 (residual)
- **What:** The API process has `/api/health` (liveness with the database), `GET /api/ready` (readiness),
  `GET /api/metrics` (Prometheus, with `NODESTRO_METRICS_TOKEN`), request ids (`X-Request-ID`) and JSON logs
  (`NODESTRO_LOG_FORMAT=json`). Since 2026-11-06 a worker writes a heartbeat file (`NODESTRO_WORKER_HEARTBEAT_FILE`)
  and `nodestro-server worker-health` checks it (age, database, duties) for the container health check. Left: the
  API's readiness and metrics do not show whether any worker is alive (that needs a heartbeat row in the database,
  a runtime migration), workers have no metrics of their own, and there is no tracing (OpenTelemetry).
- **Next step:** A worker heartbeat table in the runtime's server schema, exposed by `/api/metrics`; then optional
  OpenTelemetry as an extra that is off by default. The nodestro-server RUNBOOK and compose file should use the
  heartbeat file and `worker-health` for the worker container.
- **Depends on:** nodestro-runtime (server schema migration for the heartbeat row); nodestro-server (RUNBOOK
  monitoring section, compose health check)

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

### API-067 · Service connections on the portable setup page

- **Priority:** P3 Low · **Status:** Open · **Refs:** NAL-A1, nodestro-runtime ADR-0001, `portable_connections.py`
- **What:** An exported package asks for the service connections its built-in tools are bound to only in the
  terminal (`configure`) or through `NODESTRO_CONNECTION_<ID>_SECRET` and `DATA_DIR/secrets/connections.json`. The
  setup page of the web interface shows provider keys but not connections yet. No built-in tool needs a connection
  before NAL-A2, so no export is affected today.
- **Next step:** Add a connections section to the local-operator setup page (write-only secret, `secret_set` only),
  together with the first tool that needs a connection.

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
  The export's own `Dockerfile` is pinned by digest from nodestro-runtime's `container_images.BASE_IMAGES` since
  2026-11-06 (plain tag only for a Python version the runtime has no digest for).
- **Next step:** Decide whether the export should build its sandbox image itself (for example a pinned
  `docker/sandbox.Dockerfile` shipped in the export); a real-daemon test of both routes and of the server worker's
  Docker watch (`DockerWatch`, which keeps the proof warm; tested with stand-ins and by hand against a daemon that
  refused or could not be reached only) in the CI `docker` job (needs a runner with a Docker daemon).
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

### API-040 · Run budget decisions: browser and server image checks

- **Priority:** P3 Low · **Status:** Open · **Refs:** BUDGET-1, `api_budget.py`, runtime `docs/BUDGET.md`
- **What:** The routes are tested with the desktop app, simulated principals and a PostgreSQL server (tenants,
  roles). The decision flow through a real worker process of the server image and a browser session is covered by
  the Studio and server suites, not here. Only simulated model calls are used.
- **Next step:** Keep the server image e2e of budget decisions green with every pin bump.

## Platform coverage

### API-047 · Portable packages: platforms tested only by the CI smoke test

- **Priority:** P2 Medium · **Status:** Blocked · **Refs:** `.github/workflows/portable.yml`, `docs/portable-export.md`,
  backend audit round 3 API3-11
- **What:** Linux x86-64 is tested end to end (offline container without Python or network, a real local model
  run, restart, browser E2E). Linux ARM64, Windows x64/ARM64 and macOS Apple Silicon/Intel are covered only by the
  CI smoke test (build, self-test, start, a run without a model call, stop, restart): no local models, no browser,
  no Gatekeeper/SmartScreen. Windows ARM64 has no `sqlite-vec` wheel (semantic search falls back to full text). Releases using Gemini cannot be
  packaged for macOS Intel or Windows ARM64 (no `cryptography` wheel since 46.0.4). What the offline mode restricts
  and what not (it is not a firewall for person-written tools) is documented in `docs/portable-export.md` and the
  package README since 2026-11-06.
- **Next step:** Run a package with local models on real macOS and Windows machines, and a browser check there.
- **Depends on:** macOS and Windows machines or CI runners with local models

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

- **Priority:** P3 Low · **Status:** Blocked · **Refs:** settings/providers round 2026-11-06 (result folder stream)
- **What:** Opening the result folder from the portable web interface was tested with a stub only. (The standard
  export's README shows the container with bind mounts for `DATA_DIR` and `OUTPUT_DIR` since 2026-11-06.)
- **Next step:** Exercise it on real Windows, macOS and Linux desktops in the portable smoke run.
- **Depends on:** real desktop machines (no desktop session in CI)

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


### API-MUP-B6 · Server start verifies the schema of the orkexa PostgreSQL backend

- **Priority:** P2 Medium · **Status:** In progress · **Refs:** MP-B6, [MUP-ONA][mup-ona]
- **What:** When the runtime moves to `orkexa.backends.postgres`, the server profile must check the schema at start
  (`verify_schema`, read only) and follow the pin cascade.
- **Done (`mup/b6-api-verify-schema`):** `check`, `api` and `worker` call the runtime's `require_knowledge_schema`
  (`verify_schema` with `PostgresLayout("nodestro_")`) after the migration check; tests T-UPG-7 (API) and
  T-NOD-HTTP (identical ids in two tenants).
- **Next step:** pins to the merge commits of runtime B6 and orkexa B5, then the cascade (sdk, studio, server,
  local-docker).

[mup-ona]: https://github.com/peterkarlhuber/nodestro-studio/blob/master/docs/architecture/mup-ona/MUP-ONA.md

## Cross-repository dependencies

| This repo item | Other repo | Other item | Relation |
|---|---|---|---|
| (done 2026-11-08) | tabrion, nodestro-runtime | TABRION QA-18 (AC-3) | `tests/test_optional_edges.py`: the desktop path (`nodestro_api.cli`, `local_container`, the app of `create_app` started and asked for `/api/health`) works with `axentra` blocked and with psycopg blocked, one test per edge with a mutation probe each; a runtime pin that makes either a base dependency fails it |
| (done, TABRION MIG-NOD-1) | nodestro-runtime, tabrion | runtime RT-074, TABRION VER-1 (RC-2) | `tests/postgres_support.py` runs the PostgreSQL skip/require rule on `tabrion.testing`; the `dev` extra pins `tabrion[testing,postgres]` at the commit nodestro-runtime's `dev` extra pins (v0.1.0 final `d5821fa`). A Tabrion pin bump moves runtime first, then this pin; nodestro-studio's `check_versions.py` checks both |
| (pointer) | nodestro-studio | ST-052 (Dependabot policy: weekly groups, cooldown, manual merge; auto-merge prerequisites) | Full entry in studio |
| (pointer) | nodestro-runtime | RT-001 (independent S-2 review, covers the approval routes here) | full entry in runtime |
| API-005 | nodestro-runtime | RT-010 | runtime adds old-key decryption; the operator command lives here |
| API-009 | nodestro-runtime | RT-014 | runtime provides deletes |
| API-011 | nodestro-server | SRV-007 (RUNBOOK monitoring section) | server documents the heartbeat file and `worker-health` |
| (pointer) | nodestro-sdk | SDK-005 | the server contract `openapi/nodestro-api-server.json` exists now (API-012 done); the SDK contract tests can use it |
| API-054 | nodestro-studio | portable export dialog (studio #121, ST-047) | one independent review covers both; full entry here |
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
