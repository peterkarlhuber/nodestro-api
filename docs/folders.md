# Granted folders (N-10)

The routes of granted folders (`nodestro_api.api_folders`, router group `approvals`). The model itself (the grant, the
tools `list_folder`, `read_file` and `search_files`, the path guard, the limits and the access log) is described in
nodestro-runtime's `docs/folders.md`.

**Off by default.** Only while `NODESTRO_FOLDER_GRANTS=1`, and never in the server profile, because a server has no
person's folders. While it is off, every route but `GET /api/folders/state` answers `404 folder.disabled`, and nothing
is read or written.

| Route | Permission | What it does |
|---|---|---|
| `GET /api/folders/state` | `read` | `enabled`, the default `limits` and the `bounds` of each limit (`lowest`, `highest`). Always answers, so the studio knows whether to show the folders page |
| `GET /api/folders` | `admin` | Every folder grant of the workspace, newest first, with revoked and expired ones (`status`) |
| `POST /api/folders` | `admin` | Grant a folder for reading (`201`): `name`, `path` (absolute), `include`, `exclude`, `limits`, `index`, `default_excludes` (default `true`: the common secret files `.env*`, `*.pem`, `*.key`, `id_rsa*`, `id_ed25519*`, `.npmrc`, `.pypirc`, `.netrc`, `*.kdbx`, `*.p12`, `*.pfx`, `*.ppk`, `credentials*` are left out; the grant is logged as `folder.granted`, with code `default_excludes_off` when they are off; `false` turns that off for this grant), `scope` (`all` or `agents`), `agents`, `mode`, `expires_at`. `index: true` keeps a search index of the folder (N-12): the grant wakes the indexer |
| `GET /api/folders/{id}` | `admin` | One folder grant. An unknown id answers `404 folder.not_found` |
| `GET /api/folders/{id}/accesses` | `admin` | Its access log, newest first (`limit`, at most 1,000): every call of the folder tools with agent, run, outcome and code, never a path |
| `DELETE /api/folders/{id}` | `admin` | Revoke a folder grant. The next tool call on it is refused. Revoking twice changes nothing |
| `POST /api/folders/browse` | `admin`, local session | The folder browser (N-11): the sub-folders of a folder of this computer, for choosing the folder to grant. See below |

## Who writes

- The writer (`created_by`, and `revoked_by` of its access grant) is always the request's signed-in person, never a
  field of the body. On the desktop, that is `local`, the owner.
- A principal that is not a person, such as an API key (`api:<id>`), `system` or anything with a `:`, is refused with
  `403 access.person_required`.
- No other route writes folder grants. `tests/test_access_api.py` checks that only `api_access` and `api_folders`
  build a `Person` or use a grant writer.
- The routes use the same session and CSRF protection as every other route of the studio API. None is reachable with
  an API key, from a published API, the app contract or Nodestro's MCP server.

A folder grant is an access grant of the access model v2 (resource `folder`, action `read`). It also shows under
`GET /api/access/grants` and in "who may do what and why". Revoking it there revokes the folder grant too. Its audit
is the access model's (`access_granted`, `access_grant_revoked`). A grant for "this conversation" is not offered here
(`scope` is `all` or `agents`).

## The folder browser (N-11)

A browser cannot hand a folder of this computer to the backend, so the studio browses folders through
`POST /api/folders/browse` with `{"path": "<absolute path>"}` (`null` or no `path`: the home folder, else the root of
the file system). It answers `path` (canonical), `parent` (`null` at a file-system or drive root), `folders`
(`name`, `path`, `link`), `truncated`, `places` (`home`, `root`, `drive`, `media`) and `separator`.

- **Folder names only.** Never a file, a file name, a size or a content. Names starting with `.` are left out (on
  Windows also `$` names and folders with the hidden or system attribute).
- **No system folders**, no other user's home folder, no credential folder (`.ssh`, `.aws`, keychains ...) and not
  Nodestro's data folder: they are neither listed nor opened (`422 folder.not_browsable`, `reason: hidden`). The lists
  are the path guard's (`orkexa.fs.browse_folder`, `orkexa.fs.system_folders`). The user's own home folder and
  everything below it can be browsed.
- **No existence answers in hidden places.** A file, a folder or a missing name in or below such a folder answers
  `reason: hidden` alike (never `not_found`, `not_a_folder` or `no_access`), so the browser enumerates neither files
  nor users. The result folder of the embedded interface answers `output.protected` for them on browsing, choosing
  and creating a folder.
- **Links.** `..` and links are resolved before the checks. A symbolic link or junction is listed only when its
  target is a folder that passes the same checks; its `path` is the target (`link: true`), never the link. The folder
  is opened once after the checks and must still be the folder that was checked.
- **Only the authenticated local session.** The request must have come through the session that the desktop app's
  launch secret opened (or Nodestro Local Docker's access key: a browser session or `Authorization: Bearer`). Without
  a launch secret (the `nodestro` command in a terminal) it answers `403 folder.session_required`. It needs `admin`,
  the folder grant switch and a non-server profile like every other route here.
- **CSRF.** It is a `POST` with a JSON body, although it changes nothing: it gets the protection of every change
  (an `Origin` must be the API's own, and with `NODESTRO_LAUNCH_CSRF=1` the session's `X-Nodestro-CSRF` value and an
  `Origin` are required), a link or an image cannot trigger it, and the path stays out of access logs. A request
  that the browser marks as from another site (`Sec-Fetch-Site` other than `same-origin` or `none`) is refused with
  `403 request.cross_site`.
- At most 500 folders are answered and 10,000 entries read within 10 seconds; beyond, `truncated` is `true`.

Browsing is not granting: a folder chosen here is checked again by `POST /api/folders`, which is stricter (no home
folder, no file-system root). The embedded web interface's result folder browser (`output_folder.py`,
`GET /ui/api/setup/output/browse`) uses the same code path (`nodestro_api.folder_browser`).

## Problem codes

| Code | Status | When |
|---|---|---|
| `folder.disabled` | 404 | The switch is off, or this is a server |
| `folder.not_found` | 404 | No folder grant has this id (`id`) |
| `folder.name_taken` | 409 | An active folder grant already has this name (compared case-folded) |
| `folder.not_grantable` | 422 | The folder can never be granted. `reason`: `protected_root` (a drive or file-system root, the home folder or a folder above it, a user's home folder, a system folder, a credential folder, Nodestro's data folder or a folder inside it), `network_path`, `link`, `not_found`, `not_a_directory`, `invalid_path`, `safety_limit`, `temp_root` (a shared temporary folder itself, such as `/tmp`; a folder inside one can be granted) |
| `folder.invalid_name` | 422 | The name is empty, longer than 80 characters, not printable or has a slash |
| `folder.invalid_pattern` | 422 | An include or exclude pattern is not valid |
| `access.person_required` | 403 | The caller is not a person |
| `folder.not_browsable` | 422 | The folder browser does not open this path. `reason`: `hidden` (a system folder, another user's home folder, a credential folder or the data folder, or a folder inside one), `not_found`, `not_a_folder`, `no_access`, `changed` (the folder changed while it was checked), `invalid_path`, `network_path` |
| `folder.session_required` | 403 | The folder browser was asked without the authenticated local session |
| `request.cross_site` | 403 | The folder browser was asked from another site (`Sec-Fetch-Site`) |

The result folder of a portable application (`output_folder.py`) uses the same system folder lists as the path
guard (`orkexa.fs.system_folders`, `orkexa.fs.allowed_folders`) and the same folder browser.

## The search index (N-12)

A grant with `index: true` keeps a search index of the folder in the runtime (`nodestro_runtime.folder_index`, the
runtime's `docs/folders.md`, "The search index"); `search_files` then searches it. The routes keep the index in step
with the rights: a grant wakes the indexer for that folder, and `DELETE /api/folders/{id}`, `POST /api/access/denies`,
`DELETE /api/access/grants/{id}` and `DELETE /api/access/denies/{id}` call `Workspace.folder_access_changed`, which
drops the index (chunks and vectors) of every folder grant that ended before the answer and syncs the indexes soon.
An expiry is dropped by the runtime's sweep. The API shows no index state yet. Backups and the data export leave a
folder's index out (review M-1, [backup.md](backup.md)). Tests:
`tests/test_folders_api.py::test_a_folder_index_is_built_on_a_grant_and_dropped_at_once_on_a_revocation`,
`tests/test_folders_api.py::test_backups_and_the_data_export_hold_nothing_of_a_folder_index`.
