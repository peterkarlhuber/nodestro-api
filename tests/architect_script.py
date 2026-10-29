"""Scripted answers for the Workflow Architect: used by the tests and the browser test server.

Each function returns responses for :class:`conftest.ScriptedClient`, keyed by a phrase of
the system prompt they answer. Nothing here calls the API.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

from conftest import parsed

# Phrases of the system prompts (see nodestro.architect and nodestro.assistants).
ANALYST = "requirements analyst of the Workflow Architect"
PLANNER = "Design the architecture of a new workflow"
REVISER_PLAN = "Revise the given workflow according to the user's"
AGENT = "expert multi-agent systems architect working inside Nodestro"
CODER = "expert Python developer"
AGENT_REVISION = "You revise one agent of a workflow"
REPAIR = "You repair the configuration of one agent"

WORD_TOOL = '''\
def keyword_density(text: str, keyword: str) -> float:
    """Share of words in a text that equal a keyword.

    Args:
        text: The text to analyse.
        keyword: The word to count (case-insensitive).

    Returns:
        The share of matching words between 0 and 1.
    """
    words = text.lower().split()
    if not keyword.strip():
        raise ValueError("keyword must not be empty")
    if not words:
        return 0.0
    return words.count(keyword.lower()) / len(words)
'''

WORD_TESTS = """\
from tool import keyword_density


def test_share():
    assert keyword_density("a b a c", "a") == 0.5


def test_empty_text():
    assert keyword_density("", "a") == 0.0


def test_invalid_keyword():
    try:
        keyword_density("a", " ")
    except ValueError:
        return
    raise AssertionError("no error")
"""


def analysis(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "title": "Blog-Artikel mit SEO",
        "goal": "Aus einem Thema entsteht ein geprüfter Blogartikel.",
        "inputs": ["Thema"],
        "outputs": ["Artikel", "SEO-Hinweise"],
        "constraints": ["Sachlich bleiben"],
        "assumptions": ["Der Artikel ist etwa 800 Wörter lang."],
        "question": None,
    }
    values.update(overrides)
    return parsed(lambda model: model(**values))


def create_plan(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "summary": "Recherche und SEO-Analyse laufen parallel, danach schreibt ein Autor den Artikel.",
        "agents": [
            {
                "key": "research",
                "name": "Recherche",
                "purpose": "Sammelt Fakten zum Thema.",
                "icon": "search",
                "inputs": ["start"],
                "task": "Recherchiere zum Thema: {{start}}",
                "server_tools": ["web_search"],
            },
            {
                "key": "seo",
                "name": "SEO-Analyse",
                "purpose": "Findet Suchbegriffe.",
                "icon": "target",
                "inputs": ["start"],
                "task": "Finde Suchbegriffe für {{start}}",
                "output_fields": [
                    {"name": "keywords", "type": "string_list", "description": "Suchbegriffe"},
                    {"name": "Main Keyword", "type": "string", "description": "Hauptbegriff"},
                ],
            },
            {
                "key": "writer",
                "name": "Autor",
                "purpose": "Schreibt den Artikel.",
                "icon": "pen",
                "inputs": ["research", "seo"],
                "task": "Schreibe den Artikel aus {{research}} und {{seo}}.",
                "new_tools": [{"name": "keyword_density", "requirement": "Anteil eines Suchbegriffs im Text."}],
            },
        ],
        "final": ["writer"],
        "example_input": "Wärmepumpen im Altbau",
        "assumptions": ["Zielgruppe sind Laien."],
    }
    values.update(overrides)
    return parsed(lambda model: model(**values))


def agent_spec(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "name": "Spezialist",
        "description": "Ein spezialisierter Agent.",
        "system_prompt": "Du bist ein sorgfältiger Spezialist.",
        "model": "claude-sonnet-5-5",
        "effort": "medium",
        "tools": [],
        "missing_tools": [],
        "output_format": None,
        "max_tokens": 8000,
        "max_tool_rounds": 4,
    }
    values.update(overrides)
    return parsed(lambda model: model(**values))


def tool_proposal(code: str = WORD_TOOL, tests: str = WORD_TESTS, name: str = "keyword_density") -> Any:
    return parsed(
        lambda model: model(function_name=name, description="Share of a keyword in a text.", code=code, tests=tests)
    )


def create_script(agents: int = 3, *, delay: float = 0.0) -> dict[str, list[Any]]:
    """A complete "create" job: analysis, plan, three agents, one tool."""
    script = {
        ANALYST: [analysis()],
        PLANNER: [create_plan()],
        AGENT: [agent_spec() for _ in range(agents)],
        CODER: [tool_proposal()],
    }
    if delay:
        return {key: [slow(response, delay) for response in values] for key, values in script.items()}
    return script


def revise_plan(agent_a: str, agent_b: str, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "summary": "Ein Kritiker prüft die Analysen vor dem Ergebnis.",
        "goal": "Die Analysen werden kritisch geprüft.",
        "agents": [
            {"key": agent_a, "action": "keep", "name": "A", "purpose": ""},
            {
                "key": agent_b,
                "action": "change",
                "name": "B",
                "purpose": "Analysiert.",
                "changes": "Begründungen angeben.",
            },
            {
                "key": "critic",
                "action": "add",
                "name": "Kritiker",
                "purpose": "Prüft die bisherigen Analysen kritisch.",
                "icon": "shield",
                "inputs": [agent_b],
                "task": "Prüfe kritisch: {{" + agent_b + "}}",
            },
        ],
        "final": ["critic"],
        "assumptions": ["Der Kritiker ersetzt das bisherige Ergebnis."],
    }
    values.update(overrides)
    return parsed(lambda model: model(**values))


def revision(**overrides: Any) -> Any:
    values: dict[str, Any] = {"system_prompt": "Du analysierst und begründest jede Aussage."}
    values.update(overrides)
    return parsed(lambda model: model(**values))


def slow(response: Any, seconds: float) -> Callable[[dict[str, Any]], Any]:
    """The response after a pause, so a browser can watch the progress."""

    def respond(request: dict[str, Any]) -> Any:
        threading.Event().wait(seconds)
        return response(request) if callable(response) else response

    return respond


def blocking(response: Any, release: threading.Event, entered: threading.Event | None = None) -> Callable[..., Any]:
    """The response once ``release`` is set (for cancelling a job in flight)."""

    def respond(request: dict[str, Any]) -> Any:
        if entered is not None:
            entered.set()
        release.wait(10)
        return response(request) if callable(response) else response

    return respond
