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
import logging
import mimetypes
from pathlib import Path
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .app_contract import form_fields, ui_form_fields
from .serving import ApiError, KeyAuthenticatedRoute, KeyIdentity, parse_token, read_body

if TYPE_CHECKING:
    from .cross_origin import CrossOrigin
    from .output_folder import OutputFolder
    from .pairing import Pairing
    from .standalone import StandaloneHost

logger = logging.getLogger(__name__)

#: The interface embedded in this package (``scripts/vendor_app_ui.py``).
WEBUI_DIR = Path(__file__).resolve().parent / "webui"
#: A newer interface the operator of a portable package installed (``ui install``, NAL-C3), checked by the launcher.
_installed: Path | None = None


def use_ui_dir(folder: Path | None) -> None:
    """Serve the interface in ``folder`` instead of the embedded one (``None``: the embedded one again)."""
    global _installed
    _installed = folder


def ui_dir() -> Path:
    """The folder of the interface this process serves."""
    return _installed if _installed is not None else WEBUI_DIR


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
#: from content, no frames, no plugins. Trusted Types (NAL-C5): the interface writes no HTML from strings at all.
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; font-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types 'none'"
)
_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
#: Body limits of the sign-in and of the provider setup (``413 request.too_large`` above).
_SIGN_IN_BYTES = 4096
_SETUP_BYTES = 16384


@dataclass
class UiSession:
    identity: KeyIdentity
    operator: bool
    #: The paired device the session belongs to (NAL-C4): it ends as soon as the device is revoked or expires.
    device_id: str | None = None
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

    def create(self, identity: KeyIdentity, *, operator: bool, device_id: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda key: self._sessions[key].seen)
                del self._sessions[oldest]
            self._sessions[self._key(token)] = UiSession(identity=identity, operator=operator, device_id=device_id)
        return token

    def drop_device(self, device_id: str | None) -> None:
        """End every session of one paired device (or of every device: ``None``)."""
        with self._lock:
            for key in [
                key
                for key, item in self._sessions.items()
                if item.device_id is not None and (device_id is None or item.device_id == device_id)
            ]:
                del self._sessions[key]

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
    #: How the application serves TLS (NAL-C4): ``certificate`` (own), ``proxy``, ``self_signed`` or ``none``.
    tls_mode: str = "none"
    #: The SHA-256 fingerprint of a self-issued certificate (``tls_mode`` ``self_signed``), for the person to compare.
    tls_fingerprint: str | None = None


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
        pairing: Pairing | None = None,
        cross_origin: CrossOrigin | None = None,
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
        #: Pairing by code (NAL-C4); ``None`` unless the launcher switched it on (``NODESTRO_PAIRING``).
        self.pairing = pairing
        #: The interface from other origins (NAL-C5); ``None`` unless ``NODESTRO_CROSS_ORIGIN`` and pairing are on.
        self.cross_origin = cross_origin if pairing is not None else None

    # ---- identities -------------------------------------------------------------------------------------------

    def operator_identity(self) -> KeyIdentity:
        return KeyIdentity(
            id="operator",
            name="Operator",
            scopes=["run", "read", "approve"],
            publications=[self.host.served.publication_id],
        )

    def session_of(self, request: Request) -> UiSession | None:
        token = request.cookies.get(COOKIE)
        session = self.sessions.get(token)
        # A revoked or expired device loses its access at once, not when its session ends.
        if (
            session is not None
            and session.device_id is not None
            and (self.pairing is None or self.pairing.device(session.device_id) is None)
        ):
            self.sessions.drop(token)
            return None
        return session

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
            else {
                "scopes": session.identity.scopes,
                "operator": session.operator,
                "name": session.identity.name,
                "device": session.device_id is not None,
            },
            "pairing": self.pairing is not None,
            "tls": {"mode": self.options.tls_mode, "fingerprint": self.options.tls_fingerprint},
        }

    def config(self, session: UiSession) -> dict[str, Any]:
        """The workflow's form, result schema, limits and (for the operator) where files go."""
        served = self.host.served
        # The app contract's form mapping (the same as ``InfoOut.form``), under the names this interface reads.
        fields = ui_form_fields(form_fields(served.workflow, served.limits.max_upload_mb))
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
    folder = ui_dir().resolve()
    target = (folder / Path(*parts)).resolve()
    if folder not in target.parents or not target.is_file() or target.suffix not in _TYPES:
        return None
    if target.suffix == ".html":
        # The page itself is served only at ``/``, with its Content Security Policy (not as a plain asset).
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


#: The web interface's routes, which check access in their handlers (no principal: an export has no logins), with
#: how; :func:`nodestro_api.permissions.check_routes` accepts exactly these without a declared requirement (MUP-ONA
#: A5). A new route of :func:`build_router` must be added here, or the application is not built.
WEB_UI_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/"): "public: the interface's page (no data; the API calls need a session)",
    ("GET", "/ui/assets/{path:path}"): "public: the interface's static files",
    ("GET", "/ui/launch"): "the operator's one-time launch token (UiSessions.use_launch_token)",
    ("GET", "/ui/api/bootstrap"): "public: what the sign-in page needs (language, whether a session exists)",
    ("POST", "/ui/api/session"): "signs in with an API key (verified; failures counted per address)",
    ("DELETE", "/ui/api/session"): "ends the session of the request's own cookie (same origin)",
    ("GET", "/ui/api/config"): "session (WebUi.require)",
    ("GET", "/ui/api/setup"): "operator session (WebUi.require(operator=True))",
    ("PUT", "/ui/api/setup/providers/{provider}"): "operator session (WebUi.require(operator=True))",
    ("DELETE", "/ui/api/setup/providers/{provider}"): "operator session (WebUi.require(operator=True))",
    ("PUT", "/ui/api/setup/output"): "operator session (WebUi.require(operator=True))",
    ("DELETE", "/ui/api/setup/output"): "operator session (WebUi.require(operator=True))",
    ("GET", "/ui/api/setup/output/browse"): "operator session (WebUi.require(operator=True))",
    ("POST", "/ui/api/setup/output/folders"): "operator session (WebUi.require(operator=True))",
    ("POST", "/ui/api/setup/output/open"): "operator session (WebUi.require(operator=True))",
    ("POST", "/ui/control/stop"): "the launcher's control token (UiOptions.control_token)",
    ("POST", "/ui/control/launch"): "the launcher's control token (UiOptions.control_token)",
    ("POST", "/ui/api/pair"): "redeems a one-time pairing code (NAL-C4; TLS or loopback, attempts limited)",
    ("POST", "/ui/api/device/session"): "renews a session from the rotating device cookie (NAL-C4)",
    ("DELETE", "/ui/api/device"): "a paired device's session removes its own device (NAL-C4)",
    ("POST", "/ui/control/pair"): "the launcher's control token (UiOptions.control_token)",
    ("GET", "/ui/control/devices"): "the launcher's control token (UiOptions.control_token)",
    ("POST", "/ui/control/devices/revoke"): "the launcher's control token (UiOptions.control_token)",
    ("POST", "/api/app/token"): "redeems a pairing code from an allowed origin for an access token (NAL-C5)",
    ("POST", "/api/app/token/refresh"): "renews an access token from the rotating refresh cookie (NAL-C5)",
    ("DELETE", "/api/app/token"): "an access token revokes its own device (NAL-C5)",
}


def build_router(ui: WebUi) -> APIRouter:
    """The interface's pages and endpoints (``/``, ``/ui/...``); the workflow itself runs through the published API."""
    router = APIRouter(include_in_schema=False, route_class=KeyAuthenticatedRoute)

    def json_answer(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    @router.get("/")
    def index() -> Response:
        page = ui_dir() / "index.html"
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
            page = ui_dir() / "index.html"
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

    _pairing_routes(router, ui, json_answer, control)
    _token_routes(router, ui, json_answer)
    return router


def _secure(request: Request) -> bool:
    """The request reached the application over TLS (its own or a proxy's), or comes from this computer."""
    client = request.client.host if request.client else ""
    return request.url.scheme == "https" or client in ("127.0.0.1", "::1")


def _pairing_routes(
    router: APIRouter, ui: WebUi, json_answer: Callable[..., JSONResponse], control: Callable[[Request], None]
) -> None:
    """Pairing by code and the device rights it creates (NAL-C4, ``docs/pairing.md``)."""
    from . import pairing as pairing_module

    def pairing_on() -> Pairing:
        if ui.pairing is None:
            raise ApiError(404, "pairing.disabled", "Pairing is not switched on for this application")
        return ui.pairing

    def device_identity(device: pairing_module.Device) -> KeyIdentity:
        # An app user of this package's app with the code's scopes: never the operator.
        return KeyIdentity(
            id=device.id, name=device.name, scopes=device.scopes, publications=[ui.host.served.publication_id]
        )

    def signed_in(device: pairing_module.Device, secret: str) -> JSONResponse:
        answer = json_answer(
            {"device": {"id": device.id, "name": device.name, "scopes": device.scopes}, "operator": False}
        )
        ui._cookie(answer, ui.sessions.create(device_identity(device), operator=False, device_id=device.id))
        answer.set_cookie(
            pairing_module.DEVICE_COOKIE,
            f"{device.id}.{secret}",
            httponly=True,
            samesite="strict",
            secure=ui.options.secure_cookies,
            path=pairing_module.DEVICE_PATH,
            max_age=pairing_module.DEVICE_DAYS * 86400,
        )
        return answer

    def refused(exc: pairing_module.PairingError) -> ApiError:
        return ApiError(exc.status, exc.key, str(exc))

    @router.post("/ui/api/pair")
    async def pair(request: Request) -> JSONResponse:
        found = pairing_on()
        check_same_origin(request)
        if not _secure(request):
            raise ApiError(403, "pairing.tls_required", "Pairing needs an HTTPS connection")
        try:
            body = json.loads(await read_body(request, _SIGN_IN_BYTES) or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        client = request.client.host if request.client else "unknown"
        try:
            device, secret = found.redeem(
                str(body.get("code") or ""),
                address=client,
                name=str(body.get("name") or ""),
                agent=request.headers.get("user-agent", ""),
            )
        except pairing_module.PairingError as exc:
            if ui.auth_failed is not None and exc.key == "pairing.code_invalid":
                ui.auth_failed(client)
            raise refused(exc) from None
        logger.info("Paired device %s", pairing_module.short(device.id))
        return signed_in(device, secret)

    @router.post("/ui/api/device/session")
    def renew(request: Request) -> JSONResponse:
        found = pairing_on()
        check_same_origin(request)
        if not _secure(request):
            raise ApiError(403, "pairing.tls_required", "Pairing needs an HTTPS connection")
        try:
            # Only a device of this interface (no origin): one paired from another origin renews there (NAL-C5).
            device, secret = found.renew(request.cookies.get(pairing_module.DEVICE_COOKIE, ""), origin=None)
        except pairing_module.PairingError as exc:
            answer = json_answer({"error": {"status": exc.status, "code": exc.key, "message": str(exc)}}, exc.status)
            answer.delete_cookie(pairing_module.DEVICE_COOKIE, path=pairing_module.DEVICE_PATH)
            return answer
        ui.sessions.drop(request.cookies.get(COOKIE))
        return signed_in(device, secret)

    @router.delete("/ui/api/device")
    def forget(request: Request) -> JSONResponse:
        found = pairing_on()
        session = ui.require(request)
        if session.device_id is None:
            raise ApiError(403, "pairing.not_a_device", "This browser is not signed in as a paired device")
        found.revoke(session.device_id)
        ui.sessions.drop_device(session.device_id)
        answer = json_answer({"removed": True})
        answer.delete_cookie(COOKIE, path="/")
        answer.delete_cookie(pairing_module.DEVICE_COOKIE, path=pairing_module.DEVICE_PATH)
        return answer

    @router.post("/ui/control/pair")
    async def new_code(request: Request) -> JSONResponse:
        control(request)
        found = pairing_on()
        try:
            body = json.loads(await read_body(request, _SIGN_IN_BYTES) or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        scopes = body.get("scopes")
        try:
            code = found.create_code(
                name=str(body.get("name") or ""),
                scopes=[str(item) for item in scopes] if isinstance(scopes, list) else None,
                minutes=int(body.get("minutes") or pairing_module.CODE_MINUTES),
            )
        except (pairing_module.PairingError, ValueError, TypeError) as exc:
            if isinstance(exc, pairing_module.PairingError):
                raise refused(exc) from None
            raise ApiError(400, "pairing.scopes_invalid", "Invalid pairing options") from None
        return json_answer(
            {
                **code.model_dump(mode="json"),
                "display": pairing_module.display(code.code),
                "tls": {"mode": ui.options.tls_mode, "fingerprint": ui.options.tls_fingerprint},
            }
        )

    @router.get("/ui/control/devices")
    def devices(request: Request) -> JSONResponse:
        control(request)
        return json_answer({"devices": [item.public() for item in pairing_on().devices()]})

    @router.post("/ui/control/devices/revoke")
    async def revoke(request: Request) -> JSONResponse:
        control(request)
        found = pairing_on()
        try:
            body = json.loads(await read_body(request, _SIGN_IN_BYTES) or b"{}")
        except ValueError:
            body = {}
        target = str(body.get("id") or "") if isinstance(body, dict) else ""
        origin = str(body.get("origin") or "") if isinstance(body, dict) else ""
        every = isinstance(body, dict) and body.get("all") is True
        if origin:
            # The operator removed an origin from the allowed ones (NAL-C5): its devices end with it.
            count = (
                ui.cross_origin.revoke_origin(origin) if ui.cross_origin is not None else found.revoke_origin(origin)
            )
            return json_answer({"revoked": count})
        if not target and not every:
            raise ApiError(400, "pairing.device_required", "Name a device or all")
        count = found.revoke(None if every else target)
        ui.sessions.drop_device(None if every else target)
        if ui.cross_origin is not None:
            ui.cross_origin.tokens.drop_device(None if every else target)
        return json_answer({"revoked": count})


def _token_routes(router: APIRouter, ui: WebUi, json_answer: Callable[..., JSONResponse]) -> None:
    """The token profile for the interface on another origin (NAL-C5, ``docs/cross-origin.md``)."""
    from . import cross_origin as cross
    from . import pairing as pairing_module

    def profile_on(request: Request) -> tuple[CrossOrigin, str]:
        """The cross-origin profile and the request's allowed origin, or the refusal."""
        found = ui.cross_origin
        if found is None:
            raise ApiError(404, "token.disabled", "Access from other origins is not switched on for this application")
        origin = cross.request_origin(request.headers)
        if origin is None or cross.same_origin(request.headers) or not found.origins.allowed(origin):
            raise ApiError(403, "cors.origin_not_allowed", "This origin may not use this backend")
        if request.headers.get(cross.APP_HEADER) != "1":
            raise ApiError(403, "token.header_required", "This request must come from the app interface")
        if not _secure(request):
            raise ApiError(403, "pairing.tls_required", "Pairing needs an HTTPS connection")
        return found, origin

    def name() -> str:
        instance = getattr(ui.host, "instance", None)
        return cross.cookie_name(instance().id) if instance is not None else cross.REFRESH_COOKIE

    def issued(found: CrossOrigin, device: pairing_module.Device, secret: str, origin: str) -> JSONResponse:
        identity = KeyIdentity(
            id=device.id, name=device.name, scopes=device.scopes, publications=[ui.host.served.publication_id]
        )
        app = ui.config(UiSession(identity=identity, operator=False, device_id=device.id))
        answer = json_answer(
            {
                "access_token": found.tokens.issue(device.id, origin),
                "token_type": "Bearer",
                "expires_in": cross.ACCESS_SECONDS,
                "device": {"id": device.id, "name": device.name, "scopes": device.scopes},
                "app": app,
            }
        )
        answer.headers.append(
            "set-cookie",
            cross.refresh_cookie(f"{device.id}.{secret}", max_age=pairing_module.DEVICE_DAYS * 86400, name=name()),
        )
        return answer

    def cleared(body: dict[str, Any], status: int = 200) -> JSONResponse:
        answer = json_answer(body, status)
        answer.headers.append("set-cookie", cross.refresh_cookie("", max_age=0, name=name()))
        return answer

    @router.post(cross.TOKEN_PATH)
    async def grant(request: Request) -> JSONResponse:
        found, origin = profile_on(request)
        try:
            body = json.loads(await read_body(request, _SIGN_IN_BYTES) or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict) or body.get("grant_type") != "pairing_code":
            raise ApiError(400, "token.grant_invalid", "The grant type is pairing_code")
        client = request.client.host if request.client else "unknown"
        try:
            device, secret = found.pairing.redeem(
                str(body.get("code") or ""),
                address=client,
                name=str(body.get("name") or ""),
                agent=request.headers.get("user-agent", ""),
                origin=origin,
            )
        except pairing_module.PairingError as exc:
            if ui.auth_failed is not None and exc.key == "pairing.code_invalid":
                ui.auth_failed(client)
            raise ApiError(exc.status, exc.key, str(exc)) from None
        logger.info("Paired device %s from another origin", pairing_module.short(device.id))
        return issued(found, device, secret, origin)

    @router.post(cross.REFRESH_PATH)
    def refresh(request: Request) -> JSONResponse:
        found, origin = profile_on(request)
        cookie = request.cookies.get(name(), "")
        try:
            device, secret = found.pairing.renew(cookie, origin=origin)
        except pairing_module.PairingError as exc:
            # A browser that never paired asks on every load: only a cookie that was sent and refused counts.
            if ui.auth_failed is not None and cookie:
                ui.auth_failed(request.client.host if request.client else "unknown")
            return cleared({"error": {"status": exc.status, "code": exc.key, "message": str(exc)}}, exc.status)
        return issued(found, device, secret, origin)

    @router.delete(cross.TOKEN_PATH)
    def revoke(request: Request) -> JSONResponse:
        found, origin = profile_on(request)
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        device = found.device_of(token, origin) if token else None
        if device is None:
            raise ApiError(401, "auth.invalid", "The access token is invalid or expired")
        found.pairing.revoke(device.id)
        found.tokens.drop_device(device.id)
        logger.info("Device %s removed itself", pairing_module.short(device.id))
        return cleared({"removed": True})


def guess_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


__all__ = [
    "COOKIE",
    "LANGUAGES",
    "WEB_UI_ROUTES",
    "UiOptions",
    "UiSessions",
    "WebUi",
    "build_router",
    "check_same_origin",
]
