"""Request ids and structured logs of the server profile (API-011)."""

from __future__ import annotations

import io
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from nodestro_api import observability
from nodestro_api.observability import HEADER, JsonFormatter, RequestIdFilter, RequestIdMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/work")
    def work() -> dict[str, str]:
        logging.getLogger("nodestro_api.test").warning("working on %s", "it")
        return {"request_id": observability.request_id()}

    return app


@pytest.fixture
def written() -> io.StringIO:
    found = io.StringIO()
    handler = logging.StreamHandler(found)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("nodestro_api.test")
    log.addHandler(handler)
    yield found
    log.removeHandler(handler)


def test_every_answer_and_log_line_carries_the_request_id(written: io.StringIO) -> None:
    client = TestClient(_app(), base_url="http://127.0.0.1")
    answer = client.get("/work")
    given = answer.headers[HEADER]
    assert len(given) == 16 and answer.json() == {"request_id": given}
    line = json.loads(written.getvalue().splitlines()[-1])
    assert line["request_id"] == given and line["message"] == "working on it" and line["level"] == "WARNING"
    assert observability.request_id() == "-"  # outside a request


def test_a_proxys_request_id_is_kept_and_an_unsafe_one_replaced() -> None:
    client = TestClient(_app(), base_url="http://127.0.0.1")
    assert client.get("/work", headers={HEADER: "lb-1234.abc_9"}).headers[HEADER] == "lb-1234.abc_9"
    for unsafe in ("a b", "x" * 65, "evil\\nline", ""):
        replaced = client.get("/work", headers={HEADER: unsafe}).headers[HEADER]
        assert replaced != unsafe and len(replaced) == 16


def test_the_log_format_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_LOG_FORMAT", "JSON")
    assert observability.log_format() == "json"
    monkeypatch.setenv("NODESTRO_LOG_FORMAT", "xml")
    assert observability.log_format() == "text"
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    try:
        handler = observability.configure_logging("json")
        assert root.handlers == [handler] and isinstance(handler.formatter, JsonFormatter)
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, None)
        assert handler.filter(record) and json.loads(handler.format(record))["request_id"] == "-"
    finally:
        for item in list(root.handlers):
            root.removeHandler(item)
        for item in before:
            root.addHandler(item)
        root.setLevel(level)
