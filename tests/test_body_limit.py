"""Request bodies are limited while they are read (API2-03, API-001)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from nodestro_api.app import create_app
from nodestro_api.body_limit import MIB, BodyLimits
from nodestro_api.context import AppConfig

WORKFLOW = b'{"workflow": {"name": "x", "nodes": [], "edges": []}, "junk": "'


def _body(size: int) -> bytes:
    return WORKFLOW + b"a" * (size - len(WORKFLOW) - 2) + b'"}'


def test_a_large_json_body_is_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    # Below the limit: answered as before.
    assert (
        client.post(
            "/api/workflows/check", content=_body(MIB), headers={"content-type": "application/json"}
        ).status_code
        == 200
    )
    # Declared above the limit: refused before anything is read.
    answer = client.post("/api/workflows/check", content=_body(3 * MIB), headers={"content-type": "application/json"})
    assert answer.status_code == 413
    assert answer.json() == {
        "detail": "The request is too large: at most 2 MB are accepted here",
        "code": "request.too_large",
        "params": {"limit_mb": 2},
    }


def test_a_body_without_length_is_counted_while_it_is_read(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    sent = 0

    def chunks() -> Iterator[bytes]:
        nonlocal sent
        yield WORKFLOW
        for _ in range(64):  # 64 x 64 KiB = 4 MiB, sent chunked (no Content-Length)
            sent += 1
            yield b"a" * (64 * 1024)
        yield b'"}'

    answer = client.post("/api/workflows/check", content=chunks(), headers={"content-type": "application/json"})
    assert answer.status_code == 413 and answer.json()["code"] == "request.too_large"
    # Nothing is created by a refused request.
    big = {"name": "x" * (3 * MIB), "nodes": [], "edges": []}
    assert client.post("/api/workflows", json=big).status_code == 413
    assert client.get("/api/workflows").json() == []


def test_uploads_have_a_larger_limit(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    data = b"knowledge line\n" * (3 * MIB // 15)
    answer = client.post("/api/knowledge", files={"files": ("big.txt", data, "text/plain")})
    assert answer.status_code == 201, answer.text[:200]


def test_the_limits_come_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODESTRO_MAX_REQUEST_BYTES", "1000")
    monkeypatch.setenv("NODESTRO_MAX_UPLOAD_BYTES", "4000")
    app = create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/api/workflows/check", content=_body(1500)).status_code == 413
        refused = client.post("/api/knowledge", files={"files": ("a.txt", b"x" * 5000, "text/plain")})
        assert refused.status_code == 413 and refused.json()["params"] == {"limit_mb": 1}
        assert client.post("/api/knowledge", files={"files": ("a.txt", b"x" * 1500, "text/plain")}).status_code == 201
    monkeypatch.setenv("NODESTRO_MAX_REQUEST_BYTES", "2MB")
    with pytest.raises(ValueError, match="NODESTRO_MAX_REQUEST_BYTES"):
        create_app(AppConfig(data_dir=tmp_path / "data2"), integrations=[], scheduler=False)


def test_limits_per_route() -> None:
    limits = BodyLimits()
    assert limits.for_request("POST", "/api/workflows") == 2 * MIB
    assert limits.for_request("PUT", "/api/workflows/abc") == 2 * MIB
    assert limits.for_request("POST", "/api/knowledge") == 256 * MIB
    assert limits.for_request("POST", "/api/inputs") == 256 * MIB
    assert limits.for_request("POST", "/api/backup/inspect") == 2048 * MIB
    assert limits.for_request("POST", "/api/runs/import") == 51 * MIB
    assert limits.for_request("POST", "/api/workflows/import") == 16 * MIB
    assert limits.for_request("POST", "/api/batches") == 16 * MIB
    assert limits.for_request("GET", "/api/knowledge") == 2 * MIB
    # The published APIs check their own limit (and answer in their own format).
    assert limits.for_request("POST", "/api/published/slug/runs") is None
