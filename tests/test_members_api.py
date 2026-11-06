"""Members and roles managed by a tenant's administrators (API-019; needs PostgreSQL).

Runs with every identity backend (``test_identity.backend``): Nodestro's store, mirrored to Axentra, and Axentra.
"""

from __future__ import annotations

from typing import Any

import pytest
import stub_oidc
from test_identity import Server, backend, database, identity, server  # noqa: F401  (fixtures)


def _id(studio: Server, subject: str) -> str:
    return studio.identity.login(stub_oidc.ISSUER, subject).id


def _audit(browser: Any, kind: str) -> list[dict[str, Any]]:
    answer = browser.get("/api/access-audit", params={"kind": kind})
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


def test_an_owner_lists_and_changes_roles_with_an_audit_trail(server: Server) -> None:  # noqa: F811
    owner = server.member("acme", "olga", "owner")
    editor = server.member("acme", "erin", "editor")
    server.member("globex", "gary", "owner")  # another tenant
    members = owner.get("/api/members")
    assert members.status_code == 200, members.text
    found = {item["user_id"]: item for item in members.json()}
    erin, olga, gary = _id(server, "erin"), _id(server, "olga"), _id(server, "gary")
    assert found[erin]["role"] == "editor" and found[olga]["you"] is True and found[erin]["you"] is False
    assert gary not in found  # only this tenant's members

    changed = owner.put(f"/api/members/{erin}/role", json={"role": "viewer"})
    assert changed.status_code == 200 and changed.json()["role"] == "viewer", changed.text
    # The change applies to the member's next request.
    assert editor.post("/api/workflows", json={"name": "x", "nodes": [], "edges": []}).status_code == 403
    assert editor.get("/api/auth/me").json()["role"] == "viewer"
    [event] = _audit(owner, "role_changed")
    assert (event["actor"], event["target_id"], event["details"]) == (
        olga,
        erin,
        {"before": "editor", "role": "viewer"},
    )
    # The same role again changes and records nothing.
    assert owner.put(f"/api/members/{erin}/role", json={"role": "viewer"}).status_code == 200
    assert len(_audit(owner, "role_changed")) == 1

    removed = owner.delete(f"/api/members/{erin}")
    assert removed.status_code == 204, removed.text
    assert editor.get("/api/workflows").status_code == 401  # the session ended at once
    assert erin not in {item["user_id"] for item in owner.get("/api/members").json()}
    [event] = _audit(owner, "member_removed")
    assert (event["actor"], event["target_id"], event["details"]) == (olga, erin, {"role": "viewer"})
    assert owner.delete(f"/api/members/{erin}").status_code == 404


def test_only_administrators_manage_members(server: Server) -> None:  # noqa: F811
    server.member("acme", "olga", "owner")
    viewer_id = None
    for role in ("editor", "operator", "viewer"):
        browser = server.member("acme", f"{role}-user", role)
        viewer_id = _id(server, f"{role}-user")
        assert browser.get("/api/members").status_code == 403
        assert browser.put(f"/api/members/{viewer_id}/role", json={"role": "admin"}).status_code == 403
        assert browser.delete(f"/api/members/{viewer_id}").status_code == 403
    assert server.identity.role("acme", str(viewer_id)) == "viewer"
    assert server.browser().get("/api/members").status_code == 401


def test_an_administrator_cannot_reach_the_owner_role(server: Server) -> None:  # noqa: F811
    server.member("acme", "olga", "owner")
    admin = server.member("acme", "adam", "admin")
    server.member("acme", "erin", "editor")
    olga, adam, erin = _id(server, "olga"), _id(server, "adam"), _id(server, "erin")
    for user_id, role in ((adam, "owner"), (erin, "owner"), (olga, "viewer")):
        refused = admin.put(f"/api/members/{user_id}/role", json={"role": role})
        assert refused.status_code == 403 and refused.json()["code"] == "identity.owner_required", refused.text
    refused = admin.delete(f"/api/members/{olga}")
    assert refused.status_code == 403 and refused.json()["code"] == "identity.owner_required"
    assert server.identity.role("acme", olga) == "owner" and server.identity.role("acme", adam) == "admin"
    # Below the owner role an administrator manages members, also other administrators.
    assert admin.put(f"/api/members/{erin}/role", json={"role": "admin"}).status_code == 200
    assert admin.put(f"/api/members/{erin}/role", json={"role": "operator"}).status_code == 200
    assert admin.delete(f"/api/members/{erin}").status_code == 204
    assert _audit(admin, "role_changed")[0]["actor"] == adam


def test_the_last_owner_stays(server: Server) -> None:  # noqa: F811
    owner = server.member("acme", "olga", "owner")
    olga = _id(server, "olga")
    for answer in (
        owner.put(f"/api/members/{olga}/role", json={"role": "admin"}),
        owner.delete(f"/api/members/{olga}"),
    ):
        assert answer.status_code == 409 and answer.json()["code"] == "identity.last_owner", answer.text
    assert server.identity.role("acme", olga) == "owner"
    assert _audit(owner, "role_changed") == [] and _audit(owner, "member_removed") == []
    # With a second owner the first one may step down.
    server.member("acme", "otto", "owner")
    assert owner.put(f"/api/members/{olga}/role", json={"role": "admin"}).status_code == 200


def test_members_of_other_tenants_are_out_of_reach(server: Server) -> None:  # noqa: F811
    owner = server.member("acme", "olga", "owner")
    server.member("globex", "gary", "owner")
    server.member("globex", "gina", "editor")
    gary, gina = _id(server, "gary"), _id(server, "gina")
    for user_id in (gary, gina, "u_000000000000000000000000", "not-an-id"):
        assert owner.put(f"/api/members/{user_id}/role", json={"role": "viewer"}).status_code == 404
        answer = owner.delete(f"/api/members/{user_id}")
        assert answer.status_code == 404 and answer.json()["code"] == "identity.not_a_member"
    assert server.identity.role("globex", gina) == "editor" and server.identity.role("globex", gary) == "owner"
    assert server.identity.role("acme", gina) is None  # never added here either
    invalid = owner.put(f"/api/members/{_id(server, 'olga')}/role", json={"role": "superuser"})
    assert invalid.status_code in (400, 422)


@pytest.mark.parametrize("path", ["/api/members"])
def test_the_desktop_has_no_member_routes(make_app: Any, path: str) -> None:
    client = make_app()
    assert client.get(path).status_code == 404
