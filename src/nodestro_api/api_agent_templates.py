"""Agent templates with versions, the shipped agent library, import and export (NAL-B1).

A saved agent template keeps every changed save as an immutable version (:mod:`nodestro_runtime.agent_templates`);
``GET /api/agents/templates`` still lists the latest version of each, so older clients keep working. The shipped
templates come from ``nodestro-examples`` (``/api/library/agents``) and are read-only: copying one stores an
ordinary template of the person's own with ``origin`` ``builtin:...``; a newer package version never changes it.

An export (``nodestro-agent/1``) carries the agent and its description, never secrets, service connections,
knowledge files or approval rules. An import is untrusted content: it is validated, at most 1 MB, its approval rules
(``tool_policies``) are reset to the defaults (so a file cannot bring exceptions such as "never ask"), service
connections and knowledge files are dropped, and the answer names what the template still needs in this workspace.

Permissions: ``read`` lists, reads versions, exports and checks; ``edit`` creates, changes, deletes, copies and
imports.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from nodestro_examples import agents as agent_library
from nodestro_runtime.agent_builder import ConfigIssue
from nodestro_runtime.agent_templates import AgentTemplates
from nodestro_runtime.providers import check_model
from nodestro_runtime.schemas import (
    AgentConfig,
    AgentTemplate,
    AgentTemplateIn,
    AgentTemplateVersion,
    KnowledgeConfig,
    Language,
    TemplateMeta,
)
from pydantic import BaseModel, Field, ValidationError

from . import library_models
from .api import Ctx, request_language, service_connections
from .permissions import EDIT, READ
from .serving import ApiError

router = APIRouter(prefix="/api")

#: The format of an exported agent template.
EXPORT_FORMAT: Literal["nodestro-agent/1"] = "nodestro-agent/1"
#: The largest import accepted (the serialized body).
MAX_IMPORT_BYTES = 1_000_000


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return str(getattr(principal, "id", "") or "")


def _templates(ctx: Ctx) -> AgentTemplates:
    return AgentTemplates(ctx.store)


def _get(ctx: Ctx, template_id: str) -> AgentTemplate:
    found = _templates(ctx).get(template_id)
    if found is None:
        raise KeyError(template_id)
    return found


# ---- the person's templates -----------------------------------------------------------------------------------


@router.get("/agents/templates", dependencies=READ)
def list_templates(ctx: Ctx) -> list[AgentTemplate]:
    """Every template in its latest version."""
    return ctx.store.list("templates", AgentTemplate)


@router.post("/agents/templates", status_code=201, dependencies=EDIT)
def create_template(body: AgentTemplateIn, request: Request, ctx: Ctx) -> AgentTemplate:
    """A new template (version 1); its origin is always ``user``: only copies and imports set another one."""
    meta = body.meta.model_copy(update={"origin": "user"})
    return _templates(ctx).create(body.agent, meta, note=body.note, created_by=_actor(request))


@router.put("/agents/templates/{template_id}", dependencies=EDIT)
def update_template(template_id: str, body: AgentTemplateIn, request: Request, ctx: Ctx) -> AgentTemplate:
    """Store a change as the next version (an unchanged save makes none). A body without ``meta`` (an older client)
    keeps the stored description; the origin never changes."""
    current = _get(ctx, template_id)
    meta = body.meta.model_copy(update={"origin": current.meta.origin}) if "meta" in body.model_fields_set else None
    return _templates(ctx).save(template_id, body.agent, meta, note=body.note, created_by=_actor(request))


@router.delete("/agents/templates/{template_id}", status_code=204, dependencies=EDIT)
def delete_template(template_id: str, ctx: Ctx) -> Response:
    """Delete a template with all its versions; agents copied from it into workflows stay as they are."""
    if not _templates(ctx).delete(template_id):
        raise KeyError(template_id)
    ctx.organize.forget("template", template_id)
    return Response(status_code=204)


@router.get("/agents/templates/{template_id}/versions", dependencies=READ)
def list_template_versions(template_id: str, ctx: Ctx) -> list[AgentTemplateVersion]:
    """Every version, newest first."""
    _get(ctx, template_id)
    return list(reversed(_templates(ctx).versions(template_id)))


@router.get("/agents/templates/{template_id}/versions/{version}", dependencies=READ)
def get_template_version(template_id: str, version: int, ctx: Ctx) -> AgentTemplateVersion:
    _get(ctx, template_id)
    return _templates(ctx).version(template_id, version)


class TemplateCheck(BaseModel):
    """What a template needs that this workspace lacks; empty ``issues``: it can be used as it is."""

    issues: list[ConfigIssue] = Field(default_factory=list)


def _issue(code: str, message: str, *, field: str, **params: Any) -> ConfigIssue:
    return ConfigIssue(level="error", field=field, code=code, params=params, message=message)


def _check(ctx: Ctx, agent: AgentConfig, meta: TemplateMeta) -> TemplateCheck:
    issues = list(ctx.builder.validate(agent))
    for kind in meta.requirements.services:
        if not ctx.connections.connections(type=kind):
            issues.append(
                _issue(
                    "template.service_missing",
                    f"The template needs a service connection of kind {kind}; none is set up",
                    field="tools",
                    service=kind,
                )
            )
    return TemplateCheck(issues=issues)


@router.get("/agents/templates/{template_id}/check", dependencies=READ)
def check_template(template_id: str, ctx: Ctx) -> TemplateCheck:
    """What the template's latest version needs in this workspace: tools, service connections, a usable model."""
    template = _get(ctx, template_id)
    return _check(ctx, template.agent, template.meta)


# ---- export and import ----------------------------------------------------------------------------------------


class AgentExport(BaseModel):
    """An exported agent template (``nodestro-agent/1``)."""

    format: Literal["nodestro-agent/1"] = EXPORT_FORMAT
    agent: AgentConfig
    meta: TemplateMeta = Field(default_factory=TemplateMeta)


def _portable(agent: AgentConfig) -> AgentConfig:
    """The agent without what belongs to one workspace or must not travel: service connections, knowledge files,
    approval rules and the origin of a workflow agent."""
    return agent.model_copy(
        update={
            "tool_settings": {
                name: settings.model_copy(update={"connection_id": None})
                for name, settings in agent.tool_settings.items()
            },
            "tool_policies": {},
            "knowledge": KnowledgeConfig(
                **agent.knowledge.model_dump(exclude={"file_ids"}),
            ),
            "origin": None,
        }
    )


@router.get("/agents/templates/{template_id}/export", dependencies=READ)
def export_template(template_id: str, ctx: Ctx) -> Response:
    """The latest version as a file to share; nothing of this workspace's secrets or connections is in it."""
    template = _get(ctx, template_id)
    document = AgentExport(agent=_portable(template.agent), meta=template.meta)
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in template.agent.name).strip("-") or "agent"
    return Response(
        content=document.model_dump_json(indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}.nodestro-agent.json"'},
    )


class ImportResult(BaseModel):
    template: AgentTemplate
    #: What the imported template still needs here (tools, service connections, model).
    check: TemplateCheck
    #: What the import left out of the file: ``tool_policies``, ``connections``, ``knowledge``.
    removed: list[str] = Field(default_factory=list)


@router.post("/agents/templates/import", status_code=201, dependencies=EDIT)
def import_template(body: dict[str, Any], request: Request, ctx: Ctx) -> ImportResult:
    """Store an exported template as a new template of this workspace (version 1, origin ``import:<sha256>``)."""
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_IMPORT_BYTES:
        raise ApiError(413, "template.import_too_large", "An agent template file may have at most 1 MB", limit=1)
    if body.get("format") != EXPORT_FORMAT:
        raise ApiError(
            422,
            "template.import_invalid",
            f"This file is not an exported agent template ({EXPORT_FORMAT})",
            format=str(body.get("format", ""))[:40],
        )
    try:
        document = AgentExport.model_validate(body)
    except ValidationError as exc:
        raise ApiError(
            422, "template.import_invalid", "This agent template file is not valid", format=EXPORT_FORMAT
        ) from exc
    agent = document.agent
    removed = []
    if agent.tool_policies:
        removed.append("tool_policies")
    if any(settings.connection_id for settings in agent.tool_settings.values()):
        removed.append("connections")
    if agent.knowledge.file_ids:
        removed.append("knowledge")
    agent = _portable(agent)
    meta = document.meta.model_copy(update={"origin": f"import:{hashlib.sha256(raw).hexdigest()}"})
    template = _templates(ctx).create(agent, meta, note="import", created_by=_actor(request))
    return ImportResult(template=template, check=_check(ctx, template.agent, template.meta), removed=removed)


# ---- the shipped agent library --------------------------------------------------------------------------------


class LibraryAgentSummary(BaseModel):
    id: str
    number: int
    category: str
    name: str
    description: str
    icon: str
    version: str
    meta: TemplateMeta


class LibraryAgentDetail(LibraryAgentSummary):
    help: str
    #: The agent a copy would get (with the cloud tiers of this workspace; a preview, never stored).
    agent: AgentConfig


def _summary(agent_id: str, language: str, agent: AgentConfig, meta: TemplateMeta) -> dict[str, Any]:
    spec = agent_library.agent_structure(agent_id)
    return {
        "id": agent_id,
        "number": spec.number,
        "category": spec.category,
        "name": agent.name,
        "description": agent.description,
        "icon": agent.icon,
        "version": agent_library.agent_version(agent_id),
        "meta": meta,
    }


def _models(ctx: Ctx, agent_id: str, model: str | None) -> Any:
    return library_models.agent_models(ctx, agent_id, model)


@router.get("/library/agents", dependencies=READ)
def list_library_agents(request: Request, ctx: Ctx, language: Language | None = None) -> list[LibraryAgentSummary]:
    """The shipped agent templates in one language (default: the interface language); nothing is stored."""
    lang = language or request_language(request)
    found = []
    for agent_id in agent_library.agent_ids():
        agent, meta = agent_library.build_agent(agent_id, lang)
        found.append(LibraryAgentSummary(**_summary(agent_id, lang, agent, meta)))
    return found


@router.get("/library/agents/{agent_id}", dependencies=READ)
def get_library_agent(
    agent_id: str, request: Request, ctx: Ctx, language: Language | None = None
) -> LibraryAgentDetail:
    """One shipped template with its help, example requests, test cases and the agent a copy would get."""
    lang = language or request_language(request)
    agent, meta = agent_library.build_agent(agent_id, lang, models=_models(ctx, agent_id, None))
    return LibraryAgentDetail(
        **_summary(agent_id, lang, agent, meta),
        help=agent_library.agent_texts(agent_id, lang).help,
        agent=agent,
    )


class LibraryAgentCopy(BaseModel):
    #: The language of the copy; the interface language when empty.
    language: Language | None = None
    #: The model of the copy; empty: the recommendation for the agent's task (local) or its tier's model.
    model: str | None = Field(default=None, max_length=200)
    #: The service connection for each kind the agent's tools need (``meta.requirements.services``); a kind left out
    #: stays open and the check names it.
    services: dict[str, str] = Field(default_factory=dict, max_length=20)


@router.post("/library/agents/{agent_id}/copy", status_code=201, dependencies=EDIT)
def copy_library_agent(agent_id: str, body: LibraryAgentCopy, request: Request, ctx: Ctx) -> AgentTemplate:
    """Store a shipped template as a template of the person's own (version 1, origin ``builtin:...``). The copy is
    independent: a newer package never changes it."""
    language = body.language or request_language(request)
    agent_library.agent_structure(agent_id)  # 404 for an unknown id
    services = _agent_services(ctx, agent_id, body.services)
    model = check_model(body.model) if body.model else None
    agent, meta = agent_library.build_agent(agent_id, language, models=_models(ctx, agent_id, model), services=services)
    return _templates(ctx).create(agent, meta, note="library", created_by=_actor(request))


def _agent_services(ctx: Ctx, agent_id: str, chosen: dict[str, str]) -> dict[str, str]:
    needed = set(agent_library.agent_services(agent_id))
    unknown = sorted(set(chosen) - needed)
    if unknown:
        raise ApiError(
            422,
            "library.service_unknown",
            f"The template {agent_id} needs no service connection of kind {unknown[0]}",
            service=unknown[0],
        )
    return service_connections(ctx, chosen)
