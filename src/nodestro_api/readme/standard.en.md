# {title}: workflow API

This project serves the Nodestro workflow **{title}** as an HTTP API, independent of Nodestro Studio.
It was exported from release **v{number}** (workflow version v{version}, published {published},
digest `{digest}`) and contains:

- `workflow/release.json`: the published workflow, its sub-workflows and the code of its tools, frozen;
- `nodestro_runtime/` and `nodestro_api/`: the execution engine of Nodestro (nodestro-runtime {nodestro}) and
  the HTTP layer of published workflows, copied unchanged (versions, source commits and checksums in
  `RUNTIME.json`), so the workflow runs exactly as in the studio: parallel agents, decisions, approvals,
  sub-workflows, tools, budgets and result files;
- `app/`: the application (`main.py`), its configuration (`config.py`), the request models (`schemas.py`)
  and a tool to create API keys (`keys.py`);
- `tests/`: tests with a simulated model (no cost) and `examples/`.{guide}

Nothing is sent to Nodestro. The API needs access to the AI providers of its agents ({providers}) and its
own API keys for its clients.

## Requirements

- Python {python} (the versions in `requirements.lock` were taken from Nodestro running on Python {python})
- Access to {providers} (see "Configure")

### orkexa (agent library)

{agent_install}

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
# or with uv:  uv sync   (creates uv.lock; needs network access)
cp .env.example .env
```

## Configure

All settings come from environment variables or `.env` (see `app/config.py`):

| Variable | Meaning |
| --- | --- |
{provider_rows}
| `API_KEYS` | The keys of your clients (hashes only, see below). Empty: every request is refused. |
| `DATA_DIR` | Where runs, approvals, files and memory are stored (default `./data`). |
| `API_DOCS` | `local` (default: `/docs` only from this computer), `on` or `off`. |
| `RUN_RETENTION_DAYS` | Delete runs older than this many days at start (0 keeps everything). |
| `TOOL_SANDBOX` | `subprocess` (default) or `docker`, see below. |
| `OUTPUT_DIR` | A folder that also receives a copy of every result file (empty: result files stay in `DATA_DIR`). |

`TOOL_SANDBOX=subprocess` runs only tools a person wrote in the tool editor, as a process of this server.
`TOOL_SANDBOX=docker` runs every own tool in an isolated container. It needs Docker and Orkexa's sandbox image
`orkexa-sandbox`, which you build once from Orkexa's repository at the commit of the installed `orkexa`
(`docker build -t orkexa-sandbox -f docker/sandbox.Dockerfile .`; its base image is pinned by digest and its
packages by hash) and check with `python -m orkexa.dynamic.docker --self-test`. Nodestro's studio uses its own
runtime image instead, which an export cannot build. A container has no network, a read-only file system, no
capabilities and an unprivileged user; tools run in it only after the Docker daemon passed Orkexa's preflight and
the in-container self-test (checked in the background at the start), and always by the id of the image that passed,
never by the tag. AI-generated tools run only with `docker`; without it, or until that check passed, runs that use
them are refused.

### API keys

```bash
python -m app.keys new --name "My app" --scopes run,read
```

prints a key (give it to the client, it is shown once) and the line for `API_KEYS`. Permissions:
`run` (start, cancel, continue), `read` (status, result, events, files, usage), `approve` (decide
approvals). Separate several entries with `;`. Keys of Nodestro are never part of an export.

### Limits

The limits of the published API apply; override them with:

| Variable | Exported value |
| --- | --- |
{limits}

`LIMIT_DAILY_BUDGET_USD` stops new runs once the runs of the last 24 hours cost that much;
`LIMIT_RUN_BUDGET_USD` stops a single run. Exceeded limits answer `429` with a code.

## Start

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The API then answers on this computer only. To serve other computers, listen on `0.0.0.0` **behind HTTPS**
(e.g. a reverse proxy, or `--ssl-certfile`/`--ssl-keyfile`): keys must not travel unencrypted.
Interactive documentation: http://127.0.0.1:8000/docs

In a container: the included `Dockerfile` builds an image (its Python base image is pinned by digest; building
needs network access for `requirements.lock`). Mount the data folder and, if you want the result files on the host,
a result folder. In `.env`, set `DATA_DIR=/data` and `OUTPUT_DIR=/results`.

```bash
docker build -t workflow-api .
docker run --rm -p 127.0.0.1:8000:8000 --env-file .env \
  -v "$PWD/data:/data" -v "$PWD/results:/results" workflow-api
```

Without the second mount, the files copied to `OUTPUT_DIR` stay inside the container and are lost with it.

## Use

Runs are asynchronous: starting answers `202` with a `run_id`; then ask for the status, follow the events
and fetch the result.

```bash
export {key_variable}=ndk_...
{curl_start}
```

The request body (`examples/request.json`):

```json
{request}
```
{upload}
```bash
{curl_status}
{curl_result}
{curl_events}
```

Status values: `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, `cancelled`. Sending the
same `Idempotency-Key` again returns the run that already started, so retries never start a run twice.
The events are Server-Sent Events; after a lost connection, reconnect with `Last-Event-ID`; the first event is
always the complete current status. `examples/client.py` does all of this in Python.

Every client can first ask what this backend serves: `GET /api/app` names the app contract (`nodestro-app/1`), the
backend's id and how to sign in; with a key it also lists this workflow and the capabilities. `BACKEND_NAME` gives
the backend a name that clients show. The contract is described in `docs/app-api/v1.md` of nodestro-api.

## Approvals

This workflow has {approvals}. A run that reaches one pauses with the status
`awaiting_approval`; the status lists what to review. A key with the permission `approve` decides:

```bash
{curl_approve}
```

`approval_id` may be left out when only one approval waits. Every decision is stored with the run (who, when,
comment); a second decision on the same approval answers `409`. Waiting runs survive restarts.

## Result files and data

Result files the workflow writes are listed under `/runs/{run_id}/files`. Everything is stored in
`DATA_DIR/runtime.db` (runs with their steps, usage and cost, approvals, files, uploads, memory) and
`DATA_DIR/tools`. Knowledge included in the export: {knowledge_in}. Not included (the agents run without
it): {knowledge_out}. Starting memory: {memory}. Tools: {tools}.

## Errors and costs

Errors have the form `{"error": {"status", "code", "message", "params"}}`. Every run records tokens and
estimated cost (status and result: `usage`); `/api/published/{slug}/usage` sums them up. A run that was
interrupted by a restart is marked `failed` (`run.interrupted`) and can be continued with
`POST /runs/{run_id}/resume`.

## Tests

```bash
pip install pytest httpx
pytest
```

The tests use a simulated model: they cost nothing and check the routes, validation, keys, limits,
idempotency, a complete run, approvals across a restart and that the runtime is unchanged.
