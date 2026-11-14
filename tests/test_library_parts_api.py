"""Building blocks as gallery entries of their own (NAL-B3)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
import nodestro_examples as library


def test_the_gallery_lists_every_building_block(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    listed = client.get("/api/library/parts", params={"language": "de"}).json()
    assert [item["id"] for item in listed] == list(library.all_part_ids())
    market = next(item for item in listed if item["id"] == "idea-market")
    assert market["language"] == "de" and market["capabilities"] and market["templates"] == ["business-idea"]
    detail = client.get("/api/library/parts/idea-market", params={"language": "en"}).json()
    assert detail["workflow"]["nodes"] and detail["name"] == detail["workflow"]["name"]
    assert client.get("/api/library/parts/nope").status_code == 404
    # The template routes are unchanged.
    assert client.get("/api/library/business-idea").status_code == 200
    # Nothing was stored by reading.
    assert client.get("/api/workflows").json() == []


def test_a_copy_is_an_ordinary_workflow(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    answer = client.post("/api/library/parts/idea-risks/copy", json={"language": "fr"})
    assert answer.status_code == 201, answer.text
    copy = answer.json()
    assert copy["name"] == library.part_texts("idea-risks", "fr").name
    assert copy["capabilities"] == library.part_texts("idea-risks", "fr").capabilities
    assert [item["id"] for item in client.get("/api/workflows").json()] == [copy["id"]]
    assert client.post("/api/library/parts/nope/copy", json={}).status_code == 404
