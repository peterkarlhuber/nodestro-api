"""The API of granted folders (N-10): the switch, who may write (a signed-in person with ``admin``, never an API key
or a model), what can never be granted, revocation and the access log."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import tempfile
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.folders import COLLECTION, FOLDERS_ENV, FolderGrant
from nodestro_runtime.tool_approvals import AccessRules
import pytest

from nodestro_api.permissions import ALL_PERMISSIONS, Permission, Principal

FOLDERS = "/api/folders"
ROUTES: list[tuple[str, str]] = [
    ("get", FOLDERS),
    ("post", FOLDERS),
    ("get", f"{FOLDERS}/fgr_x"),
    ("get", f"{FOLDERS}/fgr_x/accesses"),
    ("delete", f"{FOLDERS}/fgr_x"),
]


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "plan.txt").write_text("The budget is 42 units\n", encoding="utf-8")
    return root


@pytest.fixture
def granting(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(FOLDERS_ENV, "1")
    return make_app()


def _call(client: TestClient, method: str, path: str, docs: Path) -> Any:
    if method == "post":
        return client.post(path, json={"name": "Docs", "path": str(docs)})
    return getattr(client, method)(path)


def _as(client: TestClient, principal: Principal) -> None:
    client.app.state.principal_resolver = lambda _connection: principal  # type: ignore[attr-defined]


def _store(client: TestClient) -> Any:
    return client.app.state.nodestro.store  # type: ignore[attr-defined]


def test_without_the_switch_every_route_but_the_state_is_not_found(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, docs: Path
) -> None:
    monkeypatch.delenv(FOLDERS_ENV, raising=False)
    client = make_app()
    state = client.get(f"{FOLDERS}/state")
    assert state.status_code == 200
    assert state.json()["enabled"] is False
    assert state.json()["limits"]["max_file_bytes"] == 10 * 1024 * 1024
    assert state.json()["bounds"]["max_file_bytes"] == {"lowest": 1, "highest": 50 * 1024 * 1024}
    for method, path in ROUTES:
        response = _call(client, method, path, docs)
        assert (response.status_code, response.json()["code"]) == (404, "folder.disabled"), (method, path)
    assert _store(client).list(COLLECTION, FolderGrant) == []
    assert AccessRules(_store(client)).grants() == []


def test_a_person_grants_reads_the_log_and_revokes(granting: TestClient, docs: Path) -> None:
    assert granting.get(f"{FOLDERS}/state").json()["enabled"] is True
    body = {
        "name": "Docs",
        "path": str(docs),
        "exclude": ["drafts/**"],
        "limits": {"max_file_bytes": 1024},
        # Fields the server owns are not taken from the body.
        "created_by": "mallory",
    }
    created = granting.post(FOLDERS, json=body)
    assert created.status_code == 201, created.text
    view = created.json()
    folder_id = view["folder"]["id"]
    assert view["status"] == "active" and view["folder"]["created_by"] == "local"
    assert view["folder"]["access"] == "read" and view["folder"]["exclude"] == ["drafts/**"]
    assert view["folder"]["limits"]["max_file_bytes"] == 1024
    # The folder grant is an access grant of the access model (N-06): it shows there too.
    [grant] = AccessRules(_store(granting)).grants()
    assert grant.id == view["folder"]["grant_id"] and grant.resource.type == "folder" and grant.actions == ("read",)
    assert [item["folder"]["id"] for item in granting.get(FOLDERS).json()] == [folder_id]
    assert granting.get(f"{FOLDERS}/{folder_id}").json()["status"] == "active"
    taken = granting.post(FOLDERS, json={"name": "docs", "path": str(docs / "notes")})
    assert (taken.status_code, taken.json()["code"]) == (409, "folder.name_taken")
    assert granting.get(f"{FOLDERS}/{folder_id}/accesses").json() == []
    revoked = granting.delete(f"{FOLDERS}/{folder_id}")
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert revoked.json()["revoked_by"] == "local"
    assert granting.delete(f"{FOLDERS}/{folder_id}").json()["status"] == "revoked"  # twice: no change
    missing = granting.get(f"{FOLDERS}/fgr_missing")
    assert (missing.status_code, missing.json()["code"]) == (404, "folder.not_found")


def test_the_data_folder_and_system_folders_cannot_be_granted(granting: TestClient, tmp_path: Path) -> None:
    data = granting.app.state.nodestro.data_dir.resolve()  # type: ignore[attr-defined]
    for path, reason in [
        (str(data), "protected_root"),
        (str(Path.home()), "protected_root"),
        (str(Path(tmp_path.anchor)), "protected_root"),
        ("relative/docs", "invalid_path"),
        (str(tmp_path / "missing"), "not_found"),
    ]:
        refused = granting.post(FOLDERS, json={"name": "x", "path": path})
        assert (refused.status_code, refused.json()["code"]) == (422, "folder.not_grantable"), path
        assert refused.json()["params"] == {"reason": reason}, path
    bad_name = granting.post(FOLDERS, json={"name": "a/b", "path": str(tmp_path)})
    assert (bad_name.status_code, bad_name.json()["code"]) == (422, "folder.invalid_name")
    assert AccessRules(_store(granting)).grants() == []


def test_only_all_or_named_agents_can_be_granted(granting: TestClient, docs: Path) -> None:
    """A grant for "this conversation" needs a conversation; the folder page grants for all agents or named ones."""
    refused = granting.post(FOLDERS, json={"name": "Docs", "path": str(docs), "scope": "conversation"})
    assert refused.status_code == 422
    agents = granting.post(FOLDERS, json={"name": "Docs", "path": str(docs), "scope": "agents", "agents": ["wf/a"]})
    assert agents.status_code == 201, agents.text
    assert agents.json()["scope"] == "agents" and agents.json()["agents"] == ["wf/a"]


def test_writing_needs_admin_and_a_person(granting: TestClient, docs: Path) -> None:
    """Reading the state needs ``read``; every other route ``admin``. A principal that is not a person (an API key,
    a machine) cannot write even with every permission: nothing is stored."""
    runner = Principal(id="runner", permissions=frozenset({Permission.READ, Permission.RUN, Permission.EDIT}))
    _as(granting, runner)
    assert granting.get(f"{FOLDERS}/state").status_code == 200
    for method, path in ROUTES:
        response = _call(granting, method, path, docs)
        assert (response.status_code, response.json()["params"]) == (403, {"permission": "admin"}), (method, path)
    for machine in ("api:key_1", "system", "mcp:client"):
        _as(granting, Principal(id=machine, permissions=ALL_PERMISSIONS))
        refused = granting.post(FOLDERS, json={"name": "Docs", "path": str(docs)})
        assert (refused.status_code, refused.json()["code"]) == (403, "access.person_required"), machine
    assert AccessRules(_store(granting)).grants() == []


def test_the_secret_files_are_left_out_unless_an_admin_turns_it_off(
    granting: TestClient, docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-10 review L8 and L7: every grant leaves out common secret files unless the request turns that off; the
    temporary folder itself cannot be granted, a folder inside it can."""
    default = granting.post(FOLDERS, json={"name": "Docs", "path": str(docs)})
    assert default.status_code == 201, default.text
    assert default.json()["folder"]["default_excludes"] is True
    keys = granting.post(FOLDERS, json={"name": "Keys", "path": str(docs / "notes"), "default_excludes": False})
    assert keys.status_code == 201, keys.text
    assert keys.json()["folder"]["default_excludes"] is False
    monkeypatch.setattr(tempfile, "tempdir", str(docs.parent))
    refused = granting.post(FOLDERS, json={"name": "Temp", "path": str(docs.parent)})
    assert (refused.status_code, refused.json()["params"]) == (422, {"reason": "temp_root"})


def test_a_folder_index_is_built_on_a_grant_and_dropped_at_once_on_a_revocation(
    make_app: Callable[..., TestClient], docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-12: a grant with ``index`` wakes the indexer; revoking it (here, or through the access API) removes the
    folder's chunks before the answer, so nothing of it stays searchable."""
    import time

    from nodestro_runtime.knowledge import FOLDER_SOURCE
    from nodestro_runtime.tool_approvals import ACCESS_ENV, ENABLE_ENV

    for name in (FOLDERS_ENV, ACCESS_ENV, ENABLE_ENV):
        monkeypatch.setenv(name, "1")
    granting = make_app()
    nodestro = granting.app.state.nodestro  # type: ignore[attr-defined]

    def chunks() -> list[str]:
        return [source for source in nodestro.knowledge.chunk_sources() if source.startswith(FOLDER_SOURCE)]

    created = granting.post(FOLDERS, json={"name": "Docs", "path": str(docs), "index": True})
    assert created.status_code == 201 and created.json()["folder"]["index"] is True
    deadline = time.monotonic() + 20
    while not chunks() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert chunks() == [f"{FOLDER_SOURCE}{created.json()['folder']['id']}/notes/plan.txt"]
    assert granting.delete(f"{FOLDERS}/{created.json()['folder']['id']}").status_code == 200
    assert chunks() == []
    # The same through the access API.
    again = granting.post(FOLDERS, json={"name": "Notes", "path": str(docs / "notes"), "index": True}).json()
    nodestro.folder_index.sync(again["folder"]["id"])
    assert chunks()
    revoked = granting.delete(f"/api/access/grants/{again['folder']['grant_id']}")
    assert revoked.status_code == 200, revoked.text
    assert chunks() == []


def test_backups_and_the_data_export_hold_nothing_of_a_folder_index(
    make_app: Callable[..., TestClient], docs: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """N-12 review M-1: a granted folder's text stays with the folder. A backup's database holds no chunk, vector or
    full-text term of a folder index and no manifest or state of it; the data export holds none of its documents.
    The index is built again from the folder while the grant holds."""
    import io
    import json
    import sqlite3
    import zipfile

    from nodestro_runtime.folder_index import FILES, STATES, FolderIndexState
    from nodestro_runtime.knowledge import FOLDER_SOURCE
    from nodestro_runtime.tool_approvals import ACCESS_ENV, ENABLE_ENV

    for name in (FOLDERS_ENV, ACCESS_ENV, ENABLE_ENV):
        monkeypatch.setenv(name, "1")
    (docs / "notes" / "ledger.txt").write_text("The quartermaster ledger of the zanzibarlantern\n", encoding="utf-8")
    client = make_app()
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    nodestro.knowledge.add(b"Customers get a refund within thirty days.", name="returns.md")
    created = client.post(FOLDERS, json={"name": "Docs", "path": str(docs), "index": True}).json()
    folder_id = created["folder"]["id"]
    nodestro.folder_index.sync(folder_id)
    folder_chunks = [source for source in nodestro.knowledge.chunk_sources() if source.startswith(FOLDER_SOURCE)]
    assert folder_chunks and nodestro.store.list(STATES, FolderIndexState)
    with sqlite3.connect(nodestro.config.data_dir / "nodestro.db") as live:  # a vector of a folder chunk
        live.execute(
            "INSERT INTO index_vectors (space, chunk_id, embedding)"
            " SELECT 'nodestro:test', id, x'00' FROM index_chunks WHERE substr(source, 1, 7) = 'folder:'"
        )
    backup = client.get("/api/backup")
    assert backup.status_code == 200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
        database = tmp_path / "restored.db"
        database.write_bytes(archive.read("nodestro.db"))
    assert b"zanzibarlantern" not in database.read_bytes()
    with sqlite3.connect(database) as copy:
        sources = [row[0] for row in copy.execute("SELECT source FROM index_chunks")]
        assert sources and not any(source.startswith(FOLDER_SOURCE) for source in sources)  # knowledge stays
        assert copy.execute("SELECT count(*) FROM index_vectors WHERE space = 'nodestro:test'").fetchone()[0] == 0
        assert copy.execute("SELECT count(*) FROM index_fts WHERE index_fts MATCH 'zanzibarlantern'").fetchone()[0] == 0
        assert copy.execute("SELECT count(*) FROM index_fts WHERE index_fts MATCH 'refund'").fetchone()[0] == 1
        collections = {row[0] for row in copy.execute("SELECT DISTINCT collection FROM nodestro_documents")}
        assert not collections & {FILES, STATES} and COLLECTION in collections
    export = client.get("/api/data/export")
    assert export.status_code == 200
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert not set(manifest["collections"].values()) & {FILES, STATES}
        assert not any(b"zanzibarlantern" in archive.read(name) for name in archive.namelist())
    # The live index is untouched.
    assert [
        source for source in nodestro.knowledge.chunk_sources() if source.startswith(FOLDER_SOURCE)
    ] == folder_chunks
