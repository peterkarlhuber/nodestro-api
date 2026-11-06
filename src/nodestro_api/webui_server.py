"""The web interface of an exported workflow: one generic static app, its sign-in and the operator's setup.

An exported project can carry a web interface (``nodestro_api/webui/``: HTML, CSS, JavaScript, translations and
the Nodestro logos; no build step, no CDN, nothing loaded from elsewhere). The same files serve every workflow:
the app reads the workflow's form, its result schema and its limits from :func:`build_router`'s ``/ui/api/config``
and runs it through the published API of the project (``/api/published/<slug>/...``), the same endpoints and
checks as any API client.

Signing in never puts a key into the page:

- **The operator** (the person who started the application on this computer) opens the app through a one-time
  launch link the launcher creates (``/ui/launch?token=...``). The answer sets a session cookie and moves on to
  ``/``; a second use of the link is refused. The operator's session may run, read and approve, sees where files
  are stored, may choose another result folder (:mod:`nodestro_api.output_folder`) and may enter the AI providers'
  keys (:mod:`nodestro_api.portable_secrets`), which are written to the computer and never sent back to a browser.
- **Other users** sign in with one of the project's API keys (``POST /ui/api/session``). The key is checked once and
  exchanged for a session with that key's permissions; the browser keeps only the cookie.

Sessions are random 256-bit tokens kept in memory (a restart signs everybody out), in an ``HttpOnly``,
``SameSite=Strict`` cookie (``Secure`` behind HTTPS), idle after 8 hours, at most 24 hours. Requests that change
something with a session cookie must carry ``X-Nodestro-UI: 1`` and, if the browser sends an ``Origin``, come from
this host: a page of another site can neither read the answers (no CORS) nor send that header without a preflight
that is never allowed. Bearer requests of API clients are unaffected. Failed sign-ins count towards the same limit as
invalid API keys.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from nodestro_runtime.engine import start_fields
from nodestro_runtime.file_formats import accept_extensions
from pydantic import BaseModel, Field

from .serving import ApiError, KeyAuthenticatedRoute, KeyIdentity, parse_token, read_body

if TYPE_CHECKING:
    from .output_folder import OutputFolder
    from .standalone import StandaloneHost

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
LANGUAGES = ("de", "en", "es", "fr", "pt-BR", "it")
COOKIE = "nodestro_app_session"
HEADER = "x-nodestro-ui"
IDLE_SECONDS = 8 * 3600
MAX_SECONDS = 24 * 3600
MAX_SESSIONS = 256
#: Static files the app may load (anything else under ``webui/`` is never served).
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}
#: The app's Content Security Policy: only its own files; images also from ``blob:`` (previews of files the user
#: chose or downloaded through the API) and ``data:`` (inline icons). No inline script, no inline style attributes
#: from content, no frames, no plugins.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; font-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
#: Body limits of the sign-in and of the provider setup (``413 request.too_large`` above).
_SIGN_IN_BYTES = 4096
_SETUP_BYTES = 16384


@dataclass
class UiSession:
    identity: KeyIdentity
    operator: bool
    created: float = field(default_factory=time.monotonic)
    seen: float = field(default_factory=time.monotonic)


class UiSessions:
    """Browser sessions of this process (lost on restart)."""

    def __init__(self) -> None:
        self._sessions: dict[str, UiSession] = {}
        self._launch: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, identity: KeyIdentity, *, operator: bool) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda key: self._sessions[key].seen)
                del self._sessions[oldest]
            self._sessions[self._key(token)] = UiSession(identity=identity, operator=operator)
        return token

    def get(self, token: str | None) -> UiSession | None:
        if not token:
            return None
        moment = time.monotonic()
        with self._lock:
            found = self._sessions.get(self._key(token))
            if found is None:
                return None
            if moment - found.seen > IDLE_SECONDS or moment - found.created > MAX_SECONDS:
                del self._sessions[self._key(token)]
                return None
            found.seen = moment
            return found

    def drop(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(self._key(token), None)

    def add_launch_token(self, token: str, *, ttl: float = 600.0) -> None:
        """A one-time launch link for the operator, valid for ``ttl`` seconds."""
        with self._lock:
            self._launch[self._key(token)] = time.monotonic() + ttl

    def use_launch_token(self, token: str) -> bool:
        with self._lock:
            expires = self._launch.pop(self._key(token), None)
        return expires is not None and expires >= time.monotonic()

    def _prune(self) -> None:
        moment = time.monotonic()
        for key in [key for key, item in self._sessions.items() if moment - item.seen > IDLE_SECONDS]:
            del self._sessions[key]
        for key in [key for key, expires in self._launch.items() if expires < moment]:
            del self._launch[key]


class UiOptions(BaseModel):
    """How the launcher configures the interface (none of it comes from a request)."""

    #: The language the interface starts in (the export's choice; the user can switch).
    language: str = "en"
    #: Cookies only over HTTPS (the application serves TLS itself or runs behind a TLS proxy).
    secure_cookies: bool = False
    #: The launcher's secret for ``/ui/control/*`` (stop, a new launch link); ``None``: no control endpoints.
    control_token: str | None = None
    #: Where result files are copied to, as the operator should see it (``None``: not configured).
    output_dir: str | None = None
    #: Why the configured output folder is not used (it could not be created or written).
    output_dir_warning: str | None = None
    #: What the launcher found about the local model server (state, models, hardware warnings).
    local_models: dict[str, Any] = Field(default_factory=dict)
    #: The interactive API reference (``/docs``) is served to this computer.
    api_docs: bool = False


class WebUi:
    """Sessions, sign-in and the interface's own endpoints for one :class:`StandaloneHost`."""

    def __init__(
        self,
        host: StandaloneHost,
        options: UiOptions | None = None,
        *,
        on_stop: Callable[[], None] | None = None,
        provider_status: Callable[[], list[dict[str, Any]]] | None = None,
        set_provider: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        remove_provider: Callable[[str], dict[str, Any]] | None = None,
        auth_failed: Callable[[str], None] | None = None,
        output: OutputFolder | None = None,
    ) -> None:
        """``output``: the operator may choose the result folder on the settings page (a portable package)."""
        self.host = host
        self.options = options or UiOptions()
        self.sessions = UiSessions()
        self.on_stop = on_stop
        self.provider_status = provider_status or (lambda: [])
        self.set_provider = set_provider
        self.remove_provider = remove_provider
        self.auth_failed = auth_failed
        self.output = output

    # ---- identities -------------------------------------------------------------------------------------------

    def operator_identity(self) -> KeyIdentity:
        return KeyIdentity(
            id="operator",
            name="Operator",
            scopes=["run", "read", "approve"],
            publications=[self.host.served.publication_id],
        )

    def session_of(self, request: Request) -> UiSession | None:
        return self.sessions.get(request.cookies.get(COOKIE))

    def identity(self, request: Request) -> KeyIdentity | None:
        """The identity of a request's session cookie, checked against cross-site requests (``None``: no session)."""
        session = self.session_of(request)
        if session is None:
            return None
        if request.method not in _SAFE:
            check_same_origin(request)
        return session.identity

    def require(self, request: Request, *, operator: bool = False) -> UiSession:
        session = self.session_of(request)
        if session is None:
            raise ApiError(401, "ui.session_required", "Sign in first")
        if request.method not in _SAFE:
            check_same_origin(request)
        if operator and not session.operator:
            raise ApiError(403, "ui.operator_only", "Only the operator of this computer may do this")
        return session

    def _cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=self.options.secure_cookies,
            path="/",
            max_age=MAX_SECONDS,
        )

    # ---- configuration for the app ----------------------------------------------------------------------------

    def bootstrap(self, request: Request) -> dict[str, Any]:
        """What the sign-in page needs (no session): name, languages, whether a session exists."""
        session = self.session_of(request)
        served = self.host.served
        return {
            "name": served.name,
            "description": served.description,
            "version": served.number,
            "language": self.options.language if self.options.language in LANGUAGES else "en",
            "languages": list(LANGUAGES),
            "session": None
            if session is None
            else {"scopes": session.identity.scopes, "operator": session.operator, "name": session.identity.name},
        }

    def config(self, session: UiSession) -> dict[str, Any]:
        """The workflow's form, result schema, limits and (for the operator) where files go."""
        served = self.host.served
        fields = []
        for item in start_fields(served.workflow):
            entry = item.model_dump(mode="json", exclude={"delivery"})
            if item.type == "file":
                entry["accept_extensions"] = accept_extensions(item.accept)
                entry["max_size_mb"] = item.max_size_mb or served.limits.max_upload_mb
            fields.append(entry)
        start = next((node for node in served.workflow.nodes if node.type == "start"), None)
        default_input = start.start.default_input if start is not None and start.start is not None else ""
        found: dict[str, Any] = {
            "slug": served.slug,
            "name": served.name,
            "description": served.description,
            "version": served.number,
            "fields": fields,
            "free_input": not fields,
            "default_input": default_input,
            "output_schema": served.output_schema,
            "approvals": served.approvals > 0,
            "max_upload_mb": served.limits.max_upload_mb,
            "missing_resources": served.missing_resources,
            "api_base": f"/api/published/{served.slug}",
            "providers": [
                {key: value for key, value in item.items() if key in ("id", "label", "configured", "required")}
                for item in self.provider_status()
            ],
            "output_dir": {"enabled": bool(self.output_state()["enabled"])},
        }
        if session.operator:
            found["output_dir"] = self.output_state()
            found["local_models"] = self.options.local_models
        return found

    def output_state(self) -> dict[str, Any]:
        """The result folder in effect, as the operator's settings page shows it."""
        if self.output is not None:
            return self.output.state()
        return {
            "enabled": bool(self.options.output_dir),
            "path": self.options.output_dir,
            "warning": self.options.output_dir_warning,
            "source": "settings",
            "default_path": self.options.output_dir,
            "can_change": False,
            "can_open": False,
        }


def check_same_origin(request: Request) -> None:
    """Refuse a state-changing cookie request that a page of another site could have sent."""
    if request.headers.get(HEADER) != "1":
        raise ApiError(403, "ui.csrf", "This request must come from the application's own page")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower():
        raise ApiError(403, "ui.csrf", "This request must come from the application's own page")


def _static(path: str) -> Path | None:
    """A file of ``webui/`` by its relative path, or ``None`` (no ``..``, no hidden files, known types only)."""
    parts = [part for part in path.split("/") if part]
    if not parts or any(part.startswith(".") or "\\" in part for part in parts):
        return None
    target = (WEBUI_DIR / Path(*parts)).resolve()
    if WEBUI_DIR.resolve() not in target.parents or not target.is_file() or target.suffix not in _TYPES:
        return None
    return target


def _headers(*, html: bool = False) -> dict[str, str]:
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store" if html else "no-cache",
    }
    if html:
        headers["Content-Security-Policy"] = CSP
        headers["X-Frame-Options"] = "DENY"
    return headers


def build_router(ui: WebUi) -> APIRouter:
    """The interface's pages and endpoints (``/``, ``/ui/...``); the workflow itself runs through the published API."""
    router = APIRouter(include_in_schema=False, route_class=KeyAuthenticatedRoute)

    def json_answer(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    @router.get("/")
    def index() -> Response:
        page = WEBUI_DIR / "index.html"
        if not page.is_file():
            return json_answer({"error": {"status": 404, "code": "ui.missing", "message": "No web interface"}}, 404)
        return FileResponse(page, media_type=_TYPES[".html"], headers=_headers(html=True))

    @router.get("/ui/assets/{path:path}")
    def asset(path: str) -> Response:
        target = _static(path)
        if target is None:
            return json_answer({"error": {"status": 404, "code": "not_found", "message": "Not found"}}, 404)
        return FileResponse(target, media_type=_TYPES[target.suffix], headers=_headers())

    @router.get("/ui/launch")
    def launch(request: Request) -> Response:
        token = request.query_params.get("token", "")
        if not token or not ui.sessions.use_launch_token(token):
            page = WEBUI_DIR / "index.html"
            # An old or used link: the app opens and offers sign-in with a key (nothing about the token is shown).
            return (
                RedirectResponse("/?launch=expired", status_code=303) if page.is_file() else Response(status_code=403)
            )
        answer = RedirectResponse("/", status_code=303)
        answer.headers["Referrer-Policy"] = "no-referrer"
        ui._cookie(answer, ui.sessions.create(ui.operator_identity(), operator=True))
        return answer

    @router.get("/ui/api/bootstrap")
    def bootstrap(request: Request) -> JSONResponse:
        return json_answer(ui.bootstrap(request))

    @router.post("/ui/api/session")
    async def sign_in(request: Request) -> JSONResponse:
        check_same_origin(request)
        # Read at most 4 KiB (no session yet: a large body must not be kept in memory, API3-02).
        try:
            body = json.loads(await read_body(request, _SIGN_IN_BYTES) or b"{}")
        except ValueError:
            body = {}
        token = str(body.get("api_key") or "").strip() if isinstance(body, dict) else ""
        parsed = parse_token(token)
        identity = ui.host.verify(*parsed) if parsed is not None else None
        if identity is None:
            if ui.auth_failed is not None:
                ui.auth_failed(request.client.host if request.client else "unknown")
            raise ApiError(401, "auth.invalid", "The API key is invalid or was revoked")
        answer = json_answer({"scopes": identity.scopes, "operator": False, "name": identity.name})
        ui._cookie(answer, ui.sessions.create(identity, operator=False))
        return answer

    @router.delete("/ui/api/session")
    def sign_out(request: Request) -> JSONResponse:
        check_same_origin(request)
        ui.sessions.drop(request.cookies.get(COOKIE))
        answer = json_answer({"signed_out": True})
        answer.delete_cookie(COOKIE, path="/")
        return answer

    @router.get("/ui/api/config")
    def config(request: Request) -> JSONResponse:
        session = ui.require(request)
        return json_answer(ui.config(session))

    @router.get("/ui/api/setup")
    def setup(request: Request) -> JSONResponse:
        ui.require(request, operator=True)
        return json_answer(
            {
                "providers": ui.provider_status(),
                "local_models": ui.options.local_models,
                "output_dir": seen_from(request, ui.output_state()),
                "api_docs": ui.options.api_docs,
            }
        )

    @router.put("/ui/api/setup/providers/{provider}")
    async def put_provider(request: Request, provider: str) -> JSONResponse:
        ui.require(request, operator=True)
        if ui.set_provider is None:
            raise ApiError(404, "ui.setup_unavailable", "Provider keys are configured outside this application")
        try:
            body = json.loads(await read_body(request, _SETUP_BYTES) or b"{}")
        except ValueError:
            raise ApiError(422, "request.invalid", "Send a JSON object") from None
        if not isinstance(body, dict):
            raise ApiError(422, "request.invalid", "Send a JSON object")
        return json_answer(ui.set_provider(provider, body))

    @router.delete("/ui/api/setup/providers/{provider}")
    def delete_provider(request: Request, provider: str) -> JSONResponse:
        ui.require(request, operator=True)
        if ui.remove_provider is None:
            raise ApiError(404, "ui.setup_unavailable", "Provider keys are configured outside this application")
        return json_answer(ui.remove_provider(provider))

    # The result folder (operator only; folder names, never files or their contents).

    def on_this_computer(request: Request) -> bool:
        return bool(request.client) and request.client.host in ("127.0.0.1", "::1")  # type: ignore[union-attr]

    def seen_from(request: Request, state: dict[str, Any]) -> dict[str, Any]:
        """``can_open`` only for a browser on the computer the application runs on."""
        return {**state, "can_open": bool(state.get("can_open")) and on_this_computer(request)}

    def output_folder(request: Request) -> OutputFolder:
        ui.require(request, operator=True)
        if ui.output is None:
            raise ApiError(404, "ui.setup_unavailable", "The result folder is configured outside this application")
        return ui.output

    async def json_object(request: Request) -> dict[str, Any]:
        try:
            body = json.loads(await read_body(request, _SETUP_BYTES) or b"{}")
        except ValueError:
            raise ApiError(422, "request.invalid", "Send a JSON object") from None
        if not isinstance(body, dict):
            raise ApiError(422, "request.invalid", "Send a JSON object")
        return body

    @router.put("/ui/api/setup/output")
    async def put_output(request: Request) -> JSONResponse:
        folder = output_folder(request)
        body = await json_object(request)
        return json_answer(
            seen_from(request, folder.choose(str(body.get("path") or ""), create=body.get("create") is True))
        )

    @router.delete("/ui/api/setup/output")
    def delete_output(request: Request) -> JSONResponse:
        return json_answer(seen_from(request, output_folder(request).reset()))

    @router.get("/ui/api/setup/output/browse")
    def browse_output(request: Request) -> JSONResponse:
        folder = output_folder(request)
        return json_answer(folder.browse(request.query_params.get("path") or None))

    @router.post("/ui/api/setup/output/folders")
    async def new_output_folder(request: Request) -> JSONResponse:
        folder = output_folder(request)
        body = await json_object(request)
        return json_answer(folder.make_folder(str(body.get("parent") or ""), str(body.get("name") or "")), 201)

    @router.post("/ui/api/setup/output/open")
    def open_output(request: Request) -> JSONResponse:
        folder = output_folder(request)
        # Only a browser on this computer: the file manager opens where the application runs.
        if not on_this_computer(request):
            raise ApiError(404, "output.open_unavailable", "The folder cannot be opened from here")
        folder.open()
        return json_answer({"opened": True})

    def control(request: Request) -> None:
        expected = ui.options.control_token
        client = request.client.host if request.client else ""
        given = request.headers.get("x-control-token", "")
        if not expected or client not in ("127.0.0.1", "::1") or not hmac.compare_digest(given, expected):
            raise ApiError(404, "not_found", "Not found")

    @router.post("/ui/control/stop")
    def stop(request: Request) -> JSONResponse:
        control(request)
        if ui.on_stop is not None:
            ui.on_stop()
        return json_answer({"stopping": True})

    @router.post("/ui/control/launch")
    def new_launch(request: Request) -> JSONResponse:
        control(request)
        token = secrets.token_urlsafe(32)
        ui.sessions.add_launch_token(token)
        return json_answer({"token": token})

    return router


def guess_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


__all__ = ["COOKIE", "LANGUAGES", "UiOptions", "UiSessions", "WebUi", "build_router", "check_same_origin"]
