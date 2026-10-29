"""The built-in documentation as the Nodestro Assistant's source of knowledge.

The documentation is written once, as Markdown chapters in
``frontend/src/docs/chapters/<language>/<chapter>.md`` (the interface shows the same
files at ``/docs``). This module reads those files; there is no second copy and no
vector database. A chapter starts with ``# Title`` and an introduction, every
``## Heading {#id}`` starts a section, and an optional ``<!-- keywords: ... -->`` line
feeds the search, exactly like ``frontend/src/docs/parse.ts``.

Where the files are: the chapters belong to the interface (``nodestro-studio``), which hands
their directory to the API (``AppConfig.docs_dir``; the studio's wheel ships them as
``nodestro_studio/docs``). ``NODESTRO_DOCS_DIR`` names the directory for an API started on its
own. Without documentation the assistant says so and answers without it.

The search is a small keyword search over titles, keywords and text in the requested
language (English as fallback), with accents folded. Its job is only to find the few
sections the assistant then reads in full, so it ranks partial matches too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import threading
import unicodedata

FALLBACK_LANGUAGE = "en"

_HEADING = re.compile(r"^##\s+(.+?)\s*\{#([a-z0-9-]+)\}\s*$")
_KEYWORDS = re.compile(r"^<!--\s*keywords:\s*(.*?)\s*-->\s*$")
_COMMENT = re.compile(r"^<!--.*-->\s*$")
_WORD = re.compile(r"[\w-]+", re.UNICODE)
# Words that say nothing about a topic; a question in natural language is full of them.
_STOP = {
    word
    for words in (
        "the a an and or of to in on for with is are be how what why where when which who do does can i my me it this "
        "that there from by as at not no use using",
        "der die das ein eine einen und oder ist sind wie was warum wo wann welche welcher wer kann ich mein mich es "
        "dies diese zu im in auf mit von für bei nicht kein den dem des ich",
        "el la los las un una y o de del en con por para es son como que qué cómo donde dónde cuando puedo mi",
        "le la les un une et ou de du des en dans avec pour est sont comment que quoi où quand je mon ma",
        "o a os as um uma e ou de do da dos das em no na com por para é são como que onde quando eu meu",
        "il lo la i gli le un una e o di del della in con per è sono come che cosa dove quando io mio",
    )
    for word in words.split()
}


def fold(text: str) -> str:
    """Lower case without accents, so "Ausführung" matches "ausfuhrung" (as in the frontend search)."""
    decomposed = unicodedata.normalize("NFD", text.lower().replace("ß", "ss"))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def plain(markdown: str) -> str:
    """Markdown reduced to readable text for snippets."""
    text = re.sub(r"<!--[\s\S]*?-->", " ", markdown)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[`*_>#|]", " ", text)
    text = re.sub(r"-{3,}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class DocSection:
    chapter: str
    id: str
    title: str
    body: str
    keywords: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.chapter}#{self.id}"

    @property
    def link(self) -> str:
        return f"/docs/{self.chapter}#{self.id}"


@dataclass
class DocChapter:
    id: str
    title: str
    intro: str
    sections: list[DocSection]


@dataclass
class DocHit:
    section: DocSection
    chapter_title: str
    snippet: str
    score: float


def parse_chapter(chapter_id: str, markdown: str) -> DocChapter:
    """Split a chapter file into its sections (see ``parse.ts``)."""
    title = ""
    intro: list[str] = []
    sections: list[DocSection] = []
    current: DocSection | None = None
    lines: list[str] = []
    fenced = False

    def finish() -> None:
        if current is not None:
            current.body = "\n".join(lines).strip()
            sections.append(current)

    for line in markdown.replace("\r\n", "\n").split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        heading = None if fenced else _HEADING.match(line)
        if not title and not fenced and line.startswith("# "):
            title = line[2:].strip()
        elif heading:
            finish()
            current, lines = DocSection(chapter=chapter_id, id=heading.group(2), title=heading.group(1), body=""), []
        elif current is not None:
            keywords = None if fenced else _KEYWORDS.match(line)
            if keywords and not any(item.strip() for item in lines):
                current.keywords = [word.strip() for word in keywords.group(1).split(",") if word.strip()]
            elif not fenced and _COMMENT.match(line):
                continue
            else:
                lines.append(line)
        else:
            intro.append(line)
    finish()
    return DocChapter(id=chapter_id, title=title, intro="\n".join(intro).strip(), sections=sections)


def default_docs_dir() -> Path | None:
    configured = os.environ.get("NODESTRO_DOCS_DIR")
    candidates = [Path(configured)] if configured else []
    return next((path for path in candidates if (path / FALLBACK_LANGUAGE).is_dir()), None)


class DocsIndex:
    """The documentation chapters per language, read once and kept in memory."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory if directory is not None else default_docs_dir()
        self._chapters: dict[str, list[DocChapter]] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.directory is not None and bool(self.chapters(FALLBACK_LANGUAGE))

    def languages(self) -> list[str]:
        if self.directory is None:
            return []
        return sorted(path.name for path in self.directory.iterdir() if path.is_dir())

    def chapters(self, language: str) -> list[DocChapter]:
        """All chapters in ``language``; a chapter missing there comes in English."""
        with self._lock:
            cached = self._chapters.get(language)
            if cached is not None:
                return cached
            chapters: list[DocChapter] = []
            if self.directory is not None:
                fallback = self.directory / FALLBACK_LANGUAGE
                ids = sorted(path.stem for path in fallback.glob("*.md")) if fallback.is_dir() else []
                for chapter_id in ids:
                    source = self.directory / language / f"{chapter_id}.md"
                    if not source.is_file():
                        source = fallback / f"{chapter_id}.md"
                    chapters.append(parse_chapter(chapter_id, source.read_text(encoding="utf-8")))
            self._chapters[language] = chapters
            return chapters

    def section(self, ref: str, language: str) -> tuple[DocChapter, DocSection | None] | None:
        """``chapter`` or ``chapter#section``; ``None`` when it does not exist."""
        chapter_id, _, section_id = ref.strip().lstrip("/").removeprefix("docs/").partition("#")
        chapter = next((item for item in self.chapters(language) if item.id == chapter_id), None)
        if chapter is None:
            return None
        if not section_id:
            return chapter, None
        section = next((item for item in chapter.sections if item.id == section_id), None)
        return (chapter, section) if section is not None else None

    def exists(self, ref: str) -> bool:
        return self.section(ref, FALLBACK_LANGUAGE) is not None

    def outline(self, language: str) -> str:
        """The table of contents as compact lines: ``chapter#section: Title``."""
        lines = []
        for chapter in self.chapters(language):
            lines.append(f"{chapter.id}: {chapter.title}")
            lines += [f"  {section.ref}: {section.title}" for section in chapter.sections]
        return "\n".join(lines)

    def search(self, query: str, language: str, limit: int = 6) -> list[DocHit]:
        """The best matching sections; titles and keywords weigh more than the text."""
        words = [word for word in _WORD.findall(fold(query)) if len(word) > 1 and word not in _STOP]
        if not words:
            return []
        hits: list[DocHit] = []
        for chapter in self.chapters(language):
            chapter_title = fold(chapter.title)
            for section in chapter.sections:
                title = fold(section.title)
                keywords = fold(" ".join(section.keywords))
                text = plain(section.body)
                body = fold(text)
                score = 0.0
                matched = 0
                for word in words:
                    in_title = word in title
                    in_keywords = word in keywords
                    in_body = body.count(word)
                    if in_title or in_keywords or in_body or word in chapter_title:
                        matched += 1
                    score += (10 if in_title else 0) + (5 if in_keywords else 0) + min(in_body, 5)
                    score += 2 if word in chapter_title else 0
                if not matched:
                    continue
                # Sections that contain every word of the query come first.
                score *= 0.5 + matched / len(words)
                hits.append(
                    DocHit(section=section, chapter_title=chapter.title, snippet=_snippet(text, words), score=score)
                )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


def _snippet(text: str, words: list[str]) -> str:
    folded = fold(text)
    positions = [index for index in (folded.find(word) for word in words) if index >= 0]
    at = min(positions) if positions else 0
    start, end = max(0, at - 60), min(len(text), at + 200)
    return ("… " if start else "") + text[start:end].strip() + (" …" if end < len(text) else "")
