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

Secrets are never part of a backup: provider keys (and base URLs with credentials in them), the sealed tokens of MCP
services and pending OAuth sign-ins, the sealed secrets of service connections, the keys of published APIs, their
idempotency records and the check value of the data directory's key. They stay on this computer and are kept when a
backup is restored; the manifest names the secrets that were left out (``secrets_excluded``: ``provider:<name>``,
``mcp:<id>``, ``connection:<id>``), and checking and restoring a backup list those this computer does not have
(``secrets_to_reenter``), so the user enters them again. Published APIs (publications, their immutable versions and
export records) are part of it; network access for them is switched off after a restore and has to be switched on
again deliberately.

The search index of a granted folder (N-12) is never part of a backup or an export either (review M-1): its chunks,
their vectors and full-text entries, and its manifest and state (``folder_index_files``, ``folder_index_state``) are
removed from the snapshot. The folder's text belongs to the folder and its grant; the workspace builds the index again
from the folder while the grant holds (also after a restore).

The core is product-neutral (N-15): it works on a :class:`BackupProfile` (the product id, the data directory and the
parts besides the database), by default the one of the app's configuration (:func:`profile_of`). The format stays
``nodestro-backup`` version 1; the manifest names the ``product`` whose data it holds, and a backup of another
product is refused (``backup.product_mismatch``). A backup without the field (every backup made before it existed)
belongs to :data:`LEGACY_PRODUCT` and stays readable. :mod:`nodestro_api.data_lifecycle` builds "export everything"
and "delete everything" on the same core.

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
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import os
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

from nodestro_runtime.folder_index import FILES as FOLDER_INDEX_FILES
from nodestro_runtime.folder_index import STATES as FOLDER_INDEX_STATES
from nodestro_runtime.knowledge import FOLDER_SOURCE
from nodestro_runtime.memory_spaces import ORIGINS
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import has_credentials
from nodestro_runtime.schemas import (
    AgentTemplate,
    AgentTemplateVersion,
    Run,
    Skill,
    SkillGroup,
    SkillVersion,
    Workflow,
    now,
)
from nodestro_runtime.storage import SCHEMA_VERSION, strip_index_sources
from orkexa import __version__ as library_version
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .assistant import ChatMessage, Conversation

if TYPE_CHECKING:
    from .context import Nodestro

logger = logging.getLogger(__name__)

FORMAT = "nodestro-backup"
FORMAT_VERSION = 1
#: The product of a backup whose manifest names none: every backup made before N-15 was the studio's.
LEGACY_PRODUCT = "nodestro"
#: A product id: lower-case letters, digits and ``-``, starting with a letter.
PRODUCT_ID = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
#: The parts of a backup besides the database: the tool files and the input files of runs.
COMPONENTS = ("tools", "inputs")
_TOOL_FILE = re.compile(r"^tools/[A-Za-z_][A-Za-z0-9_]*/v\d+\.json$")
_INPUT_FILE = re.compile(r"^inputs/(catalog\.json|blobs/[0-9a-f]{16})$")
#: Columns a table of this version has that an older database of this computer may lack (N-24: the origin of a memory
#: file), each with its definition: a restore adds them before it copies, so a backup's values are never left out.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {"memory_files": {"origin": "TEXT"}}
#: Tables that SQLite, the full-text index or the legacy ``vec0`` vector table (``index_vec`` and its shadow tables)
#: maintain themselves; they are not copied. The vectors of the semantic index (``index_spaces``, ``index_vectors``)
#: are data: they belong to the chunks of the same backup and are restored with them.
_DERIVED = re.compile(r"^(sqlite_|index_fts|index_vec($|_))")
#: The tables a restore copies, with the definition it creates a missing one with (API2-11): Nodestro's documents and
#: the tables of Orkexa's SQLite stores (the same statements as ``orkexa.retrieval``, ``memory`` and ``files``, which
#: create them on first use). A restore never runs SQL from the archive; other tables in a backup are left out.
KNOWN_TABLES: dict[str, str] = {
    "nodestro_documents": (
        "CREATE TABLE IF NOT EXISTS nodestro_documents (collection TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,"
        " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), PRIMARY KEY (collection, id))"
    ),
    "memory_files": (
        "CREATE TABLE IF NOT EXISTS memory_files (namespace TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, origin TEXT, PRIMARY KEY (namespace, path))"
    ),
    "index_chunks": (
        "CREATE TABLE IF NOT EXISTS index_chunks (id INTEGER PRIMARY KEY, namespace TEXT NOT NULL,"
        " source TEXT NOT NULL, title TEXT, file_id TEXT, metadata TEXT NOT NULL, text TEXT NOT NULL)"
    ),
    "index_meta": "CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "index_spaces": (
        "CREATE TABLE IF NOT EXISTS index_spaces (namespace TEXT NOT NULL, space TEXT NOT NULL, identity TEXT NOT NULL,"
        " dimensions INTEGER NOT NULL, state TEXT NOT NULL, PRIMARY KEY (namespace, space))"
    ),
    "index_vectors": (
        "CREATE TABLE IF NOT EXISTS index_vectors (space TEXT NOT NULL, chunk_id INTEGER NOT NULL,"
        " embedding BLOB NOT NULL, PRIMARY KEY (space, chunk_id))"
    ),
    "stored_files": (
        "CREATE TABLE IF NOT EXISTS stored_files (namespace TEXT NOT NULL, id TEXT NOT NULL, entry TEXT NOT NULL,"
        " data BLOB NOT NULL, PRIMARY KEY (namespace, id))"
    ),
}


def quote_identifier(name: str) -> str:
    """``name`` as an SQLite identifier: in double quotes, inner double quotes doubled."""
    return '"' + name.replace('"', '""') + '"'


_SECRET_SETTINGS = ("api_key",)
#: The providers' keys (and base URLs with credentials in them) stay on this computer as well.
_PROVIDER_SECRETS = "provider_keys"
#: Collections that stay on this computer like the provider keys: the keys of published APIs
#: (they are credentials, even hashed), the API's idempotency records and the check value of the
#: data directory's key (nodestro_runtime.local_vault). A restore keeps the current ones.
_LOCAL_COLLECTIONS = ("api_keys", "api_idempotency", "local_vault")
#: Sealed secrets in documents besides the settings (see nodestro_runtime.local_vault): the token of an MCP service, a
#: pending OAuth sign-in and the secret of a service connection, by collection, with the label kind of each.
_SEALED_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "mcp_servers": ("mcp", ("token_sealed", "oauth_pending_sealed")),
    "service_connections": ("connection", ("secret_sealed",)),
}
#: Where a document says how its secret is stored (``vault``: sealed in the document itself).
_STORAGE_FIELDS = {"mcp_servers": "token_storage", "service_connections": "secret_storage"}
#: What the manifest lists as ``excluded``.
EXCLUDED = (*_SECRET_SETTINGS, _PROVIDER_SECRETS, *_LOCAL_COLLECTIONS, "mcp_tokens", "connection_secrets")
#: A secret's label in ``secrets_excluded`` and ``secrets_to_reenter`` (the manifest is data: other labels are dropped).
_SECRET_LABEL = re.compile(r"^(provider|mcp|connection):[A-Za-z0-9_.:-]{1,200}$")


@dataclass(frozen=True)
class BackupProfile:
    """What the backup core works on: the ``product`` whose data it is (:data:`PRODUCT_ID`), its ``data_dir`` (the
    tool and input files, ``backups/``) and the ``components`` besides the database (a subset of
    :data:`COMPONENTS`). A component left out is neither written nor replaced by a restore."""

    product: str
    data_dir: Path
    components: tuple[str, ...] = COMPONENTS

    def __post_init__(self) -> None:
        if not PRODUCT_ID.fullmatch(self.product):
            raise ValueError(f"invalid product id: {self.product!r}")
        unknown = set(self.components) - set(COMPONENTS)
        if unknown:
            raise ValueError(f"unknown backup components: {sorted(unknown)}")


def profile_of(nodestro: Nodestro) -> BackupProfile:
    """The profile of the app's configuration (``AppConfig.product_id`` and ``data_dir``), with every component."""
    return BackupProfile(
        product=getattr(nodestro.config, "product_id", LEGACY_PRODUCT), data_dir=nodestro.config.data_dir
    )


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
    #: The product whose data the backup holds (``nodestro`` for a backup without the field).
    product: str = LEGACY_PRODUCT
    nodestro_version: str
    library_version: str
    contents: BackupContents
    #: The data that would be replaced.
    current: BackupContents
    #: Codes of things worth knowing (e.g. ``backup.newer_version``).
    notes: list[str] = Field(default_factory=list)
    #: Secrets the backup's data used that it does not hold and this computer does not have either; they have to be
    #: entered again after the restore (``provider:<name>``, ``mcp:<id>``, ``connection:<id>``).
    secrets_to_reenter: list[str] = Field(default_factory=list)


class RestoreResult(BaseModel):
    contents: BackupContents
    safety_backup: str
    #: As in :class:`BackupInfo`: the secrets to enter again now.
    secrets_to_reenter: list[str] = Field(default_factory=list)


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


def current_contents(nodestro: Nodestro, profile: BackupProfile | None = None) -> BackupContents:
    profile = profile or profile_of(nodestro)
    connection = sqlite3.connect(nodestro.sqlite_store.database, timeout=30)
    try:
        contents = _count(connection)
    finally:
        connection.close()
    contents.tools = len(list((profile.data_dir / "tools").glob("*/v*.json")))
    return contents


def backup_dir(nodestro: Nodestro, profile: BackupProfile | None = None) -> Path:
    directory = (profile or profile_of(nodestro)).data_dir / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    _private(directory, tree=False)
    return directory


def _private(path: Path, *, tree: bool = True) -> None:
    """Only the owner may read what a backup holds: folders ``0700``, files ``0600`` (POSIX; on Windows the data
    directory's permissions apply). ``tree``: everything below ``path`` as well; symbolic links are left alone."""
    if os.name != "posix" or path.is_symlink() or not path.exists():
        return
    for item in [path, *(path.rglob("*") if tree and path.is_dir() else ())]:
        if not item.is_symlink():
            item.chmod(0o700 if item.is_dir() else 0o600)


def _secrets_in(connection: sqlite3.Connection) -> set[str]:
    """The labels of the secrets a database holds (plain or sealed): ``provider:<name>`` for a provider's key or a
    base URL with credentials, ``mcp:<id>`` and ``connection:<id>`` for sealed tokens and connection secrets."""
    found: set[str] = set()
    row = connection.execute(
        "SELECT data FROM nodestro_documents WHERE collection = 'settings' AND id = 'app'"
    ).fetchone()
    settings = json.loads(row[0]) if row is not None else {}
    if isinstance(settings, dict):
        if settings.get("api_key"):
            found.add("provider:anthropic")  # the top-level key of old releases is Anthropic's
        providers = settings.get("providers")
        for name, entry in (providers if isinstance(providers, dict) else {}).items():
            if isinstance(entry, dict) and (
                entry.get("api_key") or (entry.get("base_url") and has_credentials(str(entry["base_url"])))
            ):
                found.add(f"provider:{name}")
    for collection, (kind, fields) in _SEALED_FIELDS.items():
        for key, data in connection.execute(
            "SELECT id, data FROM nodestro_documents WHERE collection = ? AND json_valid(data)", (collection,)
        ):
            document = json.loads(data)
            if isinstance(document, dict) and any(document.get(field) for field in fields):
                found.add(f"{kind}:{key}")
    return {label for label in found if _SECRET_LABEL.fullmatch(label)}


def _strip_sealed(connection: sqlite3.Connection, schema: str = "main") -> None:
    """Remove the sealed secrets of MCP services and service connections from the documents of ``schema``."""
    for collection, (_kind, fields) in _SEALED_FIELDS.items():
        paths = ", ".join(f"'$.{field}'" for field in fields)
        connection.execute(
            f"UPDATE {schema}.nodestro_documents SET data = json_remove(data, {paths})"
            " WHERE collection = ? AND json_valid(data)",
            (collection,),
        )


def _strip_folder_index(connection: sqlite3.Connection) -> None:
    """Remove every granted folder's index (N-12 review M-1, L-a): its chunks with their vectors and full-text entries,
    and its manifest and state documents (:func:`nodestro_runtime.storage.strip_index_sources`). Runs on the snapshot,
    with ``secure_delete`` on and a ``VACUUM`` after it."""
    strip_index_sources(connection, FOLDER_SOURCE, (FOLDER_INDEX_FILES, FOLDER_INDEX_STATES))


def sanitized_snapshot(nodestro: Nodestro, database: Path) -> tuple[BackupContents, list[str]]:
    """A consistent copy of the database at ``database`` without any secret (see the module's docstring), its contents
    and the labels of the secrets left out. Backups and the data export are made from it."""
    nodestro.sqlite_store.snapshot(database)
    snapshot = sqlite3.connect(database)
    try:
        excluded = sorted(_secrets_in(snapshot))
        # Overwrite what is deleted, so no secret stays in a freed page of the copy (VACUUM rewrites it anyway).
        snapshot.execute("PRAGMA secure_delete = ON")
        row = snapshot.execute(
            "SELECT data FROM nodestro_documents WHERE collection = 'settings' AND id = 'app'"
        ).fetchone()
        if row is not None:
            settings = _without_secrets(json.loads(row[0]))
            snapshot.execute(
                "UPDATE nodestro_documents SET data = ? WHERE collection = 'settings' AND id = 'app'",
                (json.dumps(settings),),
            )
        _strip_sealed(snapshot)
        _strip_folder_index(snapshot)
        snapshot.execute(
            f"DELETE FROM nodestro_documents WHERE collection IN ({', '.join('?' for _ in _LOCAL_COLLECTIONS)})",
            _LOCAL_COLLECTIONS,
        )
        snapshot.commit()
        snapshot.execute("VACUUM")
        contents = _count(snapshot)
    finally:
        snapshot.close()
    return contents, excluded


def component_files(profile: BackupProfile) -> dict[str, Path]:
    """The files of the profile's components by their path in a backup (tool files, then input files)."""
    found: dict[str, Path] = {}
    tools_root = profile.data_dir / "tools"
    if "tools" in profile.components and tools_root.is_dir():
        for path in sorted(tools_root.glob("*/v*.json")):
            found[f"tools/{path.parent.name}/{path.name}"] = path
    if "inputs" in profile.components:
        found.update(_input_files(profile.data_dir / "inputs"))
    return found


def create_backup(nodestro: Nodestro, target: Path, profile: BackupProfile | None = None) -> BackupContents:
    """Write a complete backup of the current data to ``target`` (a .zip file)."""
    profile = profile or profile_of(nodestro)
    with tempfile.TemporaryDirectory(prefix="nodestro-backup-") as directory:
        database = Path(directory) / "nodestro.db"
        contents, excluded = sanitized_snapshot(nodestro, database)
        parts = component_files(profile)
        contents.tools = sum(1 for name in parts if name.startswith("tools/"))
        files = {"nodestro.db": _sha256(database)}
        for name, path in parts.items():
            files[name] = _sha256(path)
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "product": profile.product,
            "components": list(profile.components),
            "created_at": now().isoformat(),
            "nodestro_version": __version__,
            "library_version": library_version,
            "contents": contents.model_dump(),
            "excluded": list(EXCLUDED),
            "secrets_excluded": excluded,
            "files": files,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            archive.write(database, "nodestro.db")
            for name, path in parts.items():
                archive.write(path, name)
        _private(partial)
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


def _pending_root(profile: BackupProfile) -> Path:
    return profile.data_dir / "backups" / ".pending"


def _fail(code: str, message: str, **params: Any) -> BackupError:
    return BackupError(code, message, **params)


def _document_models() -> dict[str, type[BaseModel]]:
    """The model of each collection whose documents are read with one model (lists and look-ups of the app).

    Collections not named here (settings, caches, keys of mixed documents) are checked for valid JSON only.
    Imported here: these modules import much of the app, and the backup module is imported early."""
    from nodestro_runtime.conversations import COLLECTION as OWN_CONVERSATIONS
    from nodestro_runtime.conversations import MESSAGES as OWN_MESSAGES
    from nodestro_runtime.conversations import Conversation as OwnConversation
    from nodestro_runtime.conversations import ConversationMessage as OwnMessage
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
        "agent_template_versions": AgentTemplateVersion,
        "skills": Skill,
        "skill_versions": SkillVersion,
        "skill_groups": SkillGroup,
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
        OWN_CONVERSATIONS: OwnConversation,
        OWN_MESSAGES: OwnMessage,
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


def inspect_backup(
    nodestro: Nodestro, upload: Path, file_name: str, profile: BackupProfile | None = None
) -> BackupInfo:
    """Check an uploaded backup completely and keep it for :func:`restore_backup`."""
    # Checking replaces the waiting backup; a restore running meanwhile reads from it, so the
    # check waits until that restore has finished.
    with _RESTORE_LOCK:
        return _inspect(nodestro, upload, file_name, profile or profile_of(nodestro))


def _binding(collection: str, document: dict[str, Any]) -> Any:
    """What a sealed secret of ``document`` is bound to (threat model T9, ADR-0001): an MCP service's address,
    transport, command and kind of sign-in; a connection's type, provider, reachable hosts and **all** its settings.

    A connection's settings are compared whole: its address lives there (a webhook's ``url``, an SMTP ``host`` and
    ``port``, a search endpoint), and its stored ``egress`` comes from the same document, so a backup could pair an
    old ``egress`` with a new address. Any change of the settings drops the secret, which is stricter than the
    runtime's own rule (a new path on the same host keeps it there); a restore has no reason to be lenient."""
    if collection == "mcp_servers":
        return [document.get(name) for name in ("url", "transport", "stdio", "auth")]
    egress = document.get("egress")
    return [
        document.get("type"),
        document.get("provider"),
        sorted(str(item) for item in egress) if isinstance(egress, list) else egress,
        json.dumps(document.get("settings"), sort_keys=True),
    ]


def _sealed_documents(connection: sqlite3.Connection, schema: str = "main") -> dict[tuple[str, str], dict[str, Any]]:
    """The documents of the collections with sealed secrets, by ``(collection, id)``."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for collection in _SEALED_FIELDS:
        for key, data in connection.execute(
            f"SELECT id, data FROM {schema}.nodestro_documents WHERE collection = ? AND json_valid(data)", (collection,)
        ):
            document = json.loads(data)
            if isinstance(document, dict):
                found[(collection, key)] = document
    return found


def _keepable(
    current: dict[tuple[str, str], dict[str, Any]], restored: dict[tuple[str, str], dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """This computer's sealed secrets (with how they are stored) that a restored document may keep: the same MCP
    service or connection, bound to the same address and use. Any other secret has to be entered again."""
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    for (collection, key), document in current.items():
        values = {field: document[field] for field in _SEALED_FIELDS[collection][1] if document.get(field)}
        other = restored.get((collection, key))
        if values and other is not None and _binding(collection, other) == _binding(collection, document):
            storage = _STORAGE_FIELDS[collection]
            kept[(collection, key)] = {**values, storage: document.get(storage)}
    return kept


def _to_reenter(nodestro: Nodestro, source: Path) -> list[str]:
    """The secrets the waiting backup in ``source`` left out (its manifest's ``secrets_excluded``, and the sealed ones
    in an older backup, which a restore leaves out as well) that this computer does not have."""
    try:
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    listed = manifest.get("secrets_excluded") if isinstance(manifest, dict) else None
    # The manifest is data: only labels of the known form count, and not more than a workspace could hold.
    wanted = {item for item in listed[:1000] if isinstance(item, str)} if isinstance(listed, list) else set()
    backup = sqlite3.connect(f"file:{source / 'nodestro.db'}?mode=ro", uri=True)
    try:
        wanted |= _secrets_in(backup)
        restored = _sealed_documents(backup)
    finally:
        backup.close()
    documents = {f"{_SEALED_FIELDS[collection][0]}:{key}": (collection, key) for collection, key in restored}
    # An MCP service or a connection needs its secret only when the backup holds it.
    wanted = {label for label in wanted if _SECRET_LABEL.fullmatch(label)}
    wanted = {label for label in wanted if label.startswith("provider:") or label in documents}
    current = sqlite3.connect(nodestro.sqlite_store.database, timeout=30)
    try:
        providers = {label for label in _secrets_in(current) if label.startswith("provider:")}
        kept = _keepable(_sealed_documents(current), restored)
    finally:
        current.close()
    present = providers | {label for label, document in documents.items() if document in kept}
    return sorted(wanted - present)


def _inspect(nodestro: Nodestro, upload: Path, file_name: str, profile: BackupProfile) -> BackupInfo:
    if not zipfile.is_zipfile(upload):
        raise _fail("backup.not_zip", "The file is not a Nodestro backup (no ZIP file)")
    root = _pending_root(profile)
    shutil.rmtree(root, ignore_errors=True)
    backup_id = uuid.uuid4().hex[:12]
    target = root / backup_id
    target.mkdir(parents=True)
    for folder in (root.parent, root, target):
        _private(folder, tree=False)
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
            product = manifest.get("product", LEGACY_PRODUCT)
            if product != profile.product:
                raise _fail(
                    "backup.product_mismatch",
                    "The backup holds the data of another product",
                    found=str(product)[:40] if isinstance(product, str) else None,
                    expected=profile.product,
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
        product=profile.product,
        nodestro_version=str(manifest.get("nodestro_version", "")),
        library_version=str(manifest.get("library_version", "")),
        contents=contents,
        current=current_contents(nodestro, profile),
        notes=notes,
        secrets_to_reenter=_to_reenter(nodestro, target),
    )


def _copy_tables(connection: sqlite3.Connection, source: Path, keep_settings: dict[str, Any]) -> None:
    """Replace every table's rows with the backup's, in one transaction."""
    connection.execute("ATTACH DATABASE ? AS restore_source", (str(source),))
    try:
        found = {
            row[0] for row in connection.execute("SELECT name FROM restore_source.sqlite_master WHERE type = 'table'")
        }
        backup_tables = [name for name in KNOWN_TABLES if name in found]
        ignored = sorted(name for name in found - set(KNOWN_TABLES) if not _DERIVED.match(name))
        if ignored:
            logger.warning("Restore: tables unknown to this version are left out: %s", ", ".join(ignored)[:500])
        # ATTACH creates an empty database for a missing file; copying from it would empty every table.
        if "nodestro_documents" not in backup_tables:
            raise _fail("backup.expired", "This backup is no longer waiting to be restored; please choose it again")
        main_tables = {row[0] for row in connection.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        marks = ", ".join("?" for _ in _LOCAL_COLLECTIONS)
        kept = connection.execute(
            f"SELECT collection, id, data FROM main.nodestro_documents WHERE collection IN ({marks})",
            _LOCAL_COLLECTIONS,
        ).fetchall()
        # This computer's MCP services and connections with their sealed secrets, by document.
        current_sealed = _sealed_documents(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name in backup_tables:
                if name not in main_tables:
                    connection.execute(KNOWN_TABLES[name])  # this version's definition, never the archive's
                table = quote_identifier(name)
                main_columns = [row[1] for row in connection.execute(f"PRAGMA main.table_info({table})")]
                for column, definition in _ADDED_COLUMNS.get(name, {}).items():
                    if column not in main_columns:  # constants of this version, never the archive's
                        connection.execute(
                            f"ALTER TABLE main.{table} ADD COLUMN {quote_identifier(column)} {definition}"
                        )
                        main_columns.append(column)
                source_columns = {row[1] for row in connection.execute(f"PRAGMA restore_source.table_info({table})")}
                # Only this version's columns, each quoted (a column name in the archive is data, not SQL).
                columns = ", ".join(quote_identifier(column) for column in main_columns if column in source_columns)
                connection.execute(f"DELETE FROM main.{table}")
                if columns:
                    connection.execute(
                        f"INSERT INTO main.{table} ({columns}) SELECT {columns} FROM restore_source.{table}"
                    )
                if name == "memory_files" and "origin" in source_columns:
                    # An origin this version does not write is unknown (None), never shown as someone's (N-24 L5).
                    known = sorted(ORIGINS)
                    connection.execute(
                        "UPDATE main.memory_files SET origin = NULL WHERE origin IS NOT NULL"
                        f" AND origin NOT IN ({', '.join('?' for _ in known)})",
                        known,
                    )
            for name in main_tables - set(backup_tables):
                if not _DERIVED.match(name):
                    connection.execute(f"DELETE FROM main.{quote_identifier(name)}")
            if "index_fts" in main_tables:
                connection.execute("INSERT INTO main.index_fts(index_fts) VALUES ('rebuild')")
            row = connection.execute(
                "SELECT data FROM main.nodestro_documents WHERE collection = 'settings' AND id = 'app'"
            ).fetchone()
            settings = _without_secrets(json.loads(row[0]) if row is not None else {})
            _keep_secrets(settings, keep_settings)
            # A backup holds no sealed secret (an older one may): those of this computer are kept for the same MCP
            # service or connection bound to the same address and use (T9); the others have to be entered again.
            restored = _sealed_documents(connection)
            kept_sealed = _keepable(current_sealed, restored)
            _strip_sealed(connection)
            for (collection, key), document in restored.items():
                storage = _STORAGE_FIELDS[collection]
                if (collection, key) in kept_sealed:
                    patch = kept_sealed[(collection, key)]
                elif document.get(storage) == "vault":
                    patch = {storage: "none"}  # its sealed secret is gone
                else:
                    continue
                connection.execute(
                    "UPDATE main.nodestro_documents SET data = json_patch(data, ?) WHERE collection = ? AND id = ?",
                    (json.dumps(patch), collection, key),
                )
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


def restore_backup(nodestro: Nodestro, backup_id: str, profile: BackupProfile | None = None) -> RestoreResult:
    """Replace all data with a checked backup; the current state is saved first."""
    if not re.fullmatch(r"[0-9a-f]{12}", backup_id):
        raise KeyError(backup_id)
    if not _RESTORE_LOCK.acquire(blocking=False):
        raise _fail("backup.restore_running", "A backup is being restored already; wait until it has finished")
    try:
        return _restore(nodestro, backup_id, profile or profile_of(nodestro))
    finally:
        _RESTORE_LOCK.release()


@contextmanager
def exclusive() -> Iterator[None]:
    """Hold the lock restores take, without waiting (``backup.restore_running`` while one runs): for other work that
    replaces all data (:func:`nodestro_api.data_lifecycle.delete_all`)."""
    if not _RESTORE_LOCK.acquire(blocking=False):
        raise _fail("backup.restore_running", "A backup is being restored already; wait until it has finished")
    try:
        yield
    finally:
        _RESTORE_LOCK.release()


def _restore(nodestro: Nodestro, backup_id: str, profile: BackupProfile) -> RestoreResult:
    source = _pending_root(profile) / backup_id
    if not (source / "nodestro.db").is_file():
        raise _fail("backup.expired", "This backup is no longer waiting to be restored; please choose it again")
    if nodestro.runs.has_live_runs():
        raise _fail("backup.runs_active", "Wait until all runs have finished, then restore the backup")
    reenter = _to_reenter(nodestro, source)
    safety = backup_dir(nodestro, profile) / backup_file_name("before-restore")
    create_backup(nodestro, safety, profile)
    data_dir = profile.data_dir
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
        # Unpacked and copied with the process's default permissions: private like the folders they replace.
        _private(staged)
        _private(staged_inputs)
        keep = nodestro.store.get_raw("settings", "app") or {}
        with nodestro.sqlite_store.maintenance() as connection:
            _copy_tables(connection, source / "nodestro.db", keep)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(staged_inputs, ignore_errors=True)
        raise
    tools = data_dir / "tools"
    if "tools" in profile.components:
        _swap(staged, tools, data_dir / f".tools-old-{backup_id}")
    if "inputs" in profile.components:
        _swap(staged_inputs, data_dir / "inputs", data_dir / f".inputs-old-{backup_id}")
    shutil.rmtree(staged, ignore_errors=True)  # a component left out of the profile stays as it is
    shutil.rmtree(staged_inputs, ignore_errors=True)
    nodestro.reload_after_restore()
    connection = sqlite3.connect(nodestro.sqlite_store.database, timeout=30)
    try:
        contents = _count(connection)
    finally:
        connection.close()
    contents.tools = len(list(tools.glob("*/v*.json")))
    shutil.rmtree(source, ignore_errors=True)
    return RestoreResult(contents=contents, safety_backup=safety.name, secrets_to_reenter=reenter)


def _swap(staged: Path, current: Path, old: Path) -> None:
    """Put the staged directory in place of the current one (two renames on the same disk)."""
    shutil.rmtree(old, ignore_errors=True)
    if current.exists():
        current.replace(old)
    staged.replace(current)
    shutil.rmtree(old, ignore_errors=True)


def local_backups(nodestro: Nodestro, profile: BackupProfile | None = None) -> list[LocalBackup]:
    """The backups kept in the data directory (safety copies made before restores)."""
    found = []
    for path in backup_dir(nodestro, profile).glob("*.zip"):
        stat = path.stat()
        found.append(
            LocalBackup(
                name=path.name, size=stat.st_size, created_at=datetime.fromtimestamp(stat.st_mtime).astimezone()
            )
        )
    return sorted(found, key=lambda item: item.created_at, reverse=True)


def local_backup_path(nodestro: Nodestro, name: str, profile: BackupProfile | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.zip", name):
        raise KeyError(name)
    path = backup_dir(nodestro, profile) / name
    if not path.is_file():
        raise KeyError(name)
    return path
