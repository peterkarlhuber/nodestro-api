"""Error texts the API sends to clients (MUP-ONA A2, PS-2).

An error answer carries a stable ``code`` (what clients and the studio evaluate), ``params`` and ``detail``, an
English technical text. ``detail`` must not tell a client more than the code does about the installation:

- an exception **without** an error code of its own (``ValueError``, ``PermissionError``, an unexpected ``KeyError``,
  a library or provider error) answers with a fixed text and the request's ``request_id``; its own text goes only to
  the log, with the same ``request_id`` and with credentials redacted (:func:`log_original`);
- an exception **with** an error code (``NodestroError``, ``ApiError``, ``AccessError``, ``RetrievalError``) keeps
  its text after :func:`client_text` replaced absolute paths and credentials.

``params`` are not changed: they are the documented parameters of each code (``error_codes.json``), and a path in
them (a desktop folder, a local model file) is one the client named or may see.
"""

from __future__ import annotations

import logging
import re
import secrets

from .observability import request_id
from .serving import safe_text

logger = logging.getLogger("nodestro_api.errors")

#: The ``detail`` of an exception without an error code of its own (``bad_request``).
GENERIC_DETAIL = "The request could not be processed"
#: What replaces an absolute path in a client text.
PATH_PLACEHOLDER = "[path]"

# POSIX: a slash that does not continue a word, a URL or a relative path, then at least two segments ("/home/x",
# "/var/lib/nodestro/file.txt"). One segment ("/api") is left alone: it is a route or a ratio, not a location.
_POSIX_PATH = re.compile(r"(?<![\w.:/~-])/[^\s/'\"<>|,;:)\]]+(?:/[^\s/'\"<>|,;:)\]]*)+")
# Windows: a drive ("C:\...", "C:/...") or a UNC path ("\\server\share").
_WINDOWS_PATH = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/]|\\\\[^\s\\'\"]+\\)[^\s'\"<>|,;)\]]*")
# "file:///..." URLs name a local path as well.
_FILE_URL = re.compile(r"\bfile://[^\s'\"<>]*", re.IGNORECASE)


def redact_paths(text: str) -> str:
    """``text`` with absolute file system paths replaced by :data:`PATH_PLACEHOLDER`."""
    text = _FILE_URL.sub(PATH_PLACEHOLDER, text)
    text = _WINDOWS_PATH.sub(PATH_PLACEHOLDER, text)
    return _POSIX_PATH.sub(PATH_PLACEHOLDER, text)


def client_text(text: str | None) -> str:
    """A text of an exception with an error code, for clients: no absolute paths, no credentials, shortened."""
    return safe_text(redact_paths(text or ""))


def current_request_id() -> str:
    """The id of the request being answered (``X-Request-ID`` of the server profile), or a new one where no
    middleware gave the request an id (the desktop API, an exported app); the log line uses the same one."""
    found = request_id()
    return found if found != "-" else secrets.token_hex(8)


def log_original(code: str, exc: BaseException, found_id: str) -> None:
    """Log the original text of an exception whose answer was generic, with the answer's ``request_id``;
    credentials are redacted like in the answers (the path stays: the log is the operator's)."""
    logger.warning(
        "%s answered generically (request_id=%s): %s: %s",
        code,
        found_id,
        type(exc).__name__,
        safe_text(str(exc)),
        extra={"request_id": found_id},
    )


__all__ = ["GENERIC_DETAIL", "PATH_PLACEHOLDER", "client_text", "current_request_id", "log_original", "redact_paths"]
