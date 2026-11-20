"""The activity log's API (N-13): ``GET /api/activity`` and ``GET /api/activity/export``, and the server's pruning.

The events come from the runtime (:mod:`nodestro_runtime.activity`): runs, tool calls and uploads write them; tests of
filters and paging also store events with chosen times. The server cases (tenant isolation, the worker's pruning on
PostgreSQL) are in ``tests/test_worker.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import csv
from datetime import UTC, datetime, timedelta
import io
import json
import time
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime import activity
from nodestro_runtime.activity import ActivityEvent
from nodestro_runtime.testing import text, tool_use
import pytest
from test_inputs import upload

from nodestro_api.permissions import Permission, Principal, nobody
from nodestro_api.worker import ServerWorker

from test_flow import edge, save
from test_runs import agent_node

CANARY = "CANARY-7f3a91-SECRET"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _store(client: TestClient) -> Any:
    return client.app.state.nodestro.store  # type: ignore[attr-defined]


def _add(client: TestClient, minutes: int, **fields: Any) -> ActivityEvent:
    values: dict[str, Any] = {"source": "tool_call", "action": "tool.called", "actor": "local", **fields}
    event = ActivityEvent(at=T0 + timedelta(minutes=minutes), **values)
    assert activity.record(_store(client), event) is not None
    return event


def _finished(client: TestClient, run: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while run["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.05)
        run = client.get(f"/api/runs/{run['id']}").json()
    return run


def _ids(answer: Any) -> list[str]:
    assert answer.status_code == 200, answer.text
    return [event["id"] for event in answer.json()["events"]]


# ---- reading ------------------------------------------------------------------------------------------------------


def test_runs_and_uploads_are_listed_newest_first(make_app: Callable[..., TestClient]) -> None:
    calls = [tool_use("notify_user", {"title": "Totals", "message": "checked"}), text("done")]
    client = make_app({"NOTE": calls})
    assert client.get("/api/activity").json() == {"events": [], "next_before": None}
    flow = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("a", "NOTE", tools=["notify_user"]),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "a"), edge("a", "out")],
    )
    run = _finished(client, client.post(f"/api/workflows/{flow}/runs", json={"input": "go"}).json())
    assert run["status"] == "succeeded", run
    uploaded = upload(client, "notes.txt", b"some notes")

    events = client.get("/api/activity").json()["events"]
    actions = [event["action"] for event in reversed(events)]
    assert actions == ["workflow.started", "tool.called", "agent.finished", "workflow.finished", "file.uploaded"]
    by_action = {event["action"]: event for event in events}
    assert by_action["tool.called"] | {"id": None, "at": None} == {
        "id": None,
        "at": None,
        "source": "tool_call",
        "action": "tool.called",
        "actor": "local",
        "agent": "A",
        "resource_type": "tool",
        "resource_id": "notify_user",
        "outcome": "success",
        "code": None,
        "run_id": run["id"],
        "node_id": "a",
        "conversation_id": None,
        "source_event": None,
    }
    assert by_action["file.uploaded"]["resource_id"] == uploaded["file"]["id"]
    assert [
        event["action"] for event in client.get("/api/activity", params={"run_id": run["id"]}).json()["events"]
    ] == [
        "workflow.finished",
        "agent.finished",
        "tool.called",
        "workflow.started",
    ]


def test_every_filter_narrows_the_list(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    first = _add(
        client, 0, actor="usr_a", agent="Writer", resource_type="tool", resource_id="web_fetch", run_id="run_1"
    )
    second = _add(client, 1, actor="usr_b", outcome="denied", code="tool.not_allowed", run_id="run_2")
    third = _add(client, 2, source="file", action="file.uploaded", resource_type="input_file", resource_id="in_1")
    fourth = _add(client, 3, source="workflow", action="workflow.finished", conversation_id="conv_1", outcome="failure")

    def found(**params: Any) -> list[str]:
        return _ids(client.get("/api/activity", params=params))

    assert found() == [fourth.id, third.id, second.id, first.id]
    assert found(source="file") == [third.id]
    assert found(source=["file", "workflow"]) == [fourth.id, third.id]
    assert found(action="tool.called") == [second.id, first.id]
    assert found(action=["file.uploaded", "workflow.finished"]) == [fourth.id, third.id]
    assert found(actor="usr_b") == [second.id]
    assert found(agent="Writer") == [first.id]
    assert found(resource_type="input_file", resource_id="in_1") == [third.id]
    assert found(resource_id="web_fetch") == [first.id]
    assert found(outcome="denied") == [second.id]
    assert found(outcome="failure") == [fourth.id]
    assert found(run_id="run_2") == [second.id]
    assert found(conversation_id="conv_1") == [fourth.id]
    assert found(actor="usr_a", outcome="denied") == []  # filters combine
    # since is inclusive, until exclusive; a time without a zone is UTC.
    assert found(since=(T0 + timedelta(minutes=1)).isoformat()) == [fourth.id, third.id, second.id]
    assert found(until=(T0 + timedelta(minutes=2)).isoformat()) == [second.id, first.id]
    naive = (T0 + timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    assert found(since=naive, until=(T0 + timedelta(minutes=3)).isoformat()) == [third.id, second.id]
    assert found(since=(T0 + timedelta(hours=1, minutes=1)).isoformat().replace("+00:00", "+01:00")) == [
        fourth.id,
        third.id,
        second.id,
    ]


def test_invalid_filters_are_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    for params in (
        {"source": "mail"},
        {"outcome": "maybe"},
        {"limit": 0},
        {"limit": 1001},
        {"since": "yesterday"},
        {"actor": "x" * 201},
        {"before": ""},
    ):
        answer = client.get("/api/activity", params=params)
        assert answer.status_code == 422, params
        assert answer.json()["code"], params
    assert client.get("/api/activity/export", params={"format": "xlsx"}).status_code == 422


def test_pages_follow_one_another_without_gaps(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    added = [_add(client, minute) for minute in range(7)]
    # Two events at the same moment: the id decides their order, and paging keeps both.
    added.append(_add(client, 6))
    expected = [event.id for event in sorted(added, key=lambda event: event.key, reverse=True)]
    seen: list[str] = []
    before: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 3}
        if before is not None:
            params["before"] = before
        page = client.get("/api/activity", params=params).json()
        pages += 1
        seen += [event["id"] for event in page["events"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == expected and pages == 3
    # A page that ends exactly at the oldest event says so.
    full = client.get("/api/activity", params={"limit": len(added)}).json()
    assert len(full["events"]) == len(added) and full["next_before"] is None
    assert client.get("/api/activity", params={"limit": len(added) - 1}).json()["next_before"] is not None
    # Paging combines with filters.
    page = client.get("/api/activity", params={"limit": 2, "until": (T0 + timedelta(minutes=5)).isoformat()}).json()
    assert [event["id"] for event in page["events"]] == [added[4].id, added[3].id]
    rest = client.get(
        "/api/activity", params={"before": page["next_before"], "until": (T0 + timedelta(minutes=5)).isoformat()}
    )
    assert _ids(rest) == [added[2].id, added[1].id, added[0].id]


# ---- export -------------------------------------------------------------------------------------------------------


def test_the_export_holds_every_matching_event_in_both_formats(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    added = [_add(client, minute, resource_id=f"tool_{minute}") for minute in range(1205)]  # more than one page
    _add(client, 2000, source="file", action="file.uploaded", resource_type="input_file", resource_id="in_x")
    newest_first = [event.id for event in reversed(added)]

    jsonl = client.get("/api/activity/export", params={"format": "jsonl", "source": "tool_call"})
    assert jsonl.status_code == 200, jsonl.text
    assert jsonl.headers["content-type"] == "application/x-ndjson; charset=utf-8"
    disposition = jsonl.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="nodestro-activity-') and disposition.endswith('.jsonl"')
    assert jsonl.headers["cache-control"] == "no-store"
    assert jsonl.headers["x-content-type-options"] == "nosniff"
    rows = [json.loads(line) for line in jsonl.text.splitlines()]
    assert [row["id"] for row in rows] == newest_first
    assert list(rows[0]) == list(activity.FIELDS)

    exported = client.get("/api/activity/export", params={"format": "csv", "source": "tool_call"})
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"] == "text/csv; charset=utf-8"
    assert exported.headers["content-disposition"].endswith('.csv"')
    table = list(csv.reader(io.StringIO(exported.text)))
    assert tuple(table[0]) == activity.FIELDS
    assert [row[0] for row in table[1:]] == newest_first

    everything = client.get("/api/activity/export").text.splitlines()  # JSON Lines by default, no filter
    assert len(everything) == len(added) + 1


def test_the_csv_export_guards_formula_cells(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    _add(client, 0, actor="=HYPERLINK(1)", agent="+Agent", resource_id="@sum", run_id="-run")
    exported = client.get("/api/activity/export", params={"format": "csv"})
    row = dict(zip(activity.FIELDS, list(csv.reader(io.StringIO(exported.text)))[1], strict=True))
    assert (row["actor"], row["agent"], row["resource_id"], row["run_id"]) == (
        "'=HYPERLINK(1)",
        "'+Agent",
        "'@sum",
        "'-run",
    )
    # JSON Lines carries the values as they are (no spreadsheet reads it).
    [line] = client.get("/api/activity/export", params={"format": "jsonl"}).text.splitlines()
    assert json.loads(line)["actor"] == "=HYPERLINK(1)"


# ---- what never appears -------------------------------------------------------------------------------------------


def test_no_content_or_secret_reaches_the_activity_api(make_app: Callable[..., TestClient]) -> None:
    """The canary goes into a prompt, a run's input, tool arguments, a model's answer, an uploaded file's name and
    contents, a knowledge file and a stored secret; neither the list nor either export contains it."""
    calls = [tool_use("notify_user", {"title": CANARY, "message": f"<b>{CANARY}</b>"}), text(f"answer {CANARY}")]
    client = make_app({"NOTE": calls})
    flow = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("a", f"NOTE {CANARY}", tools=["notify_user"]),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "a"), edge("a", "out")],
    )
    run = _finished(client, client.post(f"/api/workflows/{flow}/runs", json={"input": f"go {CANARY}"}).json())
    assert run["status"] == "succeeded", run
    assert CANARY in json.dumps(client.get(f"/api/runs/{run['id']}").json())  # the run itself holds it
    upload(client, f"{CANARY}.txt", f"contents {CANARY}".encode())
    knowledge = client.post("/api/knowledge", files=[("files", (f"{CANARY}.md", f"# {CANARY}".encode()))])
    assert knowledge.status_code in (200, 201), knowledge.text
    settings = client.put("/api/settings", json={"provider_keys": {"openai": f"sk-{CANARY}"}})
    assert settings.status_code == 200, settings.text

    listed = client.get("/api/activity")
    assert len(listed.json()["events"]) >= 6  # run, tool call, agent step, uploads
    for answer in (
        listed,
        client.get("/api/activity/export", params={"format": "jsonl"}),
        client.get("/api/activity/export", params={"format": "csv"}),
    ):
        assert answer.status_code == 200
        assert CANARY not in answer.text
        assert CANARY.lower() not in answer.text.lower()


# ---- access -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/activity", "/api/activity/export"])
def test_only_administrators_read_the_activity_log(make_app: Callable[..., TestClient], path: str) -> None:
    client = make_app()
    _add(client, 0)
    app = client.app
    app.state.principal_resolver = nobody  # type: ignore[attr-defined]
    answer = client.get(path)
    assert answer.status_code == 401 and answer.json()["code"] == "access.login_required"
    for permissions in ({Permission.READ}, {Permission.READ, Permission.EDIT, Permission.RUN}):
        member = Principal(id="member", permissions=frozenset(permissions), tenant_id="local", role="editor")
        app.state.principal_resolver = lambda _connection, member=member: member  # type: ignore[attr-defined]
        answer = client.get(path)
        assert answer.status_code == 403, permissions
        assert answer.json()["code"] == "access.forbidden"
        assert answer.json()["params"] == {"permission": "admin"}
        assert "tool.called" not in answer.text
    admin = Principal(id="admin", permissions=frozenset({Permission.READ, Permission.ADMIN}), role="admin")
    app.state.principal_resolver = lambda _connection: admin  # type: ignore[attr-defined]
    assert client.get(path).status_code == 200


def test_the_routes_only_read(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert client.request(method, "/api/activity").status_code == 405
        assert client.request(method, "/api/activity/export").status_code == 405


# ---- the server's pruning -----------------------------------------------------------------------------------------


class _Leader:
    def __init__(self, leads: bool) -> None:
        self.leads = leads

    def acquire(self) -> bool:
        return self.leads


def _worker(stores: dict[str, Any], statuses: dict[str, str], *, leads: bool = True) -> ServerWorker:
    """A worker with only what :meth:`ServerWorker.prune_activity` uses (the PostgreSQL case is in test_worker)."""
    worker = object.__new__(ServerWorker)
    worker.leader = _Leader(leads)  # type: ignore[assignment]
    worker.identity = SimpleNamespace(  # type: ignore[assignment]
        tenants=lambda: [SimpleNamespace(id=tenant, status=status) for tenant, status in statuses.items()]
    )
    worker.scheduling = SimpleNamespace(get=lambda tenant: SimpleNamespace(store=stores[tenant]))  # type: ignore[assignment]
    return worker


def test_the_leading_worker_prunes_each_active_tenants_activity_log(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    acme, globex, initech = make_app(), make_app(), make_app()
    old = timedelta(days=-40)
    for client in (acme, globex, initech):
        activity.record(
            _store(client), ActivityEvent(source="file", action="file.uploaded", at=datetime.now(UTC) + old)
        )
        activity.record(_store(client), ActivityEvent(source="file", action="file.deleted"))
    stores = {"acme": _store(acme), "globex": _store(globex), "initech": _store(initech)}
    statuses = {"acme": "active", "globex": "active", "initech": "suspended"}

    monkeypatch.setenv(activity.RETENTION_ENV, "30")
    assert asyncio.run(_worker(stores, statuses, leads=False).prune_activity()) == {}  # only the leader prunes
    monkeypatch.setenv(activity.RETENTION_ENV, "0")
    assert asyncio.run(_worker(stores, statuses).prune_activity()) == {}  # 0: kept without limit
    monkeypatch.setenv(activity.RETENTION_ENV, "60")
    assert asyncio.run(_worker(stores, statuses).prune_activity()) == {}  # nothing is that old
    monkeypatch.setenv(activity.RETENTION_ENV, "30")
    assert asyncio.run(_worker(stores, statuses).prune_activity()) == {"acme": 1, "globex": 1}
    assert asyncio.run(_worker(stores, statuses).prune_activity()) == {}
    assert [event["action"] for event in acme.get("/api/activity").json()["events"]] == ["file.deleted"]
    # The suspended tenant keeps its events until it is resumed.
    assert len(initech.get("/api/activity").json()["events"]) == 2
    statuses["initech"] = "active"
    assert asyncio.run(_worker(stores, statuses).prune_activity()) == {"initech": 1}


def test_a_failing_tenant_does_not_stop_the_pruning_of_others(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    good = make_app()
    activity.record(_store(good), ActivityEvent(source="file", action="file.uploaded", at=T0 - timedelta(days=400)))

    class Broken:
        def transaction(self) -> Any:
            raise RuntimeError("storage unavailable")

    monkeypatch.delenv(activity.RETENTION_ENV, raising=False)  # the default, 90 days
    worker = _worker({"acme": Broken(), "globex": _store(good)}, {"acme": "active", "globex": "active"})
    assert asyncio.run(worker.prune_activity()) == {"globex": 1}
