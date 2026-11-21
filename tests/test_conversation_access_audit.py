"""Who sees what of a conversation with an own agent (N-04; W3-S1 audit).

A conversation's contents are its title, its messages, its files and the answers' runs: their input (the conversation),
output (the answer), tool calls, result files, checkpoints, events and costs. Only the conversation's owner sees them;
on the desktop, its only person. These tests prove it for every route the application has, found automatically from
the routes themselves (``check_routes``), so a route added later is covered too:

- every ``GET`` route, with the ids of the owner's conversation, answer run, message and file in its path and query,
  answers nothing that holds the conversation's secret marker to another member (an administrator included) or to a
  principal without an owner identity;
- every route with a run id, a conversation id or a file id in its path answers ``404`` to them, for every method;
- the routes that take a run or file id in their body (preview, assistant attachments, knowledge adoption, run starts
  with a file field) refuse them;
- the activity log (N-13) and the usage and skill statistics name the answer runs without contents.

The same on a PostgreSQL server with two members and a principal without a role:
``test_conversation_access_audit_server.py``."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.conversations import CONVERSATIONS_ENV
import pytest

from nodestro_api.permissions import ALL_PERMISSIONS, Permission, Principal

from conftest import text, tool_use

BASE = "/api/conversations"
CLOUD = "anthropic:claude-sonnet-5-5"
#: The marker in everything the owner's conversation holds: its title, message, file name and content, the answer.
SECRET = "qx7zanzibar"
#: Server roles never hold ``install`` (desktop only: backups, diagnostics), so the viewers here do not either.
MEMBER_PERMISSIONS = ALL_PERMISSIONS - {Permission.INSTALL}
ALICE = Principal(id="usr_alice", role="owner", permissions=MEMBER_PERMISSIONS)
BOB = Principal(id="usr_bob", role="admin", permissions=MEMBER_PERMISSIONS)
#: A principal without a role (no member of the tenant): a service, a machine. Before W3-S1 it saw everything.
NOBODY = Principal(id="svc_reporting", permissions=MEMBER_PERMISSIONS)
#: Routes not of the studio: the published APIs and the app contract authenticate with keys of their own and serve
#: publications only (an answer's run is never a publication's run).
_OTHER_AUTHENTICATION = ("/api/published", "/api/app/")


@dataclass
class Owned:
    """What the owner made: an own-agent conversation with an answer, a file and a tool call; an assistant one."""

    conversation_id: str
    run_id: str
    message_id: str
    file_id: str
    assistant_id: str

    def path_values(self, path: str) -> dict[str, str]:
        values = {
            "run_id": self.run_id,
            "conversation_id": self.assistant_id if path.startswith("/api/assistant/") else self.conversation_id,
            "file_id": self.file_id,
            "message_id": self.message_id,
            "node_id": "agent",
            "input_id": self.file_id,
            "request_id": "tap_x",
        }
        return values

    def query(self) -> dict[str, str]:
        """Query values a route may filter by; a route ignores the ones it does not declare."""
        return {
            "q": SECRET,
            "query": SECRET,
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "trigger_ref": self.conversation_id,
            "workflow_id": self.conversation_id,
            "resource_id": self.run_id,
        }


def as_principal(client: TestClient, principal: Principal | None) -> None:
    client.app.state.principal_resolver = lambda _connection: principal  # type: ignore[attr-defined]


def make_owned(client: TestClient, *, wait: Callable[[str], None] | None = None) -> Owned:
    """The owner's conversation: title, a file and a message with the marker, answered with a tool call."""
    template = client.post(
        "/api/agents/templates",
        json={"agent": {"name": "Helper", "system_prompt": "AGENT", "tools": ["calculator"]}},
    )
    assert template.status_code == 201, template.text
    created = client.post(
        BASE, json={"agent": {"template_id": template.json()["id"]}, "model": CLOUD, "title": f"{SECRET} title"}
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    uploaded = client.post(
        f"{BASE}/{conversation_id}/files",
        files=[("files", (f"{SECRET}-notes.txt", f"{SECRET} file body".encode(), "text/plain"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()[0]["file"]["id"]
    sent = client.post(
        f"{BASE}/{conversation_id}/messages",
        json={"content": f"What is 6 times 7? {SECRET} message", "file_ids": [file_id]},
    )
    assert sent.status_code == 201, sent.text
    answer = sent.json()["assistant"]
    if wait is not None:
        wait(answer["run_id"])
    assistant = client.post("/api/assistant/conversations", json={"title": f"{SECRET} assistant"})
    assert assistant.status_code == 201, assistant.text
    return Owned(conversation_id, answer["run_id"], answer["id"], file_id, assistant.json()["id"])


def _routes(client: TestClient) -> list[tuple[str, str, str]]:
    from nodestro_api.permissions import check_routes

    return sorted(
        {
            (item.method, item.path, item.access)
            for item in check_routes(client.app)  # type: ignore[arg-type]
            if item.path.startswith("/api/") and not item.path.startswith(_OTHER_AUTHENTICATION)
        }
    )


def leaking_reads(client: TestClient, owned: Owned) -> list[tuple[str, int]]:
    """Every ``GET`` route whose answer holds the marker of the owner's conversation, as the current principal."""
    found = []
    for method, path, _access in _routes(client):
        if method != "GET":
            continue
        url = path.format_map(_Missing(owned.path_values(path)))
        response = client.get(url, params=owned.query())
        if SECRET in response.text:
            found.append((path, response.status_code))
    return found


def reachable_by_id(client: TestClient, owned: Owned) -> list[tuple[str, str, int, int]]:
    """Every route with a run, conversation or file id of the owner in its path that answers differently than for an
    unknown id, or succeeds: ``(method, path, status, status for an unknown id)``. ``404`` is always right; a body that
    is invalid for the route may be refused (``422``) before the id is looked up, alike for both ids, so it reveals
    nothing either."""
    found = []
    for method, path, _access in _routes(client):
        if not any(name in path for name in ("{run_id}", "{conversation_id}", "{file_id}", "{input_id}")):
            continue
        if path.startswith(("/api/benchmark-runs", "/api/knowledge/")):
            continue  # benchmark runs and knowledge files have ids of their own
        url = path.format_map(_Missing(owned.path_values(path)))
        response = client.request(method, url, json={})
        unknown = client.request(method, path.format_map(_Missing({})), json={})
        if response.status_code == 404:
            continue
        if response.status_code < 400 or (response.status_code, _code(response)) != (
            unknown.status_code,
            _code(unknown),
        ):
            found.append((method, path, response.status_code, unknown.status_code))
    return found


def _code(response: Any) -> str:
    try:
        return str(response.json().get("code", ""))
    except ValueError:
        return ""


class _Missing(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "x_unknown"


# ---- the desktop app with several principals ----------------------------------------------------------------------


@pytest.fixture
def owned_app(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Owned]]:
    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    client = make_app({"AGENT": [tool_use("calculator", {"expression": "6 * 7"}), text(f"It is 42. {SECRET} answer")]})
    as_principal(client, ALICE)
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    owned = make_owned(client, wait=lambda run_id: client.portal.call(ctx.wait, run_id))  # type: ignore[union-attr]
    run = client.get(f"/api/runs/{owned.run_id}").json()
    assert run["status"] == "succeeded" and SECRET in run["output"]
    yield client, owned


@pytest.mark.parametrize("viewer", [BOB, NOBODY], ids=["another_member_admin", "no_role_principal"])
def test_no_read_route_shows_anything_of_another_persons_conversation(
    owned_app: tuple[TestClient, Owned], viewer: Principal
) -> None:
    client, owned = owned_app
    # The scan finds the marker where the owner may see it (it is a real probe, not a blind one).
    seen_by_owner = {path for path, _status in leaking_reads(client, owned)}
    assert {
        "/api/conversations",
        "/api/conversations/{conversation_id}",
        "/api/conversations/{conversation_id}/export",
        "/api/conversations/{conversation_id}/files/{file_id}/content",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/events",
        "/api/runs/{run_id}/export",
        "/api/inputs/{file_id}",
        "/api/inputs/{file_id}/content",
        "/api/search",
        "/api/assistant/conversations",
    } <= seen_by_owner, seen_by_owner
    as_principal(client, viewer)
    assert leaking_reads(client, owned) == []


@pytest.mark.parametrize("viewer", [BOB, NOBODY], ids=["another_member_admin", "no_role_principal"])
def test_every_route_with_an_id_of_another_persons_conversation_answers_not_found(
    owned_app: tuple[TestClient, Owned], viewer: Principal
) -> None:
    client, owned = owned_app
    as_principal(client, viewer)
    assert reachable_by_id(client, owned) == []
    # Nothing was changed by them.
    as_principal(client, ALICE)
    detail = client.get(f"{BASE}/{owned.conversation_id}")
    assert detail.status_code == 200 and len(detail.json()["messages"]) == 2
    assert client.get(f"/api/inputs/{owned.file_id}/content").text == f"{SECRET} file body"
    assert client.get(f"/api/runs/{owned.run_id}").json()["status"] == "succeeded"


@pytest.mark.parametrize("viewer", [BOB, NOBODY], ids=["another_member_admin", "no_role_principal"])
def test_routes_that_take_a_run_id_in_their_body_refuse_another_persons_answer_run(
    owned_app: tuple[TestClient, Owned], viewer: Principal
) -> None:
    """Knowledge adoption reads a run's step output, proposals and result files (the answer), the context preview its
    input and outputs, an assistant attachment the whole run: another person's answer run is not found there."""
    client, owned = owned_app
    adopt = {"items": [{"kind": "node_output", "run_id": owned.run_id, "node_id": "agent"}], "collection": "Leak"}
    own = client.post("/api/knowledge/adopt/check", json=adopt)
    assert own.status_code == 200 and SECRET in own.text  # the owner may take her answer over
    as_principal(client, viewer)
    for kind in ("node_output", "result_file", "proposal"):
        body = {
            "items": [{"kind": kind, "run_id": owned.run_id, "node_id": "agent", "file_id": "f_1"}],
            "collection": "Leak",
        }
        for path, extra in (
            ("/api/knowledge/adopt/check", {}),
            ("/api/knowledge/adopt", {"confirmed": True, "tokens": ["t"]}),
        ):
            refused = client.post(path, json={**body, **extra})
            assert (refused.status_code, refused.json()["code"]) == (404, "run.not_found"), (kind, path, refused.text)
    workflow = {
        "name": "W",
        "nodes": [{"id": "agent", "type": "agent", "agent": {"name": "A", "system_prompt": "S"}}],
        "edges": [],
    }
    preview = client.post(
        "/api/workflows/preview", json={"workflow": workflow, "node_id": "agent", "run_id": owned.run_id}
    )
    assert (preview.status_code, preview.json()["code"]) == (404, "run.not_found")
    if viewer is not NOBODY:  # a principal without an owner identity has no assistant conversation to attach to
        mine = client.post("/api/assistant/conversations", json={}).json()["id"]
        attached = client.post(
            f"/api/assistant/conversations/{mine}/attachments", json={"kind": "run", "ref_id": owned.run_id}
        )
        assert (attached.status_code, attached.json()["code"]) == (404, "run.not_found")
    assert client.get("/api/knowledge").json() == []


@pytest.mark.parametrize("viewer", [ALICE, BOB, NOBODY], ids=["owner", "another_member_admin", "no_role_principal"])
def test_a_conversation_file_is_no_input_of_a_run_batch_or_form_field(
    owned_app: tuple[TestClient, Owned], viewer: Principal
) -> None:
    """A conversation's file stays in the conversation (like an assistant file, API-055): no run, batch or form field
    takes it by its id, not even its owner's (she uploads it as an input file on purpose). Before, a form field or a
    batch row of another person took it, and the run gave its contents to their agent."""
    client, owned = owned_app
    as_principal(client, viewer)
    form = {
        "id": "start",
        "type": "start",
        "start": {"fields": [{"name": "docs", "label": "Docs", "type": "file", "required": False}]},
    }
    agent = {"id": "a", "type": "agent", "agent": {"name": "A", "system_prompt": "AGENT", "model": CLOUD}}
    created = client.post(
        "/api/workflows",
        json={"name": "Reader", "nodes": [form, agent], "edges": [{"id": "e", "source": "start", "target": "a"}]},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    started = client.post(f"/api/workflows/{workflow_id}/runs", json={"values": {"docs": [owned.file_id]}})
    assert started.status_code in (400, 404, 422), started.text
    assert started.json()["code"] in ("form.invalid", "form.file_missing", "input.not_found", "not_found"), started.text
    rows = [{"values": {"docs": [owned.file_id]}}, {"input": "x", "file_ids": [owned.file_id]}]
    batch = client.post("/api/batches", json={"workflow_id": workflow_id, "rows": rows})
    assert (batch.status_code, batch.json()["code"]) == (400, "batch.no_valid_rows"), batch.text
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    catalog = ctx.file_catalog()
    assert owned.file_id not in catalog and owned.file_id not in list(catalog)


def test_the_activity_log_and_the_statistics_name_the_answers_without_their_contents(
    owned_app: tuple[TestClient, Owned],
) -> None:
    """An administrator reads the activity log (N-13) and every member the usage and skill statistics: they count
    and name an answer's run (its id, the fixed workflow name ``Conversation``, the tool it called, its cost), never
    the conversation's title, messages, files or answer (accepted and documented, runtime docs/conversations.md)."""
    client, owned = owned_app
    as_principal(client, BOB)
    events = client.get("/api/activity", params={"run_id": owned.run_id}).json()["events"]
    assert events, "the answer's actions are in the activity log"
    assert {event["run_id"] for event in events} == {owned.run_id}
    for fmt in ("jsonl", "csv"):
        exported = client.get("/api/activity/export", params={"format": fmt})
        assert exported.status_code == 200 and owned.run_id in exported.text and SECRET not in exported.text
    usage = client.get("/api/usage")
    assert usage.status_code == 200 and SECRET not in usage.text
    for path in ("/api/skill-stats", "/api/pricing/history", "/api/notifications"):
        response = client.get(path)
        assert response.status_code == 200 and SECRET not in response.text, path
