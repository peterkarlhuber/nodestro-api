"""API-016 (X-03): one machine-readable list of the problem codes of the API and the runtime."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from nodestro_runtime import error_codes as runtime_codes

from nodestro_api import error_codes
from nodestro_api.app import create_app
from nodestro_api.context import AppConfig


def test_the_shipped_list_is_up_to_date() -> None:
    assert error_codes.CATALOG.read_text(encoding="utf-8") == error_codes.text(), (
        "run: python -m nodestro_api.error_codes --write (and give new codes a text in the studio)"
    )


def test_the_list_merges_the_runtime_and_the_api() -> None:
    found = error_codes.collect().codes
    # Every runtime code is in it, with its parameters.
    for code, params in runtime_codes.catalog().items():
        assert code in found and set(params) <= set(found[code].params), code
    assert found["run.nodes_failed"].source == "runtime"
    # The API's own codes, also those without a namespace and those after an HTTP status.
    assert found["not_found"].source == "api" and "name" in found["not_found"].params
    assert found["run.not_found"].source == "api" and {"id", "kind"} <= set(found["run.not_found"].params)
    assert found["access.login_required"].source == "api"
    assert found["agent.MaxToolRoundsError"].params == ["request_id", "type"]  # request_id: MUP-ONA A2
    assert "release.json" not in found  # a file name, not a code
    assert error_codes.collect().patterns == ["http.<status>"]


def test_the_route_answers_the_list(tmp_path: Path) -> None:
    app = create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get("/api/error-codes")
            assert response.status_code == 200
            body = response.json()
            assert body["codes"]["run.nodes_failed"] == {"params": ["nodes"], "source": "runtime"}
            assert body["patterns"] == ["http.<status>"]
            assert len(body["codes"]) == len(error_codes.current().codes)
    finally:
        app.state.nodestro.close()
