"""The data-only extension store on a server (API-081): a server's tenants have no store, so every route stays closed
for every member, also an owner, and nothing is written into the server's data directory."""

from __future__ import annotations

from typing import Any

from test_extensions_api import EXTENSIONS, sha, upload, wheel
from test_worker import connect, schema, server  # noqa: F401  (PostgreSQL server fixtures)


def test_a_server_has_no_extension_store(server: Any) -> None:  # noqa: F811
    alice = server.member("acme", "alice", "owner")
    data = wheel()
    for response in (
        alice.get(EXTENSIONS),
        upload(alice, data),
        alice.delete(f"{EXTENSIONS}/{sha(data)}"),
        alice.post(f"{EXTENSIONS}/{sha(data)}/revoke"),
    ):
        assert (response.status_code, response.json()["code"]) == (404, "extension_store.disabled")
    acme = server.app.state.tenants.get("acme")
    assert acme.extension_store is None
