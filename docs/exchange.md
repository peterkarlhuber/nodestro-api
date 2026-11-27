# Exchange files over the API (N-14)

The routes below move MCP services, service connections and knowledge collections between workspaces as versioned
files. The formats, the shared import check, the limits and the codes are described in nodestro-runtime
`docs/exchange-formats.md` (`nodestro_runtime.exchange`, `nodestro_runtime.mcp.exchange`); this page names the routes,
their permissions and their answers.

| Route | Permission | What it does |
| --- | --- | --- |
| `GET /api/mcp/export` | `admin` | `nodestro-mcp/1` of every server (`ids`: only these) |
| `POST /api/mcp/import` | `admin` | preview (`dry_run=true`) or add the servers of a `nodestro-mcp/1` file |
| `GET /api/connections/export` | `admin` | `nodestro-connection/1` of every connection (`ids`: only these) |
| `POST /api/connections/import` | `admin` | preview (`dry_run=true`) or create the connections of a `nodestro-connection/1` file |
| `GET /api/knowledge/collections/export` | `read` | `nodestro-collection/1` (ZIP) of the collection `name` |
| `POST /api/knowledge/collections/import` | `edit` | preview (form field `dry_run`) or store a `nodestro-collection/1` file (form field `file`; `collection`: another name) |
| `POST /api/workflows/import` | `edit` | unchanged, and checks `format` when the file names one (`nodestro-workflow/1`) |

MCP services and connections are administration (like adding them by hand), so exporting and importing them needs
`admin`; on the desktop the owner has it. A collection is knowledge: exporting it needs `read` (like reading its
files), importing it `edit` (like an upload).

## Answers

- An export answers the file as a download (`Content-Disposition: attachment`). The JSON formats are described in
  the OpenAPI document (`McpPackage`, `ConnectionPackage`).
- An import answers an `ImportReport` (`200`), for the preview and for applying: `items` with `status` (`ready`,
  `refused`, `created`, `exists`), `code` and `params` of a refused entry, `id` of a created one and `needs_secret`;
  `secrets_to_reenter` after applying (`mcp:<id>`, `connection:<id>`, the labels of a backup's
  `secrets_to_reenter`); `left_out`; for a collection `collection`, `collection_exists` and `embedding`
  (`package`, `workspace`, `matches`). An interface shows the preview first and applies after the person confirmed.
- Refusals are error answers with a code: `413` `exchange.too_large`, `exchange.too_many_entries`,
  `exchange.zip_bomb`; `422` `exchange.version_newer` (`params.version`, `params.supported`),
  `exchange.format_invalid`, `exchange.format_mismatch`, `exchange.invalid_json`, `exchange.content_invalid`
  (`params.problems`: field names and reasons), `exchange.items_refused` (an entry the preview refuses; nothing is
  stored), `exchange.zip_invalid`, `exchange.unsafe_path`, `exchange.unexpected_file`, `exchange.incomplete`,
  `exchange.checksum`, `exchange.collection_name_invalid`; `404` `exchange.collection_not_found`; `409`
  `exchange.collection_exists` and `exchange.secret_detected` (an export that would hold a secret; `params.places`
  names the fields, never the values).

## Limits

The JSON imports are read as ordinary JSON requests (`NODESTRO_MAX_REQUEST_BYTES`, default 2 MiB) and refused above
1 MB by the import check. A collection import is an upload (`NODESTRO_MAX_UPLOAD_BYTES`, default 256 MiB, see
`body_limit`), and the import check refuses a file above 256 MiB, with more than 500 documents or 512 MiB unpacked.
A collection export is written to a temporary file of the server and deleted after it was sent.

## What an import never does

An imported MCP server is stored switched off, without a token and with no tool approved; a stdio server still needs
the Docker sandbox (`sandbox.isolation_required`). An imported connection has no secret and no check. Neither gets a
secret of this workspace: they are new entries with new ids. A collection import never changes the workspace's
settings and never takes vectors: its documents are indexed again here. No import changes a `NODESTRO_*` switch.
