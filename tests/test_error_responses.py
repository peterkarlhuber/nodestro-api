"""Error answers tell clients no paths, inputs or library internals (MUP-ONA A2, PS-2): an exception without an error
code of its own answers with a fixed text and a ``request_id``, and its original text goes only to the log."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from nodestro_runtime.problems import NodestroError
from orkexa import ProviderError, ToolExecutionError
from orkexa.retrieval import RetrievalError
from pydantic import BaseModel, field_validator
import pytest

from nodestro_api.api import install_error_handlers
from nodestro_api.error_codes import CATALOG as ERROR_CODES
from nodestro_api.error_text import GENERIC_DETAIL, client_text, redact_paths
from nodestro_api.serving import ApiError

SECRET_PATH = "/home/x/secret.txt"
CATALOG = json.loads(ERROR_CODES.read_text(encoding="utf-8"))["codes"]


class Strict(BaseModel):
    value: str

    @field_validator("value")
    @classmethod
    def _refuse(cls, value: str) -> str:
        raise ValueError(f"cannot use {value}")


def _app() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)
    raised: dict[str, BaseException] = {
        "value": ValueError(f"{SECRET_PATH} not found"),
        "permission": PermissionError(f"[Errno 13] Permission denied: '{SECRET_PATH}'"),
        "key": KeyError("OPENAI_API_KEY"),
        "provider": ProviderError("Invalid key sk-ant-abcdefghijklmnopqrstuvwxyz at /srv/app", provider="anthropic"),
        "agent": ToolExecutionError(f"tool crashed in {SECRET_PATH}"),
        "retrieval": RetrievalError("vector_unavailable", "No vectors for this space"),
        "nodestro": NodestroError("local.file_missing", f"{SECRET_PATH} is missing", path="model.gguf"),
        "api": ApiError(409, "output.exists", "C:\\Users\\x\\out.txt exists (api_key=abcdefgh1234)"),
    }

    @app.get("/raise/{kind}")
    def raise_it(kind: str) -> None:
        raise raised[kind]

    @app.get("/items/{item_id}")
    def item(item_id: str) -> None:
        raise KeyError(item_id)

    @app.get("/model")
    def model() -> None:
        # A pydantic ValidationError inside a route is a ValueError: its text carries the input.
        Strict.model_validate({"value": "hunter2-secret-value"})

    @app.post("/body")
    def body(payload: Strict) -> None:  # pragma: no cover - the validator refuses every value
        del payload

    return TestClient(app)


# MUP-ONA T-API-ERR-1
@pytest.mark.parametrize("kind", ["value", "permission"])
def test_a_value_or_permission_error_answers_without_its_text(kind: str) -> None:
    answer = _app().get(f"/raise/{kind}")
    assert answer.status_code == 400
    body = answer.json()
    assert body["code"] == "bad_request" and body["detail"] == GENERIC_DETAIL
    assert body["params"]["request_id"]
    assert "/home" not in answer.text and "secret" not in answer.text


# MUP-ONA T-API-ERR-1
def test_texts_with_an_error_code_keep_their_text_without_paths_and_credentials() -> None:
    client = _app()
    nodestro = client.get("/raise/nodestro")
    assert nodestro.status_code == 400 and nodestro.json()["code"] == "local.file_missing"
    assert nodestro.json()["detail"] == "[path] is missing"
    assert nodestro.json()["params"] == {"path": "model.gguf"}  # documented parameters stay as they are
    api = client.get("/raise/api")
    assert api.status_code == 409 and api.json()["code"] == "output.exists"
    assert "Users" not in api.text and "abcdefgh1234" not in api.text
    retrieval = client.get("/raise/retrieval")
    assert retrieval.status_code == 422 and retrieval.json()["code"] == "agent.RetrievalError"
    assert retrieval.json()["detail"] == "RetrievalError: No vectors for this space"


# MUP-ONA T-API-ERR-1
def test_library_and_provider_errors_answer_generically_with_their_code() -> None:
    client = _app()
    provider = client.get("/raise/provider")
    assert provider.status_code == 502 and provider.json()["code"] == "provider_api"
    assert provider.json()["params"]["provider"] == "anthropic" and provider.json()["params"]["request_id"]
    assert "sk-ant" not in provider.text and "/srv" not in provider.text and "Invalid key" not in provider.text
    agent = client.get("/raise/agent")
    assert agent.status_code == 422 and agent.json()["code"] == "agent.ToolExecutionError"
    assert agent.json()["params"]["type"] == "ToolExecutionError"
    assert "/home" not in agent.text and "crashed" not in agent.text


# MUP-ONA T-API-ERR-2
def test_a_secret_input_is_not_echoed_by_validation_errors() -> None:
    client = _app()
    inside = client.get("/model")  # a ValidationError raised by the route's own code
    assert inside.status_code == 400 and inside.json()["code"] == "bad_request"
    assert "hunter2" not in inside.text and "input_value" not in inside.text
    request = client.post("/body", json={"value": "hunter2-secret-value"})  # the request body itself
    assert request.status_code == 422 and request.json()["code"] == "request.invalid"
    assert "hunter2" not in request.text
    assert request.json()["params"]["errors"] == [
        {"loc": ["body", "value"], "msg": "Value error, cannot use [input]", "type": "value_error"}
    ]


# MUP-ONA T-API-ERR-3
def test_a_key_error_names_only_the_route_id() -> None:
    client = _app()
    foreign = client.get("/raise/key")
    assert foreign.status_code == 404 and foreign.json()["code"] == "not_found"
    assert "OPENAI_API_KEY" not in foreign.text and foreign.json()["params"]["request_id"]
    own = client.get("/items/abc123")
    assert own.status_code == 404
    assert own.json() == {"detail": "Not found: abc123", "code": "not_found", "params": {"name": "abc123"}}


# MUP-ONA T-API-ERR-4
def test_the_log_has_the_original_text_and_the_same_request_id(caplog: pytest.LogCaptureFixture) -> None:
    client = _app()
    with caplog.at_level(logging.WARNING, logger="nodestro_api.errors"):
        answer = client.get("/raise/value")
        provider = client.get("/raise/provider")
    found_id = answer.json()["params"]["request_id"]
    lines = [record.getMessage() for record in caplog.records]
    assert any(SECRET_PATH in line and found_id in line for line in lines)
    assert any(getattr(record, "request_id", None) == found_id for record in caplog.records)
    provider_line = next(line for line in lines if provider.json()["params"]["request_id"] in line)
    assert "Invalid key" in provider_line and "sk-ant-abcdefghij" not in provider_line  # credentials stay redacted


# MUP-ONA T-API-ERR-4
def test_the_server_request_id_is_the_answer_s_request_id() -> None:
    from nodestro_api.observability import RequestIdMiddleware

    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/fail")
    def fail() -> None:
        raise ValueError(SECRET_PATH)

    answer = TestClient(app).get("/fail", headers={"X-Request-ID": "proxy-id-1"})
    assert answer.json()["params"]["request_id"] == "proxy-id-1" == answer.headers["x-request-id"]


# MUP-ONA T-API-ERR-5
@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("value", 400, "bad_request"),
        ("permission", 400, "bad_request"),
        ("key", 404, "not_found"),
        ("provider", 502, "provider_api"),
        ("agent", 422, "agent.ToolExecutionError"),
        ("retrieval", 422, "agent.RetrievalError"),
        ("nodestro", 400, "local.file_missing"),
        ("api", 409, "output.exists"),
    ],
)
def test_status_and_codes_are_unchanged(kind: str, status: int, code: str) -> None:
    answer = _app().get(f"/raise/{kind}")
    assert (answer.status_code, answer.json()["code"]) == (status, code)
    assert set(answer.json()) == {"detail", "code", "params"}
    assert code in CATALOG


def test_path_redaction_keeps_routes_and_urls() -> None:
    assert redact_paths("open /var/lib/nodestro/a.txt failed") == "open [path] failed"
    assert redact_paths("C:\\Users\\me\\x.txt and \\\\server\\share\\y") == "[path] and [path]"
    assert redact_paths("see https://example.com/v1/models and /api") == "see https://example.com/v1/models and /api"
    assert redact_paths("file:///etc/passwd") == "[path]"
    assert redact_paths("3/4 done, ./relative/path stays") == "3/4 done, ./relative/path stays"
    assert "sk-ant" not in client_text("key sk-ant-abcdefghijklmnopqrstuvwxyz")
