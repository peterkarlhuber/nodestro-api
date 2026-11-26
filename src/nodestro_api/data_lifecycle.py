"""Export everything and delete everything (N-15, DAT-6), built on the backup core (:mod:`nodestro_api.backup`).

**Export everything** (:func:`export_all`) writes the data of a workspace as readable files, for the user to keep or
take elsewhere, not for a restore (that is what a backup is for). It is a ZIP file with

- ``manifest.json``: format ``nodestro-export`` version 1, the ``product``, creation time, versions, contents, the
  secrets left out (``excluded``, ``secrets_excluded``) and a SHA-256 per file,
- ``documents/<collection>.jsonl``: every stored document, one JSON object per line (``id``, ``updated_at``,
  ``data``), from the same sanitized snapshot a backup is made of: no provider key, no sealed token or connection
  secret, no key of a published API, no idempotency record and no check value of the data directory's key,
- ``memory.jsonl``: the files of every memory space (``namespace``, ``path``, ``content``, ``updated_at``),
- ``stored-files.jsonl`` and ``stored-files/<n>``: the stored files (knowledge files and others) with their entry,
- ``tools/<name>/v<n>.json`` and ``inputs/``: as in a backup.

The search index and its vectors are left out: they are derived from the files and built again on import.

**Delete everything** (:func:`delete_all`) leaves no data in the data directory. It refuses while anything works in
the background (runs, tool jobs, installations, a reindex: ``data.busy``) or a restore runs, and then

1. empties every table of the database, secrets, keys and the check value of the data directory's key included,
   with ``secure_delete`` on, rebuilds the database (``VACUUM``) and empties its write-ahead log, so no deleted
   value stays in a free page or in the log,
2. removes every other file and folder in the data directory (tool and input files, backups and safety backups,
   exports and their caches, Python environments, local models, paired devices, the instance file), symbolic links
   without following them.

Only what the running process needs to keep its data directory stays: the (now empty) database file with its
write-ahead log and shared-memory index, the directory lock and the private ``launch`` folder of the current session.
Folders outside the data directory (an output folder the user chose) are not touched. Afterwards the workspace starts
over as a new installation; the caller records the deletion in the access audit trail, which is then its only entry.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import TYPE_CHECKING, Any
import zipfile

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now
from nodestro_runtime.workspace import DATABASE, LOCK_FILE
from orkexa import __version__ as library_version
from pydantic import BaseModel, Field

from . import __version__
from .backup import (
    EXCLUDED,
    BackupContents,
    BackupProfile,
    _sha256,
    component_files,
    current_contents,
    exclusive,
    profile_of,
    quote_identifier,
    sanitized_snapshot,
)

if TYPE_CHECKING:
    from .context import Nodestro

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "nodestro-export"
EXPORT_FORMAT_VERSION = 1
#: What a deletion leaves in the data directory: the database the running process keeps open (emptied), its
#: write-ahead log and shared-memory index, the directory lock and the private folder of the current launch.
KEPT = frozenset({DATABASE, f"{DATABASE}-wal", f"{DATABASE}-shm", LOCK_FILE, "launch"})
#: The full-text index and its shadow tables are emptied by rebuilding it, not row by row.
_FTS = re.compile(r"^index_fts($|_)")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class ExportInfo(BaseModel):
    file_name: str
    product: str
    contents: BackupContents
    secrets_excluded: list[str] = Field(default_factory=list)


class DeleteResult(BaseModel):
    """What a deletion removed: the contents before it and the number of files and folders in the data directory."""

    deleted_at: datetime
    contents: BackupContents
    removed_entries: int


def export_file_name(product: str) -> str:
    return f"{product}-export-{now().strftime('%Y%m%d-%H%M%S')}.zip"


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text  # kept as it is: the export is a copy, never a judge


def export_all(nodestro: Nodestro, target: Path, profile: BackupProfile | None = None) -> ExportInfo:
    """Write everything the workspace holds, without secrets, as readable files to ``target`` (a .zip file)."""
    profile = profile or profile_of(nodestro)
    with tempfile.TemporaryDirectory(prefix="nodestro-export-") as directory:
        database = Path(directory) / "snapshot.db"
        contents, excluded = sanitized_snapshot(nodestro, database)
        parts = component_files(profile)
        contents.tools = sum(1 for name in parts if name.startswith("tools/"))
        entries: dict[str, bytes] = {}
        collections: dict[str, str] = {}
        connection = sqlite3.connect(database)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            by_collection: dict[str, list[dict[str, Any]]] = {}
            for collection, key, data, updated in connection.execute(
                "SELECT collection, id, data, updated_at FROM nodestro_documents ORDER BY collection, id"
            ):
                by_collection.setdefault(collection, []).append(
                    {"id": key, "updated_at": updated, "data": _loads(data)}
                )
            for number, (collection, rows) in enumerate(sorted(by_collection.items())):
                # A collection name is data of the database: a file name only when it is a plain one.
                name = collection if _SAFE_NAME.fullmatch(collection) else f"collection-{number}"
                collections[f"documents/{name}.jsonl"] = collection
                entries[f"documents/{name}.jsonl"] = _jsonl(rows)
            if "memory_files" in tables:
                memory = [
                    {"namespace": namespace, "path": path, "content": content, "updated_at": updated}
                    for namespace, path, content, updated in connection.execute(
                        "SELECT namespace, path, content, updated_at FROM memory_files ORDER BY namespace, path"
                    )
                ]
                entries["memory.jsonl"] = _jsonl(memory)
            if "stored_files" in tables:
                listed = []
                for number, (namespace, key, entry, data) in enumerate(
                    connection.execute("SELECT namespace, id, entry, data FROM stored_files ORDER BY namespace, id")
                ):
                    listed.append(
                        {"namespace": namespace, "id": key, "entry": _loads(entry), "file": f"stored-files/{number}"}
                    )
                    entries[f"stored-files/{number}"] = bytes(data)
                entries["stored-files.jsonl"] = _jsonl(listed)
        finally:
            connection.close()
        files = {name: _digest(content) for name, content in entries.items()}
        for name, path in parts.items():
            files[name] = _sha256(path)
        manifest = {
            "format": EXPORT_FORMAT,
            "format_version": EXPORT_FORMAT_VERSION,
            "product": profile.product,
            "created_at": now().isoformat(),
            "nodestro_version": __version__,
            "library_version": library_version,
            "contents": contents.model_dump(),
            "excluded": list(EXCLUDED),
            "secrets_excluded": excluded,
            "collections": collections,
            "files": files,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for name, content in entries.items():
                archive.writestr(name, content)
            for name, path in parts.items():
                archive.write(path, name)
        partial.replace(target)
    return ExportInfo(file_name=target.name, product=profile.product, contents=contents, secrets_excluded=excluded)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---- delete everything ------------------------------------------------------------------------------------------


def _empty_database(connection: sqlite3.Connection) -> None:
    """Delete every row of every table (the full-text index by a rebuild), overwrite the freed pages, rebuild the file
    and empty the write-ahead log."""
    connection.execute("PRAGMA secure_delete = ON")
    tables = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    connection.execute("BEGIN IMMEDIATE")
    try:
        for name, sql in tables:
            if _FTS.match(name):
                continue
            try:
                connection.execute(f"DELETE FROM main.{quote_identifier(name)}")
            except sqlite3.Error:
                # A virtual table whose module this connection has not loaded (the legacy vector table): its
                # shadow tables are ordinary tables and are emptied in this loop.
                if not str(sql or "").upper().startswith("CREATE VIRTUAL"):
                    raise
        if any(name == "index_fts" for name, _sql in tables):
            connection.execute("INSERT INTO main.index_fts(index_fts) VALUES ('rebuild')")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
    connection.execute("VACUUM")
    busy, _log, _moved = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        logger.warning("Delete everything: the write-ahead log could not be emptied yet (a reader is open)")


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def delete_all(nodestro: Nodestro, profile: BackupProfile | None = None) -> DeleteResult:
    """Delete every piece of data of the workspace (see the module's docstring). The caller confirmed it."""
    profile = profile or profile_of(nodestro)
    with exclusive():
        busy = nodestro.background_work()
        if busy:
            raise NodestroError(
                "data.busy",
                "Wait until all runs and background work have finished, then delete the data",
                work=busy[:20],
            )
        contents = current_contents(nodestro, profile)
        with nodestro.sqlite_store.maintenance() as connection:
            _empty_database(connection)
        removed = 0
        for path in sorted(profile.data_dir.iterdir()):
            if path.name in KEPT and not path.is_symlink():
                continue
            _remove(path)
            removed += 1
        nodestro.reload_after_restore()
    logger.warning("Delete everything: every piece of data in the data directory was deleted (%d entries)", removed)
    return DeleteResult(deleted_at=now(), contents=contents, removed_entries=removed)
