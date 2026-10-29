"""The global search: workflows, the agents in them, agent templates, tools, knowledge and examples.

Every word of the query must appear (ignoring case and accents) in one of an item's
fields; matches in names count more than matches in descriptions or prompts. Knowledge
files reuse the existing searches: by name and description (``KnowledgeBase.files``)
and by content (the full-text index). Examples from the library are searched in the
interface language and open in the gallery. The documentation is searched in the browser,
because it lives there in the interface language.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
import unicodedata

import nodestro_examples as library
from nodestro_runtime.schemas import AgentTemplate, Workflow
from pydantic import BaseModel

from .organize import Organize

if TYPE_CHECKING:
    from .context import Nodestro

ResultKind = Literal["workflow", "agent", "template", "tool", "knowledge", "example"]


class SearchResult(BaseModel):
    kind: ResultKind
    id: str
    title: str
    subtitle: str = ""
    #: The text around the first match, when it is not in the title.
    snippet: str = ""
    workflow_id: str | None = None
    node_id: str | None = None
    score: float = 0
    favorite: bool = False
    tags: list[str] = []


def fold(text: str) -> str:
    """Lower case without accents: "Größe" -> "grosse"."""
    text = text.lower().replace("ß", "ss")
    return "".join(char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char))


def _snippet(text: str, words: list[str], width: int = 90) -> str:
    folded = fold(text)
    position = min((folded.find(word) for word in words if word in folded), default=-1)
    if position < 0:
        return ""
    start = max(position - width // 3, 0)
    piece = " ".join(text[start : start + width].split())
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


def _score(words: list[str], fields: list[tuple[str, float]]) -> tuple[float, str]:
    """Score of an item whose (text, weight) fields contain every word; 0 when one is missing."""
    folded = [(fold(text), weight, text) for text, weight in fields if text]
    total = 0.0
    for word in words:
        best = max(
            (weight * (2 if value.startswith(word) else 1) for value, weight, _ in folded if word in value), default=0
        )
        if not best:
            return 0, ""
        total += best
    snippet = ""
    for value, weight, text in folded:
        if weight < 3 and any(word in value for word in words):
            snippet = _snippet(text, words)
            break
    return total, snippet


def search(nodestro: Nodestro, query: str, *, language: str = "en", limit: int = 40) -> list[SearchResult]:
    words = [word for word in fold(query).split() if word]
    if not words or max(len(word) for word in words) < 2:
        return []
    organize = nodestro.organize.state().items

    def extra(kind: str, item_id: str) -> tuple[list[tuple[str, float]], Organize]:
        info = organize.get(f"{kind}:{item_id}") or Organize()
        return [(" ".join(info.tags), 2.5)], info

    results: list[SearchResult] = []
    for workflow in nodestro.store.list("workflows", Workflow):
        tag_fields, info = extra("workflow", workflow.id)
        score, snippet = _score(words, [(workflow.name, 4), (workflow.description, 1.5), *tag_fields])
        if score:
            results.append(
                SearchResult(
                    kind="workflow",
                    id=workflow.id,
                    title=workflow.name,
                    subtitle=workflow.description,
                    snippet=snippet,
                    workflow_id=workflow.id,
                    score=score + (1 if info.favorite else 0),
                    favorite=info.favorite,
                    tags=info.tags,
                )
            )
        for node in workflow.nodes:
            if node.type != "agent" or node.agent is None:
                continue
            agent = node.agent
            score, snippet = _score(
                words,
                [
                    (node.label or agent.name, 3.5),
                    (agent.description, 1.5),
                    (agent.model, 1),
                    (" ".join(agent.tools), 1),
                    (agent.system_prompt, 0.6),
                    (agent.task_template, 0.5),
                ],
            )
            if score:
                results.append(
                    SearchResult(
                        kind="agent",
                        id=node.id,
                        title=node.label or agent.name,
                        subtitle=workflow.name,
                        snippet=snippet,
                        workflow_id=workflow.id,
                        node_id=node.id,
                        score=score,
                    )
                )
    for template in nodestro.store.list("templates", AgentTemplate):
        agent = template.agent
        tag_fields, info = extra("template", template.id)
        score, snippet = _score(
            words,
            [
                (agent.name, 4),
                (agent.description, 1.5),
                (agent.model, 1),
                (" ".join(agent.tools), 1),
                (agent.system_prompt, 0.6),
                *tag_fields,
            ],
        )
        if score:
            results.append(
                SearchResult(
                    kind="template",
                    id=template.id,
                    title=agent.name,
                    subtitle=agent.description,
                    snippet=snippet,
                    score=score + (1 if info.favorite else 0),
                    favorite=info.favorite,
                    tags=info.tags,
                )
            )
    for tool in nodestro.tools.all():
        tag_fields, info = extra("tool", tool.name)
        score, snippet = _score(
            words, [(tool.name.replace("_", " "), 4), (tool.description, 1.5), (tool.requirement or "", 1), *tag_fields]
        )
        if score:
            results.append(
                SearchResult(
                    kind="tool",
                    id=tool.name,
                    title=tool.name,
                    subtitle=tool.description,
                    snippet=snippet,
                    score=score + (1 if info.favorite else 0),
                    favorite=info.favorite,
                    tags=info.tags,
                )
            )
    files = {item.id: item for item in nodestro.knowledge.files()}
    found: dict[str, SearchResult] = {}
    for item in files.values():
        score, snippet = _score(words, [(item.name, 4), (item.description, 1.5)])
        if score:
            found[item.id] = SearchResult(
                kind="knowledge", id=item.id, title=item.name, subtitle=item.description, snippet=snippet, score=score
            )
    for hit in nodestro.knowledge.search(query, None, 8):
        file_id = hit.source
        if file_id in files and file_id not in found:
            found[file_id] = SearchResult(
                kind="knowledge",
                id=file_id,
                title=files[file_id].name,
                subtitle=files[file_id].description,
                snippet=" ".join(hit.text.split())[:160],
                score=1 + min(hit.score, 1),
            )
    results.extend(found.values())
    for template_id in library.template_ids():
        score, snippet = _score(words, library.search_fields(template_id, language))
        if score:
            example = library.summary(template_id, language)
            results.append(
                SearchResult(
                    kind="example",
                    id=template_id,
                    title=example.name,
                    subtitle=example.field,
                    snippet=snippet,
                    # Below the user's own items with the same match.
                    score=score * 0.9,
                )
            )
    results.sort(key=lambda item: item.score, reverse=True)
    return results[:limit]
