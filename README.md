# Nodestro API

The HTTP backend of [Nodestro](https://github.com/peterkarlhuber/nodestro-studio) as a package of its own. It
provides

- the **studio REST API** under `/api` that the Nodestro Studio interface uses (workflows, runs, approvals,
  knowledge, tools, the Workflow Architect, the Nodestro Assistant, batches, test series, schedules, backups),
- the **published workflow APIs** under `/api/published/{slug}`, protected by Nodestro's own API keys,
- **Server-Sent Events** for runs, published runs, the Workflow Architect and the assistant,
- the **standalone export** of a published workflow as an independent FastAPI project.

Workflows are executed by [nodestro-runtime](https://github.com/peterkarlhuber/nodestro-runtime); this package never
runs a workflow itself. Agents are built on [orkexa](https://github.com/peterkarlhuber/orkexa). The platform overview
is [PLATFORM.md](https://github.com/peterkarlhuber/nodestro-studio/blob/master/docs/PLATFORM.md).

## Contents

| Area | Modules |
| --- | --- |
| Application | `app` (`create_app`), `context` (`AppConfig`; `Nodestro` extends the runtime `Workspace`), `cli` (`nodestro-api`), `local_access` (Host/Origin guard) |
| Studio REST API `/api` | `api`, `api_data`, `api_inputs`, `api_examples`, `api_python`, `api_architect`, `api_assistant`, `api_automation`, `api_guide`, `api_publish`, `api_pricing` (model prices, reconciliation), `api_mcp` (connected MCP services), `api_local_models` (local models and the semantic knowledge index), `model_readiness` (missing local models: refusal and readiness), `api_budget`, `api_run_chat`, `api_tool_approvals`, `api_notifications`, `api_reference` (the API reference page) |
| Published APIs `/api/published/{slug}` | `serving` (shared with exports), `publishing`, `release_gate` (opt-in release gate), `api_keys`, `network` (separate listener for the local network) |
| Export | `exporter` (ZIP with runtime, lock file, tests; orkexa from Git or bundled), `standalone`, `export_models`; portable packages: `portable_build`, `portable` (the launcher), `portable_secrets`, `webui_server` (see [docs/portable-export.md](docs/portable-export.md)) |
| Services | `pricing_setup` (price sync on desktop and server), `assistants`, `architect`, `assistant`, `assistant_context`, `docs_index`, `batches`, `benchmarks`, `schedules`, `backup`, `organize`, `search`, `diagnostics` |
| Contract | `openapi` and `openapi/nodestro-api.json` |

## Installation and start

The Nodestro components are pinned by commit (see `pyproject.toml`; the tested combination of all components is
`nodestro-studio/platform/versions.json`).

```bash
pip install "nodestro-api @ git+https://github.com/peterkarlhuber/nodestro-api.git@<commit>"
nodestro-api                       # http://localhost:8000/api, reference under /api/docs
```

Options: `--host`, `--port`, `--data-dir`, `--profile`, `--reload`, or the variables `NODESTRO_HOST`,
`NODESTRO_PORT`, `NODESTRO_DATA_DIR` and `NODESTRO_PROFILE`; a `.env` file in the current directory is read without
overriding the environment. The default is `127.0.0.1:8000` with the data directory `~/.nodestro`, which the API
shares with the studio. An empty `NODESTRO_DATA_DIR` means the default directory.

**Operating profile.** `--profile desktop` (the default) is the behavior described here: one person, one data
directory, the studio API on this computer only. `--profile server` is refused with `profile.invalid` before any
file is touched: a server is started with `nodestro-server` (see [The server](#the-server-nodestro-server)), never
with `nodestro-api`. An unknown profile name is refused with `profile.invalid`. `create_app` applies the same rules
as the command line.

The API needs neither the studio nor a built frontend. `NODESTRO_STATIC_DIR` names a built interface to serve next to
it, and `NODESTRO_DOCS_DIR` the documentation chapters the Nodestro Assistant searches (without it, the documentation
search returns no hits). Without provider credentials the API starts normally; runs, the architect and the assistant
then answer with a clear error (`api_key_missing`; published runs `503 model.unavailable`).

In Python:

```python
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig

app = create_app(AppConfig.from_env())  # a FastAPI application, e.g. for uvicorn
```

Integrations (such as the OCR of `nodestro-integrations`) are found through their entry point once they are
installed; `create_app(..., integrations=[...])` sets them explicitly.

## Two security models

**The studio API has no login yet**, so it is offered only on this computer. Every route declares the permission it
needs (`read`, `edit`, `run`, `admin` or `install`, see `nodestro_api.permissions`), and FastAPI checks it on every
request. `admin` covers a tenant's settings, keys and publishing; `install` covers the installation itself (backups
and restore, diagnostics, the network listener). In
the desktop profile the only principal is the local owner, who holds every permission, so nothing changes for the
studio; without a principal a route answers `401 access.login_required`, without the permission `403
access.forbidden`. The application refuses to start when a route declares no permission.

Every FastAPI application of this package checks this when it is built (`nodestro_api.permissions.check_routes`,
deny by default): the desktop studio, the server's studio, the network listener of published APIs and an exported
project. Deliberately public routes (`/health`, an export's API reference) declare `PUBLIC`; published endpoints are
authenticated by their keys (`KEY_AUTHENTICATED_ROUTES`); the routes of an export's web interface check their session
or token in the handler and are listed one by one with how they do it (`WEB_UI_ROUTES` in
`nodestro_api.webui_server`). A new route that fits none of these stops the application from being built, and
`tests/test_route_factories.py` finds every `FastAPI(...)` construction, so a new application needs the check too.

Because there is no login, the studio API cannot be exposed to a network: `--host 0.0.0.0`, `::`, any address
other than a loopback address, and `NODESTRO_ALLOWED_HOSTS` with names other than loopback names (or `*`) are
refused at start with `access.network_needs_login`, on the command line, through the environment and in
`create_app`. Requests carrying proxy headers (`Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Real-IP`) are
refused with `403 request.forwarded_not_allowed`, so a reverse proxy cannot pass network clients through. To keep
web pages in the user's browser from using it, it also

- answers only requests for `localhost`, loopback addresses and the address chosen with `--host` (protection
  against DNS rebinding; other host names get `403 request.host_not_allowed`),
- accepts changing requests and WebSockets only from its own origin (exactly the scheme, host and port the request
  was sent to) or without `Origin`; pages of other sites and of other programs on this computer (another port) get
  `403 request.origin_not_allowed`,
- answers no CORS requests. For frontend development, `NODESTRO_DEV_CORS=1` allows the studio's development server
  (`http://localhost:5173`, `http://127.0.0.1:5173`) to read answers and send changes; never set it otherwise (it is
  ignored in Nodestro Local Docker). `npm run dev` itself needs no flag: its proxy forwards `/api` with the
  development server's own origin.

`NODESTRO_ALLOWED_HOSTS` can add further names that point to this computer (comma-separated, for example
`studio.localhost`). Host and origin checks do not replace authentication; the studio API is reachable from a
network only as a server, with its login (`nodestro-server`).

**Desktop app: per-launch secret.** The desktop app starts the API with a random secret (at least 32 characters,
refused otherwise with `access.launch_secret_weak`; `--reload` is refused with it). It hands the secret over on a pipe,
never in the environment or on the command line: any process of the same OS user can read another process's initial
environment and arguments (`sysctl` `KERN_PROCARGS2` on macOS, `/proc/<pid>/environ` on Linux). The app passes
`--secrets-fd 0`, makes the backend's standard input a pipe, writes one JSON object
`{"launch_secret": "...", "vault_key": "..." | null}` and closes it. The backend reads it before anything else, replaces
its standard input by the null device, and keeps both values in memory only (`nodestro_api.handoff`); no process it
starts (uv, OCR, sandboxed tools, MCP servers) inherits them. A malformed handoff stops the start with
`access.handoff_invalid`. `--secrets-fd N` also takes another inherited descriptor (3 and higher, POSIX).
Operators who start the backend themselves can still set `NODESTRO_LAUNCH_SECRET` and `NODESTRO_VAULT_KEY`: the
backend takes both out of `os.environ` when it reads them, so its child processes do not inherit them, but they remain
visible in the backend's own initial environment; after a handoff both variables are ignored. The app's window opens
`/api/launch?token=<secret>` once; the answer sets a session cookie (`HttpOnly`, `SameSite=Strict`) and continues
to the studio from the backend's own origin (the token is redacted in the access log). A second launch is refused (`403 access.launch_used`), and every other request without that cookie gets
`401 access.launch_required`, except `/api/health` and the key-protected `/api/published` routes. This keeps other
programs and users of the same computer and web pages in the browser out of the desktop's studio API. It is not a
user login and does not lift the loopback-only rule. Without the variable (the `nodestro` command) nothing changes.

**Server logins.** `nodestro_api.server_app.create_tenant_app` builds the studio API for many tenants with logins;
`nodestro-server api` starts it (see [The server](#the-server-nodestro-server)). What it does
(`nodestro_api.identity`, needs `pip install "nodestro-api[server]"`):

- Login with OpenID Connect at the operator's identity provider: authorization code flow with PKCE (`S256`),
  `state` (stored hashed, used once, expires after 10 minutes) and `nonce`. The ID token must be signed with an
  asymmetric key of the provider (never `none` or a shared secret) and match issuer, audience, expiry and nonce.
- Every tenant has an access audit trail (`GET /api/access-audit`, permission `admin`, read only, newest first):
  logins, tenant switches (refused ones too), logouts, API key changes and the operator's commands (`create-tenant`,
  `set-role`, `remove-member`, `revoke-sessions`, `tenant suspend|resume`, actor `operator`). It holds ids and codes,
  never a secret; with `NODESTRO_AXENTRA_AUDIT=1` each event is mirrored into Axentra's audit log in the same
  transaction (`nodestro.access.<kind>`). The desktop records its API key changes the same way.
- A tenant's administrators manage its members (`GET /api/members`, `PUT /api/members/{user_id}/role`,
  `DELETE /api/members/{user_id}`, permission `admin`, server only): only within their own tenant, only an owner
  gives, takes or changes the owner role, the last owner stays (`409 identity.last_owner`), and every change is in
  the access audit trail with the administrator as actor. Invitations do not exist yet (setup link or `set-role`).
- One address starts at most 60 logins a minute per API process (`NODESTRO_LOGIN_STARTS_PER_MINUTE`, `0` turns it
  off; then `429 identity.rate_limited`), and oversized setup tokens, states and codes are refused. Behind a proxy
  the address is the proxy's unless uvicorn trusts it (`FORWARDED_ALLOW_IPS`); a proxy rate limit stays advisable.
- A login alone grants nothing: a user needs a role in a tenant (`owner`, `admin`, `editor`, `operator`,
  `viewer`). The first owner of a tenant logs in with a one-time setup link (`/api/auth/login?setup=<token>`) that
  an operator creates; there is no default administrator and no password.
- The session is a random token in a `__Host-` cookie (`HttpOnly`, `Secure`, `SameSite=Lax`); the database keeps
  only its hash. It ends after 12 hours, after 2 hours without requests, at logout, and at once when the membership
  is removed. Roles map to permissions: `viewer` reads, `operator` also runs, `editor` also edits, `admin` and
  `owner` also administer the tenant; no role gets `install`.
- Every request works on the context of its session's tenant: one context per tenant on that tenant's PostgreSQL
  stores and its own file area. Resources of other tenants answer `404`, as if they did not exist.
- Changing requests must carry the studio's own `Origin` (`403 access.cross_site` otherwise); this is protection
  against cross-site requests, not a replacement for the login.
- With a job queue (`TenantComposition.queue`) the API processes execute no runs: they store and queue them.
  Worker processes (`nodestro_api.worker.ServerWorker`) claim the jobs with a lease they keep renewing, execute the
  runs and write their events to the database, where every API process streams them from. A worker that dies
  leaves its run `run.interrupted` (found by the reaper of another worker); it is never executed again by itself.
  The worker that holds the scheduler lock fires the tenants' schedules, and each slot fires once.
- A tenant's provider keys are stored encrypted with the server's secret key (per-tenant key, AES-256-GCM); the
  server's own environment variables such as `ANTHROPIC_API_KEY` are never used for any tenant.
- Not part of it yet: published workflow APIs with tenant-bound keys, the network listener and backups in the studio
  (operators use `pg_dump`). Batches, test series, assistant drafts and Python environment installations still run
  in the API process; a batch or test series reads its runs, which workers execute, from the database every two
  seconds to learn that they ended. The API process that drives a batch or test series holds a lease of 60 seconds
  and renews it every few seconds; when that process stops, the next read by any API process (after the lease ran
  out) marks it `interrupted` (`batch.interrupted`, `benchmark.interrupted`), and a person continues it there. A
  process that finds another owner on its batch stops driving it (`409 lease.lost` for a request).

Uploaded files are served with `X-Content-Type-Options: nosniff`; HTML, SVG and XML files are always downloaded, and
everything except PDF carries a restrictive `Content-Security-Policy`, so an uploaded page cannot run scripts in the
studio's origin.

**Security headers** (`nodestro_api.security_headers`, desktop, Local Docker and server alike). Every answer that does
not set its own carries `Content-Security-Policy: default-src 'self'; script-src 'self' 'sha256-…'; style-src 'self'
'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none';
frame-ancestors 'none'` (the hash allows the studio page's one inline theme script; CodeMirror, React Flow and
Swagger UI insert inline styles), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
`Referrer-Policy: no-referrer`; `Strict-Transport-Security: max-age=31536000` only over HTTPS (on a server whose
origin is `https://` always, also behind a TLS proxy). The API reference (`/api/docs`) loads a vendored Swagger UI
(`swagger-ui-dist` 5.34.0, `nodestro_api/swagger_ui/VENDORED.md`) and nothing from other sites.

**Request size limits** (`nodestro_api.body_limit`, every profile). Request bodies are counted while they are read
(a missing or wrong `Content-Length` changes nothing) and refused with `413 request.too_large` (`params.limit_mb`)
above the route's limit. Set in bytes through the environment:

| Variable | Default | Routes |
| --- | --- | --- |
| `NODESTRO_MAX_REQUEST_BYTES` | 2 MiB | every route not listed below |
| `NODESTRO_MAX_LARGE_REQUEST_BYTES` | 16 MiB | workflow import, batch plan and start, creating and changing a test series |
| `NODESTRO_MAX_UPLOAD_BYTES` | 256 MiB | knowledge and input file uploads, batch files |
| `NODESTRO_MAX_BACKUP_BYTES` | 2 GiB | uploading a backup (desktop) |

Trace import keeps its 50 MB limit (`trace.too_large`), the studio's test of a published API and the published APIs
themselves their 80 MB (published APIs answer in their own error format). Keep a reverse proxy's limit
(`client_max_body_size`) at or above the largest one you need.

**Published workflow APIs use API keys** (`Authorization: Bearer ndk_<id>_<secret>` or `X-API-Key`). A key is shown
once when it is created or rotated; only a SHA-256 hash is stored. Each key names the publications it may use and
its permissions:

| Scope | Allows |
| --- | --- |
| `run` | start, cancel and continue runs |
| `read` | the API description, run status, results, events, files, usage |
| `approve` | decide approvals |

A key never reaches other publications (they answer `404` as if they did not exist), runs of other publications or
the studio API. Revoking a key takes effect at once; rotating creates a new key with the same name, permissions and
publications and revokes the old one in the same step. Failed authentications are limited per client address; those
of a well-formed key are counted per key id instead (its first 8 characters, never the secret), with a ten times
higher limit per address, so one client's wrong key does not lock out other keys sent through the same reverse proxy.

## The server (`nodestro-server`)

`pip install "nodestro-api[server]"` adds the command `nodestro-server`, which runs the processes of the server
profile. Its configuration comes only from the environment and is checked before anything starts; every missing or
unsafe value is listed at once (`profile.invalid`), and nothing has a default that would weaken it:

| Variable | Meaning |
| --- | --- |
| `NODESTRO_PROFILE` | `server` |
| `NODESTRO_DATA_DIR` | the tenants' files (`<dir>/tenants/<tenant>`) |
| `NODESTRO_DATABASE_URL` (or `_FILE`) | PostgreSQL connection string; `NODESTRO_DATABASE_SCHEMA` optionally names a schema |
| `NODESTRO_PUBLIC_ORIGIN` | the studio's origin as browsers see it, `https://…` (`http://localhost…` for development) |
| `NODESTRO_OIDC_ISSUER`, `NODESTRO_OIDC_CLIENT_ID`, `NODESTRO_OIDC_CLIENT_SECRET` (or `_FILE`) | the identity provider; the redirect URI to register is `<origin>/api/auth/callback` |
| `NODESTRO_SECRET_KEY` (or `_FILE`) | 32 random bytes, base64 (`openssl rand -base64 32`): encrypts tenant secrets |
| `NODESTRO_STATIC_DIR`, `NODESTRO_DOCS_DIR` | the built studio interface and its documentation (optional) |
| `DOCKER_HOST` (optional), `NODESTRO_DOCKER` | the container runtime for tool code (local socket by default) |
| `NODESTRO_MODEL_ENDPOINTS` | comma-separated origins tenants may use for OpenAI-compatible, Ollama and vLLM endpoints (none by default; others are refused, so tenants cannot make the server reach internal services) |
| `NODESTRO_BEDROCK_TENANT_ROLES` (or `_FILE`) | Amazon Bedrock per tenant: JSON `{"<tenant id>": {"role_arn": "arn:aws:iam::<account>:role/<name>", "external_id": "…"}}`. The API and worker processes assume a tenant's role with STS (the server's own AWS credentials as base, `ExternalId`, `RoleSessionName=nodestro-<tenant>`, regional STS endpoint, 30 minutes); the server's own AWS chain is never used for a tenant, and a tenant sees only `operator_role: true`. A tenant without an entry cannot use Bedrock (`provider.bedrock_not_available`) |
| `NODESTRO_BEDROCK_DATA_RESIDENCY`, `NODESTRO_BEDROCK_ALLOWED_REGIONS` | `eu_only` forces EU-only data residency for every tenant (they cannot relax it); a comma-separated list limits the regions tenants may choose (with `eu_only` only EU regions) |
| `NODESTRO_WORKER_LEASE_SECONDS`, `…_POLL_SECONDS`, `…_REAP_SECONDS` | worker timing (defaults 60, 1, 15) |
| `NODESTRO_WORKER_MAX_RUNS`, `NODESTRO_WORKER_MAX_WAITING` | runs one worker holds at once, and how many of them may wait for a person's tool approval (defaults 2 and 1; waiting must stay below the runs, so one slot always executes) |
| `NODESTRO_MAX_RUN_BUDGET_USD` | The highest limit a budget decision may set for one run (BUDGET-1), on top of the tenant setting `max_run_budget_usd`; the lower one applies. Empty: no operator maximum |
| `NODESTRO_TOOL_APPROVALS` | `1` asks a person before tool calls that change or reach out. Off by default. Order: first `POST /api/tool-approvals/legacy` (once; stamps existing bindings of workflows, versions and published releases, whose digest changes once), review the report, then set this and restart, only after the security acceptance |
| `NODESTRO_PRICE_SYNC`, `NODESTRO_PRICE_LITELLM`, `NODESTRO_PRICE_SNAPSHOT_FILE` | reference price sync of the workers (`on`/`off`, default `on`; LiteLLM's cost map `on`/`off`; a reviewed Orkexa snapshot file), see "Model prices" |
| `NODESTRO_LOG_FORMAT` | `json` writes one JSON object per log line (`time`, `level`, `logger`, `message`, `request_id`, `exception`) for `api` and `worker`; default plain text. Every API answer carries `X-Request-ID` (a proxy's short id is kept), and its log lines name it. `GET /api/ready` (no login) answers 503 while the process starts, stops or cannot reach its database, for a load balancer; `/api/health` stays the liveness check |
| `NODESTRO_LOGIN_STARTS_PER_MINUTE` | login starts per address and minute in one API process (default 60, `0` off) |
| `NODESTRO_BEHIND_PROXY`, `FORWARDED_ALLOW_IPS`, `NODESTRO_REQUIRE_CLIENT_ADDRESS` | behind a reverse proxy: `NODESTRO_BEHIND_PROXY=1` states that there is one, and uvicorn's `FORWARDED_ALLOW_IPS` must then name the proxy's address(es), so the API takes the client address from `X-Forwarded-For` (only from those addresses). Without it every client has the proxy's address and shares its per-address limits (login starts, failed API keys). `check` prints `warning: proxy.client_address_unknown` and `api` logs it; with `NODESTRO_REQUIRE_CLIENT_ADDRESS=1` both refuse instead |

Commands:

- `nodestro-server check`: settings, database (reachable, schema current, runtime role), Docker and the identity
  provider (its discovery document at `NODESTRO_OIDC_ISSUER` answers, names that issuer and https endpoints, else
  `identity.provider`); exit code 0 when ready. A superuser or `BYPASSRLS` runtime role is refused
  (`storage.role_unsafe`) with Axentra or a separate migration role and printed as `warning:` otherwise (row-level
  security does not protect the tenants then). Entries of `NODESTRO_BEDROCK_TENANT_ROLES` for tenants that do not
  exist are printed as `warning: bedrock.tenant_unknown` with their tenant ids (a typo leaves a tenant without Bedrock).
- `nodestro-server migrate`: applies the database migrations under an advisory lock. A database that already has
  tables needs a backup first: `--backup-dir DIR` runs `pg_dump` (custom format; the password is passed in the
  environment, never on the command line), `--backup-taken` states that the operator made one.
- `nodestro-server backup DIR`: writes a `pg_dump` (custom format) of the database to `DIR/nodestro-<time>.dump`
  and prints its path; restore it with `pg_restore` (the runbook in the `nodestro-server` repository).
- `nodestro-server api [--host 0.0.0.0] [--port 8080]`: the studio with logins. Runs are queued for workers. Put a
  TLS-terminating reverse proxy in front of it; the session cookie is `Secure`.
- `nodestro-server worker`: executes queued runs, reaps runs of dead workers and fires schedules. SIGTERM lets a
  running run finish first.
- `nodestro-server create-tenant ID NAME` and `nodestro-server setup-link TENANT`: create a tenant and print a
  one-time link (24 hours) that makes the person who logs in with it the tenant's first owner. The id
  `nodestro-price-reference` is reserved (the server's reference prices).
- `nodestro-server pricing status|sync|approve VERSION|reject VERSION [--note …]`: the reference prices: their state
  and open reviews, a sync now, and the decision on a review.
- Members and tenants (`members`, `set-role`, `remove-member`, `revoke-sessions`, `suspend-tenant`, `resume-tenant`),
  tenant secrets (`secrets reencrypt`), Axentra platform services (`axentra`), input file storage (`files`),
  owners of assistant conversations from before the owner field (`assistant backfill-owners [--dry-run]`: the owner
  their files name; conversations without files or with files of several people stay visible to the whole tenant)
  and database roles (`db`): `nodestro-server <command> --help` shows the arguments, and the runbook in the
  `nodestro-server` repository (`docs/RUNBOOK.md`) describes when to use them.

API and worker processes start only when the database schema is current and Docker is reachable: on a server tool
code runs only in containers (ADR-0006), so a server without a container runtime does not start
(`sandbox.unavailable`). Containers need no bind mounts, so Docker may run on another host (`DOCKER_HOST`), which
keeps the Docker daemon's privileges away from the API and worker containers. Images and the development
deployment are in the `nodestro-server` repository.

## Missing local models

An interactive action whose local model is not ready is refused with **409 `model.local_unavailable`** before
anything is generated or stored; `params` carry `model`, `cause`, `problem` (the runtime's `ModelProblem`: cause,
location, mode, allowed actions, sizes, running or stopped download) and `alternatives` (ready models that can do the
step). `nodestro_api.model_readiness` applies this to the Workflow Architect (`POST /api/architect/jobs`, needs
structured output), the assistant (send and retry; nothing is stored when refused), agent design and the tool jobs,
the agent test (its tools), a studio run (every local model of the workflow), and the knowledge index rebuild (the
embedding model). Install permission (`install`; never on the server profile, where `blocked` is `operator`) decides
whether `download` and `setup_server` are offered.

- `POST /api/local-models/readiness` (`run`): `{model, action, capabilities}` gives the same answer (`ready`,
  `problem`, `alternatives`); the studio asks it again after a download or setup.
- `POST /api/local-models/pull` takes `attach: true`: a running download of the same model is returned instead of
  `local.job_running`.
- The Workflow Architect (`ArchitectRequest.model`) and the assistant (`SendRequest.model`, `RetryRequest.model`)
  take a model for this one request; it must be one of the alternatives (`409 model.choice_invalid`) and is never
  saved in the settings.
- Unattended starts (schedules, batches, published APIs) are not asked: their check reports
  `check.local_model_missing` inside `workflow.blocked`, and nothing is downloaded.

## Model prices

Orkexa resolves prices and calculates every cost; Nodestro keeps the prices, syncs and validates them and shows
them (`/api/pricing`, permission `read`; changes need `admin`):

| Route | |
| --- | --- |
| `GET /api/pricing` | every effective price: rates, status (`current`, `stale`, `unknown`, `pending_review`, `custom`), quality, source, check date, version |
| `GET /api/pricing/status`, `GET /api/pricing/history` | active version, last check and error, staleness, open reviews; the price history |
| `POST /api/pricing/sync` | check the prices now (desktop) |
| `GET /api/pricing/reviews`, `POST …/reviews/{version}/approve` / `reject` | changes held back (over 20 % without official confirmation, removals) |
| `PUT` / `DELETE /api/pricing/overrides/{provider}/{model}` | an individual price (contract price or correction); it wins over the reference price |
| `GET` / `PUT` / `DELETE /api/pricing/billing-keys/{provider}` | admin keys of the providers' billing APIs (stored encrypted, servers only) |
| `GET` / `POST /api/pricing/reconciliation` | estimates against the providers' bills (OpenAI Costs API, Anthropic Cost API) |

On the desktop the API process checks the prices at start when due and then daily (`NODESTRO_PRICE_SYNC`,
`NODESTRO_PRICE_LITELLM`, `NODESTRO_PRICE_SNAPSHOT_FILE`). On a server the workers do, one at a time under an
advisory lock; tenants read the server's reference prices and keep their own overrides, and `POST /sync` and
review decisions answer 403 for them. Offline the last valid prices stay; prices without a check for 48 hours are
`stale`, after seven days critical. The desktop never stores billing admin keys (it has no encrypted store): set
`NODESTRO_OPENAI_ADMIN_KEY` / `NODESTRO_ANTHROPIC_ADMIN_KEY` or send the key with one `POST /reconciliation`.
`GET /api/usage` separates known, partial and unknown costs (`cost_state`) and names the price versions, sources
and the lowest quality used.

## Connected MCP services

Administrators connect remote MCP servers (Streamable HTTP) and approve their tools one by one (ADR-0007 in the
studio repository; the client lives in the runtime, `nodestro_runtime.mcp`). Routes under `/api/mcp`; listing needs
`read`, everything else `admin`:

| Route | |
| --- | --- |
| `GET /api/mcp/status` | SDK available, token vault present, the operator's endpoint list, local servers possible, unattended use enabled, tools that change data enabled |
| `GET` / `POST /api/mcp/servers`, `GET` / `PUT` / `DELETE /api/mcp/servers/{id}` | servers; the token is write-only (`token_set` only, `""` removes it, a new address drops it) |
| `POST /api/mcp/servers/{id}/discover` | connect, negotiate the protocol version, list the tools; new and changed definitions wait for a decision |
| `GET /api/mcp/servers/{id}/tools` | tools with their definition, the approved definition, hash, status, classification and why a tool is not usable |
| `POST /api/mcp/servers/{id}/tools/{name}/decision` | approve with a classification (`read`, `write`, `destructive`) and the reviewed `expected_hash`, or reject |
| `GET /api/mcp/events` | administration and policy events (no secrets, no tool arguments) |
| `GET /api/mcp/stats` | calls in flight, the operator's limits (`NODESTRO_MCP_*`) and each server's health since the start: calls, failures, circuit breaker, last error code |

Approved tools appear in `GET /api/tools` with `kind: "mcp"`. Until per-call approval exists only `read` tools are
usable. Tokens are sealed with the server's secret key (per tenant) or, on the desktop, with the key from the operating
system's keychain, which the desktop app hands over on `--secrets-fd` (an operator can set `NODESTRO_VAULT_KEY`); without either they are kept for the session only. Servers need the operator's
list `NODESTRO_MCP_ENDPOINTS`; unattended runs need `NODESTRO_MCP_UNATTENDED=1` (after the security acceptance).

## Published workflow APIs

A **publication** is the API of one workflow; a **release** is an explicitly published, saved version, frozen with
its sub-workflow versions, tool code and approved Python libraries. Saving the workflow again never changes a
release; publishing again creates the next one, and any release can be made active again (rollback).
`/versions/{n}/runs` calls a release directly. Knowledge files are read as they are at run time, and persistent
memory is shared with the studio; the publish check says so.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/published/{slug}` | input and output schema, example request, limits, versions |
| `POST /api/published/{slug}/runs` | start a run (JSON, or `multipart/form-data` with files); `202` with the status |
| `GET .../runs`, `GET .../runs/{id}` | latest runs, status of one run |
| `GET .../runs/{id}/result` | the result of a finished run (`409` while it runs) |
| `GET .../runs/{id}/events` | progress as Server-Sent Events |
| `GET/POST .../runs/{id}/approvals`, `/approve` | waiting approvals and decisions |
| `POST .../runs/{id}/cancel`, `/resume` | cancel, or continue a failed or cancelled run |
| `GET .../runs/{id}/files[/{file}]` | result files |
| `GET .../usage`, `GET .../openapi.json` | runs, tokens and cost; the OpenAPI description of this API |

- **Errors** have the form `{"error": {"status", "code", "message", "params"}}` with a stable `code`
  (the studio API uses `{"detail", "code", "params"}`). Messages never contain keys or credentials. A run or start
  that failed because a local model is not installed (`run.local_model_missing`, `check.local_model_missing`)
  carries `params.hint`, a short sentence for the operator (no model name or address of the installation).
- **Limits** per publication: parallel runs, waiting runs, runs and requests per minute and key, a daily cost limit
  over the runs of the last 24 hours, a cost limit per run, the upload size per file (default 10 MB) and 80 MB per
  request. Exceeded limits answer `429` with `Retry-After`; a request that is too large answers `413`, also when it
  is sent without `Content-Length`.
- **Idempotency:** a request with `Idempotency-Key` that is repeated with the same body within 24 hours returns the
  run it started (`200`, `Idempotent-Replayed: true`) instead of starting another one; the same key with a different
  body answers `409 idempotency.conflict`. Keys are separate per publication and API key. The key is reserved with one
  atomic insert in the store before the run starts, so concurrent retries never start two runs, also not on several
  server processes sharing one PostgreSQL database; a retry that arrives while the first request is still starting
  its run answers `409 idempotency.in_progress` with `Retry-After: 1`.
- **Events:** the stream starts with a `status` event, then sends progress events until `end`. After a reconnect
  with `Last-Event-ID`, the events since then are sent again, with increasing ids. `?text=true` adds the agents' text
  as it is written.
- **Synchronous calls** (`?wait=<seconds>`) are available only when enabled for the publication.
- **Costs** are estimates from the providers' token counts; requests of models without a known price are reported
  as unpriced instead of free.

The published endpoints answer only this computer on the studio's port. **Network access** is a separate listener
(`/api/api-network`, default port 8100) that serves the published endpoints of publications shared with the network,
plus `/health`, and nothing else. Without a TLS certificate and key it speaks plain HTTP, so keys would travel
unencrypted; the studio says so before enabling it. When a new listener configuration cannot start (port taken,
certificate unusable), the previous listener keeps running and the status reports the error. After a backup is
restored, network access is off.

**Nodestro as an MCP server** (ADR-0007 decision D-8, WP-11) is off by default and needs two deliberate switches:
the operator starts Nodestro with `NODESTRO_MCP_SERVER=1`, and an administrator offers a publication with
`PATCH /api/publications/{id}` `{"mcp_tool": true}` (recorded in its history as `mcp_tool_on`/`mcp_tool_off`). Then
`/api/published/_mcp` answers the MCP protocol (Streamable HTTP, stateless, JSON answers) for clients on this computer
only. An MCP client authenticates with an API key (`Authorization: Bearer`); it sees the offered, enabled publications
of that key as tools (name = slug, input schema = the request schema) when the key has the `run` scope. A tool call is
an ordinary run of the published API through the same code path: limits, budgets, approvals, the release's frozen
tools and the run history apply unchanged. With synchronous calls enabled the call waits up to the API's sync timeout
and answers with the output; otherwise, or for a run that stops at an approval step, it answers with the run id and
status. While switched off the endpoint answers `404 mcp_server.disabled`.

## Standalone export

A release can be exported as a FastAPI project that runs without Nodestro: the release snapshot, the runtime and the
API layer (copied unchanged, listed with versions and SHA-256 checksums in `RUNTIME.json`), exact dependency
versions (`requirements.lock`), key tooling (`python -m app.keys`), examples, a Dockerfile and its own tests with a
simulated model. orkexa is installed from its repository at the pinned commit, or bundled as source code
(`vendor/orkexa`, only after confirming its redistribution). The project contains no provider or API keys; keys are
configured as hashes in `API_KEYS`. The README of the project is written in the export language (`de`, `en`, `es`,
`fr`, `it` or `pt-BR`; templates in `nodestro_api/readme/`).

`POST /api/publications/{id}/exports` builds this source project synchronously (`kind: "standard"`, the default);
`POST /api/publications/{id}/export/check` lists the problems first (errors block, warnings and notes do not), and
`GET /api/exports/{id}/download` returns the ZIP file. A **portable export** (`kind: "portable"`) is a ready-to-run
package for one platform with its own Python, every library, start scripts and optionally the local models and Ollama;
it is built as a background job (`POST /api/publications/{id}/export-jobs`, `GET /api/export-jobs/{id}`). See
[docs/portable-export.md](docs/portable-export.md).

`GET /api/publication-overview` (read permission, like the other publication reads) lists every publication of the
installation (on a server: of the tenant) with its workflow's current name, the active release's workflow version and
date, and its export records, newest first: the studio's *Publications* page reads it in one request. It only reads
the stored publications, releases and export records.

Amazon Bedrock in an export: the manifest keeps only the region and the data residency (`settings.bedrock`; an
`eu_only` stays in force), never an AWS profile name, a role or a key. The exported API signs in with the AWS default
credential chain of the machine it runs on; `AWS_REGION` (or `AWS_DEFAULT_REGION`) overrides the exported region,
and without any region a run answers 503 (`provider.region_missing`). The export check refuses Bedrock settings
without a region (`export.region_missing`) and warns that an AWS profile (`export.aws_profile_not_exported`) or a
server's tenant role (`export.aws_role_not_exported`) does not go along.

## Backups

`GET /api/backup` creates a ZIP file with a consistent SQLite snapshot, the tools and the input files of runs, and a
manifest with SHA-256 checksums. Provider keys, base URLs with credentials in them, API keys and idempotency records
are never part of a backup and stay on this computer when one is restored. A restore checks the file completely
first (paths, checksums, database integrity, every stored document, and whether it fits on the disk), saves the
current state as a safety backup, copies the tool and input files next to the current ones, replaces all tables in
one transaction and only then swaps the directories. One restore runs at a time, and none while runs are executing.

## The contract

`openapi/nodestro-api.json` is the OpenAPI document of exactly this API. A test fails while the file does not match
the code; after changing routes or schemas, `python -m nodestro_api.openapi openapi/nodestro-api.json` writes it
again. The SDKs in [nodestro-sdk](https://github.com/peterkarlhuber/nodestro-sdk) are generated from it and tested
against a running server. `openapi/nodestro-api-server.json` is the document of the server profile (logins and
tenant switching added, no published APIs yet); `python -m nodestro_api.openapi --server
openapi/nodestro-api-server.json` writes it, and a test checks it the same way.

Error answers of the studio API have the form `{"detail", "code", "params"}`; clients evaluate `code` and
`params`, never `detail` (an English technical text). `detail` names no file system path, input value, credential
or library internals: an exception without an error code of its own (`bad_request`, a `not_found` for a key the
request did not name, `provider_api`, `agent.<error>` except `agent.RetrievalError`) answers with a fixed text and
`params.request_id`, and its original text is logged (logger `nodestro_api.errors`, credentials redacted) with the
same request id; on a server it is the `X-Request-ID` of the answer. Texts of codes the API or the runtime raise on
purpose keep their wording, with absolute paths replaced by `[path]` and credentials redacted. Validation errors
(`request.invalid`) list `loc`, `msg` and `type`, and a message that repeats the input shows `[input]` instead.

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

The tests run against the real application with scripted provider clients (`nodestro_runtime.testing`) and never
call a paid provider. They cover the REST API, published APIs, keys, approvals, continued runs, batches, schedules,
files, Python libraries, backups, the network listener, the Host/Origin guard and the export; the exported project
passes its own tests. `NODESTRO_NETWORK_TESTS=1` enables the test that installs a real package from PyPI. CI runs on
Python 3.11 and 3.13 and fails when the skipped tests differ from `tests/ci/skipped-<job>.txt` (every expected skip
with its reason; `python scripts/check_skips.py junit.xml tests/ci/skipped-test-3.13.txt` after
`pytest --junitxml=junit.xml`), builds the wheel and the source distribution, installs the wheel into a fresh
environment and calls the started server over HTTP. It needs no secret: orkexa and the Nodestro repositories are public and are
installed anonymously.

Open work (known bugs, follow-ups and planned improvements) is listed in [docs/TODO.md](docs/TODO.md).

## Limitations

- One process per data directory: limits and key changes are synchronized within the process, not between several
  server processes (Idempotency-Key reservations are atomic in the store). The runtime enforces it with a lock file
  (`nodestro.lock`): a second `nodestro` or `nodestro-api` on the same data directory stops at start. Do not run the app with several uvicorn workers.
- The interactive reference `/api/docs` serves a vendored copy of Swagger UI (`nodestro_api/swagger_ui`, version and
  checksums in its `VENDORED.md`) and works offline; the OpenAPI document itself is `/api/openapi.json`.
- Without network access enabled, the published endpoints answer only this computer; with it, TLS is the
  operator's responsibility (certificate, key, or a reverse proxy).
- The desktop's studio API has no login and only listens on loopback addresses; the published workflow APIs (with
  API keys) are the way to reach a desktop from a network. A server (`nodestro-server`) has logins.
- Proxy headers are detected by name; a proxy that forwards requests without any of them looks like a local client.
- Nodestro provides technical means for traceability (immutable releases, recorded approvals, usage records,
  sanitized exports) and data minimization (secrets stay out of backups and exports). It does not by itself make an
  installation compliant with the GDPR, the EU AI Act or ISO/IEC 42001; these obligations remain with the operator.

## Provenance and license

Extracted from `nodestro` 0.1 (15e2076), see [PROVENANCE.md](PROVENANCE.md) and [CHANGELOG.md](CHANGELOG.md).
MIT, see [LICENSE](LICENSE).
