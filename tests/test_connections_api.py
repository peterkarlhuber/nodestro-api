"""Service connections and the built-in tool catalog over the API (NAL-A1, nodestro-runtime ADR-0001).

Desktop cases use ``make_app``; role permissions and tenant isolation run on the multi-tenant server and need
PostgreSQL (the fixtures of ``test_identity``). Nothing here contacts a service: checks are offline (ADR-0001).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import logging
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

MakeApp = Callable[..., TestClient]
MARKER = "CONN-SECRET-MARKER-5b1e2c9d"


def _create(client: TestClient, **extra: Any) -> dict[str, Any]:
    body = {"type": "search", "provider": "test", "name": "Test search", **extra}
    answer = client.post("/api/connections", json=body)
    assert answer.status_code == 201, answer.text
    return answer.json()  # type: ignore[no-any-return]


def test_types_status_and_the_tool_catalog(make_app: MakeApp) -> None:
    client = make_app()
    types = {item["name"]: item for item in client.get("/api/connections/types").json()}
    assert {"search", "smtp", "webhook"} <= set(types) and "database" not in types
    # The runtime with the web tools (NAL-A2) adds real search providers; "test" is always there.
    assert "test" in types["search"]["providers"] and types["smtp"]["secret_required"] is True
    status = client.get("/api/connections/status").json()
    assert status["contract"] == 1 and status["mode"] == "desktop"
    catalog = client.get("/api/tools/builtin").json()
    names = [item["name"] for item in catalog]
    assert {"calculator", "current_datetime", "days_between", "text_statistics", "table_totals"} <= set(names)
    calculator = next(item for item in catalog if item["name"] == "calculator")
    assert calculator["origin"] == "nodestro"
    spec = calculator["spec"]
    assert spec["network"] == "none" and spec["offline"] is True and spec["connection_type"] is None
    assert spec["timeout_seconds"] > 0 and spec["capability"]["reach"] == "internal"


def test_connections_are_created_changed_checked_and_deleted(make_app: MakeApp) -> None:
    client = make_app()
    assert client.get("/api/connections").json() == []
    made = _create(client)
    assert made["id"].startswith("conn_") and made["secret_set"] is False and made["check"]["status"] == "unchecked"
    cid = made["id"]
    unchecked = client.post(f"/api/connections/{cid}/check").json()["check"]
    assert unchecked["status"] == "failed" and unchecked["code"] == "connection.secret_missing"
    client.put(f"/api/connections/{cid}/secret", json={"secret": "key"})
    checked = client.post(f"/api/connections/{cid}/check").json()
    assert checked["check"]["status"] == "ok"
    changed = client.patch(f"/api/connections/{cid}", json={"description": "for tests"})
    assert changed.status_code == 200 and changed.json()["description"] == "for tests"
    assert client.get("/api/connections", params={"type": "smtp"}).json() == []
    duplicate = client.post("/api/connections", json={"type": "search", "provider": "test", "name": "TEST SEARCH"})
    assert duplicate.status_code in (400, 409) and duplicate.json()["code"] == "connection.name_taken"
    reserved = client.post("/api/connections", json={"type": "database", "name": "DB"})
    assert reserved.status_code == 400 and reserved.json()["code"].startswith("connection.type")
    events = client.get("/api/connections/events").json()
    assert [item["kind"] for item in events][:3] == ["updated", "checked", "secret_set"]
    assert client.delete(f"/api/connections/{cid}").status_code == 204
    assert client.get(f"/api/connections/{cid}").status_code == 404


@pytest.mark.parametrize(
    "settings",
    [
        {"url": "https://user:pw@hooks.example.com/x"},
        {"url": "https://hooks.example.com/x?token=abc"},
        {"url": "http://hooks.example.com/x"},
        {"url": "https://10.0.0.1/x"},
    ],
)
def test_unsafe_webhook_addresses_are_refused(make_app: MakeApp, settings: dict[str, str]) -> None:
    answer = make_app().post("/api/connections", json={"type": "webhook", "name": "Hook", "settings": settings})
    assert answer.status_code == 400 and answer.json()["code"] == "connection.address_invalid"
    assert "pw" not in answer.text and "abc" not in answer.text


def test_secrets_are_write_only_and_never_in_answers_or_logs(
    make_app: MakeApp, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_app()
    caplog.set_level(logging.DEBUG)
    made = _create(
        client,
        type="smtp",
        provider="generic",
        name="Mail",
        settings={"host": "smtp.example.com", "from_address": "bot@example.com"},
    )
    cid = made["id"]
    stored = client.put(f"/api/connections/{cid}/secret", json={"secret": MARKER})
    assert stored.status_code == 200 and stored.json()["secret_set"] is True
    assert stored.json()["secret_storage"] in ("vault", "session")
    seen = [
        stored.text,
        client.get(f"/api/connections/{cid}").text,
        client.get("/api/connections").text,
        client.get("/api/connections/events").text,
        client.post(f"/api/connections/{cid}/check").text,
        client.get("/openapi.json").text,
        caplog.text,
    ]
    assert all(MARKER not in text for text in seen)
    removed = client.put(f"/api/connections/{cid}/secret", json={"secret": ""})
    assert removed.json()["secret_set"] is False


def test_a_bound_connection_is_published_as_a_reference_only(make_app: MakeApp, tmp_path: Path) -> None:
    """Export rule: a release names the connection's id, type and provider; never settings or the secret."""
    client = make_app()
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    made = _create(client)
    client.put(f"/api/connections/{made['id']}/secret", json={"secret": MARKER})
    from nodestro_runtime.release import ReleaseBundle
    from nodestro_runtime.schemas import AgentConfig, Node, ToolSettings, Workflow
    from nodestro_runtime.tool_spec import builtin_spec

    from nodestro_api.publishing import Publications

    spec = builtin_spec("calculator")
    assert spec is not None
    ctx.tools.specs["calculator"] = spec.model_copy(update={"connection_type": "search"})
    agent = AgentConfig(tools=["calculator"], tool_settings={"calculator": ToolSettings(connection_id=made["id"])})
    found = Publications(ctx)._required_connection(agent, "calculator")
    assert found == {made["id"]: "search:test"}
    bundle = ReleaseBundle(
        workflow_id="wf",
        workflow=Workflow(id="wf", name="W", nodes=[Node(id="a", type="agent", agent=agent)], edges=[]),
        builtin_tools=["calculator"],
        builtin_versions={"calculator": 1},
        required_connections=found,
    )
    assert MARKER not in bundle.model_dump_json()
    assert bundle.tool_marker("calculator") == "builtin:calculator@1"


@pytest.fixture
def portable_root(tmp_path: Path) -> Iterator[Path]:
    from nodestro_runtime.release import ReleaseBundle
    from nodestro_runtime.schemas import Workflow

    root = tmp_path / "package"
    (root / "workflow").mkdir(parents=True)
    bundle = ReleaseBundle(
        workflow_id="wf",
        workflow=Workflow(id="wf", name="W", nodes=[], edges=[]),
        required_connections={"conn_aaaaaaaaaaaa": "smtp:generic"},
    )
    (root / "workflow" / "release.json").write_text(bundle.model_dump_json(), encoding="utf-8")
    yield root


def test_configure_stores_connections_owner_only_and_the_environment_wins(
    portable_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    from nodestro_api import portable, portable_connections

    settings = portable.Settings(portable_root, values={"data": {"data_dir": "data"}})
    answers = iter(["smtp.example.com", "", "", "", "bot@example.com", ""])
    portable.configure_connections(
        settings, portable.Console("en"), ask=lambda _prompt: next(answers), ask_secret=lambda _prompt: MARKER
    )
    output = capsys.readouterr().out
    assert "not configured yet" in output and "saved" in output and MARKER not in output
    stored = json.loads(portable_connections.path_of(settings.data_dir).read_text(encoding="utf-8"))
    entry = stored["conn_aaaaaaaaaaaa"]
    assert entry["type"] == "smtp" and entry["settings"]["host"] == "smtp.example.com" and entry["secret"] == MARKER
    if os.name == "posix":
        assert portable_connections.path_of(settings.data_dir).stat().st_mode & 0o077 == 0
    static = portable_connections.static(settings.data_dir, environ={})
    assert static.missing({"conn_aaaaaaaaaaaa": "smtp:generic"}) == []
    variable = portable_connections.secret_variable("conn_aaaaaaaaaaaa")
    replaced = portable_connections.static(settings.data_dir, environ={variable: "from-env"})
    assert replaced.resolve("conn_aaaaaaaaaaaa").secret == "from-env"
    # A secret set in the environment is not asked for and not written to the file.
    monkeypatch.setenv(variable, "from-env")
    answers = iter([""] * 6)
    portable.configure_connections(
        settings, portable.Console("en"), ask=lambda _prompt: next(answers), ask_secret=lambda _prompt: "typed"
    )
    again = json.loads(portable_connections.path_of(settings.data_dir).read_text(encoding="utf-8"))
    assert again["conn_aaaaaaaaaaaa"]["secret"] == MARKER


def test_the_export_lists_connections_but_never_contains_them(make_app: MakeApp) -> None:
    from nodestro_runtime.release import ReleaseBundle
    from nodestro_runtime.schemas import Workflow

    from nodestro_api import exporter
    from nodestro_api.export_models import network_needs

    bundle = ReleaseBundle(
        workflow_id="wf",
        workflow=Workflow(id="wf", name="W", nodes=[], edges=[]),
        required_connections={"conn_aaaaaaaaaaaa": "search:test"},
    )
    needs = network_needs(bundle, [])
    assert [(item.kind, item.name, item.detail) for item in needs] == [
        ("connection", "conn_aaaaaaaaaaaa", "search:test")
    ]
    strings = exporter.readme_strings("de")
    rows = exporter._connection_rows(bundle.required_connections, strings)
    assert "NODESTRO_CONNECTION_CONN_AAAAAAAAAAAA_SECRET" in rows and "Dienstverbindung" in rows


def test_a_standalone_release_refuses_runs_without_its_connections() -> None:
    import asyncio
    from types import SimpleNamespace

    from nodestro_runtime.connections import StaticConnections

    from nodestro_api.serving import ApiError
    from nodestro_api.standalone import StandaloneHost

    required = {"conn_aaaaaaaaaaaa": "search:test"}
    host = SimpleNamespace(
        connections=StaticConnections({}, environ={}),
        bundle=SimpleNamespace(required_connections=required),
        providers=lambda: [],
    )
    with pytest.raises(ApiError) as refused:
        asyncio.run(StandaloneHost.prepare_start(host, None))  # type: ignore[arg-type]
    assert refused.value.status == 503 and refused.value.code == "connection.missing"
    entry = {"type": "search", "provider": "test", "settings": {}, "secret": "key"}
    host.connections = StaticConnections({"conn_aaaaaaaaaaaa": entry}, environ={})
    asyncio.run(StandaloneHost.prepare_start(host, None))  # type: ignore[arg-type]
