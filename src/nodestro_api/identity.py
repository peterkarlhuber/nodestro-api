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

Two login flows serve the same routes, cookies and error codes (:class:`LoginFlow`): Nodestro's own OpenID Connect
client (:class:`NodestroLoginFlow`, identity backends ``nodestro`` and ``mirror``) and Axentra's
:class:`~axentra.identity.oidc.OidcService` (:class:`AxentraLoginFlow`, backend ``axentra``: Axentra keeps the login
state, verifies the provider's answer, finds or creates the user and audits the login; Nodestro starts the session
in the chosen tenant). A login ends the session the browser held before, and a tenant switch issues a new session
and ends the old one (session rotation). A suspended tenant is closed (``403 identity.tenant_suspended``).

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
import os
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from nodestro_runtime.identity_store import ROLE_GRANTS, ROLES, IdentityStore, Role
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.storage_postgres import TENANT_ID
from pydantic import BaseModel
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from .permissions import PUBLIC, READ, AccessError, Permission, Principal

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
assert all(ROLE_PERMISSIONS[role] == {Permission(p) for p in ROLE_GRANTS[role]} for role in ROLES)
#: The tenant permissions a session may carry from the identity backend (never ``install``).
_TENANT_PERMISSIONS = frozenset({Permission.READ, Permission.EDIT, Permission.RUN, Permission.ADMIN})
#: Signature algorithms accepted for ID tokens (asymmetric only: never ``none`` or a shared secret).
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class LoginError(Exception):
    """A login that cannot continue; ``code`` (and ``status``) is shown, the details only logged."""

    def __init__(self, code: str, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.code = code
        self.status = status


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


# ---- login flows -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class StartedLogin:
    """Where to send the browser, and the ``state`` the login cookie binds to it."""

    url: str
    state: str


@dataclass(frozen=True)
class FinishedLogin:
    """A verified callback: who logged in and what the login started with."""

    issuer: str
    subject: str
    #: Only an address the provider marked verified.
    email: str | None
    name: str | None
    return_to: str
    tenant_id: str | None = None
    bootstrap_hash: bytes | None = None
    #: The Axentra user the flow found or created (:class:`AxentraLoginFlow`); checked against Nodestro's link.
    account: str | None = None


class LoginFlow:
    """Starts and finishes OpenID Connect logins for :func:`auth_router` (raises :class:`LoginError`)."""

    def start(
        self, *, return_to: str, tenant_id: str | None, bootstrap_token: str | None, ip: str | None
    ) -> StartedLogin:
        raise NotImplementedError

    def finish(
        self, *, state: str, code: str, error: str | None, bound: str, ip: str | None, user_agent: str
    ) -> FinishedLogin:
        raise NotImplementedError


class NodestroLoginFlow(LoginFlow):
    """Nodestro's own client (:class:`OidcClient`) with login states in ``nodestro_login_states``."""

    def __init__(self, identity: IdentityStore, oidc: OidcClient) -> None:
        self.identity = identity
        self.oidc = oidc

    def start(
        self, *, return_to: str, tenant_id: str | None, bootstrap_token: str | None, ip: str | None
    ) -> StartedLogin:
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(24)
        state = self.identity.create_login_state(
            nonce=nonce,
            code_verifier=verifier,
            return_to=return_to,
            tenant_id=tenant_id,
            bootstrap_token=bootstrap_token,
        )
        url = self.oidc.authorization_url(state=state, nonce=nonce, code_verifier=verifier)
        return StartedLogin(url=url, state=state)

    def finish(
        self, *, state: str, code: str, error: str | None, bound: str, ip: str | None, user_agent: str
    ) -> FinishedLogin:
        started = self.identity.take_login_state(state)
        if started is None:
            raise LoginError("identity.login_expired", "unknown, used or expired login state")
        if not bound or not secrets.compare_digest(bound.encode(), state.encode()):
            # A callback URL that another browser started (login CSRF), or a browser that lost the login cookie.
            raise LoginError("identity.login_expired", "a login callback came from a browser that did not start it")
        if error or not code:
            raise LoginError(
                "identity.login_failed", f"the identity provider refused the login: {(error or 'no code')[:200]}"
            )
        claims = self.oidc.exchange(code=code, code_verifier=started.code_verifier, nonce=started.nonce)
        return FinishedLogin(
            issuer=str(claims["iss"]),
            subject=str(claims["sub"]),
            email=claims.get("email") if claims.get("email_verified") is True else None,
            name=claims.get("name"),
            return_to=started.return_to,
            tenant_id=started.tenant_id,
            bootstrap_hash=started.bootstrap_hash,
        )


#: Axentra's login error codes -> Nodestro's (and their HTTP status).
_AXENTRA_LOGIN_ERRORS: dict[str, tuple[str, int]] = {
    "login_expired": ("identity.login_expired", 400),
    "access_denied": ("identity.login_failed", 400),
    "login_failed": ("identity.login_failed", 400),
    "email_not_verified": ("identity.login_failed", 400),
    "domain_not_allowed": ("identity.login_failed", 400),
    "signup_disabled": ("identity.login_failed", 400),
    "provider_unavailable": ("identity.provider", 400),
    "account_disabled": ("identity.account_disabled", 403),
    "rate_limited": ("identity.rate_limited", 429),
}
#: The provider id of Nodestro's identity provider in Axentra.
AXENTRA_PROVIDER = "nodestro"


class AxentraLoginFlow(LoginFlow):
    """Axentra's :class:`~axentra.identity.oidc.OidcService` behind Nodestro's routes, cookies and error codes.

    Axentra stores the login state (its nonce, PKCE verifier and Nodestro's context, encrypted), consumes it once,
    checks the browser binding, exchanges the code, verifies the ID token and finds or creates the Axentra user of
    the identity ``(issuer, subject)`` (never linked by e-mail; a user without a verified address is allowed, as in
    Nodestro). It starts no session: Nodestro starts one in the chosen tenant (:meth:`IdentityStore.create_session`
    writes both sides)."""

    def __init__(self, service: Any, provider_id: str = AXENTRA_PROVIDER) -> None:
        self.service = service
        self.provider_id = provider_id

    @staticmethod
    def _error(exc: Any) -> LoginError:
        code, status = _AXENTRA_LOGIN_ERRORS.get(exc.code, ("identity.login_failed", 400))
        return LoginError(code, f"{exc.code}: {exc}", status)

    def start(
        self, *, return_to: str, tenant_id: str | None, bootstrap_token: str | None, ip: str | None
    ) -> StartedLogin:
        from axentra.identity.oidc import OidcLoginError

        context = {}
        if tenant_id is not None:
            context["tenant"] = tenant_id
        if bootstrap_token:
            # Only the hash, as in Nodestro's own login states: the setup token itself is never stored.
            context["bootstrap"] = hashlib.sha256(bootstrap_token.encode("utf-8")).hexdigest()
        try:
            started = self.service.start(self.provider_id, return_to=return_to, ip=ip, context=context)
        except OidcLoginError as exc:
            raise self._error(exc) from None
        return StartedLogin(url=started.authorization_url, state=started.state)

    def finish(
        self, *, state: str, code: str, error: str | None, bound: str, ip: str | None, user_agent: str
    ) -> FinishedLogin:
        from axentra.identity.oidc import OidcLoginError

        try:
            result = self.service.finish(
                self.provider_id,
                state=state,
                code=code or None,
                error=error,
                browser_state=bound,
                ip=ip,
                user_agent=user_agent,
                start_session=False,
            )
        except OidcLoginError as exc:
            raise self._error(exc) from None
        found = result.identity
        bootstrap = result.context.get("bootstrap")
        return FinishedLogin(
            issuer=found.provider,
            subject=found.subject,
            email=found.email or None,
            name=found.name or None,
            return_to=safe_return_path(result.return_to),
            tenant_id=result.context.get("tenant"),
            bootstrap_hash=bytes.fromhex(bootstrap) if bootstrap else None,
            account=result.user_id,
        )


def axentra_login_flow(identity: Any, settings: OidcSettings, *, transport: Any | None = None) -> AxentraLoginFlow:
    """The :class:`AxentraLoginFlow` of an :class:`~nodestro_runtime.axentra_identity.AxentraIdentityStore`: one
    Axentra provider entry built from Nodestro's OIDC settings (same issuer, client, callback URL and scopes;
    sign-up of unknown identities on, never linked by e-mail, users without a verified address allowed: exactly
    Nodestro's rules). ``transport`` replaces the network in tests."""
    if not secure_url(settings.issuer):
        raise ValueError("The identity provider must be reached over https (http only on localhost)")
    from axentra.config.settings import OidcProviderSettings
    from axentra.identity.oidc import OidcService

    platform = identity.platform
    # Validated from a mapping: Axentra's later settings declare fields with a positional ``Field`` default (e.g.
    # ``id_token_max_age_seconds``), which type checkers read as required keyword arguments.
    provider = OidcProviderSettings.model_validate(
        {
            "id": AXENTRA_PROVIDER,
            "name": "Nodestro",
            "issuer": settings.issuer,
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "scopes": list(settings.scopes),
            "allow_signup": True,
            "allow_signup_without_email": True,
            "link_by_verified_email": False,
        }
    )
    service = OidcService(
        platform.db,
        platform.settings.model_copy(update={"oidc": [provider]}),
        platform.identity,
        platform.secrets,
        redirect_uri=lambda _provider: settings.redirect_uri,
    )
    if transport is not None:
        service.use_transport(transport)
    return AxentraLoginFlow(service)


# ---- sessions and principals ---------------------------------------------------------------------


class SessionResolver:
    """The principal of a request from its session cookie (``None``: not logged in)."""

    def __init__(self, identity: IdentityStore) -> None:
        self.identity = identity

    def __call__(self, connection: HTTPConnection) -> Principal | None:
        token = connection.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        try:
            session = self.identity.session(token)
        except NodestroError as exc:
            if exc.code == "identity.tenant_suspended":
                raise AccessError(403, exc.code, "This tenant is suspended") from None
            raise
        if session is None:
            return None
        if session.permissions is None:
            permissions = ROLE_PERMISSIONS[session.role]
        else:  # decided by the identity backend (axentra mode: Axentra's role definitions)
            permissions = frozenset(Permission(p) for p in session.permissions) & _TENANT_PERMISSIONS
        return Principal(id=session.user_id, permissions=permissions, tenant_id=session.tenant_id, role=session.role)


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

    def items(self) -> list[tuple[str, Any]]:
        """``(tenant id, context)`` of every opened context."""
        with self._lock:
            return list(self._open.items())

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
        "identity.tenant_suspended": "This tenant is suspended",
        "identity.account_disabled": "This account is disabled",
        "identity.rate_limited": "Too many logins were started; please wait a minute",
    }
    return JSONResponse({"detail": messages.get(code, code), "code": code, "params": {}}, status_code=status)


#: Logins one address may start per minute in one API process (``NODESTRO_LOGIN_STARTS_PER_MINUTE``, ``0``: off);
#: the same budget as Axentra's own login flow (API-002). Behind a proxy the address is the proxy's unless the
#: operator sets uvicorn's ``FORWARDED_ALLOW_IPS``; the RUNBOOK's proxy rate limit stays the first line.
LOGIN_STARTS_PER_MINUTE = 60
#: The longest setup token, login state and authorization code a login request may carry (API-002).
MAX_SETUP_TOKEN = 256
MAX_LOGIN_STATE = 512
MAX_AUTH_CODE = 4096


def login_starts_per_minute() -> int:
    raw = os.environ.get("NODESTRO_LOGIN_STARTS_PER_MINUTE", "").strip()
    try:
        return max(0, int(raw)) if raw else LOGIN_STARTS_PER_MINUTE
    except ValueError:
        return LOGIN_STARTS_PER_MINUTE


class LoginStartLimit:
    """At most ``limit`` login starts per address in a sliding minute, in this process (``0``: no limit)."""

    def __init__(self, limit: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self._clock = clock
        self._lock = threading.Lock()
        self._starts: dict[str, list[float]] = {}

    def allow(self, address: str | None) -> bool:
        if self.limit <= 0:
            return True
        now = self._clock()
        with self._lock:
            if len(self._starts) > 10_000:  # forget quiet addresses so the table stays small
                self._starts = {key: hits for key, hits in self._starts.items() if hits and now - hits[-1] < 60}
            hits = [hit for hit in self._starts.get(address or "", []) if now - hit < 60]
            if len(hits) >= self.limit:
                self._starts[address or ""] = hits
                return False
            hits.append(now)
            self._starts[address or ""] = hits
            return True


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


#: Records one access audit event in a tenant's trail (``tenant id, event``; API-004); never raises.
AuditSink = Callable[[str, Any], None]


def auth_router(identity: IdentityStore, oidc: OidcClient | LoginFlow, audit: AuditSink | None = None) -> APIRouter:
    """``/api/auth``: start a login, the provider's callback, the current session, switching tenants, logout.

    ``oidc`` is the login flow (an :class:`OidcClient` is Nodestro's own flow, :class:`NodestroLoginFlow`)."""
    flow = oidc if isinstance(oidc, LoginFlow) else NodestroLoginFlow(identity, oidc)
    router = APIRouter(prefix="/api/auth")
    lifetime = int(identity.session_lifetime.total_seconds())
    login_lifetime = int(identity.login_lifetime.total_seconds())
    starts = LoginStartLimit(login_starts_per_minute())

    def audited(tenant_id: str, kind: str, actor: str, *, outcome: str = "success", **details: Any) -> None:
        if audit is None:
            return
        from .access_audit import AccessEvent

        event = AccessEvent.model_validate(
            {
                "kind": kind,
                "actor": actor,
                "target_type": "session",
                "target_id": actor,
                "outcome": outcome,
                "details": details,
            }
        )
        audit(tenant_id, event)

    @router.get("/login", dependencies=PUBLIC, include_in_schema=False)
    def login(
        request: Request, return_to: str | None = None, tenant: str | None = None, setup: str | None = None
    ) -> Response:
        if tenant is not None and not TENANT_ID.fullmatch(tenant):
            tenant = None
        if setup is not None and len(setup) > MAX_SETUP_TOKEN:
            return _login_error("identity.bootstrap_invalid")
        if not starts.allow(_client_ip(request)):
            return _login_error("identity.rate_limited", 429)
        try:
            started = flow.start(
                return_to=safe_return_path(return_to),
                tenant_id=tenant,
                bootstrap_token=setup or None,
                ip=_client_ip(request),
            )
        except LoginError as exc:
            logger.warning("Login could not start: %s", exc)
            return _login_error(exc.code, 429 if exc.status == 429 else 502)
        answer = RedirectResponse(started.url, status_code=303, headers={"Cache-Control": "no-store"})
        set_login_cookie(answer, started.state, login_lifetime)
        return answer

    @router.get("/callback", dependencies=PUBLIC, include_in_schema=False)
    def callback(request: Request, state: str = "", code: str = "", error: str | None = None) -> Response:
        answer = _callback(request, state, code, error)
        clear_login_cookie(answer)
        return answer

    def _callback(request: Request, state: str, code: str, error: str | None) -> Response:
        if len(state) > MAX_LOGIN_STATE:
            return _login_error("identity.login_expired")
        if len(code) > MAX_AUTH_CODE or (error is not None and len(error) > 200):
            return _login_error("identity.login_failed")
        try:
            finished = flow.finish(
                state=state,
                code=code,
                error=error,
                bound=request.cookies.get(LOGIN_COOKIE, ""),
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:400],
            )
        except LoginError as exc:
            logger.warning("Login failed: %s", exc)
            return _login_error(exc.code, exc.status)
        user = identity.login(finished.issuer, finished.subject, email=finished.email, name=finished.name)
        if finished.account is not None:
            link = getattr(identity, "link", None)
            linked = link.lookup("user", user.id) if link is not None else None
            if linked != finished.account:
                logger.error("Login of %s: Axentra found another user than Nodestro's link", user.id)
                return _login_error("identity.login_failed")
        try:
            if finished.bootstrap_hash is not None:
                tenant_id = identity.use_bootstrap(finished.bootstrap_hash, user.id)
            else:
                memberships = identity.memberships(user.id)
                if not memberships:
                    return _login_error("identity.no_tenant", 403)
                active = {item.tenant_id for item in memberships if item.tenant_status == "active"}
                if finished.tenant_id in active:
                    tenant_id = str(finished.tenant_id)
                elif not active or finished.tenant_id in {item.tenant_id for item in memberships}:
                    # Only suspended tenants, or the one asked for is suspended.
                    return _login_error("identity.tenant_suspended", 403)
                else:
                    tenant_id = sorted(active)[0]
            token = identity.create_session(user.id, tenant_id)
        except NodestroError as exc:
            return _login_error(exc.code, 403)
        previous = request.cookies.get(SESSION_COOKIE)
        if previous:
            identity.end_session(previous)  # rotation: the session this browser held before ends with the login
        audited(tenant_id, "login", user.id, setup_link=finished.bootstrap_hash is not None)
        answer = RedirectResponse(finished.return_to, status_code=303, headers={"Cache-Control": "no-store"})
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
            # Suspended tenants are closed: they are not offered.
            tenants={
                item.tenant_id: item.role
                for item in identity.memberships(principal.id)
                if item.tenant_status == "active"
            },
        )

    @router.post("/switch", dependencies=READ)
    def switch(body: SwitchIn, request: Request) -> Response:
        principal: Principal = request.state.principal
        try:
            token = identity.create_session(principal.id, body.tenant_id)
        except NodestroError as exc:
            # Recorded in the tenant the user is in now; the tenant asked for only when it is a valid id.
            asked = body.tenant_id if TENANT_ID.fullmatch(body.tenant_id) else None
            audited(principal.tenant_id, "tenant_switched", principal.id, outcome="denied", code=exc.code, to=asked)
            if exc.code == "identity.tenant_suspended":  # only for a member (the membership is checked first)
                return _login_error(exc.code, 403)
            # The same answer for unknown tenants and tenants of others: nothing about them is revealed.
            return _login_error("identity.not_a_member", 404)
        audited(body.tenant_id, "tenant_switched", principal.id, came_from=principal.tenant_id)
        identity.end_session(request.cookies.get(SESSION_COOKIE, ""))
        answer = Response(status_code=204)
        set_session_cookie(answer, token, lifetime)
        return answer

    @router.post("/logout", dependencies=PUBLIC)
    def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            try:
                session = identity.session(token)  # the logout is public: read whose session ends first
            except NodestroError:
                session = None
            identity.end_session(token)
            if session is not None:
                audited(session.tenant_id, "logout", session.user_id)
        answer = Response(status_code=204)
        clear_session_cookie(answer)
        return answer

    return router


__all__ = [
    "ALGORITHMS",
    "AXENTRA_PROVIDER",
    "LOGIN_COOKIE",
    "ROLE_PERMISSIONS",
    "SESSION_COOKIE",
    "AxentraLoginFlow",
    "FinishedLogin",
    "LoginError",
    "LoginFlow",
    "Me",
    "NodestroLoginFlow",
    "OidcClient",
    "OidcSettings",
    "RedactAuthSecrets",
    "SameOriginMiddleware",
    "SessionResolver",
    "StartedLogin",
    "TenantWorkspaces",
    "auth_router",
    "axentra_login_flow",
    "clear_login_cookie",
    "clear_session_cookie",
    "redact_auth_logs",
    "safe_return_path",
    "set_login_cookie",
    "set_session_cookie",
]
