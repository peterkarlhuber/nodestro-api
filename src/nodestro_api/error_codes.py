"""One machine-readable list of every problem code the studio API can answer with (backend audit X-03, API-016).

Errors, validation issues and status messages carry a stable ``code`` with ``params``; the studio and SDK users turn
them into text. The list merges the runtime's catalog (:mod:`nodestro_runtime.error_codes`) with the codes of this
package, found by the same scanner (``ApiError``/``AccessError``/``NodestroError``/``_error``/``problem`` calls and
code constants in their namespaces), plus the families put together at run time (``agent.<Orkexa error class>``
from the error handler; ``http.<status>`` for other HTTP errors).

- ``error_codes.json`` (in the package) keeps the list; ``tests/test_error_codes.py`` fails while it is out of date,
  and ``python -m nodestro_api.error_codes --write`` writes it again.
- ``GET /api/error-codes`` (``read``) answers it next to the OpenAPI document (``/api/openapi.json``), so a client
  can check that it knows every code.
"""

from __future__ import annotations

from functools import cache
import json
from pathlib import Path
import re
import sys
from typing import Literal

from fastapi import APIRouter
from nodestro_runtime import error_codes as runtime_codes
from pydantic import BaseModel, Field

from .permissions import READ

router = APIRouter(prefix="/api")

#: The calls of this package that build an error answer or a problem.
_BUILDERS = re.compile(r"(?:Error|_error|problem|Problem|issue|Issue)$")
#: The API also answers with a few codes without a namespace (``not_found``, ``bad_request``).
_CODE = re.compile(r"[a-z][a-z0-9_]*(?:\.[A-Za-z0-9_]+)*")
#: Constants in a code namespace that are not codes.
NOT_CODES = frozenset({"release.json"})
#: Code families whose last part is filled in at run time.
PATTERNS = ("http.<status>",)
_PACKAGE = Path(__file__).resolve().parent
CATALOG = _PACKAGE / "error_codes.json"

Source = Literal["runtime", "api", "both"]


class ErrorCode(BaseModel):
    #: The names of the code's parameters (``params`` of the answer); a code may come without some of them.
    params: list[str] = Field(default_factory=list)
    #: Where the code is raised: the runtime (also inside runs and steps), this API, or both.
    source: Source


class ErrorCodeCatalog(BaseModel):
    """Every problem code of the studio API and the runtime, sorted, with its parameters."""

    codes: dict[str, ErrorCode]
    #: Families such as ``http.<status>``, whose last part is a value.
    patterns: list[str] = Field(default_factory=list)


def api_codes() -> dict[str, set[str]]:
    """The codes of this package with their parameter names (scanned), plus ``agent.<Orkexa error class>`` and
    ``<kind>.not_found``."""
    from .api import _NOT_FOUND_KINDS

    found = runtime_codes.scan(_PACKAGE, exclude=NOT_CODES, builders=_BUILDERS, pattern=_CODE)
    for name in runtime_codes.orkexa_errors():
        found.setdefault(f"agent.{name}", set()).add("type")
    for kind in _NOT_FOUND_KINDS.values():  # the not-found answer of a route's own record (api.install_error_handlers)
        found.setdefault(f"{kind}.not_found", set()).update({"name", "id", "kind"})
    return found


def collect() -> ErrorCodeCatalog:
    runtime = {code: set(params) for code, params in runtime_codes.catalog().items()}
    api = api_codes()
    codes: dict[str, ErrorCode] = {}
    for code in sorted(set(runtime) | set(api)):
        source: Source = "both" if code in runtime and code in api else "runtime" if code in runtime else "api"
        params = sorted(runtime.get(code, set()) | api.get(code, set()))
        codes[code] = ErrorCode(params=params, source=source)
    return ErrorCodeCatalog(codes=codes, patterns=list(PATTERNS))


@cache
def current() -> ErrorCodeCatalog:
    """The list shipped with the package."""
    return ErrorCodeCatalog.model_validate_json(CATALOG.read_text(encoding="utf-8"))


def text(found: ErrorCodeCatalog | None = None) -> str:
    """The list as the repository keeps it: one code per line, so a change shows as the lines it adds or removes."""
    found = found or collect()
    lines = [
        f"  {json.dumps(code)}: {json.dumps(entry.model_dump(), sort_keys=True)}" for code, entry in found.codes.items()
    ]
    return '{\n "codes": {\n' + ",\n".join(lines) + "\n },\n" + f' "patterns": {json.dumps(found.patterns)}\n' + "}\n"


@router.get("/error-codes", dependencies=READ)
def error_codes() -> ErrorCodeCatalog:
    """Every problem code the API and the runtime answer with (``code`` of an error, an issue or a status), with the
    names of its parameters and where it is raised. A client can check that it has a text for each of them."""
    return current()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--write"]:
        CATALOG.write_text(text(), encoding="utf-8")
        current.cache_clear()
    else:
        sys.stdout.write(text())


if __name__ == "__main__":  # pragma: no cover
    main()
