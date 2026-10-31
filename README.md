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
| Studio REST API `/api` | `api`, `api_data`, `api_inputs`, `api_examples`, `api_python`, `api_architect`, `api_assistant`, `api_automation`, `api_guide`, `api_publish`, `api_pricing` (model prices, reconciliation) |
| Published APIs `/api/published/{slug}` | `serving` (shared with exports), `publishing`, `release_gate` (opt-in release gate), `api_keys`, `network` (separate listener for the local network) |
| Export | `exporter` (ZIP with runtime, lock file, tests; orkexa from Git or bundled), `standalone` |
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

Because there is no login, the studio API cannot be exposed to a network: `--host 0.0.0.0`, `::`, any address
other than a loopback address, and `NODESTRO_ALLOWED_HOSTS` with names other than loopback names (or `*`) are
refused at start with `access.network_needs_login`, on the command line, through the environment and in
`create_app`. Requests carrying proxy headers (`Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Real-IP`) are
refused with `403 request.forwarded_not_allowed`, so a reverse proxy cannot pass network clients through. To keep
web pages in the user's browser from using it, it also

- answers only requests for `localhost`, loopback addresses and the address chosen with `--host` (protection
  against DNS rebinding; other host names get `403 request.host_not_allowed`),
- accepts changing requests only from its own pages, from this computer's origins, or without `Origin`
  (cross-site requests get `403 request.origin_not_allowed`),
- allows CORS only for the studio's development server (`localhost:5173`).

`NODESTRO_ALLOWED_HOSTS` can add further names that point to this computer (comma-separated, for example
`studio.localhost`). Host and origin checks do not replace authentication; the studio API is reachable from a
network only as a server, with its login (`nodestro-server`).

**Desktop app: per-launch secret.** The desktop app starts the API with a random secret in `NODESTRO_LAUNCH_SECRET`
(at least 32 characters, refused otherwise with `access.launch_secret_weak`; `--reload` is refused with it). The
backend removes the variable from its environment, so no process it starts inherits it. The app's window opens
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
  in the API process.

Uploaded files are served with `X-Content-Type-Options: nosniff`; HTML, SVG and XML files are always downloaded, and
everything except PDF carries a restrictive `Content-Security-Policy`, so an uploaded page cannot run scripts in the
studio's origin.

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
publications and revokes the old one in the same step. Failed authentications are limited per client address.

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
| `NODESTRO_WORKER_LEASE_SECONDS`, `…_POLL_SECONDS`, `…_REAP_SECONDS` | worker timing (defaults 60, 1, 15) |
| `NODESTRO_PRICE_SYNC`, `NODESTRO_PRICE_LITELLM`, `NODESTRO_PRICE_SNAPSHOT_FILE` | reference price sync of the workers (`on`/`off`, default `on`; LiteLLM's cost map `on`/`off`; a reviewed Orkexa snapshot file), see "Model prices" |

Commands:

- `nodestro-server check`: settings, database (reachable, schema current) and Docker; exit code 0 when ready.
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

API and worker processes start only when the database schema is current and Docker is reachable: on a server tool
code runs only in containers (ADR-0006), so a server without a container runtime does not start
(`sandbox.unavailable`). Containers need no bind mounts, so Docker may run on another host (`DOCKER_HOST`), which
keeps the Docker daemon's privileges away from the API and worker containers. Images and the development
deployment are in the `nodestro-server` repository.

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
  (the studio API uses `{"detail", "code", "params"}`). Messages never contain keys or credentials.
- **Limits** per publication: parallel runs, waiting runs, runs and requests per minute and key, a daily cost limit
  over the runs of the last 24 hours, a cost limit per run, the upload size per file (default 10 MB) and 80 MB per
  request. Exceeded limits answer `429` with `Retry-After`; a request that is too large answers `413`, also when it
  is sent without `Content-Length`.
- **Idempotency:** a request with `Idempotency-Key` that is repeated with the same body within 24 hours returns the
  run it started (`200`, `Idempotent-Replayed: true`) instead of starting another one; the same key with a different
  body answers `409 idempotency.conflict`. Keys are separate per publication and API key.
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

## Standalone export

A release can be exported as a FastAPI project that runs without Nodestro: the release snapshot, the runtime and the
API layer (copied unchanged, listed with versions and SHA-256 checksums in `RUNTIME.json`), exact dependency
versions (`requirements.lock`), key tooling (`python -m app.keys`), examples, a Dockerfile and its own tests with a
simulated model. orkexa is installed from its repository at the pinned commit, or bundled as source code
(`vendor/orkexa`, only after confirming its redistribution). The project contains no provider or API keys; keys are
configured as hashes in `API_KEYS`. The README of the project is written in English or German.

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
against a running server.

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy && pytest
```

The tests run against the real application with scripted provider clients (`nodestro_runtime.testing`) and never
call a paid provider. They cover the REST API, published APIs, keys, approvals, continued runs, batches, schedules,
files, Python libraries, backups, the network listener, the Host/Origin guard and the export; the exported project
passes its own tests. `NODESTRO_NETWORK_TESTS=1` enables the test that installs a real package from PyPI. CI runs on
Python 3.10 and 3.13, builds the wheel and the source distribution, installs the wheel into a fresh environment and
calls the started server over HTTP. It needs the repository secret `NODESTRO_READ` (read access to orkexa and the
Nodestro repositories).

## Limitations

- One process per data directory: limits, idempotency and key changes are synchronized within the process, not
  between several server processes. The runtime enforces it with a lock file (`nodestro.lock`): a second `nodestro`
  or `nodestro-api` on the same data directory stops at start. Do not run the app with several uvicorn workers.
- The interactive reference `/api/docs` loads Swagger UI from a CDN and needs internet access; the OpenAPI document
  itself (`/api/openapi.json`) is served locally.
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
