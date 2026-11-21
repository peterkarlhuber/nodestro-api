"""Conversations without an owner on a server (W3-S1, API-073; ``nodestro_api.conversation_owners``).

A Nodestro Assistant conversation from before owners were recorded, or a conversation with an own agent restored from
a desktop, has no owner. On a server nobody sees it, administrators included, on any route, until the operator gives
it an owner with ``nodestro-server assistant backfill-owners --tenant --user --conversation``: explicit, a member
of the tenant only, never replacing an owner, audited, deleting nothing. The desktop's single person sees it as
before."""

from __future__ import annotations

from collections.abc import Callable, Iterator
import os
from pathlib import Path
from typing import Any
import uuid

from fastapi.testclient import TestClient
from nodestro_runtime import conversations as own
from nodestro_runtime.conversations import CONVERSATIONS_ENV
from nodestro_runtime.schemas import TemplateRef
import postgres_support
import pytest
from test_conversation_access_audit import SECRET, Owned, agent_script, leaking_reads, make_owned, reachable_by_id
from test_identity import _wait
from test_worker import Background, connect, make_worker, schema, server  # noqa: F401  (PostgreSQL fixtures)

from nodestro_api import access_audit
from nodestro_api.assistant import Conversation as AssistantConversation
from nodestro_api.conversation_owners import ASSISTANT, assign

from conftest import text

BASE = "/api/conversations"


def _store(server: Any, tenant: str) -> Any:  # noqa: F811
    return server.app.state.tenants.get(tenant).store


def _drop_owner(store: Any, owned: Owned) -> None:
    """Make the owner's conversations what older data is: an assistant conversation from before the owner field, a
    conversation with an own agent restored from a desktop."""
    for collection, conversation_id in ((ASSISTANT, owned.assistant_id), (own.COLLECTION, owned.conversation_id)):
        raw = store.get_raw(collection, conversation_id)
        assert raw is not None and raw["owner"] is not None
        store.put_raw(collection, conversation_id, {**raw, "owner": None})


def _owners(store: Any, owned: Owned) -> tuple[Any, Any]:
    return store.get_raw(ASSISTANT, owned.assistant_id)["owner"], store.get_raw(own.COLLECTION, owned.conversation_id)[
        "owner"
    ]


def test_a_conversation_without_owner_is_nobodys_on_a_server_until_the_operator_assigns_one(
    server: Any,  # noqa: F811
    make_worker: Callable[..., Background],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    alice = server.member("acme", "alice", "owner")
    bob = server.member("acme", "bob", "admin")
    victor = server.member("acme", "victor", "viewer")
    mallory = server.member("globex", "mallory", "owner")
    owned = make_owned(alice)
    worker = make_worker({"AGENT": agent_script()})
    worker.call(worker.worker.runner.run_once())
    assert _wait(alice, owned.run_id)["status"] == "succeeded"
    # The assistant conversation holds a file too (the files route of an assistant conversation, /api/inputs).
    uploaded = alice.post(
        f"/api/assistant/conversations/{owned.assistant_id}/files",
        files=[("files", (f"{SECRET}-chat.txt", f"{SECRET} chat file".encode(), "text/plain"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    chat_file = uploaded.json()[0]["file"]["id"]
    assert SECRET in alice.get(f"/api/inputs/{chat_file}/content").text

    store = _store(server, "acme")
    _drop_owner(store, owned)
    assistant_count = len(store.list(ASSISTANT, AssistantConversation))

    # Nobody sees either conversation, its messages, files, answer run, search hits or events: not the member who
    # wrote in it, not an administrator, a viewer or another tenant.
    chat_owned = Owned(owned.assistant_id, owned.run_id, owned.message_id, chat_file, owned.assistant_id)
    for viewer in (alice, bob, victor, mallory):
        assert leaking_reads(viewer, owned) == []
        assert reachable_by_id(viewer, owned) == []
        assert reachable_by_id(viewer, chat_owned) == []
        assert viewer.get("/api/assistant/conversations").json() == []
        assert viewer.get(BASE).json() == []
    # Reading never assigns an owner (not to the one who is logged in either), and nothing was deleted.
    assert _owners(store, owned) == (None, None)
    assert len(store.list(ASSISTANT, AssistantConversation)) == assistant_count
    assert store.get_raw(own.COLLECTION, owned.conversation_id) is not None
    assert store.get_raw("runs", owned.run_id) is not None

    # The operator's assignment gives it back to exactly the named member.
    alice_id = alice.get("/api/auth/me").json()["user_id"]
    done = assign(store, alice_id, [owned.assistant_id, owned.conversation_id])
    assert set(done.assigned) == {owned.assistant_id, owned.conversation_id}
    assert [item["id"] for item in alice.get("/api/assistant/conversations").json()] == [owned.assistant_id]
    assert [item["id"] for item in alice.get(BASE).json()] == [owned.conversation_id]
    assert alice.get(f"/api/runs/{owned.run_id}").status_code == 200
    assert alice.get(f"/api/inputs/{chat_file}/content").text == f"{SECRET} chat file"
    for viewer in (bob, victor, mallory):
        assert leaking_reads(viewer, owned) == []


def test_the_desktop_person_still_sees_conversations_without_owner(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop's single person (and Local Docker's) sees every conversation of the workspace, as before."""
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    client = make_app({"AGENT": [text("ok")]})
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    owned = make_owned(client, wait=lambda run_id: client.portal.call(ctx.wait, run_id))  # type: ignore[union-attr]
    for collection, conversation_id in ((ASSISTANT, owned.assistant_id), (own.COLLECTION, owned.conversation_id)):
        assert ctx.store.get_raw(collection, conversation_id)["owner"] is None  # the desktop records no owner
    assert [item["id"] for item in client.get("/api/assistant/conversations").json()] == [owned.assistant_id]
    assert [item["id"] for item in client.get(BASE).json()] == [owned.conversation_id]
    assert client.get(f"/api/inputs/{owned.file_id}/content").status_code == 200
    assert client.get(f"/api/runs/{owned.run_id}").status_code == 200


# ---- the operator's command ----------------------------------------------------------------------------------------

DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


@pytest.fixture
def process_logging() -> Iterator[None]:
    """``server.main`` sets up the logging of a whole server process (the database error redaction as the log record
    factory and on ``logging.lastResort``, the root handler): put it back, so later tests in the same worker see the
    logging as it was (``test_db_error_texts``)."""
    import logging

    factory = logging.getLogRecordFactory()
    resort = list(logging.lastResort.filters) if logging.lastResort is not None else []
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        logging.setLogRecordFactory(factory)
        if logging.lastResort is not None:
            logging.lastResort.filters[:] = resort
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.fixture
def command_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, process_logging: None) -> Iterator[str]:
    import base64

    postgres_support.require_postgres()
    import psycopg
    from psycopg import sql

    name = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    environ = {
        "NODESTRO_PROFILE": "server",
        "NODESTRO_DATA_DIR": str(tmp_path / "server"),
        "NODESTRO_DATABASE_URL": DSN,
        "NODESTRO_DATABASE_SCHEMA": name,
        "NODESTRO_PUBLIC_ORIGIN": "https://studio.test",
        "NODESTRO_OIDC_ISSUER": "https://login.example",
        "NODESTRO_OIDC_CLIENT_ID": "nodestro-studio",
        "NODESTRO_OIDC_CLIENT_SECRET": "client-secret",
        "NODESTRO_SECRET_KEY": base64.b64encode(os.urandom(32)).decode(),
    }
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    yield name
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def test_the_operator_assigns_owners_explicitly_audited_and_never_over_an_owner(
    command_schema: str, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from nodestro_runtime.identity_store import IdentityStore
    from nodestro_runtime.storage_postgres import PostgresDatabase

    from nodestro_api import server as command

    command.main(["migrate"])
    command.main(["create-tenant", "acme", "Acme"])
    command.main(["create-tenant", "globex", "Globex"])
    database = PostgresDatabase(DSN, schema=command_schema)
    try:
        identity = IdentityStore(database)
        alice = identity.login("https://login.example", "alice-sub")
        identity.set_role("acme", alice.id, "owner")
        bob = identity.login("https://login.example", "bob-sub")
        identity.set_role("acme", bob.id, "editor")
        outsider = identity.login("https://login.example", "mallory-sub")
        identity.set_role("globex", outsider.id, "owner")
        store = database.store("acme")
        legacy = AssistantConversation(title=f"{SECRET} legacy")
        owned = AssistantConversation(title="Bob's", owner=bob.id)
        restored = own.Conversation(
            tenant_id="acme", title=f"{SECRET} restored", agent=TemplateRef(template_id="tpl_x", version=1), model="m"
        )
        for collection, item in ((ASSISTANT, legacy), (ASSISTANT, owned), (own.COLLECTION, restored)):
            store.put(collection, item.id, item)
        capsys.readouterr()

        def owners() -> dict[str, Any]:
            found: dict[str, Any] = {}
            for collection, item_id in ((ASSISTANT, legacy.id), (ASSISTANT, owned.id), (own.COLLECTION, restored.id)):
                raw = store.get_raw(collection, item_id)
                assert raw is not None, item_id  # never deleted
                found[item_id] = raw["owner"]
            return found

        def assigned_events() -> list[access_audit.AccessEvent]:
            return access_audit.events(store, kind="conversation_owner_assigned")

        # The report reads only.
        command.main(["assistant", "backfill-owners", "--tenant", "acme"])
        listed = json.loads(capsys.readouterr().out)["tenants"]["acme"]["without_owner"]
        assert listed == {legacy.id: "assistant", restored.id: "agent"}
        assert owners() == {legacy.id: None, owned.id: bob.id, restored.id: None} and assigned_events() == []

        # No default owner, no wildcard: the user, the tenant and each conversation are named.
        for argv in (
            ["assistant", "backfill-owners", "--tenant", "acme", "--user", alice.id],
            ["assistant", "backfill-owners", "--user", alice.id, "--conversation", legacy.id],
            ["assistant", "backfill-owners", "--tenant", "acme", "--conversation", legacy.id],
        ):
            with pytest.raises(SystemExit) as stopped:
                command.main(argv)
            assert stopped.value.code == 2, argv
        # Only a member of the tenant becomes an owner.
        for who, code in ((outsider.id, "identity.not_a_member"), ("nobody@example.com", "identity.user_unknown")):
            with pytest.raises(SystemExit) as stopped:
                command.main(
                    ["assistant", "backfill-owners", "--tenant", "acme", "--user", who, "--conversation", legacy.id]
                )
            assert str(stopped.value.code).startswith(f"{code}:"), stopped.value.code
        assert owners() == {legacy.id: None, owned.id: bob.id, restored.id: None} and assigned_events() == []

        # A dry run writes nothing.
        argv = ["assistant", "backfill-owners", "--tenant", "acme", "--user", alice.id]
        argv += ["--conversation", legacy.id, "--conversation", owned.id, "--conversation", restored.id]
        argv += ["--conversation", "chat_unknown"]
        capsys.readouterr()
        command.main([*argv, "--dry-run"])
        planned = json.loads(capsys.readouterr().out)["tenants"]["acme"]
        assert planned["assigned"] == {legacy.id: "assistant", restored.id: "agent"}
        assert owners() == {legacy.id: None, owned.id: bob.id, restored.id: None} and assigned_events() == []

        # The assignment: only where no owner is, an existing owner stays; each one is audited without contents.
        command.main(argv)
        done = json.loads(capsys.readouterr().out)["tenants"]["acme"]
        assert (done["owned"], done["unknown"]) == ([owned.id], ["chat_unknown"])
        assert owners() == {legacy.id: alice.id, owned.id: bob.id, restored.id: alice.id}
        events = assigned_events()
        assert sorted(event.details["conversation_id"] for event in events) == sorted([legacy.id, restored.id])
        for event in events:
            assert (event.actor, event.target_type, event.target_id, event.outcome) == (
                "operator",
                "user",
                alice.id,
                "success",
            )
            assert event.at is not None and SECRET not in event.model_dump_json()

        # Never overwritten: naming another member for an owned conversation changes nothing.
        command.main(
            ["assistant", "backfill-owners", "--tenant", "acme", "--user", bob.id, "--conversation", legacy.id]
        )
        again = json.loads(capsys.readouterr().out)["tenants"]["acme"]
        assert (again["assigned"], again["owned"]) == ({}, [legacy.id])
        assert owners()[legacy.id] == alice.id and len(assigned_events()) == 2
        # Nothing was deleted; another tenant's store was not touched.
        assert len(store.list(ASSISTANT, AssistantConversation)) == 2
        assert database.store("globex").list(ASSISTANT, AssistantConversation) == []
    finally:
        database.close()
