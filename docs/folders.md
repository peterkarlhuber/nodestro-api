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
| `POST /api/folders` | `admin` | Grant a folder for reading (`201`): `name`, `path` (absolute), `include`, `exclude`, `limits`, `index`, `scope` (`all` or `agents`), `agents`, `mode`, `expires_at` |
| `GET /api/folders/{id}` | `admin` | One folder grant. An unknown id answers `404 folder.not_found` |
| `GET /api/folders/{id}/accesses` | `admin` | Its access log, newest first (`limit`, at most 1,000): every call of the folder tools with agent, run, outcome and code, never a path |
| `DELETE /api/folders/{id}` | `admin` | Revoke a folder grant. The next tool call on it is refused. Revoking twice changes nothing |

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

## Problem codes

| Code | Status | When |
|---|---|---|
| `folder.disabled` | 404 | The switch is off, or this is a server |
| `folder.not_found` | 404 | No folder grant has this id (`id`) |
| `folder.name_taken` | 409 | An active folder grant already has this name (compared case-folded) |
| `folder.not_grantable` | 422 | The folder can never be granted. `reason`: `protected_root` (a drive or file-system root, the home folder or a folder above it, a user's home folder, a system folder, a credential folder, Nodestro's data folder or a folder inside it), `network_path`, `link`, `not_found`, `not_a_directory`, `invalid_path`, `safety_limit` |
| `folder.invalid_name` | 422 | The name is empty, longer than 80 characters, not printable or has a slash |
| `folder.invalid_pattern` | 422 | An include or exclude pattern is not valid |
| `access.person_required` | 403 | The caller is not a person |

The result folder of a portable application (`output_folder.py`) uses the same system folder lists as the path
guard (`orkexa.fs.system_folders`, `orkexa.fs.allowed_folders`).
