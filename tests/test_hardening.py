"""Regression tests of the 2026-10-30 quality round: limits over large run histories, request
size limits, Server-Sent Event ids after a reconnect, idempotency, key rotation and revocation,
the network listener's configuration changes, uploaded file headers, backups and shutdown.

No test calls a provider; every agent answers from a script.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import socket
import struct
from typing import Any
import zipfile

from fastapi.testclient import TestClient
import httpx
from nodestro_runtime.schemas import Run, RunEvent, Workflow, new_id, now
import pytest
from test_published_api import PUBLISHED, auth, finish, form_workflow, new_key, publish

from nodestro_api import backup, serving
from nodestro_api.app import create_app
from nodestro_api.architect import ArchitectJob
from nodestro_api.context import AppConfig
from nodestro_api.serving import ServingCore

from conftest import text


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _published(make_app: Callable[..., TestClient], **workflow: Any) -> tuple[TestClient, dict[str, Any], str]:
    client = make_app({"WRITER": [text("Offer for ACME")] * 10})
    publication = publish(client, form_workflow(client, **workflow))["publication"]
    return client, publication, new_key(client, publication["id"])


def _studio_runs(client: TestClient, template_id: str, count: int) -> None:
    """``count`` stored studio runs that are newer than every API run (a busy batch, for example)."""
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    template = store.get_raw("runs", template_id)
    for _ in range(count):
        copy = {**template, "id": new_id("run"), "trigger": "manual", "trigger_ref": None, "release_id": None}
        copy["created_at"] = now().isoformat()
        store.put_raw("runs", copy["id"], copy)


# ---- limits over large run histories ----------------------------------------------------------


def test_limits_count_api_runs_behind_more_than_5000_newer_runs(make_app: Callable[..., TestClient]) -> None:
    client, publication, token = _published(make_app)
    slug, pid = publication["slug"], publication["id"]
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token)).json()[
        "run_id"
    ]
    finish(client, slug, token, run_id)
    _studio_runs(client, run_id, 5100)
    client.patch(f"/api/publications/{pid}", json={"limits": {**publication["limits"], "daily_budget_usd": 0.0001}})
    refused = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token))
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["code"] == "limit.daily_budget"
    assert client.get(f"{PUBLISHED}/{slug}/usage", headers=auth(token)).json()["runs"] == 1
    assert [item["run_id"] for item in client.get(f"{PUBLISHED}/{slug}/runs", headers=auth(token)).json()] == [run_id]
    assert [item["id"] for item in client.get(f"/api/publications/{pid}/runs").json()] == [run_id]
    releases = client.get(f"/api/workflows/{publication['workflow_id']}/api").json()["releases"]
    assert releases[0]["runs"] == 1


def test_waiting_runs_behind_more_than_5000_newer_runs_still_count(make_app: Callable[..., TestClient]) -> None:
    client, publication, token = _published(make_app, approval=True)
    slug, pid = publication["slug"], publication["id"]
    client.patch(f"/api/publications/{pid}", json={"limits": {**publication["limits"], "max_waiting_runs": 1}})
    run_id = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=auth(token)).json()[
        "run_id"
    ]
    assert finish(client, slug, token, run_id)["status"] == "awaiting_approval"
    _studio_runs(client, run_id, 5100)
    refused = client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "B"}}, headers=auth(token))
    assert refused.status_code == 429, refused.text
    assert refused.json()["error"]["code"] == "limit.waiting"
    assert client.get(f"/api/workflows/{publication['workflow_id']}/api").json()["pending_approvals"] == 1
    # The waiting run keeps its publication from being deleted, and switching off cancels it.
    assert client.delete(f"/api/publications/{pid}").json()["code"] == "api.runs_active"
    switched = client.post(f"/api/publications/{pid}/enabled", json={"enabled": False, "cancel_running": True})
    assert switched.json()["cancelled"] == 1


# ---- request size ------------------------------------------------------------------------------


def _chunks(size: int) -> Iterator[bytes]:
    body = json.dumps({"values": {"customer": "x" * size}}).encode()
    for start in range(0, len(body), 512):
        yield body[start : start + 512]


def test_request_size_is_limited_without_content_length(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, publication, token = _published(make_app)
    monkeypatch.setattr(serving, "_MAX_BODY_BYTES", 4096)
    url = f"{PUBLISHED}/{publication['slug']}/runs"
    headers = {**auth(token), "Content-Type": "application/json"}
    # A chunked body has no Content-Length; it is counted while it is read.
    refused = client.post(url, content=_chunks(10_000), headers=headers)
    assert refused.status_code == 413, refused.text
    assert refused.json()["error"]["code"] == "request.too_large"
    # A Content-Length that is too small does not get a larger body in either.
    lying = client.post(url, content=b"{" + b" " * 9000 + b"}", headers={**headers, "Content-Length": "10"})
    assert lying.status_code in (400, 413)
    many = [("files", (f"f{index}.txt", b"y" * 1000, "text/plain")) for index in range(10)]
    multipart = client.post(url, files=many, data={"values": '{"customer": "A"}'}, headers=auth(token))
    assert multipart.status_code == 413
    accepted = client.post(url, content=_chunks(100), headers=headers)
    assert accepted.status_code == 202, accepted.text
    finish(client, publication["slug"], token, accepted.json()["run_id"])


def test_invalid_query_numbers_answer_422(make_app: Callable[..., TestClient]) -> None:
    client, publication, token = _published(make_app)
    slug = publication["slug"]
    for path in (f"{PUBLISHED}/{slug}/runs?limit=many", f"{PUBLISHED}/{slug}/usage?days=week"):
        answer = client.get(path, headers=auth(token))
        assert answer.status_code == 422, path
        assert answer.json()["error"]["code"] == "request.invalid"


# ---- idempotency ---------------------------------------------------------------------------------


def test_a_replayed_request_does_not_count_as_a_new_run(make_app: Callable[..., TestClient]) -> None:
    client, publication, token = _published(make_app)
    slug, pid = publication["slug"], publication["id"]
    client.patch(f"/api/publications/{pid}", json={"limits": {**publication["limits"], "runs_per_minute": 1}})
    headers = {**auth(token), "Idempotency-Key": "order-1"}
    body = {"values": {"customer": "A"}}
    first = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers)
    again = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers)
    assert first.status_code == 202 and again.status_code == 200, again.text
    assert again.json()["run_id"] == first.json()["run_id"]
    other = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=auth(token))
    assert other.json()["error"]["code"] == "limit.runs"
    finish(client, slug, token, first.json()["run_id"])


def test_the_same_idempotency_key_of_another_key_or_api_starts_its_own_run(
    make_app: Callable[..., TestClient],
) -> None:
    client, publication, token = _published(make_app)
    slug = publication["slug"]
    second = new_key(client, publication["id"])
    body = {"values": {"customer": "A"}}
    first = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers={**auth(token), "Idempotency-Key": "k"})
    other = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers={**auth(second), "Idempotency-Key": "k"})
    assert first.status_code == 202 and other.status_code == 202
    assert first.json()["run_id"] != other.json()["run_id"]
    for item in (first, other):
        finish(client, slug, token, item.json()["run_id"])


def test_an_expired_idempotency_key_starts_a_new_run(make_app: Callable[..., TestClient]) -> None:
    client, publication, token = _published(make_app)
    slug = publication["slug"]
    headers = {**auth(token), "Idempotency-Key": "old"}
    body = {"values": {"customer": "A"}}
    first = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers).json()["run_id"]
    finish(client, slug, token, first)
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    for key, record in store.items("api_idempotency", _Raw):
        store.put_raw("api_idempotency", key, {**record.model_dump(), "at": (now() - timedelta(hours=25)).isoformat()})
    later = client.post(f"{PUBLISHED}/{slug}/runs", json=body, headers=headers)
    assert later.status_code == 202 and later.json()["run_id"] != first
    finish(client, slug, token, later.json()["run_id"])


def test_concurrent_identical_requests_start_one_run(make_app: Callable[..., TestClient]) -> None:
    client, publication, token = _published(make_app)
    slug = publication["slug"]
    headers = {**auth(token), "Idempotency-Key": "same"}
    with ThreadPoolExecutor(4) as pool:
        answers = list(
            pool.map(
                lambda _: client.post(f"{PUBLISHED}/{slug}/runs", json={"values": {"customer": "A"}}, headers=headers),
                range(4),
            )
        )
    assert sorted(answer.status_code for answer in answers) == [200, 200, 200, 202]
    assert len({answer.json()["run_id"] for answer in answers}) == 1
    finish(client, slug, token, answers[0].json()["run_id"])


def test_a_failed_idempotency_record_still_answers_with_the_started_run(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, publication, token = _published(make_app)
    store = client.app.state.nodestro.store  # type: ignore[attr-defined]
    original = store.put_raw

    def failing(collection: str, key: str, value: Any) -> None:
        if collection == "api_idempotency":
            raise OSError("disk full")
        original(collection, key, value)

    monkeypatch.setattr(store, "put_raw", failing)
    started = client.post(
        f"{PUBLISHED}/{publication['slug']}/runs",
        json={"values": {"customer": "A"}},
        headers={**auth(token), "Idempotency-Key": "x"},
    )
    assert started.status_code == 202, started.text
    finish(client, publication["slug"], token, started.json()["run_id"])


class _Raw(serving.BaseModel):
    run_id: str
    request: str
    at: str


# ---- Server-Sent Events --------------------------------------------------------------------------


class _Runs:
    def __init__(self, run: Run, history: list[RunEvent], later: list[RunEvent]) -> None:
        self.run, self.history, self.later = run, history, later
        self.unsubscribed = False

    def is_live(self, _run_id: str) -> bool:
        return True

    def get(self, _run_id: str) -> Run:
        return self.run

    def subscribe(self, _run_id: str) -> tuple[list[RunEvent], asyncio.Queue[RunEvent | None]]:
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        for event in [*self.later, None]:
            queue.put_nowait(event)
        return list(self.history), queue

    def unsubscribe(self, _run_id: str, _queue: Any) -> None:
        self.unsubscribed = True


class _Request:
    def __init__(self, last_event_id: str | None) -> None:
        self.headers = {"last-event-id": last_event_id} if last_event_id is not None else {}

    async def is_disconnected(self) -> bool:
        return False


def _event_ids(last_event_id: str | None) -> tuple[list[str], list[int | None], _Runs]:
    workflow = Workflow(name="W", nodes=[{"id": "start", "type": "start"}], edges=[])
    run = Run(workflow_id=workflow.id, workflow_name="W", workflow=workflow, status="running", trigger="api")
    events = [RunEvent(seq=seq, run_id=run.id, type="node_started", node_id="start") for seq in range(1, 7)]
    runs = _Runs(run, events[:5], events[3:])

    class Host:
        pass

    host = Host()
    host.runs = runs  # type: ignore[attr-defined]
    core = ServingCore(host)  # type: ignore[arg-type]

    async def collect() -> str:
        response = await core.events(_Request(last_event_id), "w", run, text=False)  # type: ignore[arg-type]
        return "".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]

    body = asyncio.run(collect())
    kinds: list[str] = []
    ids: list[int | None] = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        kinds.append(lines["event"])
        ids.append(int(lines["id"]) if "id" in lines else None)
    return kinds, ids, runs


def test_a_reconnect_replays_missed_events_with_increasing_ids() -> None:
    kinds, ids, runs = _event_ids("2")
    assert kinds == ["status", "node_started", "node_started", "node_started", "node_started", "end"]
    # The status has no id: a client that drops during the replay reconnects with the last replayed one.
    assert ids == [None, 3, 4, 5, 6, None]
    assert runs.unsubscribed


def test_a_new_client_gets_the_status_with_the_newest_id() -> None:
    kinds, ids, _ = _event_ids(None)
    assert kinds == ["status", "node_started", "end"]
    assert ids == [5, 6, None]


def test_an_up_to_date_reconnect_gets_no_replay() -> None:
    kinds, ids, _ = _event_ids("5")
    assert kinds == ["status", "node_started", "end"]
    assert ids == [5, 6, None]


# ---- keys ----------------------------------------------------------------------------------------


def test_a_key_revoked_while_it_is_verified_stays_revoked(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, publication, token = _published(make_app)
    keys = client.app.state.nodestro.api_keys  # type: ignore[attr-defined]
    key_id, secret = serving.parse_token(token)  # type: ignore[misc]
    from nodestro_api import api_keys

    real = api_keys.secret_matches

    def revoked_meanwhile(value: str, stored: str) -> bool:
        # The studio revokes the key after the request has read it.
        keys.revoke(key_id)
        return real(value, stored)

    monkeypatch.setattr(api_keys, "secret_matches", revoked_meanwhile)
    assert keys.verify(key_id, secret) is None
    monkeypatch.setattr(api_keys, "secret_matches", real)
    assert next(item for item in keys.all() if item.id == key_id).revoked_at is not None
    answer = client.get(f"{PUBLISHED}/{publication['slug']}", headers=auth(token))
    assert answer.status_code == 401


def test_concurrent_rotations_give_one_new_key(make_app: Callable[..., TestClient]) -> None:
    client, publication, token = _published(make_app)
    key_id = token.split("_")[1]

    def rotate(_: int) -> int:
        return client.post(f"/api/api-keys/{key_id}/rotate").status_code

    with ThreadPoolExecutor(6) as pool:
        codes = sorted(pool.map(rotate, range(6)))
    assert codes == [200, 400, 400, 400, 400, 400]
    keys = client.get("/api/api-keys").json()
    active = [item for item in keys if item["revoked_at"] is None]
    assert len(active) == 1 and active[0]["replaces"] == key_id
    assert client.get(f"{PUBLISHED}/{publication['slug']}", headers=auth(token)).status_code == 401


def test_keys_never_reach_other_publications_runs_or_files(make_app: Callable[..., TestClient]) -> None:
    client = make_app({"WRITER": [text("a")] * 6})
    first = publish(client, form_workflow(client))["publication"]
    second = publish(client, form_workflow(client))["publication"]
    token_a, token_b = new_key(client, first["id"]), new_key(client, second["id"])
    run_id = client.post(
        f"{PUBLISHED}/{first['slug']}/runs", json={"values": {"customer": "A"}}, headers=auth(token_a)
    ).json()["run_id"]
    finish(client, first["slug"], token_a, run_id)
    for path in ("", "/result", "/events", "/files", "/approvals"):
        answer = client.get(f"{PUBLISHED}/{second['slug']}/runs/{run_id}{path}", headers=auth(token_b))
        assert answer.status_code == 404, path
        answer = client.get(f"{PUBLISHED}/{first['slug']}/runs/{run_id}{path}", headers=auth(token_b))
        assert answer.status_code == 404, path
    # The key list of the studio shows the prefix, never the secret or its hash.
    listed = client.get("/api/api-keys").text
    assert "secret_hash" not in listed and token_a.split("_", 2)[2] not in listed


# ---- network listener -------------------------------------------------------------------------


def test_a_failed_change_keeps_the_working_listener(make_app: Callable[..., TestClient], tmp_path: Path) -> None:
    client = make_app({})
    port = _free_port()
    assert client.put("/api/api-network", json={"enabled": True, "host": "127.0.0.1", "port": port}).json()["running"]
    try:
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            taken = busy.getsockname()[1]
            status = client.put("/api/api-network", json={"enabled": True, "host": "127.0.0.1", "port": taken}).json()
        assert status["running"] is True and status["port"] == port
        assert status["error_code"] == "lan.port_unavailable"
        assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=5).json() == {"status": "ok"}
        # A certificate that cannot be loaded leaves the port free and the old listener running.
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        cert.write_text("not a certificate")
        key.write_text("not a key")
        other = _free_port()
        status = client.put(
            "/api/api-network",
            json={
                "enabled": True,
                "host": "127.0.0.1",
                "port": other,
                "tls_certfile": str(cert),
                "tls_keyfile": str(key),
            },
        ).json()
        assert status["running"] is True and status["port"] == port and status["tls"] is False
        assert status["error_code"] == "lan.start_failed"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", other))
        assert client.get("/api/api-network").json()["port"] == port
    finally:
        client.put("/api/api-network", json={"enabled": False, "host": "127.0.0.1", "port": port})


# ---- uploaded files --------------------------------------------------------------------------------


def _blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_uploaded_files_cannot_run_scripts_in_the_studio(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    files = [
        ("files", ("page.html", b"<script>alert(1)</script>", "text/html")),
        ("files", ("renamed.txt", b"<p>other</p><script>alert(2)</script>", "text/html")),
        ("files", ("doc.pdf", _blank_pdf(), "application/pdf")),
        ("files", ("notes.txt", b"hello", "text/plain")),
    ]
    added = client.post("/api/knowledge", files=files)
    assert added.status_code == 201, added.text
    by_name = {item["name"]: item["id"] for item in added.json()}
    for name in ("page.html", "renamed.txt"):
        answer = client.get(f"/api/knowledge/{by_name[name]}/content")
        assert answer.headers["content-disposition"].startswith("attachment"), name
        assert "sandbox" in answer.headers["content-security-policy"]
        assert answer.headers["x-content-type-options"] == "nosniff"
    pdf = client.get(f"/api/knowledge/{by_name['doc.pdf']}/content")
    assert pdf.headers["content-disposition"].startswith("inline")
    assert "content-security-policy" not in pdf.headers
    plain = client.get(f"/api/knowledge/{by_name['notes.txt']}/content")
    assert plain.headers["content-disposition"].startswith("inline")
    assert plain.headers["x-content-type-options"] == "nosniff"
    upload = client.post("/api/inputs", files=[("files", ("x.html", b"<script></script>", "text/html"))])
    file_id = upload.json()[0]["file"]["id"]
    content = client.get(f"/api/inputs/{file_id}/content")
    assert content.headers["content-disposition"].startswith("attachment")
    assert content.headers["x-content-type-options"] == "nosniff"


# ---- backups -------------------------------------------------------------------------------------


def _backup(client: TestClient) -> dict[str, Any]:
    data = client.get("/api/backup").content
    answer = client.post("/api/backup/inspect", files={"file": ("b.zip", data, "application/zip")})
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


def test_a_backup_that_would_not_fit_on_the_disk_is_refused(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_app({})
    data = client.get("/api/backup").content
    monkeypatch.setattr(backup, "_FREE_SPACE_RESERVE", 1 << 62)
    answer = client.post("/api/backup/inspect", files={"file": ("b.zip", data, "application/zip")})
    assert answer.status_code == 400
    assert answer.json()["code"] == "backup.too_large"
    assert not list((client.app.state.nodestro.config.data_dir / "backups" / ".pending").glob("*/nodestro.db"))  # type: ignore[attr-defined]


def test_a_failed_restore_changes_nothing(make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_app({})
    form_workflow(client)
    upload = client.post("/api/inputs", files=[("files", ("a.txt", b"input", "text/plain"))])
    assert upload.status_code == 201, upload.text
    info = _backup(client)
    form_workflow(client)
    before = len(client.get("/api/workflows").json())

    def full_disk(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup.shutil, "copytree", full_disk)
    with pytest.raises(OSError):
        client.post("/api/backup/restore", json={"id": info["id"], "confirm": True})
    monkeypatch.undo()
    assert len(client.get("/api/workflows").json()) == before
    data_dir = client.app.state.nodestro.config.data_dir  # type: ignore[attr-defined]
    assert not list(data_dir.glob(".tools-restore-*")) and not list(data_dir.glob(".inputs-restore-*"))
    # The checked backup still waits and can be restored once the disk has room.
    assert client.post("/api/backup/restore", json={"id": info["id"], "confirm": True}).status_code == 200
    assert len(client.get("/api/workflows").json()) == before - 1


def test_only_one_restore_runs_at_a_time(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    info = _backup(client)
    with backup._RESTORE_LOCK:
        answer = client.post("/api/backup/restore", json={"id": info["id"], "confirm": True})
    assert answer.json()["code"] == "backup.restore_running"
    assert client.post("/api/backup/restore", json={"id": info["id"], "confirm": True}).status_code == 200


def test_a_zip_entry_outside_the_backup_is_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "nodestro-backup", "format_version": 1, "files": {}}))
        archive.writestr("nodestro.db", b"")
        archive.writestr("../evil.txt", b"x")
    answer = client.post("/api/backup/inspect", files={"file": ("b.zip", buffer.getvalue(), "application/zip")})
    assert answer.json()["code"] == "backup.unsafe_path"


def _entry_offsets(data: bytes, name: bytes) -> tuple[int, int]:
    """Offsets of ``name``'s local header and central directory record in a small test ZIP."""
    local = next(i for i in _find_all(data, b"PK\x03\x04") if data[i + 30 : i + 30 + len(name)] == name)
    central = next(i for i in _find_all(data, b"PK\x01\x02") if data[i + 46 : i + 46 + len(name)] == name)
    return local, central


def _find_all(data: bytes, marker: bytes) -> Iterator[int]:
    index = data.find(marker)
    while index != -1:
        yield index
        index = data.find(marker, index + 1)


def _with_blob(client: TestClient, blob: bytes) -> bytes:
    """A real backup plus one input file, with a correct manifest (small, built in memory)."""
    with zipfile.ZipFile(io.BytesIO(client.get("/api/backup").content)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files["manifest.json"])
    files["inputs/blobs/0123456789abcdef"] = blob
    manifest["files"]["inputs/blobs/0123456789abcdef"] = hashlib.sha256(blob).hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_damaged_zip_entries_are_refused_clearly(make_app: Callable[..., TestClient]) -> None:
    """Headers that lie about an entry are a 400 with a code, never a 500, and leave nothing behind."""
    client = make_app({})
    name = b"inputs/blobs/0123456789abcdef"
    # 64 KiB of zeros compress to a few hundred bytes: a tiny, harmless "bomb".
    data = _with_blob(client, b"\0" * 65536)
    local, central = _entry_offsets(data, name)

    def patched(changes: list[tuple[int, bytes]]) -> bytes:
        changed = bytearray(data)
        for offset, value in changes:
            changed[offset : offset + len(value)] = value
        return bytes(changed)

    small = struct.pack("<I", 10)
    cases = {
        # Declares 10 bytes but holds 64 KiB: zipfile stops at 10 bytes, then the CRC fails.
        "lying size": (patched([(local + 22, small), (central + 24, small)]), "backup.checksum"),
        "wrong CRC": (patched([(local + 14, b"\0\0\0\0"), (central + 16, b"\0\0\0\0")]), "backup.checksum"),
        "encrypted": (patched([(local + 6, b"\x01"), (central + 8, b"\x01")]), "backup.unexpected_file"),
        "unknown method": (
            patched([(local + 8, struct.pack("<H", 99)), (central + 10, struct.pack("<H", 99))]),
            "backup.unexpected_file",
        ),
    }
    pending = client.app.state.nodestro.config.data_dir / "backups" / ".pending"  # type: ignore[attr-defined]
    for label, (archive, code) in cases.items():
        answer = client.post("/api/backup/inspect", files={"file": ("b.zip", archive, "application/zip")})
        assert answer.status_code == 400, (label, answer.text)
        assert answer.json()["code"] == code, label
        assert not list(pending.glob("*/*")), label
    # The same backup with honest headers is fine.
    answer = client.post("/api/backup/inspect", files={"file": ("b.zip", data, "application/zip")})
    assert answer.status_code == 200, answer.text


def test_checking_a_backup_waits_for_a_running_restore(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second check used to delete the backup a running restore was reading from."""
    client = make_app({})
    form_workflow(client)
    data = client.get("/api/backup").content
    info = _backup(client)
    form_workflow(client)
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    create = backup.create_backup
    checks: list[ThreadPoolExecutor] = []

    def safety_backup_then_check(*args: Any) -> Any:
        result = create(*args)
        # Another tab checks a backup while this restore runs.
        upload = nodestro.config.data_dir.parent / "second.zip"
        upload.write_bytes(data)
        pool = ThreadPoolExecutor(1)
        checks.append(pool)
        future = pool.submit(backup.inspect_backup, nodestro, upload, "second.zip")
        with pytest.raises(concurrent.futures.TimeoutError):
            future.result(timeout=0.3)  # it waits for the restore
        return result

    monkeypatch.setattr(backup, "create_backup", safety_backup_then_check)
    answer = client.post("/api/backup/restore", json={"id": info["id"], "confirm": True})
    assert answer.status_code == 200, answer.text
    assert len(client.get("/api/workflows").json()) == 1
    for pool in checks:
        pool.shutdown(wait=True)


def test_a_restore_never_copies_from_a_missing_database(make_app: Callable[..., TestClient], tmp_path: Path) -> None:
    client = make_app({})
    form_workflow(client)
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    with nodestro.store._lock, pytest.raises(backup.BackupError) as raised:
        backup._copy_tables(nodestro.store._connection, tmp_path / "gone.db", {})
    assert raised.value.code == "backup.expired"
    assert len(client.get("/api/workflows").json()) == 1


# ---- architect events ------------------------------------------------------------------------------


def test_architect_event_numbers_stay_unique_beyond_the_kept_events(make_app: Callable[..., TestClient]) -> None:
    client = make_app({})
    service = client.app.state.nodestro.architect  # type: ignore[attr-defined]
    job = ArchitectJob(mode="create", description="x", language="en")
    service.jobs[job.id] = job
    for index in range(450):
        service.emit(job, "log", code="x", params={}, message=str(index), level="info")
    service.emit(job, "phase", phase="plan")
    numbers = [event.seq for event in job.events]
    assert numbers == sorted(set(numbers)) and numbers[-1] == 451
    snapshot, last, _queue = service.follow(job.id)
    assert last == 451 and snapshot.events[-1].seq == 451


# ---- application life cycle ----------------------------------------------------------------------


def test_shutdown_stops_every_service_when_one_fails(tmp_path: Path) -> None:
    app = create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)
    nodestro = app.state.nodestro
    stopped: list[str] = []

    async def broken() -> None:
        raise RuntimeError("scheduler broke")

    async def runs_shutdown(**_kwargs: Any) -> None:
        stopped.append("runs")

    nodestro.schedules.stop = broken
    nodestro.runs.shutdown = runs_shutdown
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/health").json() == {"status": "ok"}
    assert stopped == ["runs"]
    with pytest.raises(Exception):  # noqa: B017 - the database is closed, whatever sqlite3 raises
        nodestro.store.get_raw("settings", "app")


# ---- schedules -------------------------------------------------------------------------------------


def _due_schedule(make_app: Callable[..., TestClient]) -> tuple[TestClient, Any, dict[str, Any], Any]:
    from test_automation import Clock, answer, utc
    from test_automation import form_workflow as automation_workflow

    client = make_app({"WORKER": [answer] * 5})
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    clock = Clock(utc(2026, 10, 28, 6, 0))
    ctx.schedules.clock = clock
    body = {
        "workflow_id": automation_workflow(client),
        "kind": "daily",
        "at": "08:00",
        "time_zone": "Europe/Berlin",
        "values": {"kunde": "Plan"},
    }
    created = client.post("/api/schedules", json=body).json()
    assert client.post(f"/api/schedules/{created['id']}/enable").json()["schedule"]["enabled"] is True
    clock.value = utc(2026, 10, 28, 7, 1)
    return client, ctx, created, clock


def test_a_schedule_switched_off_while_its_run_starts_stays_off(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ctx, created, _clock = _due_schedule(make_app)
    from nodestro_api import schedules

    real = schedules.launch

    async def launch_and_switch_off(*args: Any, **kwargs: Any) -> Any:
        # The slot is already taken when the run starts (a crash now would not run it again) ...
        stored = client.app.state.nodestro.store.get_raw("schedules", created["id"])  # type: ignore[attr-defined]
        assert stored["next_run_at"].startswith("2026-10-29")
        # ... and the user switches the schedule off meanwhile.
        ctx.schedules.disable(created["id"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(schedules, "launch", launch_and_switch_off)
    started = client.portal.call(ctx.schedules.tick)  # type: ignore[attr-defined]
    assert len(started) == 1
    state = client.get(f"/api/schedules/{created['id']}").json()
    assert state["enabled"] is False and state["runs"] == 1 and state["last_run_id"] == started[0]


def test_a_schedule_deleted_while_its_run_starts_stays_deleted(
    make_app: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ctx, created, _clock = _due_schedule(make_app)
    from nodestro_api import schedules

    real = schedules.launch

    async def launch_and_delete(*args: Any, **kwargs: Any) -> Any:
        ctx.schedules.delete(created["id"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(schedules, "launch", launch_and_delete)
    assert len(client.portal.call(ctx.schedules.tick)) == 1  # type: ignore[attr-defined]
    assert client.get(f"/api/schedules/{created['id']}").status_code == 404


def test_a_backup_of_a_newer_schema_is_refused(make_app: Callable[..., TestClient], tmp_path: Path) -> None:
    """A backup written by a newer release (higher ``PRAGMA user_version``) is never restored into this one."""
    import sqlite3

    client = make_app({})
    with zipfile.ZipFile(io.BytesIO(client.get("/api/backup").content)) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    database = tmp_path / "nodestro.db"
    database.write_bytes(files["nodestro.db"])
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    files["nodestro.db"] = database.read_bytes()
    manifest = json.loads(files["manifest.json"])
    manifest["files"]["nodestro.db"] = hashlib.sha256(files["nodestro.db"]).hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    answer = client.post("/api/backup/inspect", files={"file": ("b.zip", buffer.getvalue(), "application/zip")})
    assert answer.status_code == 400
    assert answer.json()["code"] == "backup.schema_too_new"
    assert answer.json()["params"] == {"found": 99, "supported": 0}


def test_the_api_uses_no_private_storage_attributes() -> None:
    """Backups and restores go through the runtime's public store methods (snapshot, maintenance)."""
    import ast

    import nodestro_api

    offenders = []
    for path in sorted(Path(nodestro_api.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in {"_connection", "_lock"}:
                base = node.value
                if isinstance(base, ast.Attribute) and base.attr in {"store", "index", "memory"}:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
