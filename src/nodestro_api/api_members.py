"""Members and roles of a tenant, managed by its administrators (server profile only; API-019).

``GET /api/members`` lists the members of the request's tenant, ``PUT /api/members/{user_id}/role`` changes a
member's role and ``DELETE /api/members/{user_id}`` removes a member (their sessions in the tenant end at once). All
three need the permission ``admin`` and work only on the tenant of the request's session: a user of another tenant is
``404 identity.not_a_member``, the same answer as an unknown id.

The roles keep their order (owner above admin above the rest): only an owner gives or takes the owner role or
changes or removes an owner, so an administrator can never raise anyone, themselves included, above their own role.
A tenant keeps at least one owner (``409 identity.last_owner``). Every change is recorded in the tenant's access audit
trail (``role_changed``, ``member_removed``; actor: the administrator), like the operator's commands. A role change
applies to the member's next request (sessions read the current role). Invitations do not exist yet: a person joins
with the operator's setup link or the ``set-role`` command.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from nodestro_runtime.identity_store import ROLES, IdentityStore, Role
from nodestro_runtime.problems import NodestroError
from pydantic import BaseModel

from .access_audit import AccessEvent, record
from .api import Ctx
from .permissions import ADMIN, Principal
from .serving import ApiError


class Member(BaseModel):
    user_id: str
    name: str | None = None
    email: str | None = None
    role: Role
    #: Whether this member is the person asking.
    you: bool = False


class RoleIn(BaseModel):
    role: Role


def _not_a_member() -> ApiError:
    return ApiError(404, "identity.not_a_member", "This account has no access to this tenant")


def _owner_only() -> ApiError:
    return ApiError(
        403, "identity.owner_required", "Only an owner of this workspace can give, take or change the owner role"
    )


def members_router(identity: IdentityStore) -> APIRouter:
    """The member routes on ``identity`` (the server's identity store)."""
    router = APIRouter(prefix="/api/members", tags=["members"])

    def principal(request: Request) -> Principal:
        found: Principal = request.state.principal
        return found

    def target_role(tenant_id: str, user_id: str) -> Role:
        role = identity.role(tenant_id, user_id)
        if role is None:
            raise _not_a_member()
        return role

    def audited(ctx: Any, kind: str, actor: str, user_id: str, **details: Any) -> None:
        event = AccessEvent.model_validate(
            {"kind": kind, "actor": actor, "target_type": "user", "target_id": user_id, "details": details}
        )
        record(ctx.store, event)

    def changed(call: Any, *args: Any) -> None:
        try:
            call(*args)
        except NodestroError as exc:
            if exc.code == "identity.last_owner":
                raise ApiError(409, exc.code, "A workspace needs at least one owner") from None
            raise

    @router.get("", dependencies=ADMIN)
    def list_members(request: Request) -> list[Member]:
        """The members of this workspace with their roles, by user id."""
        asking = principal(request)
        found = []
        for membership in identity.members(asking.tenant_id):
            user = identity.user(membership.user_id)
            found.append(
                Member(
                    user_id=membership.user_id,
                    name=user.name if user else None,
                    email=user.email if user else None,
                    role=membership.role,
                    you=membership.user_id == asking.id,
                )
            )
        return found

    @router.put("/{user_id}/role", dependencies=ADMIN)
    def set_member_role(user_id: str, body: RoleIn, request: Request, ctx: Ctx) -> Member:
        """Change a member's role (``403 identity.owner_required`` when the owner role is involved and the asking
        person is no owner; ``409 identity.last_owner`` for the last owner)."""
        asking = principal(request)
        if body.role not in ROLES:  # the model checks it too; kept for clarity
            raise ApiError(400, "identity.role", "Unknown role", role=body.role)
        before = target_role(asking.tenant_id, user_id)
        if asking.role != "owner" and "owner" in (before, body.role):
            raise _owner_only()
        if before != body.role:
            changed(identity.set_role, asking.tenant_id, user_id, body.role)
            audited(ctx, "role_changed", asking.id, user_id, before=before, role=body.role)
        user = identity.user(user_id)
        return Member(
            user_id=user_id,
            name=user.name if user else None,
            email=user.email if user else None,
            role=body.role,
            you=user_id == asking.id,
        )

    @router.delete("/{user_id}", status_code=204, dependencies=ADMIN)
    def remove_member(user_id: str, request: Request, ctx: Ctx) -> Response:
        """Remove a member from this workspace; their sessions here end at once."""
        asking = principal(request)
        before = target_role(asking.tenant_id, user_id)
        if before == "owner" and asking.role != "owner":
            raise _owner_only()
        changed(identity.remove_member, asking.tenant_id, user_id)
        audited(ctx, "member_removed", asking.id, user_id, role=before)
        return Response(status_code=204)

    return router


__all__ = ["Member", "RoleIn", "members_router"]
