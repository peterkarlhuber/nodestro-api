"""Memory spaces with an owner on a server (RT-089c; needs PostgreSQL).

A memory space belongs to its first writer. Only the owner lists, reads, writes and clears it on ``/api/memory``,
attaches it to an assistant conversation or includes it in an export; another member (an administrator included) and
another tenant get ``404 memory.space_not_found``, as for a space that does not exist. A space with files from before
owners were recorded is nobody's until the operator assigns it with ``nodestro-server memory backfill-owners``
(explicit, a member only, never over an owner, audited, deleting nothing). The desktop is unchanged
(``test_api.py``)."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("jwt")

from fastapi.testclient import TestClient
from nodestro_runtime.memory_spaces import MemorySpaces
from nodestro_runtime.storage_postgres import PostgresDatabase
from test_conversation_owners import command_schema, process_logging  # noqa: F401  (fixtures)
from test_identity import DSN, Server, database  # noqa: F401  (fixture)
from test_mcp_server_api import _server

from nodestro_api import access_audit
from nodestro_api.memory_owners import assign

SECRET = "salary zanzibar"
FILE = {"path": "/memories/note.md", "content": SECRET}


@pytest.fixture
def server(database: PostgresDatabase, tmp_path: Path) -> Iterator[Server]:  # noqa: F811
    yield from _server(database, tmp_path / "server", None)


def _refused(answer: Any) -> bool:
    return answer.status_code == 404 and answer.json()["code"] == "memory.space_not_found"


def _reads(client: TestClient, space: str) -> list[Any]:
    """Every way a member reads or changes the space ``space`` on ``/api/memory``."""
    base = f"/api/memory/{space}"
    return [
        client.get(base),
        client.get(f"{base}/file", params={"path": FILE["path"]}),
        client.put(f"{base}/file", json={"path": FILE["path"], "content": "overwritten"}),
        client.delete(f"{base}/file", params={"path": FILE["path"]}),
        client.delete(base),
    ]


def test_poc_another_member_cannot_read_a_space(server: Server) -> None:
    """PoC (RT-089c): Alice writes a note into the space ``team``; Bob, an administrator of the same tenant, lists,
    reads, overwrites and clears it. Before, every member used every space; now each route answers as for a space
    that does not exist, and Alice's note is unchanged."""
    alice = server.member("acme", "alice", "editor")
    bob = server.member("acme", "bob", "admin")
    victor = server.member("acme", "victor", "viewer")
    mallory = server.member("globex", "mallory", "owner")
    assert alice.put("/api/memory/team/file", json=FILE).status_code == 200
    assert [item["name"] for item in alice.get("/api/memory").json()] == ["team"]
    for other in (bob, victor):
        assert other.get("/api/memory").json() == []
        answers = _reads(other, "team")
        assert all(_refused(answer) or answer.status_code == 403 for answer in answers), [a.text for a in answers]
        assert all(SECRET not in answer.text for answer in answers)
    # Another tenant's ``team`` is a space of its own (tenant isolation, unchanged).
    assert mallory.get("/api/memory").json() == [] and _refused(mallory.get("/api/memory/team"))
    assert alice.get("/api/memory/team/file", params={"path": FILE["path"]}).json()["content"] == SECRET
    # Bob writes a space of his own: his, and Alice does not see it.
    assert bob.put("/api/memory/bobs/file", json=FILE).status_code == 200
    assert [item["name"] for item in bob.get("/api/memory").json()] == ["bobs"]
    assert _refused(alice.get("/api/memory/bobs"))
    # Bob cannot attach Alice's space to his assistant conversation either.
    conversation = bob.post("/api/assistant/conversations", json={}).json()["id"]
    attached = bob.post(
        f"/api/assistant/conversations/{conversation}/attachments",
        json={"kind": "memory", "ref_id": "team", "paths": [FILE["path"]]},
    )
    assert _refused(attached) and SECRET not in attached.text
    mine = alice.post("/api/assistant/conversations", json={}).json()["id"]
    attached = alice.post(
        f"/api/assistant/conversations/{mine}/attachments",
        json={"kind": "memory", "ref_id": "team", "paths": [FILE["path"]]},
    )
    assert attached.status_code == 200, attached.text


def test_a_space_without_an_owner_is_nobodys_until_the_operator_assigns_one(server: Server) -> None:
    alice = server.member("acme", "alice", "editor")
    bob = server.member("acme", "bob", "owner")
    workspace = server.app.state.tenants.get("acme")
    MemorySpaces.open(workspace.memory, "legacy").write(FILE["path"], SECRET)  # written before owners
    for person in (alice, bob):
        assert person.get("/api/memory").json() == []
        assert all(_refused(answer) for answer in _reads(person, "legacy"))
    # Reading or writing never claimed it, and nothing was deleted.
    assert workspace.memory.owner("legacy") is None
    assert [item.name for item in workspace.memory.ownerless()] == ["legacy"]
    alice_id = alice.get("/api/auth/me").json()["user_id"]
    done = assign(workspace.memory, alice_id, ["legacy"])
    assert done.assigned == ["legacy"]
    assert alice.get("/api/memory/legacy/file", params={"path": FILE["path"]}).json()["content"] == SECRET
    assert _refused(bob.get("/api/memory/legacy"))
    (event,) = access_audit.events(workspace.store, kind="memory_space_owner_assigned")
    assert (event.actor, event.target_type, event.target_id, event.details) == (
        "operator",
        "user",
        alice_id,
        {"space": "legacy"},
    )


def test_the_operator_assigns_owners_explicitly_audited_and_never_over_an_owner(
    command_schema: str,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nodestro_runtime.identity_store import IdentityStore
    from nodestro_runtime.postgres_tables import PostgresMemoryTables

    from nodestro_api import server as command

    command.main(["migrate"])
    command.main(["create-tenant", "acme", "Acme"])
    command.main(["create-tenant", "globex", "Globex"])
    opened = PostgresDatabase(DSN, schema=command_schema)
    try:
        identity = IdentityStore(opened)
        alice = identity.login("https://login.example", "alice-sub")
        identity.set_role("acme", alice.id, "editor")
        bob = identity.login("https://login.example", "bob-sub")
        identity.set_role("acme", bob.id, "editor")
        outsider = identity.login("https://login.example", "mallory-sub")
        identity.set_role("globex", outsider.id, "owner")
        store = opened.store("acme")
        spaces = MemorySpaces(PostgresMemoryTables(opened, "acme"), owners=store)
        spaces.open("legacy").write(FILE["path"], SECRET)
        spaces.open_for("bobs", bob.id).write(FILE["path"], "b")
        capsys.readouterr()

        def events() -> list[access_audit.AccessEvent]:
            return access_audit.events(store, kind="memory_space_owner_assigned")

        command.main(["memory", "backfill-owners", "--tenant", "acme"])
        listed = json.loads(capsys.readouterr().out)["tenants"]["acme"]["without_owner"]
        assert [item["name"] for item in listed] == ["legacy"] and events() == []
        # No default owner, no wildcard; only a member of the tenant.
        for argv in (
            ["memory", "backfill-owners", "--tenant", "acme", "--user", alice.id],
            ["memory", "backfill-owners", "--user", alice.id, "--space", "legacy"],
            ["memory", "backfill-owners", "--tenant", "acme", "--space", "legacy"],
        ):
            with pytest.raises(SystemExit) as stopped:
                command.main(argv)
            assert stopped.value.code == 2, argv
        with pytest.raises(SystemExit) as stopped:
            command.main(["memory", "backfill-owners", "--tenant", "acme", "--user", outsider.id, "--space", "legacy"])
        assert str(stopped.value.code).startswith("identity.not_a_member:"), stopped.value.code
        argv = ["memory", "backfill-owners", "--tenant", "acme", "--user", alice.id]
        argv += ["--space", "legacy", "--space", "bobs", "--space", "unknown"]
        capsys.readouterr()
        command.main([*argv, "--dry-run"])
        assert json.loads(capsys.readouterr().out)["tenants"]["acme"]["assigned"] == ["legacy"]
        assert spaces.owner("legacy") is None and events() == []
        command.main(argv)
        done = json.loads(capsys.readouterr().out)["tenants"]["acme"]
        assert (done["assigned"], done["owned"], done["unknown"]) == (["legacy"], ["bobs"], ["unknown"])
        assert (spaces.owner("legacy"), spaces.owner("bobs")) == (alice.id, bob.id)
        (event,) = events()
        assert (event.actor, event.target_id, event.details) == ("operator", alice.id, {"space": "legacy"})
        assert SECRET not in event.model_dump_json()
        # Never overwritten, nothing deleted.
        command.main(["memory", "backfill-owners", "--tenant", "acme", "--user", bob.id, "--space", "legacy"])
        assert json.loads(capsys.readouterr().out)["tenants"]["acme"]["owned"] == ["legacy"]
        assert spaces.owner("legacy") == alice.id and len(events()) == 1
        assert spaces.open("legacy").read(FILE["path"]) == SECRET
        spaces.close()
    finally:
        opened.close()
