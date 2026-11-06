"""The local model of every agent of a template, chosen before a copy is made (EX-011).

A workspace that uses local models (its default model is an Ollama model) gets, per agent, the model the runtime
recommends for the agent's task profile (:func:`nodestro_examples.agent_plan`) on this computer: its hardware, the
installed models and what they report, the copy's language and the European preference. A copy never downloads
anything, so only installed models are chosen; a better model that needs a download is named as ``suggested``.
Without a suitable installed model the agent keeps the workspace's default model, as before. A model the person
picks for an agent (``LibraryCopy.models``) always wins. Cloud workspaces keep the curated tiers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import nodestro_examples as library
from nodestro_runtime.local_models import SelectionPreferences, recommend
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import provider_of
from pydantic import BaseModel, Field


class AgentModel(BaseModel):
    """The model one agent of a template gets, and why."""

    key: str
    name: str
    part: str | None = None
    #: The name of :attr:`part` in the requested language (API-053; ``None`` for the template's own agents).
    part_name: str | None = None
    kind: str = "agent"
    task: str
    tier: str
    #: The model the copy uses for this agent (``None``: the tier's model of a cloud workspace).
    model: str | None = None
    installed: bool = False
    #: Machine-readable reasons: the recommendation's (``installed``, ``below_ideal``, ``no_gpu``, ...) and
    #: ``installed_alternative`` (the best model needs a download), ``default_model`` (no suitable installed model),
    #: ``manual`` (the person chose it).
    reasons: list[str] = Field(default_factory=list)
    #: A better suited model that is not installed (a download the person may start in the settings).
    suggested: str | None = None
    #: Installed models that also suit the agent, best first (the choices to offer).
    alternatives: list[str] = Field(default_factory=list)
    #: Capabilities the model must have (task profile, tools, images).
    needs: list[str] = Field(default_factory=list)


class LibraryModelPlan(BaseModel):
    #: The workspace uses local models: the agents get models per task; otherwise the tiers of the cloud provider.
    local: bool
    agents: list[AgentModel]
    #: The model of the quality checks (judges) of the copy's agents (``None``: the tier's).
    judge: str | None = None


def _installed(ctx: Any) -> tuple[list[str], dict[str, list[str]]]:
    """Installed local chat models and what they report; nothing when no local server answers (never started)."""
    service = ctx.local_models
    if service.ollama_url() is None:
        return [], {}
    try:
        models = service.installed()
    except NodestroError:
        return [], {}
    chat = [model for model in models if model.kind == "chat"]
    return [model.name for model in chat], {model.name: list(model.capabilities) for model in chat}


def _choose(
    task: str,
    needs: list[str],
    *,
    hardware: Any,
    installed: list[str],
    capabilities: Mapping[str, list[str]],
    preferences: SelectionPreferences,
    fallback: str,
) -> tuple[str, bool, list[str], str | None, list[str]]:
    found = recommend(task, hardware, installed, capabilities=capabilities, preferences=preferences, require=needs)
    present = {f"ollama:{name}" for name in installed}
    options = [model for model in [found.model, *found.alternatives] if model in present]
    suggested = found.model if found.model is not None and found.model not in present else None
    if found.model in present:
        return found.model, True, list(found.reasons), suggested, options[1:]
    if options:
        reasons = ["installed_alternative", *(reason for reason in found.reasons if reason != "needs_download")]
        return options[0], True, reasons, suggested, options[1:]
    return fallback, fallback in present, ["default_model"], suggested, []


def plan(ctx: Any, template_id: str, language: str, overrides: Mapping[str, str] | None = None) -> LibraryModelPlan:
    """The model of every agent of the template in this workspace (see the module documentation).

    ``overrides`` (agent key -> model) are the person's choices; an unknown key is a ``KeyError``."""
    agents = library.agent_plan(template_id, language)
    part_names = {part: library.part_texts(part, language).name for part in {a.part for a in agents if a.part}}
    chosen = dict(overrides or {})
    unknown = sorted(set(chosen) - {agent.key for agent in agents})
    if unknown:
        raise KeyError(f"Unknown agents: {', '.join(unknown)}")
    settings = ctx.settings()
    default = settings.default_model
    local = bool(default) and provider_of(default) == "ollama"
    if not local:
        return LibraryModelPlan(
            local=False,
            agents=[
                AgentModel(
                    key=agent.key,
                    name=agent.name,
                    part=agent.part,
                    part_name=part_names.get(agent.part or ""),
                    kind=agent.kind,
                    task=agent.task,
                    tier=agent.tier,
                    model=chosen.get(agent.key),
                    reasons=["manual"] if agent.key in chosen else [],
                    needs=list(agent.needs),
                )
                for agent in agents
            ],
        )
    hardware = ctx.local_models.hardware()
    installed, capabilities = _installed(ctx)
    wishes = ctx.selection_preferences().model_copy(update={"languages": [language]})
    result: list[AgentModel] = []
    for agent in agents:
        model, present, reasons, suggested, alternatives = _choose(
            agent.task,
            list(agent.needs),
            hardware=hardware,
            installed=installed,
            capabilities=capabilities,
            preferences=wishes,
            fallback=default,
        )
        if agent.key in chosen:
            model, reasons = chosen[agent.key], ["manual"]
            present = model in {f"ollama:{name}" for name in installed}
        result.append(
            AgentModel(
                key=agent.key,
                name=agent.name,
                part=agent.part,
                part_name=part_names.get(agent.part or ""),
                kind=agent.kind,
                task=agent.task,
                tier=agent.tier,
                model=model,
                installed=present,
                reasons=reasons,
                suggested=suggested,
                alternatives=alternatives,
                needs=list(agent.needs),
            )
        )
    judge = None
    if any(agent.judge for agent in agents):
        judge = _choose(
            "judge",
            [],
            hardware=hardware,
            installed=installed,
            capabilities=capabilities,
            preferences=wishes,
            fallback=default,
        )[0]
    return LibraryModelPlan(local=True, agents=result, judge=judge)


def choice(ctx: Any, template_id: str, language: str, overrides: Mapping[str, str] | None = None) -> Any:
    """The :class:`nodestro_examples.ModelChoice` of a copy: the tiers, plus per-agent models (local workspaces) and
    the person's choices."""
    settings = ctx.settings()
    found = plan(ctx, template_id, language, overrides)
    agents = {agent.key: agent.model for agent in found.agents if agent.model}
    return library.model_choice(settings.default_model, settings.assistant_model, agents=agents, judge=found.judge)
