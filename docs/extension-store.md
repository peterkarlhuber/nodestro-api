# The data-only extension store (API-081)

The admin routes of the runtime's data-only extension store (nodestro-runtime RT-098, its
`docs/extension-isolation.md`, owner decision 2026-11-27). A foreign extension package is never installed into the
backend's Python environment and never imported in the API process. It is kept as files in
`<data>/extensions/<sha256>/` and its tools run only in the Docker sandbox, only while `NODESTRO_EXTENSION_TOOLS=1`
(a development switch, off by default). No tool class is released. Only the desktop and Nodestro Local Docker have a
store. On a server every route answers `404 extension_store.disabled`.

| Route | Permission | What |
|---|---|---|
| `GET /api/extensions` | `admin` | `enabled`, `tools_switch`, `max_bytes`, the store's `packages` with their state in this process (`locked` with `reason`: `off`, `not_secured`, `invalid`, or `pending` until the workspace opens next; or `sandboxed` with `tools`), the `revoked` hashes and `host_refused` (packages found in the Python environment, never used) |
| `POST /api/extensions` | `admin`, a signed-in person | multipart: `file` (a pure-Python wheel, at most 8 MiB), `sha256` (the hash the administrator expects, 64 lower-case hex digits), `origin` (where it comes from, 1 to 500 characters), optionally `replaces` (the installed version this one updates). `201` with the package. The runtime checks everything before it writes anything: `422 extension_store.refused` with `reason` (`hash_mismatch`, `origin`, `too_large`, `archive`, `unsafe_path`, `unsafe_member`, `record`, `manifest`, `not_pure`, `dependency`, `data_part`, `exists`, `revoked`) |
| `DELETE /api/extensions/{sha256}` | `admin`, a signed-in person | remove a package; later calls of its tools are refused at once. `404 extension_store.not_found` |
| `POST /api/extensions/{sha256}/revoke` | `admin`, a signed-in person | revoke a hash: removed when installed, refused at every later upload |

An API key or another machine principal is refused with `403 access.person_required`. Every change and every refused
upload is an activity event (source `extension`, resource `extension_package`, the SHA-256, the actor). Tests:
`tests/test_extensions_api.py`, `tests/test_extensions_api_server.py` (PostgreSQL server).
