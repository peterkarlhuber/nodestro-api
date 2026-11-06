"""A copy of an example remembers where it came from (runtime RT-055): example, version, language and which
integrations run on sample data; read-only, kept by every save."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
import nodestro_examples as library


def _integrated() -> str:
    return next(item for item in library.template_ids() if library.structure(item).integrations)


def test_a_copy_names_its_example(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    template = _integrated()
    copy = client.post(f"/api/library/{template}/copy", json={"language": "fr"})
    assert copy.status_code == 201, copy.text
    origin = copy.json()["origin"]
    integrations = [item.id for item in library.structure(template).integrations]
    assert origin["kind"] == "example" and origin["example_id"] == template
    assert origin["version"] == library.__version__ and origin["language"] == "fr"
    assert origin["offline"] == integrations and origin["connected"] == []
    assert client.get(f"/api/workflows/{copy.json()['id']}").json()["origin"] == origin


def test_an_edit_keeps_the_origin_and_cannot_change_it(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    copy = client.post(f"/api/library/{library.template_ids()[0]}/copy", json={"language": "en"}).json()
    body = {**copy, "name": "Mine now", "origin": {**copy["origin"], "example_id": "forged"}}
    saved = client.put(f"/api/workflows/{copy['id']}", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "Mine now" and saved.json()["origin"] == copy["origin"]


def test_a_new_workflow_has_no_origin(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    created = client.post("/api/workflows", json={"name": "Mine", "origin": {"example_id": "x"}})
    assert created.status_code in (200, 201), created.text
    assert created.json()["origin"] is None
