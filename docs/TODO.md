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
| P2 Medium | 6 |
| P3 Low | 13 |
| P4 Optional | 7 |

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

### API-075 · Memory spaces with an owner on a server (RT-089c): the api's part

- **Priority:** P1 High · **Status:** In review · **Refs:** nodestro-runtime RT-089c, `nodestro_api.memory_owners`,
  `tests/test_memory_space_owners.py`
- **What:** before, every member of a tenant listed, read, wrote and cleared every memory space on `/api/memory`. Now
  on a server only a space's owner (its first writer) uses it: `/api/memory` lists only the request's own spaces; the
  space routes, an assistant attachment of kind `memory`, the export check and export (`options.memory`), the
  architect's resource list and the context preview answer another person's space, a space without an owner and a
  principal without an owner identity like a space that does not exist (`404 memory.space_not_found`).
  Administrators get no extra capability. A space without an owner gets one only with the operator's
  `nodestro-server memory backfill-owners --tenant --user --space` (member only, never over an owner, audited as
  `memory_space_owner_assigned`; nothing is deleted). The desktop is unchanged.
- **Open:** runs for no person (published APIs, schedules without a starter) that use persistent memory now fail
  closed on a server (runtime RT-089c); the SDK syncs the new audit kind from `openapi/nodestro-api.json`.
- **Next step:** Independent review together with the runtime part.

### API-074 · Egress protection by content provenance (N-08): the api's part

- **Priority:** P3 Low · **Status:** Open · **Refs:** nodestro-runtime RT-094 and `docs/content-provenance.md`,
  `tests/test_content_provenance.py`
- **What:** Behind the runtime's `NODESTRO_CONTENT_PROVENANCE` (default off, only with `NODESTRO_ACCESS_GRANTS`): the
  assistant's context (an attached or earlier sent run that holds content of a granted source, the selected files of
  a memory space, the text of a knowledge file) and a test series' AI judge (the output of such a run) need a grant
  for a cloud model (`Workspace.connect_model(..., shared_content=)`; `access.missing_grant`). The contract has
  `Run.granted_content` and the conversation value `provenance: granted_content`. Open:
  - the refusal comes when a message is sent, not when the object is attached (N-09 refuses an offline run at
    attaching already);
  - the other callers of `connect_model` (architect, guide, skills, model choice) send no run content and pass none;
  - files a person uploads into an assistant chat are not granted content: the person hands them to the model they
    chose for that chat, so sending them is the person's own act (review L2, documented here and in the runtime's
    `docs/content-provenance.md`). A local model server elsewhere counts as a cloud model here too (runtime
    `RunManager.leaves_computer`, through `connect_model`).
- **Next step:** Re-review of the fixes with runtime RT-094; activation is the owner's decision.

### API-073 · Conversation access: residual points after the W3-S1 audit

- **Priority:** P3 Low · **Status:** Open · **Refs:** W3-S1, `docs/conversations.md` ("Who has an owner
  identity"), `tests/test_conversation_access_audit.py`, `tests/test_conversation_access_audit_server.py`,
  nodestro-runtime RT-089
- **What:** The audit closed the principal without a role (`NO_OWNER`), knowledge adoption from another person's
  answer run and conversation files as run, batch and form inputs. What remains, none of it a content leak:
  - Done (owner's decision A): a conversation without an owner is nobody's on a server, administrators included,
    until the operator assigns an owner with `nodestro-server assistant backfill-owners --tenant --user
    --conversation` (member only, never over an owner, audited as `conversation_owner_assigned`;
    `tests/test_conversation_owners.py`).
  - A `POST`/`PUT` route with `{run_id}` answers another person's answer run with `404` before it checks the body,
    and an unknown id with `422` when the body is invalid: the difference shows that the id exists (never what it
    holds). Run ids are random; the administrator's activity log names them anyway.
  - The administrator's activity log names the answers' runs, conversation ids and the tools called (no contents,
    by design of N-13); the usage and skill statistics count answers (N-3).
  - Done (review): an answer no longer writes into a shared memory space (run memory). An agent that answers in a
    conversation and proposes knowledge still shares what a person adopts, as in any run.
  - Done (review): a grant for "this conversation" names only a conversation the caller sees.
  - The resume plan of a failed answer run lists its conversation files as missing (they are no run file catalog
    entries any more); resuming is not a conversation feature, an answer is sent again instead.
- **Next step:** Include the conversation routes and the owner assignment in the next independent review.

### API-071 · Independent review of Local Docker's key handoff (LD-020)

- **Priority:** P3 Low · **Status:** Needs external review · **Refs:** `nodestro_api.local_boot`,
  `tests/test_local_boot.py`, nodestro-runtime RT-082, nodestro-local-docker LD-020
- **What:** `nodestro-local boot` reads the host's key file as root with three capabilities, drops to 10001 and hands
  the key over through a pipe; `serve` refuses to start without it or as root. Internal tests (here and the
  end-to-end tests of nodestro-local-docker on rootful and rootless Docker) pass; they do not replace the independent
  review (E-P-5, bundled with nodestro-runtime RT-081).
- **Next step:** Add to the next bundled independent review brief.

### API-054 · Independent security review of the portable export

- **Priority:** P2 Medium · **Status:** Needs external review · **Refs:** #85, #87, nodestro-studio #121,
  `docs/portable-export.md`; backend audit round 3 (internal review only)
- **What:** The portable export (a standalone package with its own web interface and launcher) and the hardening of
  #87 were reviewed internally only. An internal audit does not replace an independent security review.
- **Next step:** Have the export, the exported app and the launcher reviewed independently; record the result here.
  The NAL review briefs bundle it with P-Pair (pairing, NAL-C4, and the interface on another origin, NAL-C5) and
  P-Import (`restore`, `update`, `ui install`, NAL-C3; skill and template import): nodestro-studio
  `docs/security/nal/`, ST-064. Prepared, not commissioned.
- **Depends on:** None

### API-069 · Pairing and the interface on another origin stay off until the review

- **Priority:** P2 Medium · **Status:** Needs external review · **Refs:** NAL-C4 (#149), NAL-C5 (#150),
  `docs/pairing.md`, `docs/cross-origin.md`, nodestro-studio `docs/security/nal/p-pair.md` (ST-064)
- **What:** `NODESTRO_PAIRING` and `NODESTRO_CROSS_ORIGIN` are off by default and stay off for productive use until
  the independent review P-Pair has a report without open critical or high findings, or the owner accepts the
  residual risk. Internal reviews found no critical or high finding. Residual risks are listed in both documents.
- **Next step:** The owner decides on commissioning the review; enabling either switch needs the owner's consent.

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

### API-070 · App contract v1: languages, per-app capabilities, backend id after restore (NAL-C1)

- **Priority:** P4 Optional · **Status:** Open · **Refs:** NAL-C1 (#134), `app_contract.py`, `docs/app-api/v1.md`
- **What:** `AppSummary.languages` is always `[]` because apps do not declare languages yet; capabilities are per
  backend, not per app; a restored backup keeps the backend id, so it reports itself as the same backend (C5's
  per-backend refresh cookie name is derived from that id).
- **Next step:** Fill `languages` once apps declare them; decide whether a restore onto another computer should get
  a new backend id (it would void paired devices).

### API-072 · More sources for the activity log: the access audit trail and the assistant (N-13)

- **Priority:** P4 Optional · **Status:** Open · **Refs:** N-13, `api_activity.py`, `access_audit.py`,
  `docs/activity-log.md`, nodestro-runtime RT-084
- **What:** The activity log (`GET /api/activity`) shows runtime sources only. The access audit trail (logins, tenant
  switches, API key and member changes) stays its own record: the runtime's sources are a closed list without an
  access source, and its events carry `details` that the activity log does not take. Assistant conversations write no
  events, so `conversation_id` is never set.
- **Next step:** Once the runtime offers an access source (ids, kind and outcome only), write one activity event per
  access audit event in `access_audit.record`; let the assistant write its tool calls and file actions with its
  conversation id.

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
  setup page of the web interface shows provider keys but not connections yet. Since NAL-A2, `search_web` with a
  key-based search provider needs one: such a package is set up in the terminal or by the environment variable
  (the portable smoke test proves that path on six platforms).
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
  (Assistant conversations have an owner on the server since the settings/providers round; older ones are nobody's
  until the operator assigns one with `nodestro-server assistant backfill-owners`, API-073.)
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

### API-068 · Dependency confusion: the poisoned-index job and the studio origin commit (TABRION DC-NA-1)

- **Priority:** P3 Low · **Status:** Open · **Refs:** TABRION DC-NA-1, `scripts/check_tabrion_source.py` (checksummed
  copy from nodestro-studio, `tests/test_tabrion_source_check.py`), `tests/test_tabrion_pin.py`, CI job
  `tabrion source (poisoned index)`
- **What:** the CI install runs under a poisoned index that serves `tabrion` and must get no request for it (the
  proof that the lock pin works); `tests/test_tabrion_pin.py` checks that the installed Tabrion's `direct_url.json`
  names the studio lock's commit (`NODESTRO_STUDIO_LOCK`), which only detects an accidental index install. The job is
  in `.github/ci-manifest.json` and reported by ci-ok. Open: the copy names an unmerged studio commit as its origin
  until nodestro-studio's DC-NS-2 branch is merged.
- **Next step:** after the studio merge, update the origin commit; close this entry.

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
| (in review, TABRION QA-7, [#142](https://github.com/peterkarlhuber/nodestro-api/pull/142)) | tabrion, axentra, orkexa, nodestro-runtime, tabrion-studio | TABRION QA-21 (AC-7), axentra #96 | `scripts/check_tabrion_api.py` (shared copy, SHA-256 pinned in `tests/test_tabrion_api_check.py`) refuses private Tabrion names and lists the used ones in `docs/api-consumers.md`; CI checks them with `--surface` against the snapshot of the Tabrion commit in the studio lock. Open: the copies in orkexa, nodestro-runtime and tabrion-studio (the source) still have the previous script version. Runtime edge: `db_log_redaction` (server profile, lazy import from `server.py`) imports `tabrion.errors` unguarded; Tabrion comes in through nodestro-runtime's base requirement (doc 13), and K-33 has no runtime edge nodestro-api -> tabrion (only `dev`), so declaring it needs a new edge in nodestro-studio `platform/dependency-policy.json` first |
| (done 2026-11-08) | tabrion, nodestro-runtime | TABRION QA-18 (AC-3) | `tests/test_optional_edges.py`: the desktop path (`nodestro_api.cli`, `local_container`, the app of `create_app` started and asked for `/api/health`) works with `axentra` blocked and with psycopg blocked, one test per edge with a mutation probe each; a runtime pin that makes either a base dependency fails it |
| (done 2026-11-14, TABRION QA-17) | nodestro-studio | ST-061 (dependency policy K-33) | `dependency-policy.json` is a copy of nodestro-api's edges (`--copy nodestro-api`), `scripts/check_dependency_policy.py` the unchanged checker; `tests/test_dependency_policy.py` checks both against `scripts/DEPENDENCY_POLICY_SOURCE.md` and the manifest against the copy. A K-33 change in nodestro-studio needs a new copy here; the CI `lint` step waits for the workflow review |
| (done, TABRION MIG-NOD-1) | nodestro-runtime, tabrion | runtime RT-074, TABRION VER-1 (RC-2) | `tests/postgres_support.py` runs the PostgreSQL skip/require rule on `tabrion.testing`; the `dev` extra requires `tabrion[testing,postgres]>=0.2,<0.3` (was `>=0.1,<0.2` with v0.1.0 final `d5821fa`; moved with the TABRION 0.2.1 switch); the exact commit (v0.2.2 final `5bc690e`, was v0.2.1 final `af0458d`) lives in the studio lock, which nodestro-studio's `check_versions.py` checks |
| (done, TABRION MIG-NOD-3) | nodestro-runtime, tabrion | runtime #163 (`error_texts.failure_text`), finding B-06-2 | The server's error texts and logs carry no database values: a failed `pg_dump` reports its exit code, a fixed reason and a SQLSTATE, never its output; a missing Axentra storage directory is named in the log, not in `storage.backup_failed`; `nodestro-server` and `create_server_app` install Tabrion's `DbErrorRedactionFilter` on the log record factory (`db_log_redaction.py`), so chained database errors are redacted after uvicorn's dictConfig too; canary tests in `tests/test_db_error_texts.py`. Needs nodestro-runtime at or after 9e0a73d (tabrion as a base dependency) in the studio lock. Exports name tabrion by its Git commit (`exporter._tabrion_git`, DC-1); the portable package builds tabrion from the hash-pinned source archive of `src/nodestro_api/portable_locks/tabrion.txt` (hatchling from `build.txt`, `--no-index`), whose commit must match the installed one; a lock bump that moves tabrion updates that file (`tests/test_portable_tabrion.py`) |
| (open, TABRION MIG-NOD-9) | nodestro-runtime, nodestro-studio, nodestro-desktop | runtime #176 (`repair_desktop_database_modes`, `fixable`), studio and desktop branch `claude/project-thread-bjlq25-nod9ui` | Recovery mode (`recovery.py`): the confirmed repair of the desktop database's permissions, exit codes 75 (restart) and 76 (restart into recovery mode) that the desktop shell handles. Needs runtime #176 merged and pinned in the studio lock; the studio shows the dialog and the diagnosis' button, the desktop shell restarts the backend. The confirmed repair passes the dry run's identities (`expected`), the runtime repairs all or nothing and the API restores the changed modes when its check after the repair fails. Open (low, review 2026-11-14): the busy check of the diagnosis' restart is not atomic with the shutdown (a run started in between is stopped like at any shutdown); `--reload` (development only) does not bind the exit request, so exit codes 75/76 are not passed on there |
| (accepted, low) | axentra | TABRION MIG-NOD-3 | `db_log_redaction.install_db_error_redaction` wraps the record factory only; unlike Axentra's install it does not replace `logging.Handler.handleError` (report of a failed `emit`, whose chain may hold a database error) or `Logger.makeRecord` (`extra=` with `db_sqlstate`/`db_constraint` raises `KeyError` once the filter set them). Nodestro passes no such `extra`; revisit if a log call fails to format with a database error in its chain |
| (done 2026-11-20, CI-FAST follow-up W0-1) | nodestro-studio, nodestro-sdk, nodestro-integrations | studio `platform/internal-server.txt`, same `scripts/studio_lock.sh` in sdk and integrations | A master push of a merge commit reads the lock of the studio branch of the merged PR's head branch while that branch is still ahead of the studio master (paired bump not merged yet, "lock lag"); otherwise the studio master's lock as before. Before, such pushes ran red against the old lock (14 of 23 master pushes from 2026-11-14 to 11-20). `tests/test_studio_lock.py` (curl stub, no network); the copies of the script stay identical |
| (in review 2026-11-20, Welle 1, Peter 12:12Z) | nodestro-sdk, nodestro-integrations, nodestro-runtime, nodestro-studio | `scripts/studio_lock.sh` (same copy in all four), `GH_TOKEN` on each lock step | The lock-lag compare request is authenticated with the workflow token (`permissions: contents: read`); before, unauthenticated requests hit the shared rate limit and silently fell back to the master lock (runtime 511ab38). A 404 (branch gone) reads master; any other answer reads master with a "Lock lag unknown" warning naming the HTTP status. `tests/test_studio_lock.py` covers token, anonymous, 404, 403/500/network |
| (in review, N-01, [#156](https://github.com/peterkarlhuber/nodestro-api/pull/156)) | nodestro-studio, embedding products | N-01 (product host), spike P0-9 | `nodestro_api.host.create_product_app` builds an app from named router groups (`router_groups`, one registry for the desktop, the server and product apps) with the studio's checks, guards, background services and fail-closed route check; `api.py` is split into groups along its sections. Host interface version 1, frozen by `tests/test_product_host.py`; see `docs/embedding.md`. The error-code catalog (`/api/error-codes`) stays one list for the package, not one per group |
| (in review, N-13) | nodestro-runtime, nodestro-studio, nodestro-sdk | runtime #198 (`nodestro_runtime.activity`, RT-084) | `GET /api/activity` (filters, keyset paging, `limit` at most 1000) and `GET /api/activity/export?format=jsonl\|csv` (complete, streamed download), permission `admin`, per tenant on a server; new router group `activity` (host interface version 2). The scheduler-leading worker prunes every active tenant hourly (`NODESTRO_ACTIVITY_RETENTION_DAYS`), which closes the server half of RT-084's prune item. Needs the runtime at or after a8e54a9 in the studio lock. Follow-ups: the SDK syncs the two operations from `openapi/nodestro-api.json`; the studio shows the log; more sources (API-072) |
| (in review, E-0R-1 A) | nodestro-runtime, nodestro-studio, nodestro-sdk | runtime #200 (merge a05eef4, RT-085) | Approval comments never reach a model; `agent_note` (at most 2,000 characters) is the only text of a decision later agents read. Both decision endpoints take it (`POST /api/runs/{run_id}/approvals/{node_id}`, `POST /api/published/{slug}/runs/{id}/approve`); `approval.agent_note_too_long` (`limit`) and `approval.agent_note_disabled` answer `422`. Pending approvals carry `allow_agent_note` (studio `GET /api/approvals`, published run status and `GET …/approvals`, app contract v1 `approvals.schema.json`); workflows pass `ApprovalConfig.allow_agent_note` through; new workflows from the architect and copies of library templates and parts save their approvals with `allow_agent_note: true` (the check warning is meant for workflows saved before the note existed). Canary tests in `tests/test_approval_agent_note.py`. Needs the runtime at or after a05eef4 in the studio lock. Follow-ups: the SDK syncs `agent_note`/`allow_agent_note` from `openapi/nodestro-api.json`; the studio shows the note field; nodestro-examples may set `allow_agent_note` in its templates itself; the embedded web interface (nodestro-app-ui, vendored) has no note field yet |
| (in review, N-10) | nodestro-runtime, nodestro-studio, nodestro-sdk, orkexa | runtime RT-093 (`nodestro_runtime.folders`, `folder_grants`, `folder_tools`), orkexa N-63 (`FOLDER_CASES`) | `/api/folders` behind `NODESTRO_FOLDER_GRANTS` (off; never on a server; `docs/folders.md`): a signed-in administrator grants a folder for reading, the writer is always the person, never an API key or a body field; `folder.*` codes answer `404`/`409`/`422`. `output_folder.py` reuses the path guard's folder lists. Open: the independent review and the decision to turn the switch on; the studio folders page (the texts of the codes and tools are in); grants for "this conversation" are not offered |
| (done, N-06; follow-up in review) | nodestro-runtime, nodestro-studio, nodestro-sdk | runtime #201 (merge 42ad387), #202 (merge 756b1a2), #203 (RT-086) | The access model v2 behind `NODESTRO_ACCESS_GRANTS` (off; `docs/access-grants.md`): `/api/access` (state for `read`; list, create and revoke grants and denies, `who-may` and `explain` for `admin`, per tenant), the writer is always the signed-in person (`Person`), never an API key or a body field; `access.*` codes answer `403`/`404`/`422` in the studio API and the published APIs, which never serve a `source`; every `connect_model` caller passes the person (benchmark judge unattended, model choice a probe). Needs the runtime at or after #203 in the studio lock. Merged after the independent review (#161, d818560). The review's lows: `expected` on tool grants (409 `access.tool_changed`, L-1) and agent connections in `who-may`/`explain` (`connections_considered`, L-3) are in review. Open: the decision to ever turn the switch on; the SDK syncs the routes from `openapi/nodestro-api.json`; the studio shows scope and duration on the approval card and an admin page of grants and denies |
| (in review, N-05) | nodestro-runtime, orkexa, nodestro-sdk, nodestro-studio | runtime #211, orkexa #112 (`orkexa.core.compaction`) | Conversation fields `history`, `summary_model` and the answer's `summary` in the contract; the SDK syncs its types from `openapi/nodestro-api.json`, and the studio conversation UI may offer the fields and show the summary |
| (in review, N-04) | nodestro-runtime, nodestro-sdk, nodestro-studio | runtime #204 (`nodestro_runtime.conversations`, RT-089) | `/api/conversations` behind `NODESTRO_CONVERSATIONS` (off; `docs/conversations.md`): every answer is a run of the engine, events in the run's format, owner-only on a server. Review fixes: answers' runs are the conversation owner's on every run route and list (H-1, `app_context`), deleted with it, and its grants revoked. Open: the studio UI (not part of N-04, runtime RT-089); the SDK syncs from `openapi/nodestro-api.json` |
| (in review, N-02 step 1) | nodestro-session, nodestro-studio | nodestro-session `docs/design.md`, TODO NS-002, NS-006 | `launch.py`, `local_access.py` and `handoff.py` use the shared library `nodestro-session` (session store with digests only, constant-time comparisons, cookie headers, log redaction, loopback rules, pipe handoff) without a contract change. Needs nodestro-session ([#1](https://github.com/peterkarlhuber/nodestro-session/pull/1)) in the studio lock and the K-33 edge `nodestro-api -> nodestro-session` (paired studio branch `claude/project-thread-a0oxjw`; `dependency-policy.json` here is its copy). `tests/test_session_library.py` repeats the session, cookie, bearer, launch, Host, Origin and proxy checks; they pass against the code before and after the change. Follow-ups, each a separate change behind a switch that is off by default (design.md 5.2): launch-secret TTL, CSRF header + JSON only + required Origin (studio frontend first), logout on the desktop, no terminal mode without a secret, token in the URL fragment, `bind_loopback` for the desktop backend |
| (in review, NS-002, Welle 3 P1) | nodestro-session, nodestro-studio, nodestro-desktop | nodestro-session TODO NS-002, design.md 5.2; studio branch `claude/w3-ns002` | The six hardening steps of the desktop launch, each behind its own switch, all off (`LaunchHardening`; README "Hardening switches of the desktop launch"): `NODESTRO_LAUNCH_TTL`, `NODESTRO_LAUNCH_CSRF` (Origin + JSON or form + `X-Nodestro-CSRF`), `NODESTRO_DESKTOP_LOGOUT`, `NODESTRO_TERMINAL_LAUNCH`, `NODESTRO_LAUNCH_FRAGMENT`, `NODESTRO_BIND_LOOPBACK`. Switches off: byte-identical answers against `tests/vectors/launch-switches-off.json` (recorded from 6783a52); switches on: attack cases in `tests/test_launch_hardening.py`. The studio frontend sends the CSRF value and offers the sign-out (studio branch of the same name, browser suite `launch`). Open before any switch is turned on: the desktop shell's counterparts (fragment link, TTL within its start, sign-out window, reading the address that `NODESTRO_BIND_LOOPBACK` prints) in nodestro-desktop; Nodestro Local Docker keeps its own rules (LD-010); the decision to turn each switch on is the owner's |
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
