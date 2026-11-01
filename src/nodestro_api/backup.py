"""Complete local backups: export everything to one file, check it, restore it.

A backup is a ZIP file with

- ``manifest.json``: format, versions, creation time, contents and a SHA-256 per file,
- ``nodestro.db``: a consistent snapshot of the database (SQLite backup API): workflows
  and their versions, agent templates, runs and traces, settings, favorites and tags,
  the usage ledger, the Nodestro Assistant's conversations with their attached snapshots,
  knowledge files with their search index, and all memory spaces,
- ``tools/<name>/v<n>.json``: the hand-written and generated tools of the registry,
- ``inputs/catalog.json`` and ``inputs/blobs/<id>``: the files uploaded as inputs of runs
  (their metadata is in the database), so paused and continued runs find them after a restore.

The Anthropic API key and the keys of published APIs are never part of a backup; they stay
on this computer and are kept when a backup is restored. Published APIs (publications, their
immutable versions and export records) are part of it; network access for them is switched
off after a restore and has to be switched on again deliberately.

Restoring takes two steps. :func:`inspect_backup` checks the file completely (ZIP
structure, paths, checksums, database integrity, every workflow, template and run) and
keeps it as a pending restore; nothing changes yet. :func:`restore_backup` replaces the
data only after the user confirmed: first it saves the current state as a safety
backup in ``<data dir>/backups`` and copies the backup's tool and input files next to the
current ones, then it copies all tables in one transaction (all or nothing) and finally
swaps the directories. One restore runs at a time, and checking another backup waits for it;
an archive that would not fit on the disk when unpacked is refused before it is unpacked, and
zipfile never unpacks an entry beyond its declared size.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import threading
from typing import TYPE_CHECKING, Any
import uuid
import zipfile
import zlib

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import has_credentials
from nodestro_runtime.schemas import AgentTemplate, Run, Workflow, now
from nodestro_runtime.storage import SCHEMA_VERSION
from orkexa import __version__ as library_version
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .assistant import ChatMessage, Conversation

if TYPE_CHECKING:
    from .context import Nodestro

FORMAT = "nodestro-backup"
FORMAT_VERSION = 1
_TOOL_FILE = re.compile(r"^tools/[A-Za-z_][A-Za-z0-9_]*/v\d+\.json$")
_INPUT_FILE = re.compile(r"^inputs/(catalog\.json|blobs/[0-9a-f]{16})$")
#: Tables that SQLite or the full-text index maintain themselves; they are rebuilt, not copied.
_DERIVED = re.compile(r"^(sqlite_|index_fts|index_vec)")
_SECRET_SETTINGS = ("api_key",)
#: The providers' keys (and base URLs with credentials in them) stay on this computer as well.
_PROVIDER_SECRETS = "provider_keys"
#: Collections that stay on this computer like the provider keys: the keys of published APIs
#: (they are credentials, even hashed) and the API's idempotency records. A restore keeps the
#: current ones.
_LOCAL_COLLECTIONS = ("api_keys", "api_idempotency")


#: Space that has to stay free on the disk after unpacking a backup.
_FREE_SPACE_RESERVE = 200 * 1024 * 1024
#: One restore at a time: a second one would copy over a half-restored database.
_RESTORE_LOCK = threading.Lock()


class BackupError(NodestroError):
    """The file is not a backup Nodestro can restore."""


class BackupContents(BaseModel):
    workflows: int = 0
    versions: int = 0
    templates: int = 0
    runs: int = 0
    knowledge_files: int = 0
    memory_spaces: int = 0
    memory_files: int = 0
    tools: int = 0
    #: Files uploaded as inputs of runs.
    input_files: int = 0
    usage_entries: int = 0
    conversations: int = 0


class BackupInfo(BaseModel):
    """What the user confirms before a restore."""

    id: str
    file_name: str
    created_at: datetime
    nodestro_version: str
    library_version: str
    contents: BackupContents
    #: The data that would be replaced.
    current: BackupContents
    #: Codes of things worth knowing (e.g. ``backup.newer_version``).
    notes: list[str] = Field(default_factory=list)


class RestoreResult(BaseModel):
    contents: BackupContents
    safety_backup: str


class LocalBackup(BaseModel):
    name: str
    size: int
    created_at: datetime


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _count(connection: sqlite3.Connection) -> BackupContents:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def scalar(query: str, *params: Any) -> int:
        return int(connection.execute(query, params).fetchone()[0] or 0)

    contents = BackupContents()
    if "nodestro_documents" in tables:
        documents = "SELECT count(*) FROM nodestro_documents WHERE collection = ?"
        contents.workflows = scalar(documents, "workflows")
        contents.versions = scalar(documents, "workflow_versions")
        contents.templates = scalar(documents, "templates")
        contents.runs = scalar(documents, "runs")
        contents.usage_entries = scalar(documents, "usage_log")
        contents.conversations = scalar(documents, "assistant_conversations")
        contents.input_files = scalar(documents, "input_files")
    if "stored_files" in tables:
        contents.knowledge_files = scalar("SELECT count(*) FROM stored_files WHERE namespace = 'nodestro'")
    if "memory_files" in tables:
        prefix = "nodestro:"
        contents.memory_files = scalar(
            "SELECT count(*) FROM memory_files WHERE substr(namespace, 1, ?) = ?", len(prefix), prefix
        )
        contents.memory_spaces = scalar(
            "SELECT count(DISTINCT namespace) FROM memory_files WHERE substr(namespace, 1, ?) = ?", len(prefix), prefix
        )
    return contents


def current_contents(nodestro: Nodestro) -> BackupContents:
    connection = sqlite3.connect(nodestro.sqlite_store.database, timeout=30)
    try:
        contents = _count(connection)
    finally:
        connection.close()
    contents.tools = len(list((nodestro.config.data_dir / "tools").glob("*/v*.json")))
    return contents


def backup_dir(nodestro: Nodestro) -> Path:
    directory = nodestro.config.data_dir / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def create_backup(nodestro: Nodestro, target: Path) -> BackupContents:
    """Write a complete backup of the current data to ``target`` (a .zip file)."""
    with tempfile.TemporaryDirectory(prefix="nodestro-backup-") as directory:
        database = Path(directory) / "nodestro.db"
        nodestro.sqlite_store.snapshot(database)
        snapshot = sqlite3.connect(database)
        try:
            row = snapshot.execute(
                "SELECT data FROM nodestro_documents WHERE collection = 'settings' AND id = 'app'"
            ).fetchone()
            if row is not None:
                settings = _without_secrets(json.loads(row[0]))
                snapshot.execute(
                    "UPDATE nodestro_documents SET data = ? WHERE collection = 'settings' AND id = 'app'",
                    (json.dumps(settings),),
                )
                snapshot.commit()
            snapshot.execute(
                f"DELETE FROM nodestro_documents WHERE collection IN ({', '.join('?' for _ in _LOCAL_COLLECTIONS)})",
                _LOCAL_COLLECTIONS,
            )
            snapshot.commit()
            snapshot.execute("VACUUM")
            contents = _count(snapshot)
        finally:
            snapshot.close()
        tools_root = nodestro.config.data_dir / "tools"
        tool_files = sorted(tools_root.glob("*/v*.json")) if tools_root.is_dir() else []
        contents.tools = len(tool_files)
        files = {"nodestro.db": _sha256(database)}
        for path in tool_files:
            files[f"tools/{path.parent.name}/{path.name}"] = _sha256(path)
        input_files = _input_files(nodestro.config.data_dir / "inputs")
        for name, path in input_files.items():
            files[name] = _sha256(path)
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "created_at": now().isoformat(),
            "nodestro_version": __version__,
            "library_version": library_version,
            "contents": contents.model_dump(),
            "excluded": [*_SECRET_SETTINGS, _PROVIDER_SECRETS, *_LOCAL_COLLECTIONS],
            "files": files,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.write(database, "nodestro.db")
            for path in tool_files:
                archive.write(path, f"tools/{path.parent.name}/{path.name}")
            for name, path in input_files.items():
                archive.write(path, name)
        partial.replace(target)
    return contents


def _without_secrets(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings without credentials: the legacy key, every provider's key, base URLs that carry credentials."""
    for key in _SECRET_SETTINGS:
        settings.pop(key, None)
    for entry in (settings.get("providers") or {}).values():
        if isinstance(entry, dict):
            entry.pop("api_key", None)
            if entry.get("base_url") and has_credentials(str(entry["base_url"])):
                entry.pop("base_url", None)
    return settings


def _keep_secrets(settings: dict[str, Any], current: dict[str, Any]) -> None:
    """Put this computer's credentials (from ``current``) into restored settings."""
    for key in _SECRET_SETTINGS:
        if key in current:
            settings[key] = current[key]
    providers = settings.setdefault("providers", {})
    for name, entry in (current.get("providers") or {}).items():
        if not isinstance(entry, dict):
            continue
        target = providers.setdefault(name, {})
        if entry.get("api_key"):
            target["api_key"] = entry["api_key"]
        if entry.get("base_url") and has_credentials(str(entry["base_url"])):
            target["base_url"] = entry["base_url"]
    if not providers:
        settings.pop("providers")


def _input_files(root: Path) -> dict[str, Path]:
    """The stored input files (catalog and blobs) by their path in a backup."""
    found: dict[str, Path] = {}
    if (root / "catalog.json").is_file():
        found["inputs/catalog.json"] = root / "catalog.json"
    for path in sorted((root / "blobs").glob("*")) if (root / "blobs").is_dir() else []:
        if re.fullmatch(r"[0-9a-f]{16}", path.name) and path.is_file():
            found[f"inputs/blobs/{path.name}"] = path
    return found


def backup_file_name(prefix: str = "nodestro-backup") -> str:
    return f"{prefix}-{now().strftime('%Y%m%d-%H%M%S')}.zip"


# ---- checking and restoring -----------------------------------------------------------


def _pending_root(nodestro: Nodestro) -> Path:
    return backup_dir(nodestro) / ".pending"


def _fail(code: str, message: str, **params: Any) -> BackupError:
    return BackupError(code, message, **params)


def _document_models() -> dict[str, type[BaseModel]]:
    """The model of each collection whose documents are read with one model (lists and look-ups of the app).

    Collections not named here (settings, caches, keys of mixed documents) are checked for valid JSON only.
    Imported here: these modules import much of the app, and the backup module is imported early."""
    from nodestro_runtime.input_files import InputFile
    from nodestro_runtime.python_envs import PythonEnvironment
    from nodestro_runtime.run_chat import RunMessage
    from nodestro_runtime.tool_approvals import ToolApprovalRequest
    from nodestro_runtime.tool_drafts import ToolDraft
    from nodestro_runtime.tool_provenance import AuthoredVersions
    from nodestro_runtime.usage_log import UsageEntry
    from nodestro_runtime.versions import WorkflowVersion

    from .api_guide import ApiGuide
    from .assistant_context import Snapshot
    from .batches import Batch
    from .benchmarks import RUNS, SUITES, BenchmarkRun, Suite
    from .exporter import EXPORTS, ExportRecord
    from .organize import Organize
    from .publishing import PUBLICATIONS, RELEASES, Publication, Release
    from .schedules import Schedule

    return {
        "workflows": Workflow,
        "workflow_versions": WorkflowVersion,
        "templates": AgentTemplate,
        "runs": Run,
        "schedules": Schedule,
        "batches": Batch,
        SUITES: Suite,
        RUNS: BenchmarkRun,
        PUBLICATIONS: Publication,
        RELEASES: Release,
        EXPORTS: ExportRecord,
        "api_guides": ApiGuide,
        "organize": Organize,
        "assistant_conversations": Conversation,
        "assistant_messages": ChatMessage,
        "assistant_snapshots": Snapshot,
        "input_files": InputFile,
        "usage_log": UsageEntry,
        "python_envs": PythonEnvironment,
        "tool_drafts": ToolDraft,
        "tool_provenance": AuthoredVersions,
        "tool_approvals": ToolApprovalRequest,
        "run_messages": RunMessage,
    }


def _check_database(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise _fail("backup.database_invalid", f"The database cannot be opened: {exc}") from None
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise _fail("backup.database_invalid", f"The database is damaged: {result}")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise _fail(
                "backup.schema_too_new",
                f"The backup was made by a newer Nodestro (schema version {version}; this one reads {SCHEMA_VERSION})",
                found=version,
                supported=SCHEMA_VERSION,
            )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(nodestro_documents)")}
        if not {"collection", "id", "data"} <= columns:
            raise _fail("backup.database_invalid", "The database has no Nodestro documents")
        # Every document the restore writes must at least be JSON; those of the collections Nodestro reads as a
        # whole must also match their model, or one damaged document would break a list after the restore (C-13).
        damaged = connection.execute(
            "SELECT collection, id FROM nodestro_documents WHERE NOT json_valid(data) LIMIT 1"
        ).fetchone()
        if damaged is not None:
            raise _fail(
                "backup.document_invalid",
                f"{damaged[0]}/{damaged[1]} is damaged: invalid JSON",
                collection=damaged[0],
                id=damaged[1],
            )
        for collection, model in _document_models().items():
            if collection in _LOCAL_COLLECTIONS:
                continue  # never restored: the current ones are kept
            for key, data in connection.execute(
                "SELECT id, data FROM nodestro_documents WHERE collection = ?", (collection,)
            ):
                try:
                    model.model_validate_json(data)
                except ValidationError as exc:
                    raise _fail(
                        "backup.document_invalid",
                        f"{collection}/{key} is damaged: {exc.errors()[0]['msg']}",
                        collection=collection,
                        id=key,
                    ) from None
    except sqlite3.Error as exc:
        raise _fail("backup.database_invalid", f"The database cannot be read: {exc}") from None
    finally:
        connection.close()


@contextmanager
def _reading(name: str) -> Iterator[None]:
    """Turn a damaged entry into a clear refusal instead of an internal error.

    zipfile stops at an entry's declared size and then checks its CRC, so an entry whose data
    does not match its header (a lying size, a wrong CRC, a cut-off stream) ends here.
    """
    try:
        yield
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise _fail("backup.checksum", f"{name} is damaged (checksum mismatch)", name=name) from None
    except (RuntimeError, NotImplementedError):
        # Encrypted entries and compression methods Nodestro never writes.
        raise _fail("backup.unexpected_file", f"Unexpected file in the backup: {name}", name=name) from None


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    with _reading(name):
        return archive.read(name)


def inspect_backup(nodestro: Nodestro, upload: Path, file_name: str) -> BackupInfo:
    """Check an uploaded backup completely and keep it for :func:`restore_backup`."""
    # Checking replaces the waiting backup; a restore running meanwhile reads from it, so the
    # check waits until that restore has finished.
    with _RESTORE_LOCK:
        return _inspect(nodestro, upload, file_name)


def _inspect(nodestro: Nodestro, upload: Path, file_name: str) -> BackupInfo:
    if not zipfile.is_zipfile(upload):
        raise _fail("backup.not_zip", "The file is not a Nodestro backup (no ZIP file)")
    root = _pending_root(nodestro)
    shutil.rmtree(root, ignore_errors=True)
    backup_id = uuid.uuid4().hex[:12]
    target = root / backup_id
    target.mkdir(parents=True)
    try:
        try:
            archive = zipfile.ZipFile(upload)
        except (zipfile.BadZipFile, OSError, EOFError):
            raise _fail("backup.not_zip", "The file is not a Nodestro backup (no ZIP file)") from None
        with archive:
            names = archive.namelist()
            if "manifest.json" not in names or "nodestro.db" not in names:
                raise _fail("backup.incomplete", "The backup has no manifest or no database")
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise _fail("backup.unsafe_path", f"Unsafe path in the backup: {name}", name=name)
                if (
                    name not in ("manifest.json", "nodestro.db")
                    and not _TOOL_FILE.match(name)
                    and not _INPUT_FILE.match(name)
                ):
                    raise _fail("backup.unexpected_file", f"Unexpected file in the backup: {name}", name=name)
            try:
                manifest = json.loads(_read(archive, "manifest.json"))
            except ValueError:
                raise _fail("backup.manifest_invalid", "The manifest is not valid JSON") from None
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise _fail("backup.manifest_invalid", "The manifest does not describe a Nodestro backup")
            version = manifest.get("format_version")
            if not isinstance(version, int) or version > FORMAT_VERSION:
                raise _fail(
                    "backup.format_newer", f"Backup format {version} is newer than this Nodestro", version=version
                )
            listed = manifest.get("files")
            if not isinstance(listed, dict) or set(listed) != set(names) - {"manifest.json"}:
                raise _fail("backup.incomplete", "The files in the backup do not match its manifest")
            # A small archive can unpack to far more than the disk holds (a "ZIP bomb"); zipfile
            # never reads more than the declared size of an entry, so checking those sizes suffices.
            unpacked = sum(info.file_size for info in archive.infolist())
            free = shutil.disk_usage(target).free
            if unpacked > free - _FREE_SPACE_RESERVE:
                raise _fail(
                    "backup.too_large",
                    f"Unpacking the backup needs {unpacked // 2**20} MB, but only {free // 2**20} MB are free",
                    needed_mb=unpacked // 2**20,
                    free_mb=free // 2**20,
                )
            for name in names:
                if name == "manifest.json":
                    continue
                destination = target / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with _reading(name), archive.open(name) as source, destination.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                if _sha256(destination) != listed[name]:
                    raise _fail("backup.checksum", f"{name} is damaged (checksum mismatch)", name=name)
            (target / "manifest.json").write_bytes(_read(archive, "manifest.json"))
        _check_database(target / "nodestro.db")
        for tool in target.glob("tools/*/v*.json"):
            try:
                json.loads(tool.read_text(encoding="utf-8"))
            except ValueError:
                raise _fail(
                    "backup.document_invalid", f"Tool file {tool.name} is damaged", collection="tools"
                ) from None
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    connection = sqlite3.connect(target / "nodestro.db")
    try:
        contents = _count(connection)
    finally:
        connection.close()
    contents.tools = len(list(target.glob("tools/*/v*.json")))
    created = manifest.get("created_at")
    notes = []
    if str(manifest.get("nodestro_version")) != __version__:
        notes.append("backup.other_version")
    return BackupInfo(
        id=backup_id,
        file_name=file_name,
        created_at=datetime.fromisoformat(created) if isinstance(created, str) else now(),
        nodestro_version=str(manifest.get("nodestro_version", "")),
        library_version=str(manifest.get("library_version", "")),
        contents=contents,
        current=current_contents(nodestro),
        notes=notes,
    )


def _copy_tables(connection: sqlite3.Connection, source: Path, keep_settings: dict[str, Any]) -> None:
    """Replace every table's rows with the backup's, in one transaction."""
    connection.execute("ATTACH DATABASE ? AS restore_source", (str(source),))
    try:
        backup_tables = {
            row[0]: row[1]
            for row in connection.execute("SELECT name, sql FROM restore_source.sqlite_master WHERE type = 'table'")
            if not _DERIVED.match(row[0])
        }
        # ATTACH creates an empty database for a missing file; copying from it would empty every table.
        if "nodestro_documents" not in backup_tables:
            raise _fail("backup.expired", "This backup is no longer waiting to be restored; please choose it again")
        main_tables = {row[0] for row in connection.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        marks = ", ".join("?" for _ in _LOCAL_COLLECTIONS)
        kept = connection.execute(
            f"SELECT collection, id, data FROM main.nodestro_documents WHERE collection IN ({marks})",
            _LOCAL_COLLECTIONS,
        ).fetchall()
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name, create in backup_tables.items():
                if name not in main_tables:
                    connection.execute(create)
                main_columns = [row[1] for row in connection.execute(f'PRAGMA main.table_info("{name}")')]
                source_columns = {row[1] for row in connection.execute(f'PRAGMA restore_source.table_info("{name}")')}
                columns = ", ".join(f'"{column}"' for column in main_columns if column in source_columns)
                connection.execute(f'DELETE FROM main."{name}"')
                connection.execute(
                    f'INSERT INTO main."{name}" ({columns}) SELECT {columns} FROM restore_source."{name}"'
                )
            for name in main_tables - set(backup_tables):
                if not _DERIVED.match(name):
                    connection.execute(f'DELETE FROM main."{name}"')
            if "index_fts" in main_tables:
                connection.execute("INSERT INTO main.index_fts(index_fts) VALUES ('rebuild')")
            row = connection.execute(
                "SELECT data FROM main.nodestro_documents WHERE collection = 'settings' AND id = 'app'"
            ).fetchone()
            settings = _without_secrets(json.loads(row[0]) if row is not None else {})
            _keep_secrets(settings, keep_settings)
            connection.execute(f"DELETE FROM main.nodestro_documents WHERE collection IN ({marks})", _LOCAL_COLLECTIONS)
            connection.executemany("INSERT INTO main.nodestro_documents (collection, id, data) VALUES (?, ?, ?)", kept)
            connection.execute(
                "INSERT INTO main.nodestro_documents (collection, id, data) VALUES ('settings', 'app', ?)"
                " ON CONFLICT (collection, id) DO UPDATE SET data = excluded.data",
                (json.dumps(settings),),
            )
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
    finally:
        connection.execute("DETACH DATABASE restore_source")


def restore_backup(nodestro: Nodestro, backup_id: str) -> RestoreResult:
    """Replace all data with a checked backup; the current state is saved first."""
    if not re.fullmatch(r"[0-9a-f]{12}", backup_id):
        raise KeyError(backup_id)
    if not _RESTORE_LOCK.acquire(blocking=False):
        raise _fail("backup.restore_running", "A backup is being restored already; wait until it has finished")
    try:
        return _restore(nodestro, backup_id)
    finally:
        _RESTORE_LOCK.release()


def _restore(nodestro: Nodestro, backup_id: str) -> RestoreResult:
    source = _pending_root(nodestro) / backup_id
    if not (source / "nodestro.db").is_file():
        raise _fail("backup.expired", "This backup is no longer waiting to be restored; please choose it again")
    if nodestro.runs.has_live_runs():
        raise _fail("backup.runs_active", "Wait until all runs have finished, then restore the backup")
    safety = backup_dir(nodestro) / backup_file_name("before-restore")
    create_backup(nodestro, safety)
    data_dir = nodestro.config.data_dir
    # Copy the tool and input files next to the current ones first: what can fail (a full disk)
    # fails before anything is replaced. Afterwards only renames follow the database transaction.
    staged = data_dir / f".tools-restore-{backup_id}"
    staged_inputs = data_dir / f".inputs-restore-{backup_id}"
    try:
        shutil.rmtree(staged, ignore_errors=True)
        if (source / "tools").is_dir():
            shutil.copytree(source / "tools", staged)
        else:
            staged.mkdir()
        # The input files of runs (a backup without them restores none).
        shutil.rmtree(staged_inputs, ignore_errors=True)
        if (source / "inputs").is_dir():
            shutil.copytree(source / "inputs", staged_inputs)
        else:
            (staged_inputs / "blobs").mkdir(parents=True)
        keep = nodestro.store.get_raw("settings", "app") or {}
        with nodestro.sqlite_store.maintenance() as connection:
            _copy_tables(connection, source / "nodestro.db", keep)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(staged_inputs, ignore_errors=True)
        raise
    tools = data_dir / "tools"
    _swap(staged, tools, data_dir / f".tools-old-{backup_id}")
    _swap(staged_inputs, data_dir / "inputs", data_dir / f".inputs-old-{backup_id}")
    nodestro.reload_after_restore()
    connection = sqlite3.connect(nodestro.sqlite_store.database, timeout=30)
    try:
        contents = _count(connection)
    finally:
        connection.close()
    contents.tools = len(list(tools.glob("*/v*.json")))
    shutil.rmtree(source, ignore_errors=True)
    return RestoreResult(contents=contents, safety_backup=safety.name)


def _swap(staged: Path, current: Path, old: Path) -> None:
    """Put the staged directory in place of the current one (two renames on the same disk)."""
    shutil.rmtree(old, ignore_errors=True)
    if current.exists():
        current.replace(old)
    staged.replace(current)
    shutil.rmtree(old, ignore_errors=True)


def local_backups(nodestro: Nodestro) -> list[LocalBackup]:
    """The backups kept in the data directory (safety copies made before restores)."""
    found = []
    for path in backup_dir(nodestro).glob("*.zip"):
        stat = path.stat()
        found.append(
            LocalBackup(
                name=path.name, size=stat.st_size, created_at=datetime.fromtimestamp(stat.st_mtime).astimezone()
            )
        )
    return sorted(found, key=lambda item: item.created_at, reverse=True)


def local_backup_path(nodestro: Nodestro, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.zip", name):
        raise KeyError(name)
    path = backup_dir(nodestro) / name
    if not path.is_file():
        raise KeyError(name)
    return path
