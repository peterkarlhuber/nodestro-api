"""Favorites and tags for workflows, agent templates and tools.

They are kept apart from the objects themselves (``organize`` collection, key
``<kind>:<id>``), so tools from the registry get them the same way as workflows, and
an exported workflow or template does not carry them along.
"""

from __future__ import annotations

from typing import Literal

from nodestro_runtime.storage import DocumentStore
from pydantic import BaseModel, Field, field_validator

Kind = Literal["workflow", "template", "tool"]
_COLLECTION = "organize"
MAX_TAGS = 20
MAX_TAG_LENGTH = 32


def clean_tags(tags: list[str]) -> list[str]:
    """Trimmed, de-duplicated (ignoring case) tags in the order given."""
    seen: set[str] = set()
    result = []
    for tag in tags:
        tag = " ".join(tag.split())[:MAX_TAG_LENGTH]
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result[:MAX_TAGS]


class Organize(BaseModel):
    favorite: bool = False
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags")
    @classmethod
    def _clean(cls, tags: list[str]) -> list[str]:
        return clean_tags(tags)


class OrganizeState(BaseModel):
    #: ``"<kind>:<id>"`` -> favorite and tags; items without either are left out.
    items: dict[str, Organize]
    #: Every tag in use, sorted (for suggestions and filters).
    tags: list[str]


class Organizer:
    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def get(self, kind: Kind, item_id: str) -> Organize:
        return self.store.get(_COLLECTION, f"{kind}:{item_id}", Organize) or Organize()

    def set(self, kind: Kind, item_id: str, value: Organize) -> Organize:
        key = f"{kind}:{item_id}"
        if not value.favorite and not value.tags:
            self.store.delete(_COLLECTION, key)
        else:
            self.store.put(_COLLECTION, key, value)
        return value

    def forget(self, kind: Kind, item_id: str) -> None:
        """Called when the object is deleted."""
        self.store.delete(_COLLECTION, f"{kind}:{item_id}")

    def state(self) -> OrganizeState:
        items: dict[str, Organize] = dict(self.store.items(_COLLECTION, Organize))
        tags = sorted({tag for item in items.values() for tag in item.tags}, key=str.lower)
        return OrganizeState(items=items, tags=tags)
