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
from nodestro_api.permissions import Principal

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


# ---- review H-1: an export with a memory space is its owner's alone ------------------------------------------------


def _memory_workflow(client: TestClient, space: str, prompt: str = "MEM") -> str:
    flow = {
        "name": "Notes",
        "nodes": [
            {"id": "start", "type": "start"},
            {
                "id": "agent",
                "type": "agent",
                "label": "A",
                "agent": {"name": "A", "system_prompt": prompt, "memory": {"mode": "persistent", "space": space}},
            },
            {"id": "out", "type": "output"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "agent"},
            {"id": "e2", "source": "agent", "target": "out"},
        ],
    }
    created = client.post("/api/workflows", json=flow)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def allow_workflow(client: TestClient, workflow_id: str, space: str = "Team") -> Any:
    """The owner's explicit consent (RT-089d): what the studio would do, the preview first, then the grant that names
    the digest shown."""
    preview = client.get("/api/memory-consents/preview", params={"workflow_id": workflow_id})
    assert preview.status_code == 200, preview.text
    body = {"space": space, "workflow_id": workflow_id, "digest": preview.json()["digest"]}
    return client.post("/api/memory-consents", json=body)


def test_an_export_with_a_memory_space_is_listed_and_downloaded_only_by_its_owner(server: Server) -> None:
    """Review H-1 (PoC ``test_poc_export_download_leaks_another_members_memory``): Alice exports a publication with
    her space; before, any member (``read``) listed the export and downloaded the ZIP with her memory files. Now the
    export, its download, its deletion and the publication overview answer everybody else like an unknown export."""
    from test_published_api import publish

    alice = server.member("acme", "alice", "admin")  # publishing needs admin
    bob = server.member("acme", "bob", "viewer")
    carol = server.member("acme", "carol", "owner")
    assert alice.put("/api/memory/team/file", json=FILE).status_code == 200
    publication = publish(alice, _memory_workflow(alice, "team"))["publication"]["id"]
    made = alice.post(f"/api/publications/{publication}/exports", json={"options": {"memory": ["team"]}})
    assert made.status_code == 201, made.text
    plain = alice.post(f"/api/publications/{publication}/exports", json={"options": {}})
    assert plain.status_code == 201, plain.text
    private, shared = made.json()["id"], plain.json()["id"]

    def overview(client: TestClient) -> list[str]:
        items = client.get("/api/publication-overview").json()
        return [export["id"] for item in items for export in item["exports"]]

    assert {item["id"] for item in alice.get(f"/api/publications/{publication}/exports").json()} == {private, shared}
    assert set(overview(alice)) == {private, shared}
    assert alice.get(f"/api/exports/{private}/download").status_code == 200
    for other in (bob, carol):
        assert [item["id"] for item in other.get(f"/api/publications/{publication}/exports").json()] == [shared]
        assert overview(other) == [shared]
        download = other.get(f"/api/exports/{private}/download")
        assert download.status_code == 404 and SECRET not in download.text
        assert other.get(f"/api/exports/{shared}/download").status_code == 200  # an export without memory is shared
    assert carol.delete(f"/api/exports/{private}").status_code == 404
    assert alice.get(f"/api/exports/{private}/download").status_code == 200  # still there


# ---- review H-2: a run with a memory space is its owner's alone on every run route ---------------------------------

AUDIT_SPACE, AUDIT_NOTE = "Team", "/memories/n.md"


@pytest.fixture
def scripted_server(database: PostgresDatabase, tmp_path: Path) -> Iterator[Server]:  # noqa: F811
    import httpx
    from nodestro_runtime.identity_store import IdentityStore
    import stub_oidc

    from nodestro_api.identity import OidcClient, OidcSettings
    from nodestro_api.server_app import TenantComposition, create_tenant_app

    from conftest import ScriptedClient, text, tool_use

    provider = stub_oidc.StubProvider()
    oidc = OidcClient(
        OidcSettings(
            issuer=stub_oidc.ISSUER,
            client_id=stub_oidc.CLIENT_ID,
            client_secret=stub_oidc.CLIENT_SECRET,
            redirect_uri=stub_oidc.REDIRECT_URI,
        ),
        http=httpx.Client(transport=provider.transport()),
    )
    view = tool_use("memory", {"command": "view", "path": AUDIT_NOTE})
    scripted = ScriptedClient({"MEM": [view, text("I looked at my notes.")] * 5})
    identity = IdentityStore(database)
    app = create_tenant_app(
        TenantComposition(
            database=database,
            identity=identity,
            oidc=oidc,
            origin=stub_oidc.ORIGIN,
            data_root=tmp_path / "srv",
            client_factory=lambda _provider: scripted,
            integrations=[],
        )
    )
    built = Server(app, identity, provider, [])
    yield built
    for client in built.clients:
        client.__exit__(None, None, None)


def test_a_run_that_used_a_memory_space_is_seen_only_by_its_owner(scripted_server: Server) -> None:
    """Review H-2 (PoC ``test_poc_a_viewer_reads_another_members_memory_through_her_run``): the memory tool's output is
    in the run's steps, events and trace export. Before, any member read Alice's space through her run while
    ``/api/memory`` refused them. Now the run is hers alone: the route scans of the conversation audit (every
    ``GET`` route, every route with the run's id) find it for her and nothing of it for an administrator, a viewer
    or a principal without a role; the run lists, the context preview and an assistant attachment leave it out."""
    from test_conversation_access_audit import MEMBER_PERMISSIONS, Owned, leaking_reads, reachable_by_id
    from test_conversation_access_audit import SECRET as MARKER
    from test_identity import _wait

    server = scripted_server
    alice = server.member("acme", "alice", "editor")
    bob = server.member("acme", "bob", "admin")
    victor = server.member("acme", "victor", "viewer")
    stored = alice.put(f"/api/memory/{AUDIT_SPACE}/file", json={"path": AUDIT_NOTE, "content": f"{MARKER} note"})
    assert stored.status_code == 200
    workflow_id = _memory_workflow(alice, AUDIT_SPACE)
    allowed = allow_workflow(alice, workflow_id)
    assert allowed.status_code == 201, allowed.text  # RT-089d: Alice's explicit consent
    started = alice.post(f"/api/workflows/{workflow_id}/runs", json={"input": "go"})
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    finished = _wait(alice, run_id)
    assert finished["status"] == "succeeded" and MARKER in str(finished), finished["status"]
    owned = Owned(workflow_id, run_id, "x_unknown", "x_unknown", "x_unknown")

    seen_by_owner = {path for path, _status in leaking_reads(alice, owned)}
    assert {"/api/runs/{run_id}", "/api/runs/{run_id}/export", "/api/runs/{run_id}/events"} <= seen_by_owner

    principal = Principal(id="svc_reporting", permissions=MEMBER_PERMISSIONS, tenant_id="acme")
    resolve = server.app.state.principal_resolver
    server.app.state.principal_resolver = lambda connection: (
        principal if connection.headers.get("x-test-no-role") else resolve(connection)
    )
    nobody = server.browser()
    nobody.headers["x-test-no-role"] = "1"
    for other in (bob, victor, nobody):
        assert leaking_reads(other, owned) == []
        assert reachable_by_id(other, owned) == []
        assert run_id not in other.get("/api/runs").text
        assert run_id not in other.get("/api/runs", params={"workflow_id": workflow_id}).text
    preview = bob.post(
        "/api/workflows/preview",
        json={"workflow": {"name": "W", "nodes": [], "edges": []}, "node_id": "agent", "run_id": run_id},
    )
    assert preview.status_code == 404 and MARKER not in preview.text
    mine = bob.post("/api/assistant/conversations", json={}).json()["id"]
    attached = bob.post(f"/api/assistant/conversations/{mine}/attachments", json={"kind": "run", "ref_id": run_id})
    assert attached.status_code == 404 and MARKER not in attached.text
    assert [item["id"] for item in alice.get("/api/runs").json()] == [run_id]


def test_an_agent_test_with_persistent_memory_uses_the_testers_space(scripted_server: Server) -> None:
    """Review L-1: the agent test acts for the person who asks. Before, it acted for ``system`` and a test with
    persistent memory failed on a server for everyone; now the tester uses their own space, and another member is
    refused as for a space that does not exist."""
    from test_conversation_access_audit import SECRET as MARKER

    alice = scripted_server.member("acme", "alice", "editor")
    bob = scripted_server.member("acme", "bob", "editor")
    assert alice.put(f"/api/memory/{AUDIT_SPACE}/file", json={"path": AUDIT_NOTE, "content": MARKER}).status_code == 200
    body = {
        "agent": {
            "name": "A",
            "system_prompt": "MEM",
            "model": "anthropic:claude-sonnet-5-5",
            "memory": {"mode": "persistent", "space": AUDIT_SPACE},
        },
        "input": "go",
    }
    shown = alice.post("/api/memory-consents/preview", json={"agent": body["agent"]})
    assert shown.status_code == 200, shown.text
    consent = {"space": AUDIT_SPACE, "agent": body["agent"], "digest": shown.json()["digest"]}
    allowed = alice.post("/api/memory-consents", json=consent)
    assert allowed.status_code == 201, allowed.text  # RT-089d: Alice's explicit consent for this configuration
    mine = alice.post("/api/agents/test", json=body)
    assert mine.status_code == 200 and mine.json()["status"] == "succeeded", mine.text
    assert MARKER in mine.text
    theirs = bob.post("/api/agents/test", json=body)
    assert theirs.json()["error_code"] == "memory.space_not_found" and MARKER not in theirs.text
