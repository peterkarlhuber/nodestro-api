"""Every result file format end to end: written by a result node with a scripted model, downloaded with the right
name, extension, media type and disposition, and opened as a real Word or Excel file."""

from __future__ import annotations

from datetime import datetime
import io
import json
from typing import Any, get_args
from urllib.parse import quote
import zipfile

from fastapi.testclient import TestClient
from nodestro_runtime.reports import REPORT_FORMATS
from nodestro_runtime.schemas import ReportFormat
from pypdf import PdfReader
import pytest

from conftest import text
from test_flow import edge, run, save
from test_runs import agent_node

RESULT = [
    {"position": "Miete", "betrag": 1200.5, "bezahlt": True, "fällig": "2026-01-31"},
    {"position": "Strom", "betrag": 80, "bezahlt": False, "fällig": "2026-02-28"},
]
FORMATS: list[str] = list(get_args(ReportFormat))


def workflow(client: TestClient, files: list[dict[str, Any]], name: str = "Kosten März") -> str:
    return save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER"),
            {"id": "out", "type": "output", "output": {"files": files}},
        ],
        [edge("start", "writer"), edge("writer", "out")],
        name=name,
    )


def test_every_format_is_written_and_downloaded_with_its_name_and_type(make_app: Any) -> None:
    client = make_app({"WRITER": [text(json.dumps(RESULT, ensure_ascii=False))]})
    workflow_id = workflow(client, [{"format": key, "name": "Kosten Ü"} for key in FORMATS])
    finished = run(client, workflow_id)
    assert finished["status"] == "succeeded"
    files = client.get(f"/api/runs/{finished['id']}/files").json()
    assert [entry["format"] for entry in files] == FORMATS
    for entry in files:
        info = REPORT_FORMATS[entry["format"]]
        assert entry["error_code"] is None and entry["size"] > 0, entry
        assert entry["name"] == f"Kosten Ü.{info.extension}"
        assert entry["media_type"] == info.media_type
        response = client.get(f"/api/runs/{finished['id']}/files/{entry['id']}")
        assert response.status_code == 200
        assert response.headers["content-type"].split(";")[0] == info.media_type.split(";")[0]
        assert response.headers["content-disposition"] == f"attachment; filename*=UTF-8''{quote(entry['name'])}"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert len(response.content) == entry["size"]
        # Only Markdown, HTML and PDF may be shown in the page; Word and Excel are always downloaded.
        inline = client.get(f"/api/runs/{finished['id']}/files/{entry['id']}", params={"inline": True})
        assert inline.headers["content-disposition"].startswith("inline" if info.inline else "attachment")
    by_format = {entry["format"]: entry for entry in files}

    docx = pytest.importorskip("docx")
    word = client.get(f"/api/runs/{finished['id']}/files/{by_format['docx']['id']}").content
    assert zipfile.ZipFile(io.BytesIO(word)).testzip() is None
    document = docx.Document(io.BytesIO(word))
    cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    assert "Miete" in cells and "Strom" in cells and "fällig" in cells
    assert document.core_properties.title == "Kosten Ü"

    openpyxl = pytest.importorskip("openpyxl")
    excel = client.get(f"/api/runs/{finished['id']}/files/{by_format['xlsx']['id']}").content
    sheet = openpyxl.load_workbook(io.BytesIO(excel)).worksheets[0]
    rows = [[cell.value for cell in row] for row in sheet.iter_rows()]
    assert rows[0] == ["position", "betrag", "bezahlt", "fällig"]
    assert rows[1][:3] == ["Miete", 1200.5, True] and isinstance(rows[1][3], datetime)

    pdf = client.get(f"/api/runs/{finished['id']}/files/{by_format['pdf']['id']}").content
    assert "Miete" in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


@pytest.mark.parametrize("key", ["docx", "xlsx"])
def test_office_files_can_be_created_later_and_named_after_the_workflow(make_app: Any, key: str) -> None:
    client = make_app({"WRITER": [text(json.dumps(RESULT))]})
    finished = run(client, workflow(client, [], name="Kosten: März Q1"))
    first = client.post(f"/api/runs/{finished['id']}/files", json={"format": key})
    assert first.status_code == 201, first.text
    assert first.json()["name"] == f"Kosten_ März Q1.{key}"
    second = client.post(f"/api/runs/{finished['id']}/files", json={"format": key, "name": f"Bericht.{key}"})
    assert second.json()["name"] == f"Bericht.{key}"
    third = client.post(f"/api/runs/{finished['id']}/files", json={"format": key, "name": "Bericht"})
    assert third.json()["name"] == f"Bericht (2).{key}"
    download = client.get(f"/api/runs/{finished['id']}/files/{third.json()['id']}")
    assert download.headers["content-type"] == REPORT_FORMATS[key].media_type  # type: ignore[index]
    assert zipfile.ZipFile(io.BytesIO(download.content)).testzip() is None


@pytest.mark.parametrize("value", ["doc", "xls", "DOCX", "word", ".xlsx"])
def test_unknown_formats_are_refused_before_anything_is_stored(make_app: Any, value: str) -> None:
    client = make_app({"WRITER": [text(json.dumps(RESULT))]})
    response = client.post(
        "/api/workflows",
        json={
            "name": "Flow",
            "nodes": [{"id": "out", "type": "output", "output": {"files": [{"format": value}]}}],
            "edges": [],
        },
    )
    assert response.status_code == 422
    finished = run(client, workflow(client, []))
    response = client.post(f"/api/runs/{finished['id']}/files", json={"format": value})
    assert response.status_code == 422
    assert client.get(f"/api/runs/{finished['id']}/files").json() == []


def test_stored_word_and_excel_configurations_survive_a_round_trip(make_app: Any) -> None:
    client = make_app({"WRITER": [text(json.dumps(RESULT))]})
    files: list[dict[str, Any]] = [{"format": "docx"}, {"format": "xlsx", "name": "Tabelle", "sanitize": True}]
    workflow_id = workflow(client, files)
    stored = client.get(f"/api/workflows/{workflow_id}").json()
    output = next(node for node in stored["nodes"] if node["id"] == "out")["output"]
    assert output["files"] == [
        {"format": "docx", "name": "", "sanitize": False},
        {"format": "xlsx", "name": "Tabelle", "sanitize": True},
    ]
    saved = client.put(f"/api/workflows/{workflow_id}", json=stored)
    assert saved.status_code == 200, saved.text
    again = client.get(f"/api/workflows/{workflow_id}").json()
    assert next(node for node in again["nodes"] if node["id"] == "out")["output"]["files"] == output["files"]
