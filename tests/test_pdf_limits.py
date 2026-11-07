"""PDFs over the runtime's PDF limits through the HTTP API (MUP-ONA A1, PS-1).

The runtime reads PDF text in a separate, limited process (orkexa's ``pdf_pages_isolated``; limits from
``NODESTRO_PDF_*``). The API answers a knowledge upload over a limit with ``422 document.too_complex``, reports an
input file over a limit as that file's problem, and keeps answering other requests while a PDF is being read.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import threading
import time
from typing import Any
import zlib

from fastapi.testclient import TestClient
import orkexa.retrieval
import pytest
from test_inputs import file_form
from test_published_api import PUBLISHED, auth, new_key, publish

from conftest import text
from test_flow import edge, save
from test_runs import agent_node

_FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _pdf(streams: Sequence[bytes]) -> bytes:
    """A PDF with one page per content stream, built byte for byte (as orkexa's PDF fixtures are)."""
    kids = b" ".join(b"%d 0 R" % (4 + 2 * index) for index in range(len(streams)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(streams),
    ]
    objects.append(_FONT)
    for index, stream in enumerate(streams):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >>"
            b" /Contents %d 0 R >>" % (5 + 2 * index)
        )
        objects.append(stream)
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _stream(data: bytes) -> bytes:
    packed = zlib.compress(data, 9)
    return b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed) + packed + b"\nendstream"


def text_pdf(pages: int) -> bytes:
    return _pdf([_stream(b"BT /F1 12 Tf 72 720 Td (Refund policy, page %d.) Tj ET" % page) for page in range(pages)])


def slow_pdf(pages: int = 5, operators: int = 150_000) -> bytes:
    """Content streams with many text operators: extracting their text runs for many seconds."""
    line = b"1 0 0 1 72 720 Tm (x) Tj\n"
    return _pdf([_stream(b"BT /F1 12 Tf\n%d 0 Td\n" % page + line * operators + b"ET") for page in range(pages)])


@pytest.fixture(autouse=True)
def _limits(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TIMEOUT_SECONDS", "MAX_PAGES", "MAX_CHARS", "MAX_MEMORY_MB"):
        monkeypatch.delenv(f"NODESTRO_PDF_{name}", raising=False)


def test_a_knowledge_upload_over_the_pdf_limits_answers_422(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # MUP-ONA T-PDF-10
    client = make_app()
    kept = client.post("/api/knowledge", files=[("files", ("policy.pdf", text_pdf(2), "application/pdf"))])
    assert kept.status_code == 201, kept.text
    monkeypatch.setenv("NODESTRO_PDF_MAX_PAGES", "2")
    answer = client.post(
        "/api/knowledge",
        files=[
            ("files", ("notes.md", b"# Notes\n\nkept only with the PDF", "text/markdown")),
            ("files", ("manual.pdf", text_pdf(3), "application/pdf")),
        ],
    )
    assert answer.status_code == 422, answer.text
    body = answer.json()
    assert body["code"] == "document.too_complex"
    assert body["params"] == {"name": "manual.pdf", "reason": "pages"}
    # One upload is one decision: the file before the refused PDF is not kept either.
    assert [entry["name"] for entry in client.get("/api/knowledge").json()] == ["policy.pdf"]


def test_an_input_file_over_the_pdf_limits_is_refused_with_the_same_code(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # MUP-ONA T-PDF-10
    client = make_app()
    monkeypatch.setenv("NODESTRO_PDF_MAX_PAGES", "2")
    answer = client.post("/api/inputs", files=[("files", ("manual.pdf", text_pdf(3), "application/pdf"))])
    assert answer.status_code == 201, answer.text
    [result] = answer.json()
    assert result["file"] is None
    assert result["problem"]["code"] == "document.too_complex"
    assert result["problem"]["params"] == {"name": "manual.pdf", "reason": "pages"}


def _answers_while_reading(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, send: Callable[[], Any]
) -> tuple[Any, list[float]]:
    """Run ``send`` (a request whose PDF takes longer than its 4 s time budget to read) in a thread; while the PDF is
    being read, ask ``/api/health`` three times. Returns ``send``'s answer and the durations of the health checks."""
    monkeypatch.setenv("NODESTRO_PDF_TIMEOUT_SECONDS", "4")
    reading = threading.Event()
    isolated: Callable[..., list[str]] = orkexa.retrieval.pdf_pages_isolated

    def noted(data: bytes, **limits: Any) -> list[str]:
        reading.set()
        return isolated(data, **limits)

    monkeypatch.setattr(orkexa.retrieval, "pdf_pages_isolated", noted)
    request: dict[str, Any] = {}

    def run() -> None:
        started = time.monotonic()
        request["answer"] = send()
        request["ended"] = time.monotonic()
        request["seconds"] = request["ended"] - started

    sender = threading.Thread(target=run)
    sender.start()
    try:
        assert reading.wait(30), "the request never started reading the PDF"
        time.sleep(0.5)  # the extraction is under way in its own process
        durations = []
        for _attempt in range(3):
            started = time.monotonic()
            health = client.get("/api/health")
            durations.append(time.monotonic() - started)
            assert health.status_code == 200 and health.json()["status"] == "ok"
        answered = time.monotonic()
    finally:
        sender.join(60)
    assert answered < request["ended"]  # the health checks were answered while the PDF was still being read
    assert 3.5 < request["seconds"] < 4 + 2 + 5  # the time budget stopped it (plus the grace period, slack)
    return request["answer"], durations


def test_the_api_answers_while_a_knowledge_pdf_is_being_read(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # MUP-ONA T-PDF-10: a PDF that blocks its extraction until the time budget stops it does not block the process
    client = make_app()
    answer, durations = _answers_while_reading(
        client,
        monkeypatch,
        lambda: client.post("/api/knowledge", files=[("files", ("slow.pdf", slow_pdf(), "application/pdf"))]),
    )
    assert max(durations) < 1.0, durations
    assert answer.status_code == 422, answer.text
    assert answer.json()["params"] == {"name": "slow.pdf", "reason": "timeout"}
    assert client.get("/api/knowledge").json() == []


def test_the_api_answers_while_an_input_pdf_is_being_read(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # MUP-ONA T-PDF-10
    client = make_app()
    answer, durations = _answers_while_reading(
        client,
        monkeypatch,
        lambda: client.post("/api/inputs", files=[("files", ("slow.pdf", slow_pdf(), "application/pdf"))]),
    )
    assert max(durations) < 1.0, durations
    assert answer.status_code == 201, answer.text
    assert answer.json()[0]["problem"]["params"] == {"name": "slow.pdf", "reason": "timeout"}


def test_a_published_api_answers_while_a_pdf_of_a_run_is_being_read(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # MUP-ONA T-PDF-10: a run's file is stored (and read) off the event loop
    client = make_app({"READER": [text("read")]})
    workflow_id = save(
        client,
        [file_form(accept=["pdf"]), agent_node("reader", "READER"), {"id": "out", "type": "output"}],
        [edge("start", "reader"), edge("reader", "out")],
        name="Read documents",
        budget_usd=1.0,
    )
    publication = publish(client, workflow_id, note="v1")["publication"]
    token = new_key(client, publication["id"])
    answer, durations = _answers_while_reading(
        client,
        monkeypatch,
        lambda: client.post(
            f"{PUBLISHED}/{publication['slug']}/runs",
            headers=auth(token),
            data={"values": json.dumps({"kunde": "ACME"})},
            files=[("dokumente", ("slow.pdf", slow_pdf(), "application/pdf"))],
        ),
    )
    assert max(durations) < 1.0, durations
    assert answer.status_code == 422, answer.text
    error = answer.json()["error"]
    assert (error["code"], error["params"]) == ("document.too_complex", {"name": "slow.pdf", "reason": "timeout"})
