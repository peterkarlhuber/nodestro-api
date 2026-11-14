"""The app API contract ``nodestro-app/1``: what every interface may rely on in every Nodestro backend (NAL-C1).

The contract is a fixed set of existing routes (the published workflow APIs, the embedded interface's sign-in) plus
the discovery ``GET /api/app`` (:mod:`nodestro_api.app_discovery`). This module holds what those routes share and what
does not depend on the serving layer:

- the contract's name, versions and response header (:data:`CONTRACT_HEADER` on every answer of a contract route,
  :class:`ContractHeaderMiddleware`);
- the fixed names of the capabilities and authentication methods;
- the discovery's response models (:class:`AppDiscovery` and its parts);
- the one mapping of a workflow's input form to the fields a client renders (:func:`form_fields`), used by the
  published API's info (``InfoOut.form``) and by the embedded web interface (``/ui/api/config``);
- the backend's stable identity (:func:`load_instance`, ``<data dir>/instance.json``).

The description for client authors is ``docs/app-api/v1.md``; the JSON Schemas of the responses are under
``contracts/app-api/v1``. Changes follow the versioning rules there: new optional fields, capabilities and error
codes keep the version; anything else is a new major version.

Nothing here imports the studio; an exported project ships this module byte for byte.
"""

from __future__ import annotations

from collections.abc import Iterable
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Literal

from nodestro_runtime.engine import start_fields
from nodestro_runtime.file_formats import accept_extensions
from nodestro_runtime.schemas import InputField, Workflow, now
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

CONTRACT_NAME = "nodestro-app"
#: Every major version this backend serves, oldest first.
CONTRACT_VERSIONS: tuple[int, ...] = (1,)
CONTRACT_CURRENT = 1
#: The response header that names the contract version of an answer.
CONTRACT_HEADER = "Nodestro-App-Contract"
#: The version of the form description (``InfoOut.form``).
FORM_VERSION = 1

#: Every capability name the contract defines, in the order the description lists them. A backend announces only those
#: it serves; ``notifications`` (NAL-A3) and ``pairing`` (NAL-C4) are reserved names that no backend serves yet.
CAPABILITIES: tuple[str, ...] = (
    "runs",
    "runs.sync",
    "events.sse",
    "files.upload",
    "files.download",
    "approvals",
    "cancel",
    "resume",
    "history",
    "usage",
    "notifications",
    "pairing",
)
#: What every backend of this version serves (the routes of :func:`nodestro_api.serving.build_router`).
SERVED_CAPABILITIES: tuple[str, ...] = (
    "runs",
    "runs.sync",
    "events.sse",
    "files.upload",
    "files.download",
    "approvals",
    "cancel",
    "resume",
    "history",
    "usage",
)

AuthMethod = Literal["api_key", "session", "launch_link", "pairing"]
#: The field types of form version 1; a client renders an unknown type as JSON input (contract rule).
FORM_FIELD_TYPES: tuple[str, ...] = ("text", "textarea", "number", "date", "boolean", "select", "file")

#: The name an operator gives the backend (shown to clients; empty: none). Overrides ``name`` in ``instance.json``.
BACKEND_NAME_ENV = "NODESTRO_BACKEND_NAME"
INSTANCE_FILE = "instance.json"
_INSTANCE_ID = re.compile(r"^bk_[a-z0-9]{20,64}$")


# ---- discovery -------------------------------------------------------------------------------------------------


class ContractInfo(BaseModel):
    """Which contract and versions the backend serves."""

    name: Literal["nodestro-app"] = "nodestro-app"
    versions: list[int] = Field(default_factory=lambda: list(CONTRACT_VERSIONS))
    current: int = Field(default_factory=lambda: CONTRACT_CURRENT)


class BackendInfo(BaseModel):
    """The backend: a stable id per installation or package, the operator's name for it and (signed in) its versions."""

    #: Random, created once and kept in ``<data dir>/instance.json``.
    id: str
    #: Set by the operator; empty when none was set.
    name: str = ""
    #: The Nodestro API version (only for a signed-in client).
    version: str | None = None
    #: The Nodestro runtime version (only for a signed-in client).
    runtime_version: str | None = None


_API_KEY_ONLY: list[AuthMethod] = ["api_key"]


class AuthInfo(BaseModel):
    """How a client can sign in to this backend."""

    methods: list[AuthMethod] = Field(default_factory=lambda: _API_KEY_ONLY.copy())


class TlsInfo(BaseModel):
    """Whether the request reached the backend over HTTPS."""

    enabled: bool = False


class AppLinks(BaseModel):
    info: str
    openapi: str


class AppSummary(BaseModel):
    """A published workflow the signed-in client may use."""

    slug: str
    name: str
    description: str = ""
    #: The number of the active release.
    release: int
    #: The languages the app declares (empty: none declared).
    languages: list[str] = Field(default_factory=list)
    links: AppLinks


class AppDiscovery(BaseModel):
    """The answer of ``GET /api/app``. Without credentials only ``contract``, ``backend`` (id, name), ``auth`` and
    ``tls``; with an API key or a session also ``capabilities`` and the ``apps`` that credential may use."""

    contract: ContractInfo = Field(default_factory=ContractInfo)
    backend: BackendInfo
    auth: AuthInfo = Field(default_factory=AuthInfo)
    tls: TlsInfo = Field(default_factory=TlsInfo)
    capabilities: list[str] | None = None
    apps: list[AppSummary] | None = None


# ---- the form ------------------------------------------------------------------------------------------------------


class FormField(BaseModel):
    """One field of an app's input form, in the order the form shows them (``InfoOut.form``, form version 1).

    ``type`` is one of :data:`FORM_FIELD_TYPES`; a later form version may add types, which a client that does not know
    them renders as JSON input. A field's value goes into ``values.<name>`` of a run request."""

    name: str
    type: str
    #: What the form shows (the field's name when it has no label).
    label: str
    #: A help text under the field.
    help: str = ""
    required: bool = False
    #: The preset value (a date as ``YYYY-MM-DD``).
    default: Any = None
    #: ``select``: the choices.
    options: list[str] = Field(default_factory=list)
    #: ``file``: several files may be sent (``false`` for every other type).
    multiple: bool = False
    #: ``file``: the accepted file extensions with their dot (``.pdf``), as an HTML ``accept`` attribute takes them.
    accept: list[str] = Field(default_factory=list)
    #: ``file``: the accepted formats and groups as the workflow names them (``pdf``, ``document`` ...; empty: every
    #: supported format), for showing them by name.
    accept_formats: list[str] = Field(default_factory=list)
    #: ``file``: the largest file in MB.
    max_mb: int | None = None
    #: ``file``: the most files (``null``: no own limit).
    max_files: int | None = None


def form_field(item: InputField, max_upload_mb: int) -> FormField:
    """The contract's description of one form field; ``max_upload_mb`` is the publication's upload limit."""
    is_file = item.type == "file"
    return FormField(
        name=item.name,
        type=item.type,
        label=item.label or item.name,
        help=item.description,
        required=item.required,
        default=item.default,
        options=[option for option in item.options if option.strip()] if item.type == "select" else [],
        multiple=bool(item.multiple) if is_file else False,
        accept=accept_extensions(item.accept) if is_file else [],
        accept_formats=list(item.accept) if is_file else [],
        max_mb=(item.max_size_mb or max_upload_mb) if is_file else None,
        max_files=(1 if not item.multiple else item.max_files) if is_file else None,
    )


def form_fields(workflow: Workflow, max_upload_mb: int) -> list[FormField]:
    """The input form of ``workflow`` as a client renders it (empty: the workflow takes free text in ``input``)."""
    return [form_field(item, max_upload_mb) for item in start_fields(workflow)]


def ui_form_fields(fields: Iterable[FormField]) -> list[dict[str, Any]]:
    """The form fields under the names the embedded web interface reads (``/ui/api/config``)."""
    found = []
    for item in fields:
        entry: dict[str, Any] = {
            "name": item.name,
            "label": item.label,
            "type": item.type,
            "description": item.help,
            "required": item.required,
            "default": item.default,
            "options": item.options,
            "accept": item.accept_formats,
            "multiple": item.multiple,
            "max_files": item.max_files,
            "max_size_mb": item.max_mb,
        }
        if item.type == "file":
            entry["accept_extensions"] = item.accept
        found.append(entry)
    return found


# ---- the backend's identity ----------------------------------------------------------------------------------------


class Instance(BaseModel):
    """``<data dir>/instance.json``: the backend's stable id and the operator's name for it."""

    id: str
    name: str = ""
    created_at: str = ""


_INSTANCE_LOCK = threading.Lock()


def new_instance_id() -> str:
    return f"bk_{secrets.token_hex(12)}"


def load_instance(data_dir: Path, *, name: str | None = None) -> Instance:
    """The backend's identity, created once in ``data_dir``. ``name`` (else :data:`BACKEND_NAME_ENV`) overrides the
    name stored in the file; it is never written there.

    The id is random and stays as long as the data directory does: a desktop installation, an exported project, a
    portable package each have their own. An unreadable file is kept as ``instance.json.invalid`` and a new id is
    created (clients then see a new backend)."""
    path = data_dir / INSTANCE_FILE
    with _INSTANCE_LOCK:
        instance = _read_instance(path)
        if instance is None:
            instance = Instance(id=new_instance_id(), created_at=now().isoformat())
            instance = _create_instance(path, instance)
    configured = name if name is not None else os.environ.get(BACKEND_NAME_ENV)
    if configured is not None and configured.strip():
        instance = instance.model_copy(update={"name": configured.strip()})
    return instance


def _read_instance(path: Path) -> Instance | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        instance = Instance.model_validate(json.loads(raw))
        if not _INSTANCE_ID.match(instance.id):
            raise ValueError(f"invalid id {instance.id!r}")
    except ValueError as exc:
        logger.warning("%s cannot be read (%s); a new backend id is created", path, exc)
        os.replace(path, path.with_name(f"{INSTANCE_FILE}.invalid"))
        return None
    return instance


def _create_instance(path: Path, instance: Instance) -> Instance:
    """Write ``instance`` unless another process was faster; the file that wins is the one every process uses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(instance.model_dump_json(indent=2) + "\n")
    except FileExistsError:
        return _read_instance(path) or instance
    return instance


# ---- the response header -------------------------------------------------------------------------------------------

#: The paths of the contract's routes (``docs/app-api/v1.md``, "Contract boundary"); ``_mcp`` is Nodestro's MCP server,
#: not part of the contract (no slug starts with ``_``).
_CONTRACT_PATH = re.compile(r"^(?:/api/app|/api/published/(?!_)[^/]+(?:/.*)?|/ui/api/session)$")


def is_contract_path(path: str) -> bool:
    return bool(_CONTRACT_PATH.match(path))


class ContractHeaderMiddleware:
    """Adds ``Nodestro-App-Contract: <current version>`` to every answer of a contract route, errors included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.value = str(CONTRACT_CURRENT)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_contract_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        async def tagged(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[CONTRACT_HEADER] = self.value
            await send(message)

        await self.app(scope, receive, tagged)


__all__ = [
    "CAPABILITIES",
    "CONTRACT_CURRENT",
    "CONTRACT_HEADER",
    "CONTRACT_NAME",
    "CONTRACT_VERSIONS",
    "FORM_FIELD_TYPES",
    "FORM_VERSION",
    "SERVED_CAPABILITIES",
    "AppDiscovery",
    "AppLinks",
    "AppSummary",
    "AuthInfo",
    "BackendInfo",
    "ContractHeaderMiddleware",
    "ContractInfo",
    "FormField",
    "Instance",
    "TlsInfo",
    "form_field",
    "form_fields",
    "is_contract_path",
    "load_instance",
    "ui_form_fields",
]
