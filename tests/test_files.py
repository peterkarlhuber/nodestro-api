"""Result files (md/json/csv/html/pdf) and document formats (DOCX, XLSX, scanned PDFs)."""

from __future__ import annotations

import io
import json
from typing import Any
import zipfile

from nodestro_runtime import documents, reports
from nodestro_runtime.extensions import Integration
from nodestro_runtime.schemas import Run, Workflow
from pypdf import PdfReader, PdfWriter
import pytest

from conftest import text
from test_flow import edge, run, save
from test_runs import agent_node

# ---- building Office files -----------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def make_docx() -> bytes:
    body = (
        f'<w:document xmlns:w="{_W}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Vertrag</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>Die Haftung ist </w:t></w:r><w:r><w:t>unbegrenzt.</w:t></w:r></w:p>"
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr><w:r><w:t>Kündigung 3 Monate</w:t></w:r></w:p>'
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Klausel</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Risiko</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>Haftung</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>hoch</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", body)
    return buffer.getvalue()


def make_xlsx() -> bytes:
    workbook = (
        f'<workbook xmlns="{_S}" xmlns:r="{_R}"><sheets>'
        '<sheet name="Kosten" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relations = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="x"/></Relationships>'
    )
    words = "".join(f"<si><t>{word}</t></si>" for word in ("Posten", "Betrag", "Miete", "Summe"))
    shared = f'<sst xmlns="{_S}">{words}</sst>'
    sheet = (
        f'<worksheet xmlns="{_S}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1200</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><f>SUM(B2:B2)</f><v>1200</v></c></row>'
        "</sheetData></worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relations)
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ---- documents ------------------------------------------------------------------------


def test_docx_becomes_markdown_with_headings_lists_and_tables() -> None:
    converted = documents.docx_text(make_docx())
    assert converted.source_format == "docx"
    assert "## Vertrag" in converted.text
    assert "Die Haftung ist unbegrenzt." in converted.text
    assert "- Kündigung 3 Monate" in converted.text
    assert "| Klausel | Risiko |" in converted.text and "| Haftung | hoch |" in converted.text
    assert converted.notes["tables"] == "1"


def test_xlsx_shows_values_and_formulas_without_calculating() -> None:
    converted = documents.xlsx_text(make_xlsx())
    assert "## Kosten" in converted.text
    assert "| 2 | Miete | 1200 |" in converted.text
    assert "1200 (formula: =SUM(B2:B2))" in converted.text
    assert "not calculated" in converted.text
    assert converted.notes == {"sheets": "1", "formulas": "1"}
    assert documents.rows_of_xlsx(make_xlsx()) == [
        {"Posten": "Miete", "Betrag": "1200"},
        {"Posten": "Summe", "Betrag": "1200"},
    ]


def test_damaged_office_files_are_reported_clearly() -> None:
    with pytest.raises(documents.DocumentError) as error:
        documents.convert(b"not a zip", "x.docx")
    assert error.value.code == "document.unreadable"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "b">]><x/>')
    with pytest.raises(documents.DocumentError):
        documents.convert(buffer.getvalue(), "bomb.docx")


def test_knowledge_converts_office_files_and_searches_them(make_app: Any) -> None:
    client = make_app()
    response = client.post(
        "/api/knowledge",
        files=[
            ("files", ("vertrag.docx", make_docx(), "application/octet-stream")),
            ("files", ("kosten.xlsx", make_xlsx(), "application/octet-stream")),
        ],
    )
    assert response.status_code == 201, response.text
    docx, xlsx = response.json()
    assert docx["name"] == "vertrag.docx" and docx["source_format"] == "docx" and docx["chunks"] >= 1
    assert xlsx["source_format"] == "xlsx" and xlsx["notes"]["formulas"] == "1"
    hits = client.get("/api/knowledge/search", params={"q": "Haftung"}).json()
    assert any(hit["file_id"] == docx["id"] or hit["source"] == docx["id"] for hit in hits)
    content = client.get(f"/api/knowledge/{xlsx['id']}/content").text
    assert "=SUM(B2:B2)" in content


def test_scanned_pdfs_are_recognised_by_local_ocr_when_enabled(make_app: Any) -> None:
    calls: list[str] = []

    def fake_ocr(data: bytes, name: str) -> documents.OcrResult:
        calls.append(name)
        return documents.OcrResult(
            text="## Page 1\n\nLieferschein [?Nr] 4711", pages=1, uncertain=1, words=3, language="deu"
        )

    client = make_app(ocr_runner=fake_ocr)
    first = client.post("/api/knowledge", files=[("files", ("scan.pdf", blank_pdf(), "application/pdf"))]).json()[0]
    assert first["scanned"] is True and first["ocr"] is None and calls == []
    client.put("/api/settings", json={"ocr_enabled": True})
    assert client.get("/api/settings").json()["ocr_enabled"] is True
    recognised = client.post(f"/api/knowledge/{first['id']}/ocr").json()
    assert recognised["ocr"] == "done" and recognised["ocr_uncertain"] == 1 and recognised["ocr_words"] == 3
    hits = client.get("/api/knowledge/search", params={"q": "Lieferschein"}).json()
    assert hits and "uncertain" in hits[0]["text"]


def test_ocr_unavailable_is_reported(make_app: Any) -> None:
    # Without an OCR integration (the tests install none) nothing can recognise a scan.
    client = make_app()
    entry = client.post("/api/knowledge", files=[("files", ("scan.pdf", blank_pdf(), "application/pdf"))]).json()[0]
    assert client.post(f"/api/knowledge/{entry['id']}/ocr").json()["ocr"] == "unavailable"
    assert client.get("/api/settings").json()["ocr_available"] is False


# ---- result files -----------------------------------------------------------------------


def _run(output: Any) -> Run:
    workflow = Workflow(name="Bericht", nodes=[], edges=[])
    return Run(workflow_id=workflow.id, workflow_name="Bericht", workflow=workflow, output=output, status="succeeded")


def test_markdown_html_and_pdf_from_a_result() -> None:
    result = "## Stärken\n\n- **Service** schnell\n- Preis\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n<script>x</script>"
    run_ = _run(result)
    markdown = reports.render(result, "md", title="SWOT", run=run_).decode()
    assert markdown.startswith("# SWOT") and "| 1 | 2 |" in markdown
    page = reports.render(result, "html", title="SWOT", run=run_).decode()
    assert "<strong>Service</strong>" in page and "<td>1</td>" in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    pdf = reports.render(result, "pdf", title="SWOT", run=run_)
    reader = PdfReader(io.BytesIO(pdf))
    extracted = reader.pages[0].extract_text()
    assert "SWOT" in extracted and "Stärken" in extracted and "Service schnell" in extracted


def test_long_pdfs_get_more_pages() -> None:
    result = "\n\n".join(f"Absatz {number}: " + "Wort " * 60 for number in range(80))
    pdf = reports.render(result, "pdf", title="Lang", run=_run(result))
    assert len(PdfReader(io.BytesIO(pdf)).pages) > 3


def test_csv_from_records_objects_and_markdown_tables() -> None:
    records = [{"name": "A", "score": 3}, {"name": "=HYPERLINK()", "score": -2}]
    data = reports.render(records, "csv", title="x", run=_run(records)).decode("utf-8")
    assert data.startswith("﻿name,score\r\n")
    assert "'=HYPERLINK()" in data and ",-2" in data
    assert reports.to_rows({"items": records})[0] == ["name", "score"]
    assert reports.to_rows({"a": 1, "b": "x"}) == [["field", "value"], ["a", "1"], ["b", "x"]]
    assert reports.to_rows("Text\n\n| K | V |\n|---|---|\n| a | 1 |") == [["K", "V"], ["a", "1"]]
    with pytest.raises(reports.ReportError) as error:
        reports.to_rows("nur Text")
    assert error.value.code == "report.no_table"


def test_file_names_are_safe() -> None:
    assert reports.file_name("../../etc/passwd", "x", "md") == "passwd.md"
    assert reports.file_name("Bericht Q3.pdf", "x", "pdf") == "Bericht Q3.pdf"
    assert reports.file_name("", "SWOT: KMU", "html") == "SWOT_ KMU.html"


def test_result_nodes_write_files_and_they_can_be_fetched_again(make_app: Any) -> None:
    client = make_app({"WRITER": [text('[{"punkt": "Stärke", "text": "Kontakt: anna@example.com"}]')]})
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER"),
            {
                "id": "out",
                "type": "output",
                "output": {
                    "files": [
                        {"format": "md", "name": "SWOT"},
                        {"format": "csv", "name": "SWOT"},
                        {"format": "pdf", "name": ""},
                        {"format": "json", "name": "SWOT", "sanitize": True},
                    ]
                },
            },
        ],
        [edge("start", "writer"), edge("writer", "out")],
        name="SWOT KMU",
    )
    finished = run(client, workflow_id)
    assert finished["status"] == "succeeded"
    files = client.get(f"/api/runs/{finished['id']}/files").json()
    assert [entry["name"] for entry in files] == ["SWOT.md", "SWOT.csv", "SWOT KMU.pdf", "SWOT.json"]
    assert all(entry["source"] == "output" and entry["error_code"] is None for entry in files)
    csv_file = client.get(f"/api/runs/{finished['id']}/files/{files[1]['id']}")
    assert csv_file.headers["content-type"].startswith("text/csv")
    assert "SWOT.csv" in csv_file.headers["content-disposition"]
    assert "anna@example.com" in csv_file.content.decode("utf-8")
    sanitized = json.loads(client.get(f"/api/runs/{finished['id']}/files/{files[3]['id']}").content)
    assert "anna@example.com" not in json.dumps(sanitized) and "[EMAIL_1]" in json.dumps(sanitized)
    assert files[3]["sanitized"] is True

    manual = client.post(f"/api/runs/{finished['id']}/files", json={"format": "html", "name": "SWOT"})
    assert manual.status_code == 201 and manual.json()["source"] == "manual"
    page = client.get(f"/api/runs/{finished['id']}/files/{manual.json()['id']}", params={"inline": True})
    assert page.headers["content-security-policy"].startswith("default-src 'none'")
    assert len(client.get(f"/api/runs/{finished['id']}/files").json()) == 5

    assert client.delete(f"/api/runs/{finished['id']}").status_code == 204
    assert client.get(f"/api/runs/{finished['id']}/files/{files[0]['id']}").status_code == 404


def test_a_file_that_cannot_be_written_is_shown_with_its_reason(make_app: Any) -> None:
    client = make_app({"WRITER": [text("Ein Fließtext ohne Tabelle.")]})
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER"),
            {"id": "out", "type": "output", "output": {"files": [{"format": "csv"}, {"format": "md"}]}},
        ],
        [edge("start", "writer"), edge("writer", "out")],
    )
    finished = run(client, workflow_id)
    assert finished["status"] == "succeeded"
    failed, written = client.get(f"/api/runs/{finished['id']}/files").json()
    assert failed["error_code"] == "report.no_table" and failed["size"] == 0
    assert written["error_code"] is None and written["size"] > 0
    response = client.get(f"/api/runs/{finished['id']}/files/{failed['id']}")
    assert response.status_code == 400 and response.json()["code"] == "report.no_table"
    response = client.post(f"/api/runs/{finished['id']}/files", json={"format": "csv"})
    assert response.status_code == 400 and response.json()["code"] == "report.no_table"


def test_an_integration_provides_the_ocr_engine(make_app: Any) -> None:
    class Engine:
        name = "test-ocr"

        def available(self) -> bool:
            return True

        def recognize(self, data: bytes, name: str) -> documents.OcrResult:
            return documents.OcrResult(text="## Page 1\n\nQuittung 17", pages=1, uncertain=0, words=2, language="deu")

    client = make_app(integrations=[Integration(name="test", ocr=Engine())])
    assert client.get("/api/settings").json()["ocr_available"] is True
    entry = client.post("/api/knowledge", files=[("files", ("scan.pdf", blank_pdf(), "application/pdf"))]).json()[0]
    assert client.post(f"/api/knowledge/{entry['id']}/ocr").json()["ocr"] == "done"
    assert client.get("/api/knowledge/search", params={"q": "Quittung"}).json()
