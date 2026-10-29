"""An optional, AI-written integration guide for a published API.

Everything an API needs works without AI: the endpoints, the OpenAPI description, the examples
and the README of an export are generated deterministically. This module only adds, on the
user's explicit request, a guide in prose ("how do I integrate this into my application?"),
written by the assistant model from those generated facts. The request is recorded in the
usage ledger (source ``api_docs``). A guide is a draft until the user has read and accepted it;
only an accepted guide can go into an export (``docs/AI_GUIDE.md``). It never contains code
that runs: the export's code is never written by AI.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import TYPE_CHECKING, Literal

from nodestro_runtime.engine import usage_of
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import MAX_RETRIES, model_id
from nodestro_runtime.schemas import Usage, new_id, now
from orkexa import Agent, UsageTracker
from pydantic import BaseModel, Field

from .assistants import language_rule

if TYPE_CHECKING:
    from .api_examples import ApiExamples
    from .context import Nodestro
    from .publishing import Publication, Release

GUIDES = "api_guides"


class ApiGuide(BaseModel):
    id: str = Field(default_factory=lambda: new_id("gde"))
    publication_id: str
    release_id: str
    release_number: int
    language: str
    markdown: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    created_at: datetime = Field(default_factory=now)
    accepted: bool = False
    accepted_at: datetime | None = None


class _GuideText(BaseModel):
    markdown: str = Field(description="The guide as Markdown")


_SYSTEM = """You write a short integration guide for a REST API that was generated from a workflow.
Use only the facts you are given: the endpoints, the request schema, the examples and the limits.
Never invent endpoints, fields, status codes or permissions, and never write API keys or other
secrets. Structure: what the API does, authentication, starting a run (asynchronous), following
it (status, events), approvals if the workflow has any, results and files, errors and limits,
a checklist for going live (HTTPS in front of it, key rotation, budgets). Keep it under 900 words.
"""


def _facts(publication: Publication, release: Release, samples: ApiExamples) -> str:
    bundle = release.bundle
    facts = {
        "name": publication.name,
        "description": publication.description,
        "version": release.number,
        "approval_steps": bundle.approvals(),
        "limits": publication.limits.model_dump(mode="json"),
        "endpoints": samples.endpoints,
        "request_example": samples.request,
        "curl": samples.curl,
        "responses": samples.responses,
        "output_schema": bundle.output_schema,
    }
    return json.dumps(facts, indent=2, ensure_ascii=False)


def generate(
    ctx: Nodestro, publication: Publication, release: Release, samples: ApiExamples, language: str
) -> ApiGuide:
    """Ask the assistant model for a guide (one request, recorded as ``api_docs``)."""
    model = ctx.settings().assistant_model
    tracker = UsageTracker()
    connection = ctx.connect_model(model)
    agent: Agent[_GuideText] = Agent(
        name="api-guide",
        model=model_id(model),
        system_prompt=_SYSTEM + language_rule(language, texts="the guide"),
        output_format=_GuideText,
        effort=connection.effort(model, "medium"),
        max_tokens=8000,
        max_tool_rounds=0,
        options=connection.options(),
        max_retries=MAX_RETRIES,
        usage_tracker=tracker,
        **connection.agent_kwargs(),
    )
    guide_id = new_id("gde")
    try:
        answer = agent.ask("Facts of the API:\n```json\n" + _facts(publication, release, samples) + "\n```")
    finally:
        ctx.usage_log.record("api_docs", tracker.records, label=publication.name, ref=guide_id)
    text = answer.markdown if isinstance(answer, _GuideText) else str(answer)
    guide = ApiGuide(
        id=guide_id,
        publication_id=publication.id,
        release_id=release.id,
        release_number=release.number,
        language=language,
        markdown=text.strip(),
        model=model,
        usage=usage_of(tracker.usage),
    )
    ctx.store.put(GUIDES, guide.id, guide)
    return guide


def get(ctx: Nodestro, guide_id: str) -> ApiGuide:
    guide = ctx.store.get(GUIDES, guide_id, ApiGuide)
    if guide is None:
        raise KeyError(guide_id)
    return guide


def decide(ctx: Nodestro, guide_id: str, decision: Literal["accept", "discard"]) -> ApiGuide | None:
    guide = get(ctx, guide_id)
    if decision == "discard":
        ctx.store.delete(GUIDES, guide_id)
        return None
    if not guide.markdown:
        raise NodestroError("api.guide_empty", "The guide is empty")
    guide.accepted, guide.accepted_at = True, now()
    ctx.store.put(GUIDES, guide.id, guide)
    return guide


def for_publication(ctx: Nodestro, publication_id: str) -> list[ApiGuide]:
    found = [item for item in ctx.store.list(GUIDES, ApiGuide) if item.publication_id == publication_id]
    return sorted(found, key=lambda item: item.created_at, reverse=True)
