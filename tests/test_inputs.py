"""Files as workflow inputs: formats, ZIP safety, delivery to agents, storage, knowledge adoption,
DOCX/XLSX results and the published API. Every agent answers from a script; no test calls the API.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from typing import Any
import zipfile

from fastapi.testclient import TestClient
from nodestro_runtime import file_formats, office
from nodestro_runtime.file_delivery import RunFile, plan
from nodestro_runtime.file_formats import FileFormatError, detect, extract, extract_member, inspect_archive, safe_path
from nodestro_runtime.forms import NOT_PROVIDED, check_values, prompt_values
from nodestro_runtime.input_files import InputFiles
from nodestro_runtime.schemas import InputField
from nodestro_runtime.storage import Store
from orkexa import ModelCapabilities
import pytest
from test_published_api import PUBLISHED, auth, finish, new_key, publish

from conftest import text, tool_use
from test_files import blank_pdf, make_docx, make_xlsx
from test_flow import edge, run, save, wait
from test_runs import agent_node

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x03\x08\x02\x00\x00\x00" + b"\x00" * 40


def make_zip(entries: dict[str, bytes], *, links: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            info = zipfile.ZipInfo(path)
            if path in links:
                info.external_attr = 0o120777 << 16  # a symbolic link
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def upload(client: TestClient, name: str, data: bytes, **form: Any) -> dict[str, Any]:
    response = client.post("/api/inputs", files=[("files", (name, data))], data=form)
    assert response.status_code == 201, response.text
    [result] = response.json()
    return result  # type: ignore[no-any-return]


def file_form(**field: Any) -> dict[str, Any]:
    return {
        "id": "start",
        "type": "start",
        "start": {
            "fields": [
                {"name": "kunde", "label": "Kunde", "type": "text", "required": True},
                {"name": "dokumente", "label": "Dokumente", "type": "file", "required": False, **field},
            ]
        },
    }


# ---- formats and detection ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data", "key"),
    [
        ("notiz.txt", "Grüße aus Wien".encode(), "txt"),
        ("readme.md", b"# Titel\n\nText", "md"),
        ("daten.csv", b"name;betrag\nMiete;1200\n", "csv"),
        ("daten.json", b'[{"a": 1}]', "json"),
        ("tabelle.xlsx", make_xlsx(), "xlsx"),
        ("vertrag.docx", make_docx(), "docx"),
        ("scan.pdf", blank_pdf(), "pdf"),
        ("bild.png", PNG, "png"),
        ("paket.zip", make_zip({"a.txt": b"x"}), "zip"),
    ],
)
def test_every_format_is_detected_from_its_content(name: str, data: bytes, key: str) -> None:
    assert detect(name, data).key == key


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("tool.exe", b"MZ....", "input.unsupported_format"),
        ("leer.txt", b"", "input.empty"),
        ("fake.pdf", b"not a pdf at all", "input.content_mismatch"),
        ("fake.png", b"GIF89a....", "input.content_mismatch"),
        ("kaputt.docx", b"PK\x03\x04broken", "input.damaged"),
        ("binaer.txt", b"\x00\x01\x02\xff\xfe" * 20, "input.not_text"),
        ("falsch.json", b"{nope", "input.json_invalid"),
    ],
)
def test_wrong_or_damaged_files_are_refused_with_a_reason(name: str, data: bytes, code: str) -> None:
    with pytest.raises(FileFormatError) as caught:
        detect(name, data)
    assert caught.value.code == code


def test_text_tables_and_documents_keep_their_structure() -> None:
    csv = extract("daten.csv", b"name;betrag\nMiete;1200\nStrom;80\n")
    assert csv.tables[0].columns == ["name", "betrag"] and csv.tables[0].total_rows == 2
    assert csv.info["rows"] == 2
    workbook = extract("tabelle.xlsx", make_xlsx())
    assert workbook.info["sheets"] == 1 and workbook.tables[0].name == "Kosten"
    assert "input.formulas_not_evaluated" in workbook.notes
    document = extract("vertrag.docx", make_docx())
    assert "# Vertrag" in document.text and "| Haftung | hoch |" in document.text
    records = extract("daten.json", b'[{"name": "A", "wert": 1}, {"name": "B", "wert": 2}]')
    assert records.tables and records.tables[0].columns == ["name", "wert"]


def test_windows_text_is_read_with_a_note() -> None:
    result = extract("alt.txt", "Größe".encode("cp1252"))
    assert "Größe" in result.text and "input.decoded_cp1252" in result.notes


def test_images_report_their_size() -> None:
    image = extract("bild.png", PNG)
    assert image.group == "image" and image.info["width"] == 2 and image.info["height"] == 3


# ---- ZIP archives -----------------------------------------------------------------------------


def test_archives_list_their_tree_and_read_single_files() -> None:
    data = make_zip({"docs/a.md": b"# A", "docs/sub/b.csv": b"x;y\n1;2\n", "tool.exe": b"MZ"})
    entries = {entry.path: entry for entry in inspect_archive(data)}
    assert entries["docs/a.md"].status == "ok" and entries["docs/sub/b.csv"].format == "csv"
    assert entries["tool.exe"].status == "unsupported" and entries["tool.exe"].reason == "executable"
    extracted = extract("paket.zip", data)
    assert "docs" in extracted.outline and "b.csv" in extracted.outline
    _entry, member, raw = extract_member(data, "docs/sub/b.csv", "paket.zip")
    assert raw == b"x;y\n1;2\n" and member.tables[0].columns == ["x", "y"]


@pytest.mark.parametrize("path", ["../evil.txt", "/etc/passwd", "a/../../b.txt", "C:/win.txt"])
def test_unsafe_paths_are_skipped(path: str) -> None:
    assert safe_path(path) is None
    entries = inspect_archive(make_zip({path: b"x", "ok.txt": b"fine"}))
    skipped = [entry for entry in entries if entry.status == "skipped"]
    assert skipped and skipped[0].reason == "unsafe_path"
    with pytest.raises(FileFormatError):
        file_formats.read_member(make_zip({path: b"x"}), path)


def test_links_are_never_followed() -> None:
    entries = inspect_archive(make_zip({"link": b"/etc/passwd", "a.txt": b"a"}, links=("link",)))
    assert next(entry for entry in entries if entry.path == "link").reason == "link"


def test_zip_bombs_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    bomb = make_zip({"zeros.txt": b"0" * 2_000_000})
    entry = inspect_archive(bomb, "bombe.zip")[0]
    assert entry.reason == "ratio"
    with pytest.raises(FileFormatError) as caught:
        extract_member(bomb, "zeros.txt", "bombe.zip")
    assert caught.value.code.startswith("input.archive")
    monkeypatch.setattr(file_formats, "MAX_ARCHIVE_ENTRIES", 3)
    many = make_zip({f"f{index}.txt": b"x" for index in range(5)})
    with pytest.raises(FileFormatError) as caught:
        inspect_archive(many, "viele.zip")
    assert caught.value.code == "input.archive_too_many_entries"


def test_nested_archives_are_listed_not_opened() -> None:
    data = make_zip({"inner.zip": make_zip({"x.txt": b"x"}), "a.txt": b"a"})
    extracted = extract("outer.zip", data)
    assert "input.archive_nested" in extracted.notes
    with pytest.raises(FileFormatError) as caught:
        extract_member(data, "inner.zip")
    assert caught.value.code == "input.archive_nested"


def test_unreadable_archives_are_reported() -> None:
    with pytest.raises(FileFormatError) as caught:
        extract("kaputt.zip", b"PK\x03\x04 this is not a zip")
    assert caught.value.code in ("input.archive_unreadable", "input.damaged", "input.content_mismatch")


# ---- optional fields and placeholders -------------------------------------------------------


def test_missing_optional_values_stay_empty_and_are_marked_for_agents() -> None:
    fields = [
        InputField(name="kunde", type="text", required=True),
        InputField(name="betrag", type="number"),
        InputField(name="anhang", type="file"),
    ]
    values, errors = check_values(fields, {"kunde": "ACME"})
    assert not errors and values["betrag"] is None and values["anhang"] is None
    shown = prompt_values(fields, values, {})
    assert shown["betrag"] == NOT_PROVIDED and shown["anhang"] == NOT_PROVIDED
    assert prompt_values(fields, values, {}, mark_empty=False)["betrag"] is None


def test_file_field_limits_are_checked_before_the_start(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    workflow_id = save(
        client,
        [file_form(accept=["pdf"], multiple=False), agent_node("a", "A"), {"id": "out", "type": "output"}],
        [edge("start", "a"), edge("a", "out")],
    )
    first = upload(client, "a.pdf", blank_pdf())["file"]["id"]
    second = upload(client, "b.pdf", blank_pdf())["file"]["id"]
    text_file = upload(client, "c.txt", b"hello")["file"]["id"]
    too_many = client.post(
        f"/api/workflows/{workflow_id}/runs", json={"values": {"kunde": "X", "dokumente": [first, second]}}
    )
    assert too_many.status_code == 400 and too_many.json()["code"] == "form.invalid"
    assert too_many.json()["params"]["errors"][0]["code"] == "form.too_many_files"
    wrong = client.post(f"/api/workflows/{workflow_id}/runs", json={"values": {"kunde": "X", "dokumente": [text_file]}})
    assert wrong.json()["params"]["errors"][0]["code"] == "form.file_type"
    missing = client.post(
        f"/api/workflows/{workflow_id}/runs", json={"values": {"kunde": "X", "dokumente": ["in_000000000000"]}}
    )
    assert missing.json()["params"]["errors"][0]["code"] == "form.file_missing"
    refused = upload(client, "c.txt", b"hello", accept="pdf")
    assert refused["file"] is None and refused["problem"]["code"] == "input.type_not_accepted"
    big = upload(client, "big.txt", b"x" * (2 * 1024 * 1024), max_size_mb="1")
    assert big["problem"]["code"] == "input.too_large"


# ---- runs: delivery, traces, preview --------------------------------------------------------


def test_uploaded_files_reach_the_agent_and_are_traced(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"READER": [text("Gelesen")]})
    workflow_id = save(
        client,
        [
            file_form(),
            agent_node("reader", "READER", task_template="Prüfe {{form.dokumente}} für {{form.kunde}}"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "reader"), edge("reader", "out")],
    )
    notes = upload(client, "notizen.md", b"# Notizen\n\nDie Frist endet am 31. Mai.")["file"]
    sheet = upload(client, "kosten.xlsx", make_xlsx())["file"]
    finished = run(client, workflow_id, values={"kunde": "ACME", "dokumente": [notes["id"], sheet["id"]]})
    assert finished["status"] == "succeeded"
    request = client.fake.messages.calls[0]  # type: ignore[attr-defined]
    sent = json.dumps(request["messages"], ensure_ascii=False)
    assert "Die Frist endet am 31. Mai." in sent and "Kosten" in sent
    assert (
        "notizen.md" in finished["nodes"]["reader"]["prompt"] and notes["id"] in finished["nodes"]["reader"]["prompt"]
    )
    provided = {item["name"]: item for item in finished["nodes"]["reader"]["files"]}
    assert provided["notizen.md"]["mode"] == "text" and provided["kosten.xlsx"]["mode"] == "text"
    inputs = client.get(f"/api/runs/{finished['id']}/inputs").json()
    assert {item["name"] for item in inputs} == {"notizen.md", "kosten.xlsx"}
    assert all(item["field"] == "dokumente" and item["available"] for item in inputs)
    # Input files are not knowledge.
    assert client.get("/api/knowledge").json() == []


def test_archives_reach_agents_as_overview_with_a_reading_tool(make_app: Callable[..., TestClient]) -> None:
    script = {
        "READER": [
            tool_use("input_files", {"command": "list"}, call_id="t1"),
            lambda request: tool_use(
                "input_files",
                {"command": "read", "file_id": _first_input(request), "path": "src/main.py"},
                call_id="t2",
            ),
            text("Fertig"),
        ]
    }
    client = make_app(script)
    workflow_id = save(
        client,
        [file_form(), agent_node("reader", "READER"), {"id": "out", "type": "output"}],
        [edge("start", "reader"), edge("reader", "out")],
    )
    archive = upload(client, "projekt.zip", make_zip({"src/main.py": b"print('hallo')\n", "README.md": b"# Projekt"}))[
        "file"
    ]
    assert {entry["path"] for entry in archive["entries"]} >= {"src/main.py", "README.md"}
    finished = run(client, workflow_id, values={"kunde": "X", "dokumente": [archive["id"]]})
    assert finished["status"] == "succeeded", finished
    [provided] = finished["nodes"]["reader"]["files"]
    assert provided["mode"] == "overview"
    calls = finished["nodes"]["reader"]["tool_calls"]
    assert [call["name"] for call in calls] == ["input_files", "input_files"]
    assert "print('hallo')" in calls[1]["output"]


def _first_input(request: dict[str, Any]) -> str:
    import re

    found = re.search(r"in_[0-9a-f]{12}", json.dumps(request["messages"]))
    assert found
    return found.group(0)


#: What a model supports (Orkexa's capabilities): everything, or nothing known (e.g. an undeclared endpoint).
SEES_ALL = ModelCapabilities.all("supported")
UNKNOWN = ModelCapabilities()


def test_images_are_shown_to_models_that_see_images() -> None:
    image = RunFile(id="in_1", name="bild.png", source="input", format="png", group="image", size=500)
    [seen] = plan([image], SEES_ALL)
    assert seen.mode == "image"
    [unseen] = plan([image], UNKNOWN)
    assert unseen.mode == "unavailable" and "input.model_no_images" in unseen.notes
    scan = RunFile(
        id="in_2",
        name="scan.png",
        source="input",
        format="png",
        group="image",
        size=500,
        chars=40,
        notes=["input.ocr_used"],
    )
    assert plan([scan], UNKNOWN)[0].mode in ("ocr_text", "unavailable")


def test_pdfs_need_document_input() -> None:
    pdf = RunFile(
        id="in_3", name="a.pdf", source="input", format="pdf", group="document", size=500, chars=100, info={"pages": 2}
    )
    assert plan([pdf], SEES_ALL)[0].mode == "document"
    # Without document input the text is given instead of the pages; a scan without OCR is unavailable.
    assert plan([pdf], UNKNOWN)[0].mode == "text"
    scan = replace(pdf, notes=["input.pdf_scanned"])
    [unread] = plan([scan], UNKNOWN)
    assert unread.mode == "unavailable" and "input.model_no_documents" in unread.notes


def test_large_files_and_full_contexts_come_as_overview() -> None:
    big = RunFile(id="in_1", name="gross.txt", source="input", format="txt", group="text", chars=500_000)
    assert plan([big], SEES_ALL)[0].mode == "overview"
    many = [
        RunFile(id=f"in_{i}", name=f"{i}.txt", source="input", format="txt", group="text", chars=55_000)
        for i in range(4)
    ]
    modes = [item.mode for item in plan(many, SEES_ALL)]
    assert modes[:2] == ["text", "text"] and "overview" in modes
    forced = RunFile(
        id="in_9", name="k.txt", source="input", format="txt", group="text", chars=10, delivery="on_demand"
    )
    assert plan([forced], SEES_ALL)[0].mode == "overview"


def test_files_are_kept_for_paused_runs_and_survive_a_restart(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Entwurf")], "AFTER": [text("Weiter")]})
    workflow_id = save(
        client,
        [
            file_form(),
            agent_node("writer", "WRITER"),
            {"id": "check", "type": "approval", "label": "Prüfen", "approval": {"instructions": "ok?"}},
            agent_node("after", "AFTER"),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "writer"), edge("writer", "check"), edge("check", "after", "approved"), edge("after", "out")],
    )
    item = upload(client, "a.txt", b"Inhalt")["file"]
    started = client.post(
        f"/api/workflows/{workflow_id}/runs", json={"values": {"kunde": "X", "dokumente": [item["id"]]}}
    )
    paused = wait(client, started.json()["id"])
    assert paused["status"] == "paused"
    far_future = datetime.now(timezone.utc) + timedelta(days=400)
    inputs: InputFiles = client.app.state.nodestro.inputs  # type: ignore[attr-defined]
    result = inputs.cleanup(older_than_days=1, at=far_future)
    assert result.kept_in_use == 1 and item["id"] in inputs


# ---- storage --------------------------------------------------------------------------------


def test_storage_is_content_addressed_and_cleaned_up(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    inputs = InputFiles(store, tmp_path / "inputs")
    first = inputs.add(b"gleich", name="a.txt")
    second = inputs.add(b"gleich", name="b.txt")
    assert first.blob == second.blob and first.id != second.id
    assert inputs.storage().files == 2 and inputs.storage().unreferenced == 2
    assert inputs.cleanup().removed == 0  # within the grace period
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert inputs.cleanup(at=later).removed == 2
    assert not list((tmp_path / "inputs").rglob("*.bin")) or inputs.storage().files == 0


def test_folder_uploads_keep_relative_paths_safely(tmp_path: Path) -> None:
    inputs = InputFiles(Store(tmp_path / "db.sqlite"), tmp_path / "inputs")
    assert inputs.add(b"x", name="Belege/2024/a.txt").name == "Belege/2024/a.txt"
    assert inputs.add(b"x", name="../../etc/b.txt").name == "etc/b.txt"


def test_deleting_a_run_releases_its_files_and_backups_keep_them(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"A": [text("ok")]})
    workflow_id = save(
        client,
        [file_form(), agent_node("a", "A"), {"id": "out", "type": "output"}],
        [edge("start", "a"), edge("a", "out")],
    )
    item = upload(client, "a.txt", b"Inhalt")["file"]
    finished = run(client, workflow_id, values={"kunde": "X", "dokumente": [item["id"]]})
    backup = client.get("/api/backup")
    assert backup.status_code in (200, 201), backup.text
    archive = zipfile.ZipFile(io.BytesIO(backup.content))
    assert "inputs/catalog.json" in archive.namelist()
    assert client.delete(f"/api/runs/{finished['id']}").status_code == 204
    assert client.get(f"/api/inputs/{item['id']}").status_code == 404


# ---- sub-workflows --------------------------------------------------------------------------


def test_sub_workflows_get_the_same_files_by_reference(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"CHILD": [text("Kind")]})
    child_id = save(
        client,
        [file_form(), agent_node("child", "CHILD"), {"id": "out", "type": "output"}],
        [edge("start", "child"), edge("child", "out")],
        name="Kind",
    )
    parent_id = save(
        client,
        [
            file_form(),
            {
                "id": "sub",
                "type": "subworkflow",
                "label": "Unter",
                "subworkflow": {
                    "workflow_id": child_id,
                    "values": {"kunde": "{{form.kunde}}", "dokumente": "{{form.dokumente}}"},
                },
            },
            {"id": "out", "type": "output"},
        ],
        [edge("start", "sub"), edge("sub", "out")],
        name="Eltern",
    )
    item = upload(client, "a.txt", b"Inhalt")["file"]
    finished = run(client, parent_id, values={"kunde": "X", "dokumente": [item["id"]]})
    assert finished["status"] == "succeeded", finished
    child = client.get(f"/api/runs/{finished['nodes']['sub']['child_run_id']}").json()
    assert child["values"]["dokumente"] == [item["id"]]
    assert child["file_ids"] == [item["id"]]
    empty = run(client, parent_id, values={"kunde": "X"})
    child = client.get(f"/api/runs/{empty['nodes']['sub']['child_run_id']}").json()
    assert child["values"]["dokumente"] is None and child["file_ids"] == []


# ---- knowledge adoption ---------------------------------------------------------------------


def test_adoption_needs_a_check_and_a_confirmation(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    item = upload(client, "vertrag.md", b"# Vertrag\n\nLaufzeit zwei Jahre.")["file"]
    request = {"items": [{"kind": "input", "input_id": item["id"]}], "collection": "Verträge"}
    checked = client.post("/api/knowledge/adopt/check", json=request).json()
    assert checked[0]["indexable"] and checked[0]["token"] and checked[0]["duplicate"] is None
    assert client.get("/api/knowledge").json() == []
    refused = client.post("/api/knowledge/adopt", json={**request, "confirmed": False, "tokens": [checked[0]["token"]]})
    assert refused.status_code >= 400 and refused.json()["code"] == "adopt.not_confirmed"
    stale = client.post(
        "/api/knowledge/adopt",
        json={**request, "collection": "Andere", "confirmed": True, "tokens": [checked[0]["token"]]},
    )
    assert stale.json()["code"] == "adopt.confirmation_stale"
    done = client.post("/api/knowledge/adopt", json={**request, "confirmed": True, "tokens": [checked[0]["token"]]})
    assert done.status_code == 200, done.text
    [added] = done.json()["added"]
    assert added["collection"] == "Verträge" and added["origin"] == "input_file"
    assert added["provenance"]["input_file"] == "vertrag.md"
    assert client.get("/api/knowledge/collections").json() == [{"name": "Verträge", "files": 1}]
    assert client.get(f"/api/inputs/{item['id']}").json()["knowledge_ids"] == [added["id"]]
    again = client.post("/api/knowledge/adopt/check", json=request).json()
    assert again[0]["duplicate"]["id"] == added["id"]
    twice = client.post(
        "/api/knowledge/adopt", json={**request, "confirmed": True, "tokens": [again[0]["token"]]}
    ).json()
    assert twice["added"] == [] and twice["skipped"][0]["reason"] == "duplicate"


def test_single_files_of_an_archive_can_be_adopted(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    archive = upload(client, "paket.zip", make_zip({"docs/a.md": b"# A\n\nInhalt A"}))["file"]
    whole = client.post(
        "/api/knowledge/adopt/check", json={"items": [{"kind": "input", "input_id": archive["id"]}], "collection": "X"}
    ).json()
    assert whole[0]["problem"]["code"] == "adopt.archive_select"
    request = {"items": [{"kind": "input", "input_id": archive["id"], "path": "docs/a.md"}], "collection": "X"}
    checked = client.post("/api/knowledge/adopt/check", json=request).json()
    done = client.post(
        "/api/knowledge/adopt", json={**request, "confirmed": True, "tokens": [checked[0]["token"]]}
    ).json()
    assert done["added"][0]["name"] == "a.md" and done["added"][0]["provenance"]["path"] == "docs/a.md"


def test_agent_proposals_are_stored_only_after_confirmation(make_app: Callable[..., TestClient]) -> None:
    client = make_app(
        {
            "WRITER": [
                tool_use("propose_knowledge", {"title": "Fristen", "content": "Kündigung drei Monate zum Quartal."}),
                text("Zusammenfassung des Vertrags"),
            ]
        }
    )
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node(
                "writer",
                "WRITER",
                knowledge_proposals={"enabled": True, "collection": "Verträge", "content_kind": "extracted"},
            ),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "writer"), edge("writer", "out")],
    )
    finished = run(client, workflow_id)
    proposals = finished["nodes"]["writer"]["proposals"]
    assert {item["title"] for item in proposals} >= {"Fristen"} and len(proposals) == 2
    assert client.get("/api/knowledge").json() == []
    tool_proposal = next(item for item in proposals if item["origin"] == "tool")
    source = {"kind": "proposal", "run_id": finished["id"], "node_id": "writer", "proposal_id": tool_proposal["id"]}
    request = {"items": [source], "collection": "Verträge"}
    checked = client.post("/api/knowledge/adopt/check", json=request).json()
    assert checked[0]["editable"] and checked[0]["text"].startswith("Kündigung")
    edited = {**source, "text": "Kündigung: drei Monate zum Quartalsende.", "title": "Fristen"}
    stale = client.post(
        "/api/knowledge/adopt",
        json={"items": [edited], "collection": "Verträge", "confirmed": True, "tokens": [checked[0]["token"]]},
    )
    assert stale.json()["code"] == "adopt.confirmation_stale"
    rechecked = client.post("/api/knowledge/adopt/check", json={"items": [edited], "collection": "Verträge"}).json()
    done = client.post(
        "/api/knowledge/adopt",
        json={"items": [edited], "collection": "Verträge", "confirmed": True, "tokens": [rechecked[0]["token"]]},
    ).json()
    [added] = done["added"]
    content = client.get(f"/api/knowledge/{added['id']}/content").content.decode()
    assert "Quartalsende" in content and "Source: Nodestro workflow" in content
    after = client.get(f"/api/runs/{finished['id']}").json()
    assert (
        next(p for p in after["nodes"]["writer"]["proposals"] if p["id"] == tool_proposal["id"])["adopted_file_id"]
        == added["id"]
    )


def test_results_and_result_files_can_be_adopted(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("# Bericht\n\nAlles gut.")]})
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER"),
            {"id": "out", "type": "output", "output": {"files": [{"format": "md", "name": "Bericht"}]}},
        ],
        [edge("start", "writer"), edge("writer", "out")],
    )
    finished = run(client, workflow_id)
    [result_file] = client.get(f"/api/runs/{finished['id']}/files").json()
    items = [
        {"kind": "result_file", "run_id": finished["id"], "file_id": result_file["id"]},
        {"kind": "node_output", "run_id": finished["id"], "node_id": "writer", "title": "Ergebnis"},
    ]
    checked = client.post("/api/knowledge/adopt/check", json={"items": items, "collection": "Berichte"}).json()
    assert [item["problem"] for item in checked] == [None, None]
    done = client.post(
        "/api/knowledge/adopt",
        json={
            "items": items,
            "collection": "Berichte",
            "confirmed": True,
            "tokens": [item["token"] for item in checked],
        },
    ).json()
    origins = sorted(item["origin"] for item in done["added"]) + [item["reason"] for item in done["skipped"]]
    assert "result_file" in origins


# ---- DOCX and XLSX results ------------------------------------------------------------------


REPORT = (
    "# Bericht\n\n## Teil\n\nDas ist **wichtig** und *kursiv*.\n\n- eins\n- zwei\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
)


def test_docx_results_have_headings_tables_and_formatting() -> None:
    data = office.to_docx(
        REPORT,
        "Bericht",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    archive = zipfile.ZipFile(io.BytesIO(data))
    document = archive.read("word/document.xml").decode()
    assert "Heading1" in document and "<w:b/>" in document and "<w:i/>" in document and "<w:tbl>" in document
    assert data == office.to_docx(
        REPORT,
        "Bericht",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_xlsx_results_have_typed_columns_and_several_sheets() -> None:
    value = {
        "Rechnungen": [{"nr": "R-1", "betrag": 120.5, "bezahlt": True, "datum": "2026-01-31"}],
        "Kunden": [{"name": "=HACK()", "umsatz": 3}],
    }
    data = office.to_xlsx(value, "Export", datetime(2026, 1, 1, tzinfo=timezone.utc))
    archive = zipfile.ZipFile(io.BytesIO(data))
    workbook = archive.read("xl/workbook.xml").decode()
    assert "Rechnungen" in workbook and "Kunden" in workbook
    sheets = [name for name in archive.namelist() if name.startswith("xl/worksheets/sheet")]
    content = "".join(archive.read(name).decode() for name in sheets)
    assert "<v>120.5</v>" in content and 't="b"' in content
    assert "<f>" not in content  # text that looks like a formula stays text


def test_result_nodes_write_docx_and_xlsx(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text('[{"posten": "Miete", "betrag": 1200}]')]})
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER"),
            {
                "id": "out",
                "type": "output",
                "output": {"files": [{"format": "docx", "name": "Bericht"}, {"format": "xlsx", "name": "Bericht"}]},
            },
        ],
        [edge("start", "writer"), edge("writer", "out")],
    )
    finished = run(client, workflow_id)
    files = client.get(f"/api/runs/{finished['id']}/files").json()
    assert [item["name"] for item in files] == ["Bericht.docx", "Bericht.xlsx"]
    assert all(item["error_code"] is None for item in files)
    sheet = client.get(f"/api/runs/{finished['id']}/files/{files[1]['id']}")
    assert sheet.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
    assert zipfile.ZipFile(io.BytesIO(sheet.content)).testzip() is None


# ---- published API --------------------------------------------------------------------------


def test_published_apis_take_files_as_multipart_and_base64(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"READER": [text("eins"), text("zwei")]})
    workflow_id = save(
        client,
        [file_form(accept=["text", "pdf"]), agent_node("reader", "READER"), {"id": "out", "type": "output"}],
        [edge("start", "reader"), edge("reader", "out")],
        name="Dokumente lesen",
        budget_usd=1.0,
    )
    publication = publish(client, workflow_id, note="v1")["publication"]
    token = new_key(client, publication["id"])
    slug = publication["slug"]
    started = client.post(
        f"{PUBLISHED}/{slug}/runs",
        headers=auth(token),
        data={"values": json.dumps({"kunde": "ACME"})},
        files=[("dokumente", ("brief.txt", b"Sehr geehrte Damen und Herren"))],
    )
    assert started.status_code == 202, started.text
    assert finish(client, slug, token, started.json()["run_id"])["status"] == "succeeded"
    sent = json.dumps(client.fake.messages.calls[0]["messages"], ensure_ascii=False)  # type: ignore[attr-defined]
    assert "Sehr geehrte Damen und Herren" in sent
    as_json = client.post(
        f"{PUBLISHED}/{slug}/runs",
        headers=auth(token),
        json={"values": {"kunde": "ACME", "dokumente": [{"name": "b.txt", "content_base64": "SGFsbG8="}]}},
    )
    assert as_json.status_code == 202, as_json.text
    wrong = client.post(
        f"{PUBLISHED}/{slug}/runs",
        headers=auth(token),
        data={"values": json.dumps({"kunde": "ACME"})},
        files=[("dokumente", ("bild.png", PNG))],
    )
    assert wrong.status_code == 422
    unknown = client.post(
        f"{PUBLISHED}/{slug}/runs", headers=auth(token), data={"values": "{}"}, files=[("nope", ("a.txt", b"a"))]
    )
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "request.unknown_file_part"
    # API uploads never become knowledge.
    assert client.get("/api/knowledge").json() == []
    docs = client.get(f"/api/publications/{publication['id']}/docs").json()
    assert "upload" in docs["examples"]["curl"] and "-F" in docs["examples"]["curl"]["upload"]


def test_exports_never_contain_uploaded_files(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"READER": [text("ok")]})
    workflow_id = save(
        client,
        [file_form(), agent_node("reader", "READER"), {"id": "out", "type": "output"}],
        [edge("start", "reader"), edge("reader", "out")],
        name="Export Test",
        budget_usd=1.0,
    )
    item = upload(client, "geheim.txt", b"streng vertraulich")["file"]
    run(client, workflow_id, values={"kunde": "X", "dokumente": [item["id"]]})
    publication = publish(client, workflow_id, note="v1")["publication"]
    created = client.post(f"/api/publications/{publication['id']}/exports", json={})
    assert created.status_code in (200, 201), created.text
    download = client.get(f"/api/exports/{created.json()['id']}/download")
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert not any("geheim" in name for name in archive.namelist())
    assert all(b"streng vertraulich" not in archive.read(name) for name in archive.namelist())
    runtime = {name.rsplit("/", 1)[-1] for name in archive.namelist()}
    assert {"file_formats.py", "file_delivery.py", "input_files.py", "office.py"} <= runtime
    readme = next(archive.read(name).decode() for name in archive.namelist() if name.endswith("/README.md"))
    assert "multipart/form-data" in readme and "-F" in readme


# ---- architect ------------------------------------------------------------------------------


def test_architect_file_fields_get_safe_settings() -> None:
    from nodestro_api.architect import PlannedField, _form_fields

    [field] = _form_fields(
        [
            PlannedField(
                name="Belege", label="Belege", type="file", accept=["pdf", "image", "exe"], multiple=True, max_files=500
            )
        ]
    )
    assert field.accept == ["pdf", "image"] and field.multiple and field.max_files is None
