"""Regressions found by the platform integration run, checked over HTTP: approval comments must not
reach published results or result files, and unreadable knowledge PDFs are refused cleanly.

No test calls a provider; every agent answers from a script.
"""

from __future__ import annotations

from collections.abc import Callable
import io
import os
from typing import Any

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from test_published_api import PUBLISHED, auth, finish, form_workflow, new_key, publish

from conftest import text

SECRET = "INTERNAL-REVIEW-NOTE-7f3a"


def _pdf(password: str | None = None) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    if password is not None:
        writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_an_approval_comment_stays_out_of_the_published_result(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("Offer for ACME")] * 3})
    workflow_id = form_workflow(client, approval=True)
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    for node in workflow["nodes"]:
        if node["type"] == "output":
            node["output"] = {"files": [{"format": "md"}, {"format": "pdf"}, {"format": "docx"}]}
    saved = client.put(f"/api/workflows/{workflow_id}", json=workflow)
    assert saved.status_code == 200, saved.text
    publication = publish(client, workflow_id)["publication"]
    token = new_key(client, publication["id"])
    slug = publication["slug"]
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token)).json()[
        "run_id"
    ]
    assert finish(client, slug, token, run_id)["status"] == "awaiting_approval"
    decided = client.post(
        f"{PUBLISHED}/{slug}/runs/{run_id}/approve",
        json={"decision": "approved", "comment": SECRET},
        headers=auth(token),
    )
    assert decided.status_code == 200, decided.text
    done = finish(client, slug, token, run_id)
    assert done["status"] == "succeeded"
    result = client.get(f"{PUBLISHED}/{slug}/runs/{run_id}/result", headers=auth(token))
    assert result.json()["output"] == "Offer for ACME"
    assert SECRET not in result.text
    files = client.get(f"{PUBLISHED}/{slug}/runs/{run_id}/files", headers=auth(token)).json()
    assert len(files) == 3
    for item in files:
        content = client.get(f"{PUBLISHED}/{slug}/runs/{run_id}/files/{item['id']}", headers=auth(token))
        assert content.status_code == 200 and SECRET.encode() not in content.content, item["name"]
    # The studio keeps the comment where it belongs: the approval record of the run.
    record = client.get(f"/api/runs/{run_id}").json()
    assert record["nodes"]["check"]["approval"]["comment"] == SECRET
    assert SECRET not in str(record["output"])


def test_unreadable_pdfs_are_refused_with_a_code(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    cases = {
        "truncated.pdf": (_pdf()[:150], "document.unreadable"),
        "empty.pdf": (b"", "document.unreadable"),
        "fake.pdf": (b"just text", "document.unreadable"),
        "locked.pdf": (_pdf("secret"), "document.encrypted"),
    }
    for name, (data, code) in cases.items():
        for _attempt in range(2):
            answer = client.post("/api/knowledge", files=[("files", (name, data, "application/pdf"))])
            assert answer.status_code == 400, (name, answer.text)
            assert answer.json()["code"] == code
            assert answer.json()["params"] == {"name": name}
    assert client.get("/api/knowledge").json() == []


def test_one_refused_file_refuses_the_whole_upload(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    files: list[tuple[str, Any]] = [
        ("files", ("notes.txt", b"kept only together", "text/plain")),
        ("files", ("good.pdf", _pdf(), "application/pdf")),
        ("files", ("broken.pdf", b"%PDF-1.7 broken", "application/pdf")),
    ]
    answer = client.post("/api/knowledge", files=files)
    assert answer.status_code == 400 and answer.json()["code"] == "document.unreadable"
    assert client.get("/api/knowledge").json() == []
    assert client.get("/api/knowledge/search", params={"q": "together"}).json() == []
    retried = client.post("/api/knowledge", files=files[:2])
    assert retried.status_code == 201 and len(retried.json()) == 2


def test_a_second_server_on_the_same_data_directory_is_refused(tmp_path: Any) -> None:
    import subprocess
    import sys

    from nodestro_api.app import create_app
    from nodestro_api.context import AppConfig

    first = create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)
    with TestClient(first, base_url="http://127.0.0.1") as client:
        assert client.get("/api/meta").status_code == 200
        # `nodestro-api` (and `nodestro`) stop with one sentence instead of a second server on the same data.
        refused = subprocess.run(
            [sys.executable, "-c", "from nodestro_api.cli import main; main(['--port', '1'])"],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "NODESTRO_DATA_DIR": str(tmp_path / "data")},
        )
        assert refused.returncode == 1
        assert "used by another Nodestro process" in refused.stderr
