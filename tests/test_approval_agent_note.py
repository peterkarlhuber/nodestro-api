"""The note to the agent of an approval decision through the studio API and the published API (E-0R-1 A).

A decision carries ``comment`` (for people only: the run's view, the audit trail) and ``agent_note`` (the only text of
the decision that later agents read). Both decision endpoints take the note, refuse one the approval does not take or
one that is too long with ``422`` and a code, and the waiting approval says whether it takes a note
(``allow_agent_note``). The canaries follow the comment into every model request of the run: it is never there.
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.schemas import AGENT_NOTE_MAX_CHARS
from test_published_api import PUBLISHED, auth, finish, new_key, publish

from conftest import text
from test_flow import edge, run, save, wait
from test_runs import agent_node

#: A reviewer's comment: it must never be part of a request to a model.
CANARY = "CANARY-COMMENT-7c3a"
NOTE = "Please mention the delivery time"
#: One character whose NFKC form is 18 characters long: a note within the request limit that is too long once cleaned.
WIDE = "ﷺ"


def requests(client: TestClient) -> str:
    """Every request any model got in this app, as one text."""
    return json.dumps(client.fake.messages.calls, default=str, ensure_ascii=False)  # type: ignore[attr-defined]


def note_workflow(client: TestClient, **approval: Any) -> str:
    """Draft, an approval, then an agent that reads the decision under the approval's name."""
    return save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("draft", "DRAFT"),
            {
                "id": "check",
                "type": "approval",
                "label": "Review",
                "approval": {"instructions": "Is it right?", **approval},
            },
            agent_node("send", "SEND", task_template="Draft: {{Draft}}\nReview: {{Review}}"),
            agent_node("revise", "REVISE", task_template="Draft: {{Draft}}\nReview: {{Review}}"),
            {"id": "out", "type": "output"},
        ],
        [
            edge("start", "draft"),
            edge("draft", "check"),
            edge("check", "send", "approved"),
            edge("check", "revise", "rejected"),
            edge("send", "out"),
            edge("revise", "out"),
        ],
    )


def decide(client: TestClient, run_id: str, **body: Any) -> Any:
    return client.post(f"/api/runs/{run_id}/approvals/check", json=body)


# ---- studio API ------------------------------------------------------------------------------


def test_the_note_reaches_the_later_agent_and_the_comment_never_does(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("Offer 1000 EUR")], "SEND": [text("Sent")]})
    paused = run(client, note_workflow(client, allow_agent_note=True))
    assert paused["status"] == "paused"
    pending = client.get("/api/approvals").json()
    assert [(item["node_id"], item["allow_agent_note"]) for item in pending] == [("check", True)]
    answer = decide(client, paused["id"], decision="approved", comment=CANARY, agent_note=NOTE)
    assert answer.status_code == 200, answer.text
    done = wait(client, paused["id"])
    assert done["status"] == "succeeded", done
    record = done["nodes"]["check"]["approval"]
    assert (record["comment"], record["agent_note"]) == (CANARY, NOTE)
    # The later agent reads the note; no model request of the run carries the comment.
    assert NOTE in done["nodes"]["send"]["prompt"]
    assert NOTE in requests(client)
    assert CANARY not in requests(client)


def test_the_comment_of_a_rejection_never_reaches_a_model(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("Offer 1000 EUR")], "REVISE": [text("Revised")]})
    paused = run(client, note_workflow(client, allow_agent_note=True))
    answer = decide(client, paused["id"], decision="rejected", comment=CANARY)
    assert answer.status_code == 200, answer.text
    done = wait(client, paused["id"])
    assert done["status"] == "succeeded", done
    assert done["nodes"]["revise"]["status"] == "succeeded"
    assert done["nodes"]["check"]["approval"]["agent_note"] == ""
    assert CANARY not in requests(client)


def test_a_comment_only_decision_still_works(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("Offer")], "SEND": [text("Sent")]})
    # A workflow saved before the note existed (no allow_agent_note): the note is allowed, the comment stays private.
    paused = run(client, note_workflow(client))
    assert client.get("/api/approvals").json()[0]["allow_agent_note"] is True
    answer = decide(client, paused["id"], decision="approved", comment=CANARY)
    assert answer.status_code == 200, answer.text
    done = wait(client, paused["id"])
    assert done["status"] == "succeeded", done
    assert done["nodes"]["check"]["approval"]["comment"] == CANARY
    assert done["nodes"]["check"]["approval"]["agent_note"] == ""
    assert CANARY not in requests(client)


def test_a_note_too_long_is_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("Offer")], "SEND": [text("Sent")]})
    paused = run(client, note_workflow(client, allow_agent_note=True))
    # Over the request limit: refused before the runtime sees it.
    over = decide(client, paused["id"], decision="approved", agent_note="x" * (AGENT_NOTE_MAX_CHARS + 1))
    assert over.status_code == 422, over.text
    assert over.json()["code"] == "request.invalid"
    assert over.json()["params"]["errors"][0]["loc"][-1] == "agent_note"
    # Within the request limit, but longer than the limit once cleaned (NFKC): the runtime's own code.
    wide = decide(client, paused["id"], decision="approved", agent_note=WIDE * 200)
    assert wide.status_code == 422, wide.text
    assert wide.json()["code"] == "approval.agent_note_too_long"
    assert wide.json()["params"] == {"limit": AGENT_NOTE_MAX_CHARS}
    # Nothing was decided: the approval still waits and takes a note of the limit.
    assert client.get(f"/api/runs/{paused['id']}").json()["status"] == "paused"
    exact = decide(client, paused["id"], decision="approved", agent_note="y" * AGENT_NOTE_MAX_CHARS)
    assert exact.status_code == 200, exact.text


def test_a_note_at_an_approval_that_takes_none_is_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"DRAFT": [text("Offer")], "SEND": [text("Sent")]})
    workflow_id = note_workflow(client, allow_agent_note=False)
    assert client.get(f"/api/workflows/{workflow_id}").json()["nodes"][2]["approval"]["allow_agent_note"] is False
    paused = run(client, workflow_id)
    assert client.get("/api/approvals").json()[0]["allow_agent_note"] is False
    refused = decide(client, paused["id"], decision="approved", comment=CANARY, agent_note=NOTE)
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "approval.agent_note_disabled"
    assert client.get(f"/api/runs/{paused['id']}").json()["status"] == "paused"
    answer = decide(client, paused["id"], decision="approved", comment=CANARY)
    assert answer.status_code == 200, answer.text
    assert wait(client, paused["id"])["status"] == "succeeded"
    assert CANARY not in requests(client)


def test_the_workflow_keeps_allow_agent_note(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    for value in (True, False):
        workflow_id = note_workflow(client, allow_agent_note=value)
        node = client.get(f"/api/workflows/{workflow_id}").json()["nodes"][2]
        assert node["approval"]["allow_agent_note"] is value
    older = client.get(f"/api/workflows/{note_workflow(client)}").json()["nodes"][2]
    assert older["approval"]["allow_agent_note"] is None


# ---- published API ---------------------------------------------------------------------------


def published_workflow(client: TestClient, **approval: Any) -> str:
    nodes = [
        {"id": "start", "type": "start", "start": {"fields": [{"name": "customer", "type": "text", "required": True}]}},
        agent_node("writer", "WRITER", task_template="Write for {{customer}}"),
        {"id": "check", "type": "approval", "label": "Review", "approval": {"instructions": "Ok?", **approval}},
        agent_node("final", "FINAL", task_template="Draft: {{Writer}}\nReview: {{Review}}"),
        {"id": "out", "type": "output"},
    ]
    edges = [edge("start", "writer"), edge("writer", "check"), edge("check", "final", "approved"), edge("final", "out")]
    return save(client, nodes, edges, name="Offer API", budget_usd=1.0)


def start(client: TestClient, slug: str, token: str) -> dict[str, Any]:
    started = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "ACME"}}, headers=auth(token))
    assert started.status_code == 202, started.text
    return finish(client, slug, token, started.json()["run_id"])


def test_the_published_api_takes_the_note(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Offer for ACME")], "FINAL": [text("Final offer")]})
    publication = publish(client, published_workflow(client, allow_agent_note=True))["publication"]
    token = new_key(client, publication["id"])
    slug = publication["slug"]
    paused = start(client, slug, token)
    assert paused["status"] == "awaiting_approval"
    approval = paused["approvals"][0]
    assert approval["allow_agent_note"] is True
    listed = client.get(f"{PUBLISHED}/{slug}/runs/{paused['run_id']}/approvals", headers=auth(token)).json()
    assert listed[0]["allow_agent_note"] is True
    url = f"{PUBLISHED}/{slug}/runs/{paused['run_id']}/approve"
    over = client.post(
        url, json={"decision": "approved", "agent_note": "x" * (AGENT_NOTE_MAX_CHARS + 1)}, headers=auth(token)
    )
    assert over.status_code == 422 and over.json()["error"]["code"] == "request.invalid", over.text
    wide = client.post(url, json={"decision": "approved", "agent_note": WIDE * 200}, headers=auth(token))
    assert wide.status_code == 422, wide.text
    assert wide.json()["error"]["code"] == "approval.agent_note_too_long"
    assert wide.json()["error"]["params"] == {"limit": AGENT_NOTE_MAX_CHARS}
    decided = client.post(
        url,
        json={"approval_id": approval["approval_id"], "decision": "approved", "comment": CANARY, "agent_note": NOTE},
        headers=auth(token),
    )
    assert decided.status_code == 200, decided.text
    done = finish(client, slug, token, paused["run_id"])
    assert done["status"] == "succeeded", done
    record = client.get(f"/api/runs/{paused['run_id']}").json()["nodes"]["check"]["approval"]
    assert (record["comment"], record["agent_note"]) == (CANARY, NOTE)
    assert record["decided_by"].startswith("api:")
    # The comment of a decision through the published API never reaches a model either.
    assert NOTE in requests(client)
    assert CANARY not in requests(client)


def test_the_published_api_refuses_a_note_the_approval_does_not_take(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Offer for ACME")], "FINAL": [text("Final offer")]})
    publication = publish(client, published_workflow(client, allow_agent_note=False))["publication"]
    token = new_key(client, publication["id"])
    slug = publication["slug"]
    paused = start(client, slug, token)
    assert paused["approvals"][0]["allow_agent_note"] is False
    url = f"{PUBLISHED}/{slug}/runs/{paused['run_id']}/approve"
    refused = client.post(url, json={"decision": "approved", "agent_note": NOTE}, headers=auth(token))
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "approval.agent_note_disabled"
    # A comment-only decision still works there, and its comment stays with people.
    decided = client.post(url, json={"decision": "approved", "comment": CANARY}, headers=auth(token))
    assert decided.status_code == 200, decided.text
    assert finish(client, slug, token, paused["run_id"])["status"] == "succeeded"
    assert CANARY not in requests(client)


def test_the_published_api_refuses_unknown_fields_still(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Offer for ACME")]})
    publication = publish(client, published_workflow(client))["publication"]
    token = new_key(client, publication["id"])
    paused = start(client, publication["slug"], token)
    assert paused["approvals"][0]["allow_agent_note"] is True
    answer = client.post(
        f"{PUBLISHED}/{publication['slug']}/runs/{paused['run_id']}/approve",
        json={"decision": "approved", "note": NOTE},
        headers=auth(token),
    )
    assert answer.status_code == 422 and answer.json()["error"]["code"] == "request.invalid", answer.text


# ---- new workflows ---------------------------------------------------------------------------


def test_a_library_copy_takes_the_note_explicitly(make_app: Callable[..., TestClient]) -> None:
    from test_library import copy_template

    client = make_app()
    copy = copy_template(client, "sales-offer", language="en")
    approvals = [node["approval"] for node in copy["nodes"] if node["type"] == "approval"]
    assert approvals and all(item["allow_agent_note"] is True for item in approvals)
    check = client.post(f"/api/workflows/check?workflow_id={copy['id']}", json=copy).json()
    assert "check.approval_comment_not_forwarded" not in {item["code"] for item in check["problems"]}
