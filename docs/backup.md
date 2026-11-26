# Backups, export everything and delete everything (N-15)

The backup core (`nodestro_api.backup`) and the functions built on it (`nodestro_api.data_lifecycle`) are
product-neutral: a product that embeds the API ([embedding.md](embedding.md)) uses the same core as the studio, with
its own product id. The routes are in the router group `data` and need the permission `install`, which only the local
owner of a desktop or a Local Docker installation holds (never a role on a server). Like every change, the `POST`
routes need the session's CSRF value when `NODESTRO_LAUNCH_CSRF=1` is on.

| Route | What it does |
|---|---|
| `GET /api/backup` | A complete backup (`nodestro-backup`, see below) |
| `POST /api/backup/inspect`, `POST /api/backup/local/{name}/inspect` | Check a backup completely; nothing changes yet |
| `POST /api/backup/restore` | `{"id", "confirm": true}`: replace all data; a safety backup is made first. Audited (`backup_restored`) |
| `GET /api/data/export` | Everything as readable files (`nodestro-export`), without secrets. Audited (`data_exported`) |
| `POST /api/data/delete-all` | `{"confirm": "delete all data"}`: delete every piece of data. Audited (`data_deleted`) |

The audit entries are in the access audit trail (`GET /api/access-audit`, target type `data`, target id the product
id); they name who acted and counts, never data.

## The profile

`BackupProfile(product, data_dir, components)` is what the core works on; `profile_of(workspace)` builds it from the
configuration: `AppConfig.product_id` (default `nodestro`) and `data_dir`, with every component. The components
besides the database are `tools` (the tool files) and `inputs` (the input files of runs); a profile without one
neither writes it into a backup nor replaces it on a restore. The core functions take an optional profile.

## The backup format

The format stays `nodestro-backup` version 1. The manifest gained three fields; a reader that does not know them
ignores them:

- `product`: the product whose data the backup holds. A backup of another product is refused with
  `backup.product_mismatch` (`params.found`, `params.expected`). A backup without the field, made before N-15, belongs
  to `nodestro` and stays readable (`tests/fixtures/backup-v1.zip`, made by the release before, is checked and
  restored by `tests/test_data_lifecycle.py`).
- `components`: the components the backup holds.
- `secrets_excluded`: the secrets the data used that were left out: `provider:<name>` (a provider's key or a base URL
  with credentials), `mcp:<id>` (an MCP service's sealed token or pending OAuth sign-in) and `connection:<id>` (a
  service connection's sealed secret).

`excluded` now also lists `mcp_tokens` and `connection_secrets`: before N-15 the sealed tokens of MCP services and the
sealed secrets of connections were part of a backup (sealed with the data directory's key, so unreadable on another
computer). They are left out now, and a restore never takes them from a backup, also not from an older one.

## Secrets on a restore

A restore keeps this computer's secrets: its provider keys, and the sealed token (and pending sign-in) of an MCP
service or the sealed secret of a connection with the same id, but only while the restored document is bound to the
same place and use (threat model T9, as the runtime drops a secret when one of these changes): for an MCP service its
`url`, `transport`, `stdio` command and `auth`; for a connection its `type`, `provider`, `egress` and the settings
`auth`, `security` and `username`. Otherwise the secret is dropped (its storage becomes `none`) and listed for
re-entry. `BackupInfo.secrets_to_reenter` (on checking) and `RestoreResult.secrets_to_reenter`
(after the restore) list the secrets of the backup that this computer does not have; the user enters them again.
A backup made before N-15 does not name its provider keys (they were left out without a record), so only the sealed
secrets still inside it are listed for it.

Safety backups are backups too: they hold no sealed token either, so undoing a restore with one lists the MCP tokens
and connection secrets that changed meanwhile for re-entry.

Restored tool and input files, the waiting backup and the safety backups are private to the owner (folders `0700`,
files `0600` on POSIX), as the folders they replace.

## Export everything

A ZIP file named `<product>-export-<time>.zip` with `manifest.json` (format `nodestro-export` version 1, `product`,
versions, contents, `excluded`, `secrets_excluded`, `collections` and a SHA-256 per file),
`documents/<collection>.jsonl` (a collection whose name is not a plain file name is written as
`documents/~collection-<n>.jsonl`, which no plain name can be; `collections` maps each file to its collection; every document of the same sanitized snapshot a backup is made from: one JSON object
per line with `id`, `updated_at` and `data`), `memory.jsonl` (memory files), `stored-files.jsonl` with
`stored-files/<n>` (knowledge and other stored files with their entry), and the tool and input files as in a backup.
The search index is derived and left out. The export is for the user to keep or take elsewhere; restoring goes
through a backup.

## Delete everything

Refused while runs or other background work are active (`data.busy`, `params.work`) or a restore runs
(`backup.restore_running`), and without the exact confirmation (`data.not_confirmed`). While it deletes, no run starts
(`data.deleting`, whatever starts it), and the background work is checked again once the store is locked; every other
use of the store waits until the files are removed. Then every table of the
database is emptied (secrets, API keys and the check value of the data directory's key included) with `secure_delete`
on, the file is rebuilt (`VACUUM`) and its write-ahead log emptied, and every other file and folder in the data
directory is removed, symbolic links without following them. Left are the emptied database file with its log and
index, the directory lock and the private `launch` folder of the current session. Folders outside the data directory
(an output folder) are not touched. No safety backup is kept. Network access for published APIs is switched off; the
workspace starts over as a new installation, and the audit entry of the deletion is its only record.

The answer says whether nothing is left (`complete`): `log_emptied` (the write-ahead log is tried again for a moment
while another connection reads) and `left` (entries that could not be removed, e.g. a file in use). Deleting again
removes the rest. A failure after the deletion began is recorded in the audit trail (`complete: false`, `error`)
before it is answered.
`tests/test_data_lifecycle.py` scans every file left in the data directory for the data's canary values.

## Acceptance test

`tests/test_data_lifecycle.py::test_a_backup_from_one_computer_restores_identically_on_another` runs two processes
(`tests/backup_machine.py`), each with its own data directory and its own key for the data directory's secrets: the
first fills conversations, agents, knowledge, memory, grants and denies, runs, the activity log and secrets and makes
a backup; the second restores it through the API. Every stored document, memory file, stored file, search index
entry, tool and input file is the same afterwards, except the secrets (each computer keeps its own; the connection's
secret is listed in `secrets_to_reenter`) and the second computer's audit entry of the restore. It runs in the
existing test job; a job with two containers would test nothing more, since the data directory and its key are what
differ between two computers.
