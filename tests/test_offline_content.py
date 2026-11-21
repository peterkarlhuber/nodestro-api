"""Content of a run of an offline agent or conversation (N-09) stays on local models outside the run too: the
assistant's run attachment (review M2) and a test series' AI judge (review M3), as in run chat."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.local_models import LOCAL_DEFAULT_MODEL
from nodestro_runtime.schemas import AgentConfig, NodeRun, Run, SettingsIn, Workflow, now
from nodestro_runtime.testing import text
import pytest
from test_assistant import ASSISTANT, chat, requests_of, send

from nodestro_api.assistant import ChatMessage, ContextRef, _key
from nodestro_api.benchmarks import BenchmarkRun, Suite, TestCase

SECRET = "SECRET-OFFLINE-OUTPUT"
CLOUD = "anthropic:claude-sonnet-5-5"


def _run(ctx: Any, *, offline: bool = True, **extra: Any) -> Run:
    """A finished run stored as it is, whose step holds :data:`SECRET` (no model is asked for it)."""
    agent = AgentConfig(name="Solo", system_prompt="WORK", model=CLOUD, offline=offline)
    workflow = Workflow.model_validate(
        {
            "id": "wf_offline",
            "name": "Offline",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "solo", "type": "agent", "agent": agent.model_dump(mode="json")},
            ],
            "edges": [{"id": "e", "source": "start", "target": "solo"}],
        }
    )
    run = Run(
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        workflow=workflow,
        status="succeeded",
        nodes={"solo": NodeRun(node_id="solo", status="succeeded", prompt=SECRET, output=SECRET)},
        **extra,
    )
    ctx.store.put("runs", run.id, run)
    return run


def _ctx(client: TestClient) -> Any:
    return client.app.state.nodestro  # type: ignore[attr-defined]


def _attach(client: TestClient, conversation_id: str, run_id: str) -> Any:
    return client.post(
        f"/api/assistant/conversations/{conversation_id}/attachments", json={"kind": "run", "ref_id": run_id}
    )


def _leaked(client: TestClient) -> bool:
    return any(SECRET in str(request) for request in client.fake.messages.calls)  # type: ignore[attr-defined]


# ---- the assistant's run attachment (M2) ------------------------------------------------------------------------


def _offline_conversation(client: TestClient) -> str:
    """A conversation with an own agent (N-04), switched offline in the store (its answers ran before)."""
    from nodestro_runtime.conversations import COLLECTION, Conversation
    from test_conversations_api import BASE, template

    created = client.post(BASE, json={"agent": {"template_id": template(client)}, "model": CLOUD})
    assert created.status_code == 201, created.text
    store = _ctx(client).store
    found = store.get(COLLECTION, created.json()["id"], Conversation)
    store.put(COLLECTION, found.id, found.model_copy(update={"offline": True}))
    return str(found.id)


@pytest.mark.parametrize("kind", ["agent", "conversation"])
def test_an_offline_run_is_not_attached_while_the_assistant_model_is_a_cloud_model(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from nodestro_runtime.conversations import CONVERSATIONS_ENV

    monkeypatch.setenv(CONVERSATIONS_ENV, "1")
    client = make_app({ASSISTANT: [text("Analyse")]})
    ctx = _ctx(client)
    if kind == "agent":
        run = _run(ctx)
    else:  # an answer of a conversation that is offline now; its agent is not
        run = _run(ctx, offline=False, trigger="conversation", trigger_ref=_offline_conversation(client))
    conversation_id = chat(client)
    response = _attach(client, conversation_id, run.id)
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "offline.cloud_model"
    assert response.json()["params"]["scope"] == kind
    assert client.get(f"/api/assistant/conversations/{conversation_id}").json()["attachments"] == []
    send(client, conversation_id, "Was ist passiert?")
    assert requests_of(client) and not _leaked(client)


def test_a_run_without_offline_content_is_attached_as_before(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Analyse")]})
    run = _run(_ctx(client), offline=False)
    conversation_id = chat(client)
    response = _attach(client, conversation_id, run.id)
    assert response.status_code == 200, response.text
    send(client, conversation_id, "Was ist passiert?")
    assert _leaked(client)  # the run's content reaches the assistant, unchanged


def test_an_offline_run_attached_under_a_local_model_never_reaches_a_cloud_model(
    make_app: Callable[..., TestClient],
) -> None:
    client = make_app({ASSISTANT: [text("never")]})
    ctx = _ctx(client)
    run = _run(ctx)
    conversation_id = chat(client)
    ctx.update_settings(SettingsIn(assistant_model=LOCAL_DEFAULT_MODEL))
    attached = _attach(client, conversation_id, run.id)
    assert attached.status_code == 200, attached.text
    snapshot_id = attached.json()["attachments"][0]["snapshot_id"]
    assert ctx.assistant.snapshot(conversation_id, snapshot_id).offline == "agent"
    # The assistant model is a cloud model again, or the message asks for one: refused, nothing sent or stored.
    ctx.update_settings(SettingsIn(assistant_model=CLOUD))
    for body in ({"content": "Und jetzt?"}, {"content": "Und jetzt?", "model": CLOUD}):
        response = client.post(f"/api/assistant/conversations/{conversation_id}/messages", json=body)
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "offline.cloud_model"
    assert client.get(f"/api/assistant/conversations/{conversation_id}").json()["messages"] == 0
    assert not _leaked(client)


def test_a_run_sent_earlier_binds_the_conversation_after_it_is_detached(make_app: Callable[..., TestClient]) -> None:
    """The history repeats earlier answers about the run, so detaching it does not free the conversation."""
    client = make_app()
    ctx = _ctx(client)
    run = _run(ctx)
    conversation_id = chat(client)
    ctx.update_settings(SettingsIn(assistant_model=LOCAL_DEFAULT_MODEL))
    attached = _attach(client, conversation_id, run.id).json()["attachments"][0]
    service = ctx.assistant
    assert service.offline_scope(conversation_id) == "agent"
    client.delete(f"/api/assistant/conversations/{conversation_id}/attachments/{attached['id']}")
    assert service.offline_scope(conversation_id) is None  # never sent: nothing of it is in the conversation
    message = ChatMessage(
        conversation_id=conversation_id,
        seq=1,
        role="user",
        content="Was ist passiert?",
        context=[
            ContextRef(
                attachment_id=attached["id"],
                snapshot_id=attached["snapshot_id"],
                kind="run",
                ref_id=run.id,
                label=attached["label"],
                captured_at=now(),
            )
        ],
    )
    service.store.put("assistant_messages", _key(conversation_id, 1), message)
    assert service.offline_scope(conversation_id) == "agent"
    ctx.store.delete("runs", run.id)  # the snapshot keeps it
    assert service.offline_scope(conversation_id) == "agent"


# ---- a test series' AI judge (M3) -------------------------------------------------------------------------------


def _judge(ctx: Any, run_id: str | None) -> Any:
    suite = Suite(name="S", workflow_id="wf_offline", judge_model=CLOUD)
    case = TestCase(name="A", input="go", judge_criteria="Ist korrekt")
    bench = BenchmarkRun(suite_id=suite.id, suite_name=suite.name, workflow_id=suite.workflow_id)
    tracker = ctx.tracker(None)
    return asyncio.run(ctx.benchmarks._judge(bench, suite, case, SECRET, tracker, run_id))


def test_the_judge_of_a_test_series_keeps_offline_output_on_local_models(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    ctx = _ctx(client)
    verdict = _judge(ctx, _run(ctx).id)
    assert verdict.error_code == "offline.cloud_model" and verdict.passed is False
    assert not _leaked(client)
    # A run that cannot be read fails closed.
    assert _judge(ctx, "run_gone").error_code == "offline.cloud_model"
    assert not _leaked(client)


def test_the_judge_of_a_run_without_offline_content_is_asked_as_before(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    ctx = _ctx(client)
    verdict = _judge(ctx, _run(ctx, offline=False).id)
    assert verdict.error_code != "offline.cloud_model"
    assert _leaked(client)  # the cloud judge got the output
