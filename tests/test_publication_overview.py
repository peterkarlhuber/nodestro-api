"""``GET /api/publication-overview``: every publication with its workflow, active release and exports, read only
(the studio's *Publications* page). It answers from the stored publications, releases and export records only."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from test_published_api import form_workflow, publish

from nodestro_api.permissions import Permission, Principal, nobody

from conftest import text
from test_flow import save

OVERVIEW = "/api/publication-overview"


def _export(client: TestClient, publication_id: str) -> dict[str, Any]:
    response = client.post(f"/api/publications/{publication_id}/exports", json={"options": {}})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _rename(client: TestClient, workflow_id: str, name: str) -> None:
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    assert client.put(f"/api/workflows/{workflow_id}", json={**workflow, "name": name}).status_code == 200


def test_an_installation_without_publications_lists_none(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    save(client, [{"id": "start", "type": "start"}, {"id": "out", "type": "output"}], [], name="Unpublished")
    answer = client.get(OVERVIEW)
    assert answer.status_code == 200
    assert answer.json() == []


def test_lists_every_publication_with_workflow_release_and_exports(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Angebot")] * 5})
    first = form_workflow(client)
    publication = publish(client, first, note="v1")["publication"]
    second = publish(client, first, note="v2")["publication"]
    assert second["active_release"] == 2
    # Back to version 1: the overview shows the active release, not the newest one.
    assert client.post(f"/api/publications/{publication['id']}/activate", json={"number": 1}).status_code == 200
    export = _export(client, publication["id"])
    # A second workflow, published and switched off.
    other_id = form_workflow(client)
    _rename(client, other_id, "Another flow")
    other = publish(client, other_id)["publication"]
    assert client.post(f"/api/publications/{other['id']}/enabled", json={"enabled": False}).status_code == 200
    # Renaming the workflow later: the overview names the workflow as it is now.
    _rename(client, first, "Renamed flow")

    items = client.get(OVERVIEW).json()
    assert [item["name"] for item in items] == ["Angebot Generator", "Another flow"]  # by name, as /publications
    found = {item["id"]: item for item in items}
    mine = found[publication["id"]]
    release_one = client.get(f"/api/publications/{publication['id']}/releases/1").json()
    assert mine["workflow_id"] == first
    assert mine["workflow_name"] == "Renamed flow"
    assert (mine["enabled"], mine["active_release"]) == (True, 1)
    assert mine["active_version"] == release_one["workflow_version"]
    assert mine["published_at"] == release_one["created_at"]
    assert mine["exports"] == client.get(f"/api/publications/{publication['id']}/exports").json()
    assert [record["id"] for record in mine["exports"]] == [export["id"]]
    assert mine["exports"][0]["options"]["kind"] == "standard"
    assert found[other["id"]]["enabled"] is False
    assert found[other["id"]]["exports"] == []
    # Every field of the existing list is there with the same value.
    for listed in client.get("/api/publications").json():
        assert {key: found[listed["id"]][key] for key in listed} == listed


def test_a_deleted_publication_and_its_exports_are_not_listed(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Angebot")] * 5})
    publication = publish(client, form_workflow(client))["publication"]
    _export(client, publication["id"])
    assert client.delete(f"/api/publications/{publication['id']}").status_code == 204
    assert client.get(OVERVIEW).json() == []


def test_reading_the_overview_needs_the_read_permission(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Angebot")] * 5})
    publish(client, form_workflow(client))
    app: Any = client.app
    reader = Principal(id="reader", permissions=frozenset({Permission.READ}))
    app.state.principal_resolver = lambda _connection: reader
    assert len(client.get(OVERVIEW).json()) == 1
    # Without "read" (as every other publication and export read): refused.
    runner = Principal(id="runner", permissions=frozenset({Permission.RUN, Permission.EDIT}))
    app.state.principal_resolver = lambda _connection: runner
    refused = client.get(OVERVIEW)
    assert refused.status_code == 403
    assert (refused.json()["code"], refused.json()["params"]) == ("access.forbidden", {"permission": "read"})
    assert client.get("/api/publications").status_code == 403  # the same rule as the existing list
    app.state.principal_resolver = nobody
    assert client.get(OVERVIEW).status_code == 401


def test_running_portable_export_jobs_are_listed(make_app: Callable[..., TestClient]) -> None:
    """API-063: jobs still being built appear next to the finished exports; finished jobs do not."""
    from nodestro_api import portable_build

    client = make_app({"WRITER": [text("Angebot")] * 5})
    publication = publish(client, form_workflow(client))["publication"]
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    running = portable_build.ExportJob(publication_id=publication["id"], release_number=1, step="python")
    done = portable_build.ExportJob(publication_id=publication["id"], release_number=1, state="done")
    other = portable_build.ExportJob(publication_id="pub_other", release_number=1)
    for job in (running, done, other):
        store.put(portable_build.JOBS, job.id, job)
    [item] = client.get(OVERVIEW).json()
    assert [job["id"] for job in item["export_jobs"]] == [running.id]
    assert item["export_jobs"][0]["step"] == "python" and item["exports"] == []
    assert item["active_version"] is not None and item["published_at"] is not None
