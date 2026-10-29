"""Background assistants: design an agent from a description, propose starting memory.

The agent design uses the library's ``AgentArchitect``; Nodestro only maps the
returned ``AgentSpec`` onto an :class:`~nodestro_runtime.schemas.AgentConfig`. Nothing
is applied automatically: the editor shows the proposal and the user decides.
"""

from __future__ import annotations

from nodestro_runtime.catalog import SERVER_TOOLS
from nodestro_runtime.problems import NodestroError, Problem, problem
from nodestro_runtime.providers import MAX_RETRIES, Connection, model_id, provider_of, qualify
from nodestro_runtime.schemas import AgentConfig, MemoryConfig
from nodestro_runtime.tool_library import ToolLibrary
from orkexa import Agent, UsageTracker
from orkexa.dynamic import AgentArchitect, AgentSpec, DynamicPolicy
from orkexa.dynamic.policy import clamp_effort
from pydantic import BaseModel, Field

ARCHITECT_PROMPT = """\
You are an expert multi-agent systems architect working inside Nodestro, a visual
workflow editor where each node is one agent. Design one specialized agent for the
given requirement and return it as an AgentSpec:

- `name`: a short, readable name (letters, digits, '_' or '-', starting with a letter).
- `description`: one or two sentences on the agent's role in a workflow.
- `system_prompt`: a complete, specific system prompt: role, method, quality bar,
  output style.
- Choose the cheapest allowed model and the lowest effort that does the job reliably.
  Write the model exactly as listed (provider:model).
- `tools`: only existing tools the agent really needs, by exact name. The server tools
  web_search, web_fetch and code_execution are available as tools too.
- `missing_tools`: capabilities no existing tool provides that are pure computations;
  they can be generated. Never list tools that need network, files or external services.
  Their `name` is an English snake_case identifier.
- `output_format`: always null.
- Keep `max_tokens` and `max_tool_rounds` within the limits given.
"""


#: English names of the interface languages, for the assistants' prompts.
LANGUAGE_NAMES = {
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "pt-BR": "Brazilian Portuguese",
    "it": "Italian",
}


def language_rule(language: str, *, texts: str) -> str:
    """The language rule every assistant follows; the interface language is the default."""
    name = LANGUAGE_NAMES.get(language, "English")
    return (
        f"\nLanguage: write {texts} in {name}, the language of the user's interface. "
        'Only if the user explicitly asks for another language (for example "the agent should answer '
        'in Spanish" or "write the prompt in English"), use that language instead; the language '
        "the request happens to be written in is not such a request. Technical identifiers always stay "
        "English: code, function and tool names, JSON keys, file paths.\n"
    )


class AgentDraft(BaseModel):
    """What the editor shows after "Describe agent"."""

    config: AgentConfig
    missing_tools: list[dict[str, str]] = Field(default_factory=list)
    notes: list[Problem] = Field(default_factory=list)
    cost_usd: float | None = None


class MemorySeedFile(BaseModel):
    path: str = Field(description="Path below /memories, e.g. /memories/customer/preferences.md")
    content: str = Field(description="Markdown content of the file.")


class MemorySeed(BaseModel):
    """Starting memory for an agent."""

    files: list[MemorySeedFile] = Field(description="Two to five small, well-organised memory files.")
    explanation: str = Field(description="Two sentences on what the agent will remember and why.")


MEMORY_PROMPT = """\
You set up the long-term memory of an AI agent. The agent uses a memory tool:
text files below /memories that it reads at the start of a task and updates as it learns.
Given the agent's role and what the user wants it to remember, propose a small, clear
directory structure with starting files: what to record, in which file, in which format
(headings, bullet lists, dated entries). Include a short README.md that explains the
structure to the agent. Paths are lowercase English words with hyphens, e.g.
/memories/customers/preferences.md. Never invent facts about the user; leave placeholders
where information is still unknown.
"""


def _allowed(model: str, allowed: tuple[str, ...]) -> str | None:
    """The qualified name of ``model`` if it is one of ``allowed`` (bare Claude ids count as Anthropic's)."""
    try:
        wanted = qualify(model)
        return wanted if wanted in {qualify(item) for item in allowed} else None
    except NodestroError:
        return None


def _cost(tracker: UsageTracker) -> float | None:
    cost = tracker.usage.estimated_cost_usd
    return float(cost) if cost is not None else None


class Assistants:
    def __init__(self, tools: ToolLibrary) -> None:
        self.tools = tools

    def design_agent(
        self,
        description: str,
        *,
        connection: Connection,
        model: str,
        allowed_models: list[str],
        existing_agents: list[str],
        default_model: str,
        language: str = "en",
        tracker: UsageTracker | None = None,
    ) -> AgentDraft:
        """``tracker`` collects the usage (the caller records it, also when this fails).

        ``connection`` belongs to the provider of ``model`` (the assistant model); ``allowed_models``
        are the models the designed agent may use (those of configured providers).
        """
        if not description.strip():
            raise NodestroError("assistant.description_missing", "Please describe the agent you need")
        tracker = tracker if tracker is not None else UsageTracker()
        allowed = tuple(dict.fromkeys(allowed_models)) or (qualify(default_model),)
        policy = DynamicPolicy(allowed_models=allowed, max_effort="xhigh", max_tokens=32_000, max_tool_rounds=20)
        prompt = ARCHITECT_PROMPT + language_rule(
            language,
            texts="the agent's `name`, `description` and `system_prompt` and the `requirement` of missing tools",
        )
        architect = AgentArchitect(
            model=qualify(model),
            system_prompt=prompt,
            effort=connection.effort(model, "high"),
            credentials={connection.provider: connection.settings()},
            usage_tracker=tracker,
        )
        catalog = self.tools.catalog() + [
            {"name": key, "description": info.description} for key, info in SERVER_TOOLS.items()
        ]
        spec: AgentSpec = architect.design(
            description,
            tools=catalog,
            output_formats=[],
            existing_agents=existing_agents,
            policy=policy,
        )
        return self._draft(spec, policy, default_model=default_model, cost=_cost(tracker))

    def _draft(self, spec: AgentSpec, policy: DynamicPolicy, *, default_model: str, cost: float | None) -> AgentDraft:
        notes: list[Problem] = []
        tools, server_tools = [], []
        for name in dict.fromkeys(spec.tools):
            if name in SERVER_TOOLS:
                server_tools.append(name)
            elif name in self.tools:
                tools.append(name)
            else:
                notes.append(problem("draft.unknown_tool", f"Unknown tool {name!r} was left out", name=name))
        chosen = _allowed(spec.model, policy.allowed_models)
        model = chosen or default_model
        if chosen is None:
            notes.append(
                problem(
                    "draft.model_unavailable",
                    f"Model {spec.model!r} is not available; chose {model}",
                    model=spec.model,
                    chosen=model,
                )
            )
        if server_tools and provider_of(model) != "anthropic":
            # Server tools run at Anthropic; other providers refuse them (see agent_builder).
            for name in server_tools:
                notes.append(
                    problem(
                        "draft.server_tool_provider",
                        f"Server tool {name!r} was left out: it is only available for Anthropic models",
                        name=name,
                    )
                )
            server_tools = []
        config = AgentConfig(
            name=spec.name.replace("_", " ").replace("-", " ").strip() or "Agent",
            description=spec.description,
            model=model,
            system_prompt=spec.system_prompt,
            effort=clamp_effort(spec.effort, policy.max_effort),
            tools=tools,
            server_tools=server_tools,
            max_tokens=min(max(spec.max_tokens, 1024), policy.max_tokens),
            max_tool_rounds=min(max(spec.max_tool_rounds, 0), policy.max_tool_rounds),
            memory=MemoryConfig(),
        )
        return AgentDraft(
            config=config,
            missing_tools=[{"name": t.name, "requirement": t.requirement} for t in spec.missing_tools],
            notes=notes,
            cost_usd=cost,
        )

    def propose_memory(
        self,
        *,
        role: str,
        wishes: str,
        connection: Connection,
        model: str,
        language: str = "en",
        tracker: UsageTracker | None = None,
    ) -> tuple[MemorySeed, float | None]:
        tracker = tracker if tracker is not None else UsageTracker()
        agent: Agent[MemorySeed] = Agent(
            name="Memory Assistant",
            model=model_id(model),
            system_prompt=MEMORY_PROMPT + language_rule(language, texts="the file contents and the explanation"),
            output_format=MemorySeed,
            effort=connection.effort(model, "medium"),
            max_tokens=8000,
            max_tool_rounds=0,
            options=connection.options(),
            max_retries=MAX_RETRIES,
            usage_tracker=tracker,
            **connection.agent_kwargs(),
        )
        seed = agent.ask(f"Agent role:\n{role or '(not specified)'}\n\nWhat it should remember:\n{wishes}")
        return seed, _cost(tracker)
