"""A restore copies only known tables and columns and never runs SQL from the archive (API2-11)."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from nodestro_api.backup import KNOWN_TABLES, _copy_tables, quote_identifier


def _main(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(KNOWN_TABLES["nodestro_documents"])
    connection.execute("INSERT INTO nodestro_documents (collection, id, data) VALUES ('workflows', 'old', '{}')")
    return connection


def test_unknown_tables_and_hostile_names_from_a_backup_are_not_executed(tmp_path: Path) -> None:
    source = tmp_path / "backup.db"
    with sqlite3.connect(source) as backup:
        backup.execute(KNOWN_TABLES["nodestro_documents"])
        backup.execute("INSERT INTO nodestro_documents (collection, id, data) VALUES ('workflows', 'new', '{}')")
        # A table this version does not know, with a name that would break out of "..." quoting.
        backup.execute('CREATE TABLE "planted"" (x); --" (secret TEXT)')
        backup.execute("CREATE TABLE planted (payload TEXT DEFAULT (hex(randomblob(4))))")
        # A known table whose definition in the archive differs: an extra column with a hostile name.
        backup.execute(
            'CREATE TABLE memory_files (namespace TEXT, path TEXT, content TEXT, updated_at TEXT, "x"", evil" TEXT)'
        )
        backup.execute("INSERT INTO memory_files VALUES ('team', '/a.md', 'Wissen', '2026-01-01', 'boom')")
    connection = _main(tmp_path / "main.db")
    _copy_tables(connection, source, {})
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "planted" not in tables and 'planted" (x); --' not in tables
    # The missing known table is created with this version's definition, and only its columns are copied.
    columns = [row[1] for row in connection.execute("PRAGMA table_info(memory_files)")]
    assert columns == ["namespace", "path", "content", "updated_at", "origin"]
    assert connection.execute("SELECT content FROM memory_files").fetchall() == [("Wissen",)]
    assert [
        row[0] for row in connection.execute("SELECT id FROM nodestro_documents WHERE collection = 'workflows'")
    ] == ["new"]


def test_a_restore_keeps_known_origins_and_forgets_unknown_ones(tmp_path: Path) -> None:
    """N-24 L5: an origin in the archive that this version never writes is restored as unknown (NULL)."""
    source = tmp_path / "backup.db"
    with sqlite3.connect(source) as backup:
        backup.execute(KNOWN_TABLES["nodestro_documents"])
        backup.execute(KNOWN_TABLES["memory_files"])
        backup.executemany(
            "INSERT INTO memory_files VALUES ('team', ?, 'x', '2026-01-01T00:00:00Z', ?)",
            [
                ("/memories/a.md", "tool"),
                ("/memories/b.md", "user"),
                ("/memories/c.md", "import"),
                ("/memories/d.md", "assistant"),
                ("/memories/e.md", "admin"),
                ("/memories/f.md", "User"),
                ("/memories/g.md", None),
            ],
        )
    connection = _main(tmp_path / "main.db")
    _copy_tables(connection, source, {})
    assert connection.execute("SELECT path, origin FROM memory_files ORDER BY path").fetchall() == [
        ("/memories/a.md", "tool"),
        ("/memories/b.md", "user"),
        ("/memories/c.md", "import"),
        ("/memories/d.md", "assistant"),
        ("/memories/e.md", None),
        ("/memories/f.md", None),
        ("/memories/g.md", None),
    ]


def test_identifiers_are_quoted() -> None:
    assert quote_identifier("plain") == '"plain"'
    assert quote_identifier('a"b') == '"a""b"'
