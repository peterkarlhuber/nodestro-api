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
| Studio REST API `/api` | `api`, `api_data`, `api_inputs`, `api_examples`, `api_python`, `api_architect`, `api_assistant`, `api_automation`, `api_guide`, `api_publish` |
| Published APIs `/api/published/{slug}` | `serving` (shared with exports), `publishing`, `api_keys`, `network` (separate listener for the local network) |
| Export | `exporter` (ZIP with runtime, lock file, tests; orkexa from Git or bundled), `standalone` |
| Services | `assistants`, `architect`, `assistant`, `assistant_context`, `docs_index`, `batches`, `benchmarks`, `schedules`, `backup`, `organize`, `search`, `diagnostics` |
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
directory, the studio API on this computer only. `--profile server` is refused with `profile.server_unavailable`
before any file is touched, because the server needs login, PostgreSQL, tenant isolation, workers and a container
sandbox, which do not exist yet. An unknown profile name is refused with `profile.invalid`. `create_app` applies the
same rules as the command line.

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
needs (`read`, `edit`, `run` or `admin`, see `nodestro_api.permissions`), and FastAPI checks it on every request. In
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
`studio.localhost`). Host and origin checks do not replace authentication; the studio API becomes reachable from a
network only with the server profile and its login (planned).

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
- The studio API has no login yet and only listens on loopback addresses; the published workflow APIs (with API
  keys) are the way to reach Nodestro from a network.
- Proxy headers are detected by name; a proxy that forwards requests without any of them looks like a local client.
- Nodestro provides technical means for traceability (immutable releases, recorded approvals, usage records,
  sanitized exports) and data minimization (secrets stay out of backups and exports). It does not by itself make an
  installation compliant with the GDPR, the EU AI Act or ISO/IEC 42001; these obligations remain with the operator.

## Provenance and license

Extracted from `nodestro` 0.1 (15e2076), see [PROVENANCE.md](PROVENANCE.md) and [CHANGELOG.md](CHANGELOG.md).
MIT, see [LICENSE](LICENSE).
