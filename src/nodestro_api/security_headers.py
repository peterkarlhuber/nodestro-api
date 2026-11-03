"""Security headers on every answer of the studio (API2-02): Content-Security-Policy, framing, sniffing, referrer, HSTS.

:class:`SecurityHeadersMiddleware` adds them to every HTTP response that does not set them itself (the file content
routes and the launch pages keep their stricter policies). The policy, :data:`CONTENT_SECURITY_POLICY`, is what the
built studio interface and the API reference page need, found by loading them in a browser and collecting
violations:

- scripts only from the studio's origin, plus the one inline script of ``index.html`` that applies the saved colour
  theme before the first paint (allowed by its SHA-256, :data:`THEME_SCRIPT_HASH`; it goes away once the studio
  loads it from a file);
- ``style-src 'unsafe-inline'``: the code editor (CodeMirror) and the canvas (React Flow) insert ``<style>`` elements
  and ``style`` attributes at run time, and so does Swagger UI;
- images from the origin and ``data:`` URLs (icons inside the CSS of Swagger UI and the interface); downloads the
  studio builds as ``blob:`` links (traces, workflow and API exports) are navigations and need no source here;
- no plugins (``object-src 'none'``), no ``<base>`` (``base-uri 'none'``), no framing by any page
  (``frame-ancestors 'none'``, and ``X-Frame-Options: DENY`` for older browsers).

Files (answers with ``Content-Disposition``: uploads, a run's result files) keep the route's own policy (an HTML file
is sandboxed, a PDF gets none so the browser's viewer works) and ``X-Frame-Options: SAMEORIGIN``: the studio shows
them in a frame of its own pages (result file preview).

``Strict-Transport-Security`` is sent only for HTTPS: on a request that arrived over HTTPS, or always when the
application is told it is served over HTTPS (a server behind a TLS proxy, ``hsts=True``). The desktop on
``http://127.0.0.1`` never gets it.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: ``index.html``'s inline theme script (studio ``frontend/index.html``), allowed by hash.
THEME_SCRIPT_HASH = "'sha256-Dg848yCS9DqpBwZ5Cl5PMxRcmQskfCb1akYnj1gGsRo='"

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        f"script-src 'self' {THEME_SCRIPT_HASH}",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)
#: One year; without ``includeSubDomains`` (other services of the domain are not ours to decide).
HSTS = "max-age=31536000"
REFERRER_POLICY = "no-referrer"


class SecurityHeadersMiddleware:
    """Add the security headers to every HTTP response that does not set them itself."""

    def __init__(self, app: ASGIApp, *, csp: str = CONTENT_SECURITY_POLICY, hsts: bool | None = None) -> None:
        """``hsts``: ``True`` always (served over HTTPS behind a proxy), ``False`` never, ``None`` by the request's
        scheme."""
        self.app = app
        self.csp = csp
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = self.hsts if self.hsts is not None else scope.get("scheme") == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers") or [])
                headers = MutableHeaders(scope=message)
                if "content-disposition" in headers:
                    # A file (an upload, a run's result file): the studio previews it in a frame of its own pages,
                    # so only same-origin framing; its policy is the route's own (a PDF gets none: a policy would
                    # stop the browser's PDF viewer).
                    headers.setdefault("X-Frame-Options", "SAMEORIGIN")
                else:
                    headers.setdefault("Content-Security-Policy", self.csp)
                    headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", REFERRER_POLICY)
                if https:
                    headers.setdefault("Strict-Transport-Security", HSTS)
            await send(message)

        await self.app(scope, receive, send_with_headers)


__all__ = ["CONTENT_SECURITY_POLICY", "HSTS", "THEME_SCRIPT_HASH", "SecurityHeadersMiddleware"]
