"""Logins for the server: OpenID Connect, sessions, roles and the tenant of every request (ADR-0003).

A person logs in at the operator's identity provider (authorization code flow with PKCE, ``state`` and ``nonce``).
The callback checks the ID token (signature from the provider's keys, issuer, audience, expiry, nonce), finds or
creates the user (:class:`~nodestro_runtime.identity_store.IdentityStore`) and starts a server-side session in one
tenant. The browser holds only a random session token in an ``HttpOnly; Secure; SameSite=Lax`` cookie with the
``__Host-`` prefix; the database holds its hash. :class:`SessionResolver` turns the cookie into the request's
:class:`~nodestro_api.permissions.Principal`: the user, the tenant and the permissions of the user's role there.

Nothing is granted by a login alone: a user needs a role in a tenant. The first owner of a tenant is bound with a
one-time setup link (``/api/auth/login?setup=<bootstrap token>``) that an operator creates; there is no default
administrator and no password.

A login is bound to the browser that starts it: ``/api/auth/login`` gives the browser a short-lived
``__Host-nodestro-login`` cookie holding the login's ``state``, and the callback is refused without it (no login CSRF
with a callback URL started elsewhere). Setup tokens and sign-in codes are kept out of the access log
(:func:`redact_auth_logs`).

Changing requests must come from the studio's own origin (:class:`SameOriginMiddleware`): the session cookie is
sent by the browser automatically, so the ``Origin`` header protects against cross-site requests. It is not a
replacement for the login.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
import hashlib
import logging
import re
import secrets
import threading
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from nodestro_runtime.identity_store import ROLES, IdentityStore, Role
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage_postgres import TENANT_ID
from pydantic import BaseModel
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from .permissions import PUBLIC, READ, Permission, Principal

logger = logging.getLogger(__name__)

#: The session cookie: ``__Host-`` makes browsers require ``Secure``, ``Path=/`` and no ``Domain``.
SESSION_COOKIE = "__Host-nodestro-session"
#: Binds a starting login to the browser that started it (A-01): it holds the login's ``state``, and the callback
#: is only accepted from a browser that sends the same value (no login CSRF with a callback URL from elsewhere).
LOGIN_COOKIE = "__Host-nodestro-login"
#: What each role may do in its tenant. No role holds ``install`` (the installation belongs to its operator).
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    "owner": frozenset({Permission.READ, Permission.EDIT, Permission.RUN, Permission.ADMIN}),
    "admin": frozenset({Permission.READ, Permission.EDIT, Permission.RUN, Permission.ADMIN}),
    "editor": frozenset({Permission.READ, Permission.EDIT, Permission.RUN}),
    "operator": frozenset({Permission.READ, Permission.RUN}),
    "viewer": frozenset({Permission.READ}),
}
assert set(ROLE_PERMISSIONS) == set(ROLES)
#: Signature algorithms accepted for ID tokens (asymmetric only: never ``none`` or a shared secret).
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class LoginError(Exception):
    """A login that cannot continue; ``code`` is shown, the details only logged."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def secure_url(url: str) -> bool:
    """Whether ``url`` is https, or http on this computer (loopback: development and tests only)."""
    parts = urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def safe_return_path(value: str | None) -> str:
    """A path on this site to continue at after the login (never another site: no open redirect)."""
    if not value or len(value) > 2000 or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    if any(char in value for char in "\\\r\n\t") or ":" in value.split("?", 1)[0]:
        return "/"
    return value


# ---- OpenID Connect ------------------------------------------------------------------------------


@dataclass
class OidcSettings:
    """The operator's identity provider; the client secret comes from the deployment's secrets."""

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    #: The callback URL registered at the provider: ``https://<studio>/api/auth/callback``.
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    #: Allowed clock difference to the provider, in seconds.
    leeway: int = 60


class OidcClient:
    """The authorization code flow with PKCE against one provider (``httpx`` for its endpoints)."""

    def __init__(self, settings: OidcSettings, http: Any | None = None) -> None:
        import httpx

        if not secure_url(settings.issuer):
            raise ValueError("The identity provider must be reached over https (http only on localhost)")
        self.settings = settings
        self.http = http or httpx.Client(timeout=10.0, follow_redirects=False)
        self._metadata: dict[str, Any] | None = None
        self._keys: Any | None = None
        self._lock = threading.Lock()

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            if self._metadata is None:
                url = self.settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
                data = self._get_json(url)
                if data.get("issuer") != self.settings.issuer:
                    raise LoginError("identity.provider", f"The provider names issuer {data.get('issuer')!r}")
                for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    endpoint = str(data.get(key) or "")
                    # An https provider only names https endpoints; a loopback development provider may use http.
                    if not (
                        endpoint.startswith("https://")
                        if self.settings.issuer.startswith("https://")
                        else secure_url(endpoint)
                    ):
                        raise LoginError("identity.provider", f"The provider's {key} is not an https URL")
                self._metadata = data
            return self._metadata

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            answer = self.http.get(url, headers={"Accept": "application/json"})
            answer.raise_for_status()
            data = answer.json()
        except Exception as exc:
            raise LoginError("identity.provider", f"Reading {url} failed: {exc}") from exc
        if not isinstance(data, dict):
            raise LoginError("identity.provider", f"{url} did not answer with an object")
        return data

    def authorization_url(self, *, state: str, nonce: str, code_verifier: str) -> str:
        challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": " ".join(self.settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        endpoint = str(self.metadata()["authorization_endpoint"])
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def exchange(self, *, code: str, code_verifier: str, nonce: str) -> dict[str, Any]:
        """Trade the code for tokens and return the checked claims of the ID token."""
        try:
            answer = self.http.post(
                str(self.metadata()["token_endpoint"]),
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(self.settings.client_id, self.settings.client_secret),
                headers={"Accept": "application/json"},
            )
            answer.raise_for_status()
            tokens = answer.json()
        except LoginError:
            raise
        except Exception as exc:
            raise LoginError("identity.login_failed", f"The token request failed: {exc}") from exc
        id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
        if not isinstance(id_token, str):
            raise LoginError("identity.login_failed", "The provider sent no ID token")
        return self.verify(id_token, nonce=nonce)

    def verify(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        """The claims of a valid ID token for this client and login (raises :class:`LoginError`)."""
        import jwt

        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise LoginError("identity.login_failed", f"Unreadable ID token: {exc}") from exc
        algorithm = header.get("alg")
        if algorithm not in ALGORITHMS:
            raise LoginError("identity.login_failed", f"ID token algorithm {algorithm!r} is not accepted")
        key = self._key(header.get("kid"))
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key,
                algorithms=[algorithm],
                audience=self.settings.client_id,
                issuer=self.settings.issuer,
                leeway=self.settings.leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise LoginError("identity.login_failed", f"Invalid ID token: {exc}") from exc
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise LoginError("identity.login_failed", "The ID token belongs to another login (nonce)")
        audience = claims["aud"]
        if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != self.settings.client_id:
            raise LoginError("identity.login_failed", "The ID token was issued to another client (azp)")
        return claims

    def _key(self, kid: str | None) -> Any:
        """The provider key that signed a token; the key set is read again once when ``kid`` is unknown."""
        import jwt

        for refresh in (False, True):
            keys = self._keys
            if keys is None or refresh:
                try:
                    keys = jwt.PyJWKSet.from_dict(self._get_json(str(self.metadata()["jwks_uri"])))
                except jwt.PyJWTError as exc:
                    raise LoginError("identity.provider", f"Unusable provider keys: {exc}") from exc
                self._keys = keys
            if kid is None:
                if len(keys.keys) == 1:
                    return keys.keys[0].key
                continue
            for item in keys.keys:
                if item.key_id == kid:
                    return item.key
        raise LoginError("identity.login_failed", "The ID token was signed with an unknown key")


# ---- sessions and principals ---------------------------------------------------------------------


class SessionResolver:
    """The principal of a request from its session cookie (``None``: not logged in)."""

    def __init__(self, identity: IdentityStore) -> None:
        self.identity = identity

    def __call__(self, connection: HTTPConnection) -> Principal | None:
        token = connection.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        session = self.identity.session(token)
        if session is None:
            return None
        return Principal(
            id=session.user_id,
            permissions=ROLE_PERMISSIONS[session.role],
            tenant_id=session.tenant_id,
            role=session.role,
        )


def set_session_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def set_login_cookie(response: Response, state: str, max_age: int) -> None:
    """The browser's half of a starting login (``Lax``: sent on the provider's top-level redirect back)."""
    response.set_cookie(LOGIN_COOKIE, state, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def clear_login_cookie(response: Response) -> None:
    response.delete_cookie(LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="lax")


#: Query values that are secrets of a login: the bootstrap token of a setup link, the provider's ``code`` and
#: the ``state`` of OpenID Connect and MCP sign-in callbacks.
_AUTH_QUERY = re.compile(r"([?&](?:setup|code|state)=)[^&\s\"]*")


class RedactAuthSecrets(logging.Filter):
    """Keep setup tokens and sign-in codes out of log records (uvicorn's access log prints the full request line;
    D-02). Only the values are replaced: the path and the other parameters stay readable."""

    @staticmethod
    def redact(text: str) -> str:
        return _AUTH_QUERY.sub(r"\1[redacted]", text) if "=" in text else text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            # Formatters such as uvicorn's AccessFormatter unpack the arguments: keep them, redacted.
            record.args = tuple(self.redact(arg) if isinstance(arg, str) else arg for arg in record.args)
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        return True


def redact_auth_logs() -> None:
    """Install :class:`RedactAuthSecrets` on the access log (``uvicorn.access``); installing twice is harmless."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, RedactAuthSecrets) for item in access.filters):
        access.addFilter(RedactAuthSecrets())


class SameOriginMiddleware:
    """Refuse changing requests from other sites (``403 access.cross_site``).

    Browsers send ``Origin`` with every ``POST``, ``PUT``, ``PATCH`` and ``DELETE``; a request whose origin is not
    the studio's is refused before it reaches a route. ``exempt`` prefixes (the key-authenticated published APIs)
    are called by machines from anywhere."""

    def __init__(self, app: ASGIApp, *, origin: str, exempt: Iterable[str] = ()) -> None:
        self.app = app
        self.origin = origin.rstrip("/")
        self.exempt = tuple(exempt)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in _UNSAFE_METHODS:
            path = scope.get("path", "")
            if not any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in self.exempt):
                origin = dict(scope.get("headers") or []).get(b"origin", b"").decode("latin-1")
                if origin != self.origin:
                    answer = JSONResponse(
                        {
                            "detail": "Changing requests must come from the studio itself",
                            "code": "access.cross_site",
                            "params": {},
                        },
                        status_code=403,
                    )
                    await answer(scope, receive, send)
                    return
        await self.app(scope, receive, send)


# ---- tenants -------------------------------------------------------------------------------------


class TenantWorkspaces:
    """One application context per tenant, opened on first use (``factory(tenant_id)``) and closed with
    ``close(context)`` (default: its ``aclose``)."""

    def __init__(self, factory: Callable[[str], Any], close: Callable[[Any], Awaitable[None]] | None = None) -> None:
        self.factory = factory
        self.close = close or (lambda context: context.aclose())
        self._open: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, tenant_id: str) -> Any:
        if not TENANT_ID.fullmatch(tenant_id):
            raise ValueError(f"Invalid tenant id {tenant_id!r}")
        with self._lock:
            found = self._open.get(tenant_id)
            if found is None:
                found = self._open[tenant_id] = self.factory(tenant_id)
            return found

    def opened(self) -> list[Any]:
        with self._lock:
            return list(self._open.values())

    async def aclose(self) -> None:
        for context in self.opened():
            try:
                await self.close(context)
            except Exception:
                logger.exception("Closing a tenant failed")
        with self._lock:
            self._open.clear()


# ---- routes ----------------------------------------------------------------------------------------


class Me(BaseModel):
    user_id: str
    name: str | None = None
    email: str | None = None
    tenant_id: str
    role: str
    permissions: list[str]
    #: Every tenant the user belongs to, with the role there.
    tenants: dict[str, str]


class SwitchIn(BaseModel):
    tenant_id: str


def _login_error(code: str, status: int = 400) -> JSONResponse:
    messages = {
        "identity.login_expired": "The login took too long or was already used; please log in again",
        "identity.login_failed": "The login could not be completed",
        "identity.provider": "The identity provider cannot be reached or is misconfigured",
        "identity.no_tenant": "This account has no access to Nodestro yet; ask an administrator",
        "identity.bootstrap_invalid": "This setup link is invalid, used or expired",
        "identity.not_a_member": "This account has no access to this tenant",
    }
    return JSONResponse({"detail": messages.get(code, code), "code": code, "params": {}}, status_code=status)


def auth_router(identity: IdentityStore, oidc: OidcClient) -> APIRouter:
    """``/api/auth``: start a login, the provider's callback, the current session, switching tenants, logout."""
    router = APIRouter(prefix="/api/auth")
    lifetime = int(identity.session_lifetime.total_seconds())
    login_lifetime = int(identity.login_lifetime.total_seconds())

    @router.get("/login", dependencies=PUBLIC, include_in_schema=False)
    def login(return_to: str | None = None, tenant: str | None = None, setup: str | None = None) -> Response:
        if tenant is not None and not TENANT_ID.fullmatch(tenant):
            tenant = None
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(24)
        state = identity.create_login_state(
            nonce=nonce,
            code_verifier=verifier,
            return_to=safe_return_path(return_to),
            tenant_id=tenant,
            bootstrap_token=setup or None,
        )
        try:
            url = oidc.authorization_url(state=state, nonce=nonce, code_verifier=verifier)
        except LoginError as exc:
            logger.warning("Login could not start: %s", exc)
            return _login_error(exc.code, 502)
        answer = RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})
        set_login_cookie(answer, state, login_lifetime)
        return answer

    @router.get("/callback", dependencies=PUBLIC, include_in_schema=False)
    def callback(request: Request, state: str = "", code: str = "", error: str | None = None) -> Response:
        answer = _callback(request.cookies.get(LOGIN_COOKIE, ""), state, code, error)
        clear_login_cookie(answer)
        return answer

    def _callback(bound: str, state: str, code: str, error: str | None) -> Response:
        started = identity.take_login_state(state)
        if started is None:
            return _login_error("identity.login_expired")
        if not bound or not secrets.compare_digest(bound.encode(), state.encode()):
            # A callback URL that another browser started (login CSRF), or a browser that lost the login cookie.
            logger.warning("A login callback came from a browser that did not start this login")
            return _login_error("identity.login_expired")
        if error or not code:
            logger.info("The identity provider refused the login: %s", (error or "no code")[:200])
            return _login_error("identity.login_failed")
        try:
            claims = oidc.exchange(code=code, code_verifier=started.code_verifier, nonce=started.nonce)
        except LoginError as exc:
            logger.warning("Login failed: %s", exc)
            return _login_error(exc.code)
        email = claims.get("email") if claims.get("email_verified") is True else None
        user = identity.login(str(claims["iss"]), str(claims["sub"]), email=email, name=claims.get("name"))
        try:
            if started.bootstrap_hash is not None:
                tenant_id = identity.use_bootstrap(started.bootstrap_hash, user.id)
            else:
                memberships = {item.tenant_id for item in identity.memberships(user.id)}
                if not memberships:
                    return _login_error("identity.no_tenant", 403)
                tenant_id = started.tenant_id if started.tenant_id in memberships else sorted(memberships)[0]
            token = identity.create_session(user.id, tenant_id)
        except NodestroError as exc:
            return _login_error(exc.code, 403)
        answer = RedirectResponse(started.return_to, status_code=303, headers={"Cache-Control": "no-store"})
        set_session_cookie(answer, token, lifetime)
        return answer

    @router.get("/me", dependencies=READ)
    def me(request: Request) -> Me:
        principal: Principal = request.state.principal
        user = identity.user(principal.id)
        return Me(
            user_id=principal.id,
            name=user.name if user else None,
            email=user.email if user else None,
            tenant_id=principal.tenant_id,
            role=principal.role or "",
            permissions=sorted(item.value for item in principal.permissions),
            tenants={item.tenant_id: item.role for item in identity.memberships(principal.id)},
        )

    @router.post("/switch", dependencies=READ)
    def switch(body: SwitchIn, request: Request) -> Response:
        principal: Principal = request.state.principal
        try:
            token = identity.create_session(principal.id, body.tenant_id)
        except NodestroError:
            # The same answer for unknown tenants and tenants of others: nothing about them is revealed.
            return _login_error("identity.not_a_member", 404)
        identity.end_session(request.cookies.get(SESSION_COOKIE, ""))
        answer = Response(status_code=204)
        set_session_cookie(answer, token, lifetime)
        return answer

    @router.post("/logout", dependencies=PUBLIC)
    def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            identity.end_session(token)
        answer = Response(status_code=204)
        clear_session_cookie(answer)
        return answer

    return router


__all__ = [
    "ALGORITHMS",
    "LOGIN_COOKIE",
    "ROLE_PERMISSIONS",
    "SESSION_COOKIE",
    "LoginError",
    "Me",
    "OidcClient",
    "OidcSettings",
    "RedactAuthSecrets",
    "SameOriginMiddleware",
    "SessionResolver",
    "TenantWorkspaces",
    "auth_router",
    "clear_login_cookie",
    "clear_session_cookie",
    "redact_auth_logs",
    "safe_return_path",
    "set_login_cookie",
    "set_session_cookie",
]
