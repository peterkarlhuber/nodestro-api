# Changelog

## Unreleased

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
