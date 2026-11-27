"""Independent re-review R-1: a tool classified differently while its call waits for a person's decision.

The first test is the independent reviewer's regression test (``test_rereview_reclassify_destructive.py``), taken
over unchanged in substance: a call asked as ``write``, the tool reclassified as ``destructive`` while it waits, an
operator (``run``) approves. The other tests cover both directions, rejections, administrator rights and parallel
decisions. Every case counts the calls that reached the MCP server.

The races are made deterministic like the runtime's own (RT-087): the waiting run is held between two reads of its
request (:class:`HeldPolls`), so what a test does meanwhile lands before the engine reads the request again, whatever
the timing on the machine. Without it, the racing test depended on the engine's poll interval and failed on busy CI
runners when the run read the approval before the reclassification landed.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.mcp.service import McpService
from nodestro_runtime.tool_approvals import ENABLE_ENV, ToolApprovals, audit_events
import pytest
import test_mcp_review_api as review  # reuse the fixtures of the fix round
from test_mcp_review_api import DELETE, SCRIPT, Counting, _finished, _waiting, as_role, connected, save_workflow

mcp_server = review.mcp_server

MakeApp = Callable[..., TestClient]


class HeldPolls:
    """Holds the waiting run between two reads of its approval request (as in the runtime's RT-087 tests): ``held``
    waits until the run sits in such a pause, and until ``release`` it reads nothing. What happens in between lands
    before the engine reads the request again, whatever the timing on the machine."""

    def __init__(self, approvals: ToolApprovals) -> None:
        self._entered = threading.Event()
        self._release = threading.Event()
        original = approvals.sleep

        def sleep(seconds: float) -> None:
            self._entered.set()
            if not self._release.wait(timeout=30):
                raise AssertionError("the waiting run was held too long")
            original(seconds)

        approvals.sleep = sleep

    def held(self) -> None:
        assert self._entered.wait(timeout=30), "the run never waited for its approval"

    def release(self) -> None:
        self._release.set()


def _server_and_tool(client: TestClient) -> tuple[str, dict[str, Any]]:
    [server] = client.get("/api/mcp/servers").json()
    tools = {t["name"]: t for t in client.get(f"/api/mcp/servers/{server['id']}/tools").json()}
    return server["id"], tools["delete_page"]


def _reclassify(client: TestClient, access: str) -> None:
    as_role(client, None)
    server_id, tool = _server_and_tool(client)
    answer = client.post(
        f"/api/mcp/servers/{server_id}/tools/delete_page/decision",
        json={"approve": True, "access": access, "expected_hash": tool["hash"]},
    )
    assert answer.status_code == 200, answer.text


def _start(
    make_app: MakeApp, url: str, access: str, script: dict[str, list[Any]] = SCRIPT
) -> tuple[TestClient, str, dict[str, Any]]:
    client = connected(make_app, url, script, delete_access=access)
    client.app.state.nodestro.runs.tool_gate.approvals.poll_seconds = 0.02  # type: ignore[attr-defined]
    workflow_id = save_workflow(client, [DELETE])
    run_id = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"}).json()["id"]
    request = _waiting(client)
    assert request["capability"]["access"] == access
    return client, run_id, request


def _decide(client: TestClient, run_id: str, request: dict[str, Any], decision: str) -> Any:
    return client.post(
        f"/api/runs/{run_id}/tool-approvals/{request['id']}",
        json={"decision": decision, "arguments_hash": request["arguments_hash"]},
    )


def _call_of(client: TestClient, run_id: str) -> dict[str, Any]:
    as_role(client, None)
    run = _finished(client, run_id)
    [call] = run["nodes"]["a"]["tool_calls"]
    return call  # type: ignore[no-any-return]


def test_reclassified_to_destructive_while_waiting(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    """The reviewer's test: a destructive MCP call never runs on a ``run`` approval after a reclassification."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client, run_id, request = _start(make_app, handle.url, "write")
    _reclassify(client, "destructive")
    as_role(client, "operator")
    decided = _decide(client, run_id, request, "approved")
    assert decided.status_code == 403, decided.text
    assert decided.json()["params"]["permission"] == "admin"
    call = _call_of(client, run_id)
    assert seen.deleted == [], "destructive MCP call ran on an operator's approval after reclassification"
    assert call["is_error"] and call["approval"]["code"] == "tool.approval_stale"


@pytest.mark.parametrize(("asked", "now"), [("write", "destructive"), ("destructive", "write")])
def test_an_administrator_cannot_approve_a_reclassified_call_either(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting], asked: str, now: str
) -> None:
    """Either direction: the approval was asked with another classification, so it is refused (409) for everybody;
    the request is closed and the call is not made."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client, run_id, request = _start(make_app, handle.url, asked)
    _reclassify(client, now)
    as_role(client, "admin")
    for decision in ("approved", "rejected"):
        refused = _decide(client, run_id, request, decision)
        assert refused.status_code == 409 and refused.json()["code"] == "tool.approval_stale", refused.text
    as_role(client, None)
    [stored] = client.get(f"/api/runs/{run_id}/tool-approvals").json()
    assert stored["status"] == "expired" and stored["closed_because"] == "tool.reclassified"
    assert _call_of(client, run_id)["approval"]["code"] == "tool.approval_stale"
    assert seen.deleted == []


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_an_operator_cannot_decide_a_call_asked_as_destructive_after_a_downgrade(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting], decision: str
) -> None:
    """Asked as ``destructive``, now ``write``: the stored classification still needs ``admin``."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client, run_id, request = _start(make_app, handle.url, "destructive")
    _reclassify(client, "write")
    as_role(client, "operator")
    refused = _decide(client, run_id, request, decision)
    assert refused.status_code == 403 and refused.json()["params"]["permission"] == "admin", refused.text
    assert _call_of(client, run_id)["approval"]["code"] == "tool.approval_stale"
    assert seen.deleted == []


def test_after_the_reclassification_the_next_call_is_asked_again_and_decided_by_an_admin(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    twice = {key: steps * 2 for key, steps in SCRIPT.items()}
    client, run_id, _request = _start(make_app, handle.url, "write", twice)
    _reclassify(client, "destructive")
    _call_of(client, run_id)
    workflow_id = save_workflow(client, [DELETE])
    run_id = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"}).json()["id"]
    request = _waiting(client)
    assert request["capability"]["access"] == "destructive"
    as_role(client, "operator")
    assert _decide(client, run_id, request, "approved").status_code == 403
    as_role(client, "admin")
    approved = _decide(client, run_id, request, "approved")
    assert approved.status_code == 200 and approved.json()["decided_by"] == "admin", approved.text
    call = _call_of(client, run_id)
    assert not call["is_error"] and seen.deleted == ["7"]


def test_parallel_decisions_store_exactly_one(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    """Several approvals and rejections of one ``write`` call at the same time: one wins (200), every other one is
    refused (409 ``tool.approval_decided``), and the call runs at most once."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client, run_id, request = _start(make_app, handle.url, "write")
    as_role(client, "operator")
    barrier = threading.Barrier(6)
    answers: list[Any] = []

    def decide(decision: str) -> None:
        barrier.wait()
        answers.append(_decide(client, run_id, request, decision))

    threads = [threading.Thread(target=decide, args=("approved" if i % 2 else "rejected",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    won = [a.json()["status"] for a in answers if a.status_code == 200]
    assert len(won) == 1, [a.text for a in answers]
    assert {a.json()["code"] for a in answers if a.status_code != 200} == {"tool.approval_decided"}
    call = _call_of(client, run_id)
    assert call["approval"]["status"] == won[0]
    assert seen.deleted == (["7"] if won == ["approved"] else [])


@pytest.mark.parametrize("attempt", range(4))
def test_a_reclassification_racing_an_approval_never_runs_the_call(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting], attempt: int
) -> None:
    """An operator approves while an administrator reclassifies the tool as ``destructive`` at the same moment:
    whichever wins, the call is not made.

    Both land before the waiting run reads its request again (:class:`HeldPolls`): the race is between the two
    people, and the engine must refuse whichever won. A run that read the approval before the reclassification is
    the next test's case."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client, run_id, request = _start(make_app, handle.url, "write")
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    server_id, tool = _server_and_tool(client)
    polls = HeldPolls(nodestro.runs.tool_gate.approvals)
    polls.held()
    barrier = threading.Barrier(2)
    refused: list[str] = []

    def approve() -> None:
        barrier.wait()
        try:
            nodestro.runs.decide_tool_call(
                run_id,
                request["id"],
                approved=True,
                by="operator",
                arguments=request["arguments_hash"],
                current=nodestro.tools.get(DELETE).capability,
            )
        except Exception as exc:  # refused: stale (the reclassification came first)
            refused.append(str(getattr(exc, "code", exc)))

    def reclassify() -> None:
        barrier.wait()
        nodestro.mcp.decide(
            server_id, "delete_page", approve=True, access="destructive", expected_hash=tool["hash"], actor="admin"
        )

    threads = [threading.Thread(target=approve), threading.Thread(target=reclassify)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    polls.release()
    assert refused in ([], ["tool.approval_stale"]), refused
    call = _call_of(client, run_id)
    assert call["is_error"] and call["approval"]["code"] == "tool.approval_stale"
    assert seen.deleted == []


def test_an_approval_read_before_the_reclassification_is_not_sent(
    make_app: MakeApp, monkeypatch: pytest.MonkeyPatch, mcp_server: tuple[Any, Counting]
) -> None:
    """The other order of the race above, made deterministic: the run reads the approval and its gate lets the call
    run as ``write``, then the tool is reclassified as ``destructive`` before the request reaches the MCP server (here:
    while the service fetches the credential). The service checks the binding again right before it sends and does
    not send. This is the interleaving that made the racing test fail on CI before it held the run's polls."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    handle, seen = mcp_server
    client, run_id, request = _start(make_app, handle.url, "write")
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    server_id, tool = _server_and_tool(client)
    original = McpService.credential
    armed = threading.Event()

    def credential(self: McpService, found: Any) -> Any:
        if armed.is_set():
            armed.clear()
            nodestro.mcp.decide(
                server_id, "delete_page", approve=True, access="destructive", expected_hash=tool["hash"], actor="admin"
            )
        return original(self, found)

    monkeypatch.setattr(McpService, "credential", credential)
    armed.set()
    nodestro.runs.decide_tool_call(
        run_id,
        request["id"],
        approved=True,
        by="operator",
        arguments=request["arguments_hash"],
        current=nodestro.tools.get(DELETE).capability,
    )
    call = _call_of(client, run_id)
    assert not armed.is_set(), "the reclassification did not land inside the call"
    assert call["is_error"] and (call["output"] or "").startswith("tool.approval_stale:"), call
    assert seen.deleted == [], "the call was sent although the tool was reclassified before the request"
    # Welle 5 (runtime): the record and the audit say so, as for a refusal at the engine's own gate.
    assert call["approval"]["status"] == "approved" and call["approval"]["code"] == "tool.approval_stale", call
    stale = [item for item in audit_events(nodestro.store) if item.kind == "stale" and item.request_id == request["id"]]
    assert [item.outcome for item in stale] == ["tool.reclassified"]
