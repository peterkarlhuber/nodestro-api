# Portable export and the web interface

A published workflow can be exported in two forms. Both contain the same frozen release and byte-identical copies of
the runtime, so a run behaves as in the studio.

| | Source project (`kind: "standard"`) | Ready-to-run package (`kind: "portable"`) |
| --- | --- | --- |
| Contents | FastAPI project, `requirements.lock`, tests, `Dockerfile` | the same project plus its own Python, every library for one platform, a launcher, optionally the local models with Ollama |
| Target needs | Python, pip (or uv), network to install | nothing: unzip, start `start.cmd` / `Start.command` / `start.sh` |
| Built | synchronously (`POST /publications/{id}/exports`) | as a background job (`POST /publications/{id}/export-jobs`) |
| Web interface | optional (`web_ui`), API-key sign-in | optional (`web_ui`), operator link + API-key sign-in, provider setup |
| README | `readme/standard.<lang>.md` | `readme/portable.<lang>.md` |

Both carry `openapi.json` (the same document the running application serves) and a README in the export language
(`de`, `en`, `es`, `fr`, `it`, `pt-BR`; the dialog starts with the studio's language).

## Modules

| Module | Role |
| --- | --- |
| `export_models.py` | Every model of a release with its role (agent, fallback, judge, orchestrator, embedding of semantic knowledge search; sub-workflows and team pools included); local model facts (installed, size, digest, capabilities, license, memory); network needs. |
| `exporter.py` | Options, check (`ExportCheck.portable`), project files, README rendering from `readme/`. |
| `portable_build.py` | Platforms, the job runner (one at a time, cancellable, survives restarts as `interrupted`), Python and wheels for the target via uv, Ollama for the target via Orkexa, the model package, start scripts, `settings.toml`, `SHA256SUMS`, streaming ZIP64 archive. |
| `portable.py` | The launcher in the package (`python -I -m nodestro_api.portable start|stop|status|open|configure|keys|check`). |
| `portable_secrets.py` | The AI providers' keys on the target computer (`data/secrets/providers.json`, owner-only). |
| `webui_server.py` + `webui/` | Sessions, sign-in, the interface's endpoints and the static app (vanilla JS, no build step, no CDN). |
| `output_folder.py` | The result folder chosen on the settings page (portable only): precedence `OUTPUT_DIR` from outside > choice in `data/settings/output-folder.json` > `[data] output_dir`; path checks, the folder browser (names only), "Open folder". |

## How a package is built

1. **Check** (`exporter.check`): models, licenses, offline rules, downloads, disk estimate. Errors block the job.
2. **Python**: `uv python install cpython-<host minor>-<target>` (python-build-standalone), cached in
   `DATA_DIR/export-cache/python`. The target gets the same minor version as the exporting installation so that the
   pinned library versions are the tested ones.
3. **Libraries**: `uv pip compile --python-platform <target> --only-binary :all: --generate-hashes` with the exporting
   installation's versions as constraints, then `uv pip install --target … --require-hashes --no-deps`. The lock with
   hashes is the package's `lib.lock`. Orkexa is copied from the installed source.
4. **Project**: the files of the source export (without tests and vendor copy), start scripts, `config/settings.toml`,
   licenses.
5. **Model server and models** (optional): Ollama for the target platform from Orkexa's pinned, SHA-256-checked
   artifacts; the models as an Orkexa model package (`models/`, blobs by SHA-256), verified after export and on the
   first start on the target. The package's own folder is activated in place (no copy) unless it is read-only.
6. **Archive**: `SHA256SUMS`, `PORTABLE.json`, then a streamed ZIP64 file with Unix modes and symlinks (blobs
   stored, not compressed). The build works in `DATA_DIR/export-work/<job>` and needs about twice the estimated
   size free; it never holds the package in memory. Downloads are cached and reused.

### On a server

On the server profile (`NODESTRO_PROFILE=server`) export jobs are **off**: `POST /publications/{id}/export-jobs`
answers `403 export.server_unsupported` for every tenant role. A build downloads Python and libraries, writes
hundreds of megabytes (gigabytes with models) and runs in the API process, so only the server's operator can switch
it on, for every tenant at once:

| Variable | Effect |
|---|---|
| `NODESTRO_SERVER_EXPORT_JOBS=1` | export jobs are allowed (any other value: refused) |
| `NODESTRO_SERVER_EXPORT_MAX_BYTES` | size limit of one package in bytes (default 2 GiB): a larger estimate answers `403 export.server_too_large`, a finished ZIP above it is deleted and the job fails with that code |
| `NODESTRO_SERVER_EXPORT_TENANT_QUOTA_BYTES` | bytes of stored exports one tenant may keep (default 8 GiB; the sum of its export records, packages and source projects): a build whose estimate would exceed it answers `403 export.server_quota` (the export check says so first), a finished ZIP that would exceed it is deleted and the job fails with that code. An invalid or non-positive value is the default, never unlimited |

With the opt-in, one package is built at a time on the whole server, across all tenants (`400 export.busy`
otherwise). Packages stay in the tenant's `exports` folder until a member deletes them (`DELETE /api/exports/{id}`,
permission `edit`, which frees quota); there is no automatic retention yet (it waits for the retention policy,
API-009). The standard export (`POST /publications/{id}/exports`) is not affected.

## Security model

- **No secrets in packages.** No Nodestro key, no provider key, no AWS credential. Keys are entered on the target
  computer (settings page for the operator, or `configure`) and stored owner-only, not encrypted (an encryption key
  beside the file would protect nothing; documented in the README). Environment variables win.
- **Browser.** Session cookie `nodestro_app_session` (HttpOnly, SameSite=Strict, Secure with TLS), never an API key
  in the page after sign-in. The operator signs in with a one-time launch link (10 minutes) from the start window;
  everybody else with an API key. State-changing requests need `X-Nodestro-UI: 1` and a matching `Origin`
  (also for published-API calls made with the cookie). Strict CSP (`default-src 'none'`, scripts and styles only from
  the app), `X-Frame-Options: DENY`, results rendered as DOM text (Markdown renderer without HTML), downloads through
  `fetch` + blob. Static files from a whitelist of types, no traversal.
- **Network.** Default `127.0.0.1`; the host-name guard refuses foreign `Host` headers (DNS rebinding). Serving the
  network needs TLS (`tls_cert`/`tls_key`) or `behind_proxy = true`; the launcher refuses otherwise. The model server
  listens on loopback, on its own port, with `OLLAMA_NO_CLOUD`; offline mode removes proxies from its environment.
- **Tools.** Unchanged Docker-first rule: person-written tools run as subprocesses; AI-generated tools only in the
  Docker sandbox (`[tools] sandbox = "docker"`), never as a fallback without it. An offline package refuses
  AI-generated tools at export time.
- **Local files.** On Linux and macOS the launcher keeps `data/` and `logs/` owner-only (`0700`, the log file
  `0600`): created that way, and narrowed on start when they are inside the package (older packages). A data or log
  folder configured outside the package keeps the modes its owner gave it once it exists. Key files and the
  launcher's state are `0600`.
- **Result folder.** The operator (only) may choose another result folder on the settings page:
  `PUT/DELETE /ui/api/setup/output`, `GET /ui/api/setup/output/browse` (sub-folder names only, never files, hidden
  folders left out, at most 500), `POST /ui/api/setup/output/folders` (a plain name), `POST
  /ui/api/setup/output/open` — all with the usual CSRF header/Origin check. A path must be absolute; it is normalized
  and its links resolved before the checks, which refuse the drive root, system folders (`/etc`, `/usr`, `/var`,
  `/opt`, `C:\Windows`, `Program Files`, … except the user's home and `/var/home`, `/run/media`, temp folders), the
  package's code, Python, config, data and log folders, and the interpreter's prefix. A missing folder is created
  only with `create: true` (the page asks first); the folder must pass a write probe. Before each copy the chosen
  folder is checked again (a folder swapped for a link is not written). "Open folder" exists only when the launcher
  serves loopback without a proxy, a desktop is present, and the request comes from `127.0.0.1`/`::1`; it opens the
  folder in effect, never a path from the request. Files already written are never moved.
- **Integrity.** `lib.lock` (hashes), `SHA256SUMS`, model blobs by SHA-256, Ollama archives pinned by Orkexa.

## Platforms

| Platform | Package build | Tested |
| --- | --- | --- |
| Linux x86-64 | yes | locally: build, offline start in a clean container without network or Python, real local model run, results, stop/restart, browser E2E; CI smoke (`portable.yml`) |
| Linux ARM64 | yes | CI smoke only (no local models) |
| Windows x64 | yes | CI smoke only (no local models) |
| Windows ARM64 | yes, unless the release uses Gemini | CI smoke only; semantic knowledge search falls back to full text (no `sqlite-vec` wheel) |
| macOS Apple Silicon | yes | CI smoke only (no local models) |
| macOS Intel | yes, unless the release uses Gemini | CI smoke only (no local models) |

Google's SDK (Gemini) goes into a package only when the release uses Gemini. It needs `cryptography`, which has
published no ready-built package for macOS Intel or Windows ARM64 since 46.0.4, so such a package is refused with
`export.libraries_unavailable` for those two platforms (the Windows x64 package runs on Windows 11 on ARM in its x64
emulation).

"CI smoke" builds a package of a workflow without models on the runner, runs `check`, starts it, loads the
interface, runs the workflow through the API, stops and restarts it (`tests/test_portable_smoke.py`). Packages are
not code-signed: macOS Gatekeeper and Windows SmartScreen ask once (README, troubleshooting).

## Feature compatibility

The package runs the same runtime and published-API layer as the source export, so the same rules apply.

| Feature | In an export |
| --- | --- |
| Parallel agents, decisions, loops, sub-workflows, orchestrator teams (incl. dynamic, nested) | yes |
| Approval steps (human in the loop) | yes, decided through the API or the web interface (scope `approve`) |
| Structured output, quality judge, fallback models | yes (judge/fallback models are analysed like agents) |
| Result files (Markdown, PDF, …) | yes; copies to the result folder per run |
| Uploads (form file fields) | yes (JSON Base64 or `multipart/form-data`) |
| Live progress (SSE) with reconnect | yes |
| Knowledge | only files ticked at export (context, on demand, full text; semantic search with the bundled embedding model) |
| Memory | starts with the content ticked at export, then lives in the package's database |
| Person-written tools | yes (subprocess) |
| AI-generated tools | only with Docker on the target (`sandbox = "docker"`); refused for offline packages |
| Server tools (web search, …) | need the Anthropic API and a network |
| MCP and integration tools | refused at export (no connection to the services) |
| Per-call tool approvals (S-2) | not activated (feature flag stays off) |
| Dynamic run budget approval (BUDGET-1) | not in exports: API runs are unattended and stop with `run.budget_exceeded` |
| Live run chat / steering (LRC-1) | studio only |
| Resume after restart | yes (`POST /runs/{id}/resume`) |

## Container image

Evaluated, not built in this round: the source export's `Dockerfile` remains the container route. A real offline
image needs a pinned base image with glibc for the bundled Python and Ollama; building it without a Docker daemon
means assembling an OCI layout (base layers from a registry, plus the Linux package as a layer). That is feasible but
was deferred (see `docs/TODO.md`). The Linux package runs unchanged in a container (`docker run --network none … sh
start.sh` is exactly how it was tested).
