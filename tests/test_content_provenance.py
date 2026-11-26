"""Content of a granted source (N-08, runtime ``docs/content-provenance.md``) needs a grant for a cloud model outside
the run too: the assistant's run, memory and knowledge attachments and a test series' AI judge. Off by default:
without the runtime's switch everything stays as before (``test_offline_content`` covers that path unchanged)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.schemas import now
from nodestro_runtime.testing import text
from nodestro_runtime.tool_approvals import ACCESS_ENV, ENABLE_ENV, PROVENANCE_ENV, AccessRules, Person
from nodestro_runtime.tool_policy import AccessResource
import pytest
from test_assistant import ASSISTANT, chat, requests_of
from test_offline_content import SECRET, _attach, _ctx, _judge, _leaked, _run

from nodestro_api.assistant import ContextRef


@pytest.fixture
def switched_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (ENABLE_ENV, ACCESS_ENV, PROVENANCE_ENV):
        monkeypatch.setenv(name, "1")


def _granted_run(ctx: Any) -> Any:
    """A finished run that read a granted folder (its mark set as the engine sets it)."""
    return _run(ctx, offline=False, granted_content=True)


def _message(client: TestClient, conversation_id: str) -> Any:
    return client.post(f"/api/assistant/conversations/{conversation_id}/messages", json={"content": "Was steht drin?"})


@pytest.mark.usefixtures("switched_on")
def test_an_attached_run_with_granted_content_never_reaches_a_cloud_assistant_without_a_grant(
    make_app: Callable[..., TestClient],
) -> None:
    client = make_app({ASSISTANT: [text("Analyse"), text("Analyse")]})
    ctx = _ctx(client)
    assert ctx.runs.provenance_on()
    run = _granted_run(ctx)
    conversation_id = chat(client)
    attached = _attach(client, conversation_id, run.id)
    assert attached.status_code == 200, attached.text
    assert ctx.assistant.content_granted(conversation_id)
    refused = _message(client, conversation_id)
    assert refused.status_code >= 400 and refused.json()["code"] == "access.missing_grant", refused.text
    assert requests_of(client) == [] and not _leaked(client)
    # A grant for the assistant's provider lets the content go there.
    AccessRules(ctx.store).grant(
        resource=AccessResource(type="cloud_provider", id="anthropic"),
        actions=["submit"],
        duration="until_revoked",
        by=Person("local"),
    )
    assert _message(client, conversation_id).status_code == 201


@pytest.mark.usefixtures("switched_on")
def test_a_run_without_granted_content_is_attached_and_sent_as_before(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Analyse")]})
    ctx = _ctx(client)
    run = _run(ctx, offline=False)
    conversation_id = chat(client)
    assert _attach(client, conversation_id, run.id).status_code == 200
    assert not ctx.assistant.content_granted(conversation_id)
    assert _message(client, conversation_id).status_code == 201


@pytest.mark.usefixtures("switched_on")
def test_memory_files_and_knowledge_text_are_granted_content(make_app: Callable[..., TestClient]) -> None:
    from nodestro_api.assistant import _SNAPSHOTS, Snapshot

    client = make_app()
    ctx = _ctx(client)
    service = ctx.assistant

    def sent(conversation_id: str, kind: str, **snapshot: Any) -> None:
        stored = Snapshot(
            conversation_id=conversation_id, kind=kind, ref_id="ref", label="x", fingerprint="f", data={}, **snapshot
        )
        service.store.put(_SNAPSHOTS, f"{conversation_id}:{stored.id}", stored)
        from nodestro_api.assistant import ChatMessage, _key

        message = ChatMessage(
            conversation_id=conversation_id,
            seq=len(service.messages(conversation_id)) + 1,
            role="user",
            content="?",
            context=[
                ContextRef(
                    attachment_id="att",
                    snapshot_id=stored.id,
                    kind=kind,  # type: ignore[arg-type]
                    ref_id="ref",
                    label="x",
                    captured_at=now(),
                )
            ],
        )
        service.store.put("assistant_messages", _key(conversation_id, message.seq), message)

    first, second = chat(client), chat(client)
    for conversation_id in (first, second):
        sent(conversation_id, "memory")  # only the file list
        sent(conversation_id, "knowledge")  # only the metadata
        assert not service.content_granted(conversation_id)
    sent(first, "knowledge", include_content=True)
    sent(second, "memory", paths=["/memories/plan.md"])
    assert service.content_granted(first) and service.content_granted(second)


@pytest.mark.usefixtures("switched_on")
def test_the_judge_of_a_test_series_needs_a_grant_for_granted_content(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    ctx = _ctx(client)
    verdict = _judge(ctx, _granted_run(ctx).id)
    assert verdict.error_code == "access.missing_grant" and verdict.passed is False
    assert not _leaked(client)
    # A run that cannot be read fails closed (N-09's offline check refuses it first).
    assert _judge(ctx, "run_gone").error_code in ("offline.cloud_model", "access.missing_grant")
    assert not _leaked(client)
    # Output without granted content goes to the cloud judge as before.
    assert _judge(ctx, _run(ctx, offline=False).id).error_code != "access.missing_grant"
    assert SECRET and _leaked(client)


def test_while_the_switch_is_off_nothing_changes(make_app: Callable[..., TestClient]) -> None:
    client = make_app({ASSISTANT: [text("Analyse")]})
    ctx = _ctx(client)
    assert not ctx.runs.provenance_on()
    run = _granted_run(ctx)
    conversation_id = chat(client)
    assert _attach(client, conversation_id, run.id).status_code == 200
    assert not ctx.assistant.content_granted(conversation_id)
    assert _judge(ctx, run.id).error_code != "access.missing_grant"
