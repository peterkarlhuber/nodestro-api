"""Published workflow APIs: the HTTP layer shared by Nodestro and exported projects.

A published workflow is reachable under ``/api/published/{slug}/...``. The same router
(:func:`build_router`) runs inside Nodestro (direct deployment, :mod:`nodestro_api.publishing`)
and inside an exported FastAPI project (:mod:`nodestro_api.standalone`). It never executes a
workflow itself: a run is an ordinary run of the one engine (:class:`~nodestro_runtime.engine.RunManager`)
with ``trigger="api"``, started from the immutable snapshot of a release. Decisions, approvals,
sub-workflows, tools, budgets, result files and continued runs therefore behave exactly as in
the studio.

What this module adds on top of the engine:

- authentication with Nodestro's own API keys (``Authorization: Bearer ndk_...``), scoped to
  publications and to the permissions ``run``, ``read`` and ``approve``; only a hash of a key
  is ever stored, and keys never appear in responses, events or logs;
- request schemas generated from the workflow's input form (:func:`input_model`) and a
  best-effort JSON schema of its result (:func:`output_schema`); results are never changed;
- asynchronous runs (the default) with status, result, Server-Sent Events that survive
  reconnects (``Last-Event-ID``), cancellation, continuation and approvals, plus an optional
  synchronous wait with a fixed time limit;
- limits per publication (parallel runs, waiting runs, requests per minute, a daily cost
  limit) and ``Idempotency-Key`` handling, so a client that retries never starts a run twice;
- consistent, machine-readable errors: ``{"error": {"code", "message", "params", "status"}}``.

Nothing here imports the studio (``nodestro_api.context``); an exported project ships this module
byte for byte together with the engine.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
import contextlib
from datetime import date, datetime, timedelta
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Literal, Protocol

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from nodestro_runtime.agent_builder import output_model
from nodestro_runtime.engine import RunManager, WorkflowError, node_label, start_fields
from nodestro_runtime.file_formats import FileFormatError, accept_extensions
from nodestro_runtime.forms import FormError
from nodestro_runtime.graph import WORKING, continuation, data_sources
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.reports import ResultFiles
from nodestro_runtime.run_views import approvals_of_tree
from nodestro_runtime.sanitize import Sanitizer
from nodestro_runtime.schemas import NodeRun, Run, RunEvent, RunIn, RunSummary, Usage, Workflow, now, total_cost
from nodestro_runtime.storage import DocumentStore
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

logger = logging.getLogger(__name__)

PREFIX = "/api/published"
#: What a key may do: start and cancel runs, read status, results, events and files, decide approvals.
Scope = Literal["run", "read", "approve"]
SCOPES: tuple[str, ...] = ("run", "read", "approve")
KEY_PREFIX = "ndk"
_TOKEN = re.compile(r"^ndk_([a-z0-9]{12})_([A-Za-z0-9_-]{32,64})$")
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
#: Engine status -> API status (``paused`` means: waiting for a person).
API_STATUS = {
    "queued": "queued",
    "running": "running",
    "paused": "awaiting_approval",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}
ApiStatus = Literal["queued", "running", "awaiting_approval", "succeeded", "failed", "cancelled"]
#: Events an API client receives, and the fields it gets of each (prompts and tool data stay inside).
_PUBLIC_EVENTS: dict[str, tuple[str, ...]] = {
    "run_started": ("status",),
    "node_started": ("iteration",),
    "node_finished": ("status", "error_code", "error_params", "usage", "iteration"),
    # A loop connection was followed: its nodes run again (``iteration`` counts from 1).
    "loop_iteration": ("iteration", "max_iterations"),
    "approval_requested": ("status",),
    # The reviewer's comment is internal: it stays in the run's approval record (studio, audit trail).
    "approval_decided": ("decision",),
    "evaluation_started": (),
    "evaluation": ("passed", "attempts"),
    "usage": ("usage",),
    "run_cancelling": (),
    "run_paused": ("status", "usage"),
    "run_finished": ("status", "error_code", "error_params", "usage"),
}
_MAX_BODY_BYTES = 80 * 1024 * 1024
_IDEMPOTENCY = "api_idempotency"
_IDEMPOTENCY_HOURS = 24


# ---- errors -----------------------------------------------------------------------------


class ApiError(Exception):
    """An error answer of a published API: HTTP status, stable code, English message, parameters."""

    def __init__(
        self, status: int, code: str, message: str, *, headers: dict[str, str] | None = None, **params: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.params = params
        self.headers = headers or {}

    def response(self) -> JSONResponse:
        body = {"error": {"status": self.status, "code": self.code, "message": self.message, "params": self.params}}
        return JSONResponse(body, status_code=self.status, headers=self.headers)


_SANITIZER_LOCK = threading.Lock()


def safe_text(text: str | None) -> str:
    """A message for API clients: credentials and keys replaced, shortened."""
    if not text:
        return ""
    with _SANITIZER_LOCK:
        cleaned = Sanitizer(personal_data=False).text(text)
    cleaned = re.sub(r"ndk_[a-z0-9]{12}_[A-Za-z0-9_-]+", "[API_KEY]", cleaned)
    return cleaned[:1000]


def _error_of(exc: BaseException) -> ApiError:
    """Turn what the engine or a check raised into an API error (never leaking internals)."""
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, FormError):
        return ApiError(422, "request.invalid", safe_text(str(exc)), errors=exc.params.get("errors", []))
    if isinstance(exc, FileFormatError):
        status = 413 if exc.code == "input.too_large" else 422
        return ApiError(status, exc.code, safe_text(str(exc)), **exc.params)
    if isinstance(exc, WorkflowError):
        return ApiError(409, "release.invalid", "The published workflow cannot run; publish it again", detail=exc.code)
    if isinstance(exc, NodestroError):
        return ApiError(400, exc.code, safe_text(str(exc)), **exc.params)
    logger.exception("Published API request failed")
    return ApiError(500, "internal", "An internal error occurred; the details are in the server log")


# ---- keys --------------------------------------------------------------------------------


def hash_secret(secret: str) -> str:
    """The stored form of a key's secret (SHA-256; the secret has 256 bits of randomness)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def new_key() -> tuple[str, str, str]:
    """A new key: ``(key id, secret, token)``; the token is what a client sends."""
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return key_id, secret, f"{KEY_PREFIX}_{key_id}_{secret}"


def parse_token(token: str) -> tuple[str, str] | None:
    match = _TOKEN.match(token.strip())
    return (match.group(1), match.group(2)) if match else None


def secret_matches(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash)


class KeyIdentity(BaseModel):
    """A verified key: who calls, for which publications, with which permissions."""

    id: str
    name: str = ""
    scopes: list[str] = Field(default_factory=list)
    #: Publication ids the key may use (never "all").
    publications: list[str] = Field(default_factory=list)


# ---- what is served --------------------------------------------------------------------


class ApiLimits(BaseModel):
    """How much a published API may be used; exceeded limits answer ``429``."""

    model_config = ConfigDict(extra="ignore")

    #: Runs that execute at the same time (queued or running).
    max_concurrent_runs: int = Field(default=2, ge=1, le=50)
    #: Runs that wait for an approval at the same time.
    max_waiting_runs: int = Field(default=20, ge=0, le=1000)
    #: New runs per minute and key.
    runs_per_minute: int = Field(default=10, ge=1, le=600)
    #: Requests of any kind per minute and key.
    requests_per_minute: int = Field(default=120, ge=1, le=6000)
    #: Cost of all runs of the last 24 hours in USD; new runs are refused above it (``None``: no limit).
    daily_budget_usd: float | None = Field(default=5.0, ge=0)
    #: Cost limit of one run in USD (``None``: the workflow's budget, else the default budget).
    run_budget_usd: float | None = Field(default=None, ge=0)
    #: Whether a client may wait for the result in the same request (``?wait=<seconds>``).
    sync_enabled: bool = False
    sync_timeout_seconds: int = Field(default=30, ge=1, le=120)
    #: Size limit of one uploaded file in MB.
    max_upload_mb: int = Field(default=10, ge=1, le=50)


class ServedRelease(BaseModel):
    """One release of a publication, as the router needs it."""

    publication_id: str
    slug: str
    name: str
    description: str = ""
    release_id: str
    number: int
    #: The saved workflow version the release was published from.
    workflow_version: int | None = None
    workflow: Workflow
    limits: ApiLimits = Field(default_factory=ApiLimits)
    enabled: bool = True
    active: bool = True
    #: Whether the release may be called over the network listener.
    network: bool = False
    #: Offered as a tool of Nodestro's MCP server (WP-11, off by default).
    mcp_tool: bool = False
    published_at: datetime = Field(default_factory=now)
    #: Approval nodes a run may stop at (sub-workflows included).
    approvals: int = 0
    output_schema: dict[str, Any] = Field(default_factory=dict)
    #: Referenced data that is not available here (shown in the API info, e.g. of an export).
    missing_resources: list[str] = Field(default_factory=list)


class ServingHost(Protocol):
    """What Nodestro (:mod:`nodestro_api.publishing`) or an export (:mod:`nodestro_api.standalone`) provides."""

    runs: RunManager
    files: ResultFiles

    @property
    def store(self) -> DocumentStore: ...

    def release(self, slug: str, number: int | None, *, channel: str) -> ServedRelease:
        """The active release of ``slug`` (or release ``number``); raises :class:`ApiError` 404."""

    def release_of_run(self, run: Run) -> ServedRelease | None: ...

    def verify(self, key_id: str, secret: str) -> KeyIdentity | None: ...

    def prepare_start(self, release: ServedRelease) -> Awaitable[None]:
        """Raise :class:`ApiError` when no run can start now (no model access, libraries changed)."""

    def add_upload(self, release: ServedRelease, name: str, data: bytes, media_type: str | None) -> str:
        """Store an uploaded file for a run; returns its file id."""

    def builtin_tools(self) -> set[str]: ...


# ---- request and response models -------------------------------------------------------


async def read_body(request: Request, limit: int | None = None) -> bytes:
    """The request body, refused with ``413`` above ``limit`` bytes (default 80 MB).

    The size is counted while reading, so a missing or wrong ``Content-Length`` (a chunked
    upload, for example) cannot get a larger body in. The body is kept on the request, so
    :meth:`Request.form` parses it from there afterwards.
    """
    limit = _MAX_BODY_BYTES if limit is None else limit
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise ApiError(413, "request.too_large", "The request is too large", limit_mb=limit // (1024 * 1024))
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "request.too_large", "The request is too large", limit_mb=limit // (1024 * 1024))
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body
    return body


async def multipart_payload(request: Request, release: ServedRelease) -> dict[str, Any]:
    """A ``multipart/form-data`` request as the JSON body it stands for (files as Base64).

    Parts: ``values`` (JSON object of the form's other fields) or one part per field
    (``<field>`` or ``values.<field>``); ``input`` (free text); files in a part named after
    their file field (several parts with the same name for several files) or ``files``.
    """
    file_fields = {field.name: field for field in start_fields(release.workflow) if field.type == "file"}
    try:
        form = await request.form(max_files=200, max_fields=500)
    except Exception:
        raise ApiError(400, "request.multipart_invalid", "The multipart body cannot be read") from None
    values: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    for key, item in form.multi_items():
        name = key[7:] if key.startswith("values.") else key
        if not isinstance(item, str):
            data = await item.read()
            entry = {
                "name": item.filename or "upload",
                "media_type": item.content_type
                if item.content_type not in (None, "application/octet-stream")
                else None,
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
            if name in file_fields:
                values.setdefault(name, []).append(entry)
            elif name == "files":
                files.append(entry)
            else:
                raise ApiError(422, "request.unknown_file_part", f"No file field is called {name}", field=name)
            continue
        text = str(item)
        if key == "values":
            try:
                parsed = json.loads(text or "{}")
            except ValueError:
                raise ApiError(400, "request.not_json", "The part 'values' must be a JSON object") from None
            if not isinstance(parsed, dict):
                raise ApiError(400, "request.not_json", "The part 'values' must be a JSON object")
            values.update({k: v for k, v in parsed.items() if k not in values})
        elif key == "input":
            payload["input"] = text
        else:
            values[name] = text
    for name, field in file_fields.items():
        if name in values and isinstance(values[name], list) and not field.multiple and len(values[name]) == 1:
            values[name] = values[name][0]
    if start_fields(release.workflow):
        payload["values"] = values
    elif values:
        raise ApiError(422, "request.invalid", "This workflow has no input form", fields=", ".join(values))
    if files:
        payload["files"] = files
    return payload


class FileInput(BaseModel):
    """A file sent with a run, as Base64."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, description="File name with extension, e.g. invoice.pdf")
    media_type: str | None = Field(default=None, max_length=120, description="e.g. application/pdf")
    content_base64: str = Field(min_length=1, description="The file content, Base64-encoded")


def _model_name(text: str, fallback: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text)
    name = "".join(word[:1].upper() + word[1:] for word in words)[:60]
    if not name or not name[0].isalpha():
        name = fallback
    return name


def input_model(workflow: Workflow) -> type[BaseModel]:
    """The request body of a run, built from the workflow's input form.

    With a form: ``values`` holds the fields (types, required fields, choices, descriptions),
    ``input`` an optional free text. Without a form: ``input`` is the text the run starts with.
    ``files`` are extra documents for the agents fed by the start node.
    """
    fields = start_fields(workflow)
    base = _model_name(workflow.name, "Workflow")
    if not fields:
        start = next((node for node in workflow.nodes if node.type == "start"), None)
        example = start.start.default_input if start is not None and start.start is not None else ""
        return create_model(
            f"{base}RunRequest",
            __config__=ConfigDict(extra="forbid", title=f"{base}RunRequest"),
            input=(
                str,
                Field(
                    default="",
                    max_length=200_000,
                    description="The input text of the run ({{run.input}} in the workflow)",
                    examples=[example or "..."],
                ),
            ),
            files=(list[FileInput], Field(default_factory=list, description="Documents for the first agents")),
        )
    values: dict[str, Any] = {}
    for field in fields:
        annotation: Any
        extra: dict[str, Any] = {}
        if field.type in ("text", "textarea"):
            annotation = str
            extra["max_length"] = 20_000
        elif field.type == "number":
            annotation = int | float
        elif field.type == "date":
            annotation = date
        elif field.type == "boolean":
            annotation = bool
        elif field.type == "select":
            options = tuple(option for option in field.options if option.strip())
            annotation = Literal[options] if options else str
        else:
            annotation = FileInput | list[FileInput]
        description = field.description or field.label or field.name
        if field.type == "file":
            limit = (
                "one file" if not field.multiple else (f"up to {field.max_files} files" if field.max_files else "files")
            )
            description = f"{description} ({limit}; {', '.join(accept_extensions(field.accept))})"
        has_default = field.default not in (None, "", [])
        if has_default:
            description = f"{description} (default: {field.default})"
        required = field.required and not has_default
        info = Field(
            default=... if required else None,
            title=field.label or field.name,
            description=description,
            **extra,
        )
        values[field.name] = (annotation if required else annotation | None, info)
    values_model = create_model(f"{base}Values", __config__=ConfigDict(extra="forbid", title=f"{base}Values"), **values)
    required_values = any(field.required and field.default in (None, "", []) for field in fields)
    return create_model(
        f"{base}RunRequest",
        __config__=ConfigDict(extra="forbid", title=f"{base}RunRequest"),
        values=(values_model, Field(... if required_values else None, description="The fields of the input form")),
        input=(str, Field(default="", max_length=200_000, description="Optional free text next to the form")),
        files=(list[FileInput], Field(default_factory=list, description="Documents for the first agents")),
    )


def _schema_of_source(node_id: str, workflow: Workflow, resolve: Callable[[str, int | None], Workflow] | None) -> Any:
    node = next(item for item in workflow.nodes if item.id == node_id)
    if node.agent is not None:
        try:
            model = output_model(node.agent.output)
        except NodestroError:
            model = None
        if model is not None:
            schema = model.model_json_schema()
            schema.pop("title", None)
            return schema
        return {"type": "string"}
    if node.type == "subworkflow" and node.subworkflow is not None and resolve is not None:
        try:
            child = resolve(node.subworkflow.workflow_id, node.subworkflow.version)
        except (KeyError, NodestroError):
            return {}
        return output_schema(child, resolve, depth=1)
    if node.type == "start":
        return {"type": "string"} if not start_fields(workflow) else {"type": "object"}
    return {}


def _combine(named: list[tuple[str, Any]]) -> dict[str, Any]:
    if not named:
        return {}
    if len(named) == 1:
        return named[0][1]  # type: ignore[no-any-return]
    return {"type": "object", "properties": dict(named)}


def output_schema(
    workflow: Workflow, resolve: Callable[[str, int | None], Workflow] | None = None, *, depth: int = 0
) -> dict[str, Any]:
    """A JSON schema of the run's result, derived from the workflow (best effort).

    Mirrors how the engine builds ``run.output``: the result nodes' values, or else the
    results of the last agents. Structured agent outputs keep their fields; text is a string.
    A result whose branch was not taken is missing, so combined results have no required keys.
    """
    if depth > 5:
        return {}
    outputs = [node for node in workflow.nodes if node.type == "output"]
    labels = {node.id: node_label(node) for node in workflow.nodes}
    if outputs:
        per_output = []
        for node in outputs:
            sources = data_sources(workflow, node.id)
            per_output.append(
                (labels[node.id], _combine([(labels[s], _schema_of_source(s, workflow, resolve)) for s in sources]))
            )
        return _combine(per_output)
    targets = {edge.source for edge in workflow.edges}
    sinks = [node for node in workflow.nodes if node.type in WORKING and node.id not in targets]
    return _combine([(labels[node.id], _schema_of_source(node.id, workflow, resolve)) for node in sinks])


def example_values(workflow: Workflow) -> dict[str, Any]:
    """A request body that fits the input form (for documentation and tests)."""
    fields = start_fields(workflow)
    if not fields:
        start = next((node for node in workflow.nodes if node.type == "start"), None)
        text = start.start.default_input if start is not None and start.start is not None else ""
        return {"input": text or "Please summarise the attached request."}
    values: dict[str, Any] = {}
    for field in fields:
        if field.default not in (None, "", []) and field.type != "file":
            values[field.name] = field.default
        elif field.type in ("text", "textarea"):
            values[field.name] = f"<{field.label or field.name}>"
        elif field.type == "number":
            values[field.name] = 1
        elif field.type == "date":
            values[field.name] = "2026-01-31"
        elif field.type == "boolean":
            values[field.name] = True
        elif field.type == "select":
            values[field.name] = next((option for option in field.options if option.strip()), "")
        else:
            values[field.name] = [{"name": "document.txt", "media_type": "text/plain", "content_base64": "SGVsbG8="}]
    return {"values": values}


class StepOut(BaseModel):
    node_id: str
    label: str
    type: str
    status: str
    error_code: str | None = None


class ErrorOut(BaseModel):
    code: str
    message: str
    params: dict[str, Any] = Field(default_factory=dict)


class UsageOut(BaseModel):
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = 0.0


class ApprovalItemOut(BaseModel):
    label: str
    value: Any = None


class ApprovalOut(BaseModel):
    """An approval that waits for a decision: what to review and how to answer."""

    approval_id: str
    step: str
    workflow: str
    instructions: str = ""
    waiting_since: datetime | None = None
    allow_comment: bool = True
    require_comment_on_reject: bool = False
    #: ``branch``: a rejection follows the rejected path; ``stop``: it ends the run.
    on_reject: str = "branch"
    review: list[ApprovalItemOut] = Field(default_factory=list)


class RunStatusOut(BaseModel):
    run_id: str
    status: ApiStatus
    publication: str
    version: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    error: ErrorOut | None = None
    usage: UsageOut = Field(default_factory=UsageOut)
    steps: list[StepOut] = Field(default_factory=list)
    approvals: list[ApprovalOut] = Field(default_factory=list)
    resumed_from: str | None = None
    links: dict[str, str] = Field(default_factory=dict)


class RunResultOut(BaseModel):
    run_id: str
    status: ApiStatus
    #: The workflow's result exactly as the run produced it (text or JSON).
    output: Any = None
    error: ErrorOut | None = None
    files: list[dict[str, Any]] = Field(default_factory=list)
    usage: UsageOut = Field(default_factory=UsageOut)


class ApprovalDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Which approval (from ``approvals``); may be left out when exactly one waits.
    approval_id: str | None = None
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=4000)


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Steps that run again had called tools before; the client agrees that they are called again.
    confirm_side_effects: bool = False
    #: Run every step again (a new run with the same input and version) instead of reusing results.
    restart: bool = False


class UsageSummaryOut(BaseModel):
    days: int
    runs: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    usage: UsageOut = Field(default_factory=UsageOut)
    average_duration_seconds: float | None = None
    #: Cost of the last 24 hours and the daily limit.
    last_24h_usd: float = 0.0
    daily_budget_usd: float | None = None


class InfoOut(BaseModel):
    publication: str
    name: str
    description: str = ""
    version: int
    versions: list[int] = Field(default_factory=list)
    published_at: datetime
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    example_request: dict[str, Any]
    approvals: bool
    sync: dict[str, Any]
    limits: dict[str, Any]
    missing_resources: list[str] = Field(default_factory=list)
    #: The file fields of the input form and how to send files (JSON with Base64 or multipart/form-data).
    file_inputs: list[dict[str, Any]] = Field(default_factory=list)
    uploads: dict[str, Any] = Field(default_factory=dict)


def multipart_schema(workflow: Workflow) -> dict[str, Any]:
    """The ``multipart/form-data`` variant of a run request (documentation)."""
    properties: dict[str, Any] = {
        "input": {"type": "string", "description": "Free text"},
    }
    fields = start_fields(workflow)
    if fields:
        properties["values"] = {"type": "string", "description": "The form's other fields as a JSON object"}
    for field in fields:
        if field.type != "file":
            continue
        binary = {"type": "string", "format": "binary"}
        properties[field.name] = (
            {"type": "array", "items": binary, "description": field.label or field.name}
            if field.multiple
            else {**binary, "description": field.label or field.name}
        )
    properties["files"] = {
        "type": "array",
        "items": {"type": "string", "format": "binary"},
        "description": "Extra documents",
    }
    return {"type": "object", "properties": properties}


def file_inputs(workflow: Workflow) -> list[dict[str, Any]]:
    """The file fields of a workflow's form as API clients need them."""
    return [
        {
            "field": field.name,
            "label": field.label or field.name,
            "required": field.required,
            "multiple": field.multiple,
            "max_files": 1 if not field.multiple else field.max_files,
            "max_size_mb": field.max_size_mb,
            "accept": accept_extensions(field.accept),
        }
        for field in start_fields(workflow)
        if field.type == "file"
    ]


def usage_out(usage: Usage) -> UsageOut:
    return UsageOut(
        requests=usage.requests,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=usage.estimated_cost_usd,
    )


# ---- limits --------------------------------------------------------------------------------


class _Window:
    """Counts events per key in a sliding minute."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, *, period: float = 60.0) -> float | None:
        """``None`` when allowed (and counted), else the seconds until the next slot."""
        moment = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and moment - hits[0] >= period:
                hits.popleft()
            if len(hits) >= limit:
                return max(period - (moment - hits[0]), 1.0)
            hits.append(moment)
            return None


def api_run_summaries(store: DocumentStore, publication_id: str) -> list[RunSummary]:
    """Every top-level run of a publication, newest first, however many runs are stored.

    Limits, the daily cost and waiting approvals are counted from these runs, so none may be
    left out (a fixed number of the most recent runs of all workflows could hide older ones).
    """
    # nodestro-runtime with Store.find filters in SQLite; older versions read every run.
    find = getattr(store, "find", None)
    candidates: list[RunSummary] = (
        find("runs", "trigger_ref", [publication_id], RunSummary)
        if find is not None
        else store.list("runs", RunSummary)
    )
    found = [
        summary
        for summary in candidates
        if summary.trigger == "api" and summary.trigger_ref == publication_id and summary.parent_run_id is None
    ]
    return sorted(found, key=lambda summary: summary.created_at, reverse=True)


# ---- the core ------------------------------------------------------------------------------


class ServingCore:
    """The behaviour of every published endpoint; :func:`build_router` only maps HTTP to it."""

    #: Failed authentications per client address and minute before it gets 429.
    MAX_AUTH_FAILURES = 30

    def __init__(self, host: ServingHost) -> None:
        self.host = host
        self._requests = _Window()
        self._failures = _Window()
        self._start_lock = asyncio.Lock()

    # ---- access ---------------------------------------------------------------------

    def authenticate(self, request: Request) -> KeyIdentity:
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else request.headers.get("x-api-key", "")
        client = request.client.host if request.client else "unknown"
        if not token:
            raise ApiError(
                401,
                "auth.missing",
                "Send an API key: Authorization: Bearer <key>",
                headers={"WWW-Authenticate": "Bearer"},
            )
        parsed = parse_token(token)
        identity = self.host.verify(*parsed) if parsed is not None else None
        if identity is None:
            wait = self._failures.allow(f"auth:{client}", self.MAX_AUTH_FAILURES)
            if wait is not None:
                raise ApiError(
                    429,
                    "auth.too_many_failures",
                    "Too many invalid keys; try again later",
                    headers={"Retry-After": str(int(wait))},
                )
            raise ApiError(
                401,
                "auth.invalid",
                "The API key is invalid or was revoked",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        return identity

    def authorize(self, identity: KeyIdentity, release: ServedRelease, scope: Scope) -> None:
        if release.publication_id not in identity.publications:
            # Same answer as for an unknown publication: a key learns nothing about other APIs.
            raise ApiError(404, "publication.not_found", "No published workflow with this name")
        if scope not in identity.scopes:
            raise ApiError(403, "auth.scope", f"This key may not {scope}", scope=scope)
        wait = self._requests.allow(f"req:{identity.id}", release.limits.requests_per_minute)
        if wait is not None:
            raise ApiError(
                429,
                "limit.requests",
                "Too many requests; slow down",
                headers={"Retry-After": str(int(wait))},
                limit=release.limits.requests_per_minute,
            )

    def access(
        self, request: Request, slug: str, scope: Scope, *, number: int | None = None, channel: str
    ) -> tuple[KeyIdentity, ServedRelease]:
        identity = self.authenticate(request)
        release = self.host.release(slug, number, channel=channel)
        self.authorize(identity, release, scope)
        return identity, release

    def own_run(self, release: ServedRelease, run_id: str) -> Run:
        """A run of this publication (any of its releases); 404 for everything else."""
        run = self.host.runs.get(run_id)
        if run is None or run.trigger != "api" or run.trigger_ref != release.publication_id:
            raise ApiError(404, "run.not_found", "No run with this id", run_id=run_id)
        return run

    # ---- views ----------------------------------------------------------------------

    def links(self, slug: str, run_id: str) -> dict[str, str]:
        base = f"{PREFIX}/{slug}/runs/{run_id}"
        return {
            "self": base,
            "result": f"{base}/result",
            "events": f"{base}/events",
            "approvals": f"{base}/approvals",
            "files": f"{base}/files",
            "cancel": f"{base}/cancel",
        }

    def approvals(self, run: Run) -> list[ApprovalOut]:
        found = []
        for item in approvals_of_tree(self.host.runs, run):
            found.append(
                ApprovalOut(
                    approval_id=f"{item.run_id}:{item.node_id}",
                    step=item.label,
                    workflow=" / ".join(item.path),
                    instructions=item.instructions,
                    waiting_since=item.waiting_since,
                    allow_comment=item.allow_comment,
                    require_comment_on_reject=item.require_comment_on_reject,
                    on_reject=item.on_reject,
                    review=[ApprovalItemOut(label=entry.label, value=entry.value) for entry in item.items],
                )
            )
        return found

    def status(self, slug: str, run: Run) -> RunStatusOut:
        duration = None
        if run.started_at is not None:
            end = run.finished_at or now()
            duration = round((end - run.started_at).total_seconds(), 3)
        error = (
            ErrorOut(code=run.error_code or "run.failed", message=safe_text(run.error), params=run.error_params)
            if run.error_code or run.error
            else None
        )
        steps = [
            StepOut(
                node_id=node.id,
                label=node_label(node),
                type=node.type,
                status=(run.nodes.get(node.id) or NodeRun()).status,
                error_code=(run.nodes.get(node.id) or NodeRun()).error_code,
            )
            for node in run.workflow.nodes
        ]
        return RunStatusOut(
            run_id=run.id,
            status=API_STATUS[run.status],  # type: ignore[arg-type]
            publication=slug,
            version=run.release_number,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=duration,
            error=error,
            usage=usage_out(run.usage),
            steps=steps,
            approvals=self.approvals(run) if run.status == "paused" else [],
            resumed_from=run.resumed_from,
            links=self.links(slug, run.id),
        )

    def result(self, run: Run) -> RunResultOut:
        if run.status in ("queued", "running", "paused"):
            raise ApiError(
                409,
                "run.not_finished",
                "The run has not finished yet; follow its status or events",
                run_status=API_STATUS[run.status],
            )
        status = self.status("", run)
        files = [self._file_out(entry) for entry in self.host.files.list(run.id)]
        return RunResultOut(
            run_id=run.id,
            status=status.status,
            output=run.output if run.status == "succeeded" else None,
            error=status.error,
            files=files,
            usage=status.usage,
        )

    @staticmethod
    def _file_out(entry: Any) -> dict[str, Any]:
        return {
            "id": entry.id,
            "name": entry.name,
            "format": entry.format,
            "media_type": entry.media_type,
            "size": entry.size,
            "created_at": entry.created_at.isoformat(),
            "error_code": entry.error_code,
        }

    def info(self, release: ServedRelease, versions: list[int]) -> InfoOut:
        return InfoOut(
            publication=release.slug,
            name=release.name,
            description=release.description,
            version=release.number,
            versions=versions,
            published_at=release.published_at,
            input_schema=input_model(release.workflow).model_json_schema(),
            output_schema=release.output_schema,
            example_request=example_values(release.workflow),
            approvals=release.approvals > 0,
            sync={"enabled": release.limits.sync_enabled, "timeout_seconds": release.limits.sync_timeout_seconds},
            limits=release.limits.model_dump(include={"max_concurrent_runs", "runs_per_minute", "requests_per_minute"}),
            missing_resources=release.missing_resources,
            file_inputs=file_inputs(release.workflow),
            uploads={
                "json": "files as {name, media_type, content_base64} in values.<field> or files",
                "multipart": "multipart/form-data: a part per file named after its field (or files), "
                "values as a JSON part 'values', free text as 'input'",
                "max_upload_mb": release.limits.max_upload_mb,
            },
        )

    # ---- runs -----------------------------------------------------------------------

    def _summaries(self, publication_id: str) -> list[RunSummary]:
        return api_run_summaries(self.host.store, publication_id)

    def spent_since(self, publication_id: str, since: datetime, summaries: list[RunSummary] | None = None) -> float:
        return sum(
            summary.usage.estimated_cost_usd or 0.0
            for summary in (self._summaries(publication_id) if summaries is None else summaries)
            if summary.created_at >= since
        )

    def check_limits(self, release: ServedRelease) -> None:
        limits = release.limits
        live = [
            run
            for run in self.host.runs.live_runs()
            if run.trigger == "api" and run.trigger_ref == release.publication_id and run.parent_run_id is None
        ]
        running = sum(run.status in ("queued", "running") for run in live)
        if running >= limits.max_concurrent_runs:
            raise ApiError(
                429,
                "limit.concurrent",
                "Too many runs at the same time; try again when one has finished",
                headers={"Retry-After": "10"},
                limit=limits.max_concurrent_runs,
            )
        summaries = self._summaries(release.publication_id)
        waiting = sum(summary.status == "paused" for summary in summaries)
        if waiting >= limits.max_waiting_runs and limits.max_waiting_runs >= 0:
            raise ApiError(
                429,
                "limit.waiting",
                "Too many runs wait for an approval; decide them first",
                limit=limits.max_waiting_runs,
            )
        if limits.daily_budget_usd is not None:
            spent = self.spent_since(release.publication_id, now() - timedelta(hours=24), summaries)
            if spent >= limits.daily_budget_usd:
                raise ApiError(
                    429,
                    "limit.daily_budget",
                    "The daily cost limit of this API is reached",
                    headers={"Retry-After": "3600"},
                    limit_usd=limits.daily_budget_usd,
                    spent_usd=round(spent, 4),
                )

    def parse_body(self, release: ServedRelease, raw: Any) -> BaseModel:
        model = input_model(release.workflow)
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            errors = [
                {
                    "field": ".".join(str(part) for part in item["loc"]),
                    "code": item["type"],
                    "message": item["msg"],
                }
                for item in exc.errors()
            ]
            raise ApiError(
                422,
                "request.invalid",
                "The request does not fit the workflow's input",
                errors=errors,
                fields=", ".join(dict.fromkeys(item["field"] or "body" for item in errors)),
            ) from None

    def _decode(self, release: ServedRelease, item: FileInput) -> tuple[str, bytes, str | None]:
        try:
            data = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(
                422, "file.not_base64", f"{item.name}: content_base64 is not valid Base64", name=item.name
            ) from None
        limit = release.limits.max_upload_mb * 1024 * 1024
        if len(data) > limit:
            raise ApiError(
                413,
                "file.too_large",
                f"{item.name} is larger than {release.limits.max_upload_mb} MB",
                name=item.name,
                limit_mb=release.limits.max_upload_mb,
            )
        return item.name, data, item.media_type

    def run_request(self, release: ServedRelease, body: BaseModel) -> RunIn:
        """Turn the validated body into the engine's request; uploaded files are stored first."""
        data = body.model_dump()
        uploads = [self._decode(release, FileInput(**item)) for item in data.get("files") or []]
        values = dict(data.get("values") or {})
        file_fields = {field.name for field in start_fields(release.workflow) if field.type == "file"}
        decoded_fields: dict[str, list[tuple[str, bytes, str | None]]] = {}
        for key in list(values):
            value = values[key]
            if value is None:
                values.pop(key)
            elif key in file_fields:
                items = value if isinstance(value, list) else [value]
                decoded_fields[key] = [self._decode(release, FileInput(**item)) for item in items]
            elif isinstance(value, date):
                values[key] = value.isoformat()
        file_ids = [self.host.add_upload(release, *upload) for upload in uploads]
        for key, items in decoded_fields.items():
            values[key] = [self.host.add_upload(release, *item) for item in items]
        return RunIn(input=str(data.get("input") or ""), values=values, file_ids=file_ids)

    def workflow_for(self, release: ServedRelease) -> Workflow:
        workflow = release.workflow.model_copy(deep=True)
        if release.limits.run_budget_usd is not None:
            workflow.budget_usd = release.limits.run_budget_usd
        return workflow

    async def start(
        self,
        request: Request,
        slug: str,
        *,
        number: int | None,
        channel: str,
        wait: float,
    ) -> Response:
        identity, release = self.access(request, slug, "run", number=number, channel=channel)
        raw = await read_body(request)
        if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            payload = await multipart_payload(request, release)
        else:
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError(400, "request.not_json", "The body must be JSON") from None
        idempotency = request.headers.get("idempotency-key", "").strip()[:200]
        return await self.launch(identity, release, payload, idempotency=idempotency, wait=wait)

    async def launch(
        self, identity: KeyIdentity, release: ServedRelease, payload: Any, *, idempotency: str = "", wait: float = 0
    ) -> Response:
        """Start a run of ``release`` for ``identity`` (also used by the studio's test of an API)."""
        if not release.enabled:
            raise ApiError(503, "publication.disabled", "This API is switched off", headers={"Retry-After": "60"})
        if wait > 0 and not release.limits.sync_enabled:
            raise ApiError(
                400, "sync.disabled", "Waiting for the result is not enabled for this API; poll the status instead"
            )
        body = self.parse_body(release, payload)
        fingerprint = hashlib.sha256(
            json.dumps([release.release_id, payload], sort_keys=True, default=str).encode()
        ).hexdigest()
        record_key = ""
        async with self._start_lock:
            if idempotency:
                record_key = (
                    f"{release.publication_id}:{identity.id}:{hashlib.sha256(idempotency.encode()).hexdigest()}"
                )
                earlier = self.host.store.get_raw(_IDEMPOTENCY, record_key)
                if earlier is not None and datetime.fromisoformat(earlier["at"]) > now() - timedelta(
                    hours=_IDEMPOTENCY_HOURS
                ):
                    if earlier["request"] != fingerprint:
                        raise ApiError(
                            409,
                            "idempotency.conflict",
                            "This Idempotency-Key was used for a different request",
                        )
                    run = self.host.runs.get(earlier["run_id"])
                    if run is not None:
                        return JSONResponse(
                            self.status(release.slug, run).model_dump(mode="json"),
                            status_code=200,
                            headers={"Idempotent-Replayed": "true"},
                        )
            # A replayed retry starts nothing, so only a run that really starts counts as a new run.
            wait_time = self.host_runs_per_minute(identity, release)
            if wait_time is not None:
                raise ApiError(
                    429,
                    "limit.runs",
                    "Too many new runs; slow down",
                    headers={"Retry-After": str(int(wait_time))},
                    limit=release.limits.runs_per_minute,
                )
            self.check_limits(release)
            await self.host.prepare_start(release)
            run_request = self.run_request(release, body)
            run = self.host.runs.start(
                self.workflow_for(release),
                run_request,
                trigger="api",
                trigger_ref=release.publication_id,
                version=release.workflow_version,
                release_id=release.release_id,
                release_number=release.number,
                api_key_id=identity.id,
            )
            if record_key:
                try:
                    self.host.store.put_raw(
                        _IDEMPOTENCY, record_key, {"run_id": run.id, "request": fingerprint, "at": now().isoformat()}
                    )
                except Exception:
                    # The run has started: answering 500 now would make the client retry and start it twice.
                    logger.exception("The Idempotency-Key of run %s could not be stored", run.id)
        status_code = 202
        if wait > 0:
            timeout = min(wait, release.limits.sync_timeout_seconds)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.host.runs.wait(run.id)), timeout=timeout)
            current = self.host.runs.get(run.id) or run
            if current.status in ("succeeded", "failed", "cancelled"):
                body_out = self.status(release.slug, current).model_dump(mode="json")
                body_out["output"] = current.output if current.status == "succeeded" else None
                return JSONResponse(body_out, status_code=200)
            run = current
        return JSONResponse(
            self.status(release.slug, run).model_dump(mode="json"),
            status_code=status_code,
            headers={"Location": f"{PREFIX}/{release.slug}/runs/{run.id}"},
        )

    def host_runs_per_minute(self, identity: KeyIdentity, release: ServedRelease) -> float | None:
        return self._requests.allow(f"runs:{identity.id}:{release.publication_id}", release.limits.runs_per_minute)

    def decide(self, identity: KeyIdentity, release: ServedRelease, run: Run, body: ApprovalDecisionIn) -> Run:
        if run.status != "paused" and not any(state.status == "awaiting_approval" for state in run.nodes.values()):
            raise ApiError(409, "approval.none_pending", "No approval of this run waits for a decision")
        pending = self.approvals(run)
        if body.approval_id is None:
            if not pending:
                raise ApiError(409, "approval.none_pending", "No approval of this run waits for a decision")
            if len(pending) > 1:
                raise ApiError(
                    400,
                    "approval.ambiguous",
                    "Several approvals wait; name one with approval_id",
                    approval_ids=[item.approval_id for item in pending],
                )
            approval_id = pending[0].approval_id
        else:
            approval_id = body.approval_id
        target_run, _, node_id = approval_id.partition(":")
        tree = {run.id} | {item.approval_id.partition(":")[0] for item in pending}
        if target_run not in tree:
            raise ApiError(404, "approval.not_found", "No approval with this id", approval_id=approval_id)
        try:
            self.host.runs.decide(target_run, node_id, body.decision, body.comment, decided_by=f"api:{identity.id}")
        except KeyError:
            raise ApiError(404, "approval.not_found", "No approval with this id", approval_id=approval_id) from None
        except NodestroError as exc:
            if exc.code == "approval.not_pending":
                raise ApiError(409, "approval.already_decided", "This approval was already decided") from None
            if exc.code == "approval.comment_required":
                raise ApiError(422, exc.code, "A rejection needs a comment") from None
            raise
        return self.host.runs.get(run.id) or run

    async def resume(self, identity: KeyIdentity, release: ServedRelease, source: Run, body: ResumeIn) -> Run:
        """Continue a failed or cancelled run of this API with the same release and input."""
        if source.status not in ("failed", "cancelled"):
            raise ApiError(409, "resume.not_failed", "Only failed or cancelled runs can be continued")
        own = self.host.release_of_run(source) or release
        workflow = source.workflow
        probe = Run(
            workflow_id=source.workflow_id,
            workflow_name=source.workflow_name,
            workflow=workflow,
            input=source.input,
            values=source.values,
            file_ids=source.file_ids,
            trigger="api",
            trigger_ref=source.trigger_ref,
            release_id=source.release_id,
        )
        prints = self.host.runs.fingerprints(probe)
        builtin = self.host.builtin_tools()
        preset: dict[str, NodeRun] = {}
        side_effects: set[str] = set()
        for node, action, _reason in [] if body.restart else continuation(workflow, source.nodes, prints):
            old = source.nodes.get(node.id)
            if action == "reuse" and old is not None:
                state = old.model_copy(deep=True)
                state.reused_from = source.id
                state.usage = Usage()
                if state.approval is not None and state.approval.reused_from is None:
                    state.approval.reused_from = source.id
                preset[node.id] = state
            elif old is not None:
                side_effects |= {call.name for call in old.tool_calls if call.name not in builtin and not call.is_error}
        if side_effects and not body.confirm_side_effects:
            raise ApiError(
                409,
                "resume.confirm_side_effects",
                "Steps that run again had called tools; send confirm_side_effects=true to call them again",
                tools=sorted(side_effects),
            )
        async with self._start_lock:
            self.check_limits(own)
            await self.host.prepare_start(own)
            form_files = {str(item) for value in source.values.values() if isinstance(value, list) for item in value}
            request = RunIn(
                input=(source.text_input if source.values else source.input) or "",
                values=source.values,
                file_ids=[item for item in source.file_ids if item not in form_files],
            )
            return self.host.runs.start(
                workflow,
                request,
                trigger="api",
                trigger_ref=source.trigger_ref,
                version=source.workflow_version,
                resumed_from=source.id,
                preset=preset,
                release_id=source.release_id,
                release_number=source.release_number,
                api_key_id=identity.id,
            )

    def usage_summary(self, release: ServedRelease, days: int) -> UsageSummaryOut:
        since = now() - timedelta(days=days)
        every = self._summaries(release.publication_id)
        summaries = [item for item in every if item.created_at >= since]
        result = UsageSummaryOut(days=days, runs=len(summaries), daily_budget_usd=release.limits.daily_budget_usd)
        total = Usage()
        durations = []
        for summary in summaries:
            result.by_status[API_STATUS[summary.status]] = result.by_status.get(API_STATUS[summary.status], 0) + 1
            for name in ("requests", "input_tokens", "output_tokens", "total_tokens"):
                setattr(total, name, getattr(total, name) + getattr(summary.usage, name))
            total.estimated_cost_usd = total_cost([total.estimated_cost_usd, summary.usage.estimated_cost_usd])
            if summary.finished_at is not None:
                durations.append((summary.finished_at - summary.created_at).total_seconds())
        result.usage = usage_out(total)
        if durations:
            result.average_duration_seconds = round(sum(durations) / len(durations), 3)
        result.last_24h_usd = round(self.spent_since(release.publication_id, now() - timedelta(hours=24), every), 6)
        return result

    # ---- events -----------------------------------------------------------------------

    @staticmethod
    def public_event(event: RunEvent, *, text: bool) -> dict[str, Any] | None:
        if event.type == "text":
            if not text:
                return None
            return {"type": "text", "node_id": event.node_id, "delta": event.data.get("delta", "")}
        fields = _PUBLIC_EVENTS.get(event.type)
        if fields is None:
            return None
        data = {key: event.data[key] for key in fields if key in event.data}
        return {"type": event.type, "node_id": event.node_id, "at": event.at.isoformat(), **data}

    async def events(self, request: Request, slug: str, run: Run, *, text: bool) -> StreamingResponse:
        runs = self.host.runs
        last_seen = request.headers.get("last-event-id", "")
        after = int(last_seen) if last_seen.isdigit() else 0

        def sse(kind: str, data: Any, event_id: int | None = None) -> str:
            head = f"id: {event_id}\n" if event_id is not None else ""
            return f"{head}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

        async def stream() -> AsyncIterator[str]:
            if not runs.is_live(run.id):
                current = runs.get(run.id) or run
                yield sse("status", self.status(slug, current).model_dump(mode="json"))
                yield sse("end", {"status": API_STATUS[current.status]})
                return
            history, queue = runs.subscribe(run.id)
            try:
                current = runs.get(run.id) or run
                newest = history[-1].seq if history else 0
                missed = [event for event in history if after and event.seq > after]
                # The status carries the newest id only when nothing is replayed after it: ids
                # stay increasing, and a client that drops during the replay resumes where it was.
                yield sse(
                    "status", self.status(slug, current).model_dump(mode="json"), None if missed else newest or None
                )
                # After a reconnect: what happened since the last event the client saw.
                for event in missed:
                    public = self.public_event(event, text=text)
                    if public is not None:
                        yield sse(event.type, public, event.seq)
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if item is None:
                        final = runs.get(run.id) or run
                        yield sse("end", {"status": API_STATUS[final.status]})
                        return
                    if item.seq <= newest:
                        continue
                    public = self.public_event(item, text=text)
                    if public is not None:
                        yield sse(item.type, public, item.seq)
            finally:
                runs.unsubscribe(run.id, queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# ---- HTTP --------------------------------------------------------------------------------


#: Every endpoint of a published workflow API (method, full path). Each one authenticates its caller with a Nodestro
#: API key (``ServingCore.access`` or ``ServingCore.authenticate``) before it reads or changes anything. The studio's
#: route check (:mod:`nodestro_api.permissions`) accepts exactly these routes under :data:`PREFIX` without a
#: permission, so a new endpoint must be added here, and to the test that calls every one without a key.
KEY_AUTHENTICATED_ROUTES = frozenset(
    (method, PREFIX + path)
    for method, path in [
        ("POST", "/{slug}/runs"),
        ("POST", "/{slug}/versions/{number}/runs"),
        ("GET", "/{slug}"),
        ("GET", "/{slug}/runs"),
        ("GET", "/{slug}/runs/{run_id}"),
        ("GET", "/{slug}/runs/{run_id}/result"),
        ("GET", "/{slug}/runs/{run_id}/events"),
        ("POST", "/{slug}/runs/{run_id}/cancel"),
        ("GET", "/{slug}/runs/{run_id}/approvals"),
        ("POST", "/{slug}/runs/{run_id}/approve"),
        ("POST", "/{slug}/runs/{run_id}/resume"),
        ("GET", "/{slug}/runs/{run_id}/files"),
        ("GET", "/{slug}/runs/{run_id}/files/{file_id}"),
        ("GET", "/{slug}/usage"),
        ("GET", "/{slug}/openapi.json"),
        # Nodestro's MCP server (WP-11, nodestro_api.mcp_server): same keys, off unless switched on.
        ("POST", "/_mcp"),
        ("GET", "/_mcp"),
        ("DELETE", "/_mcp"),
    ]
)


class KeyAuthenticatedRoute(APIRoute):
    """A published endpoint: authenticated by API key, every error (FastAPI's own too) in the API's error format."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except ApiError as exc:
                return exc.response()
            except RequestValidationError as exc:
                errors = [
                    {"field": ".".join(str(p) for p in item["loc"]), "code": item["type"], "message": item["msg"]}
                    for item in exc.errors()
                ]
                return ApiError(422, "request.invalid", "The request is invalid", errors=errors).response()
            except Exception as exc:
                return _error_of(exc).response()

        return guarded


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}

_BODY_DOC = {"requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}}


def build_router(
    core: ServingCore,
    *,
    channel: Literal["local", "network"] = "local",
    release: ServedRelease | None = None,
    include_in_schema: bool = True,
) -> APIRouter:
    """The endpoints of published workflows.

    ``channel="local"`` answers only clients on this computer (the studio's own port);
    ``"network"`` is the separate listener for other devices, which serves nothing else.
    ``release`` documents the request and result schemas of one release in the OpenAPI
    description (an export, or the per-API description in Nodestro).
    """
    router = APIRouter(prefix=f"{PREFIX}/{{slug}}", route_class=KeyAuthenticatedRoute, tags=["published workflow"])
    doc = include_in_schema
    body_doc: dict[str, Any] = _BODY_DOC
    if release is not None:
        body_doc = {
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{input_model(release.workflow).__name__}"},
                        "example": example_values(release.workflow),
                    },
                    "multipart/form-data": {"schema": multipart_schema(release.workflow)},
                },
            }
        }

    def guard(request: Request) -> None:
        if channel == "local":
            client = request.client.host if request.client else ""
            if client not in _LOOPBACK:
                raise ApiError(
                    403,
                    "lan.not_enabled",
                    "Published APIs answer only this computer; enable network access in Nodestro",
                )

    def int_param(request: Request, name: str, default: int, low: int, high: int) -> int:
        raw = request.query_params.get(name, "") or str(default)
        try:
            value = int(raw)
        except ValueError:
            raise ApiError(422, "request.invalid", f"{name} must be a whole number", field=name) from None
        return min(max(value, low), high)

    def wait_param(request: Request) -> float:
        raw = request.query_params.get("wait", "0")
        try:
            return max(float(raw), 0.0)
        except ValueError:
            raise ApiError(422, "request.invalid", "wait must be a number of seconds") from None

    async def start(request: Request, slug: str) -> Response:
        guard(request)
        return await core.start(request, slug, number=None, channel=channel, wait=wait_param(request))

    async def start_version(request: Request, slug: str, number: int) -> Response:
        guard(request)
        return await core.start(request, slug, number=number, channel=channel, wait=wait_param(request))

    router.add_api_route(
        "/runs",
        start,
        methods=["POST"],
        status_code=202,
        summary="Start a run of the active version",
        description="Starts the workflow asynchronously and answers 202 with the run's status. "
        "Optional: header Idempotency-Key (a retry returns the same run), query wait=<seconds> "
        "(only when synchronous calls are enabled; answers 200 with the result if it finishes in time).",
        openapi_extra=body_doc,
        responses={
            202: {"model": RunStatusOut, "description": "Started"},
            200: {"model": RunStatusOut, "description": "Finished in time (wait) or replayed (Idempotency-Key)"},
            422: {"description": "The request does not fit the input form"},
            429: {"description": "A limit is reached (see error.code)"},
        },
        include_in_schema=doc,
    )
    router.add_api_route(
        "/versions/{number}/runs",
        start_version,
        methods=["POST"],
        status_code=202,
        summary="Start a run of a specific published version",
        openapi_extra=body_doc,
        responses={202: {"model": RunStatusOut, "description": "Started"}},
        include_in_schema=doc,
    )

    async def info(request: Request, slug: str) -> InfoOut:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        versions = getattr(core.host, "versions", lambda _slug: [served.number])(slug)
        return core.info(served, versions)

    router.add_api_route("", info, methods=["GET"], summary="What this API expects and returns", include_in_schema=doc)

    async def list_runs(request: Request, slug: str) -> list[RunStatusOut]:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        limit = int_param(request, "limit", 20, 1, 100)
        wanted = request.query_params.get("status")
        found = []
        for summary in core._summaries(served.publication_id):
            if wanted and API_STATUS[summary.status] != wanted:
                continue
            run = core.host.runs.get(summary.id)
            if run is not None:
                found.append(core.status(served.slug, run))
            if len(found) >= limit:
                break
        return found

    router.add_api_route(
        "/runs", list_runs, methods=["GET"], summary="The latest runs of this API", include_in_schema=doc
    )

    async def get_run(request: Request, slug: str, run_id: str) -> RunStatusOut:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        return core.status(served.slug, core.own_run(served, run_id))

    router.add_api_route("/runs/{run_id}", get_run, methods=["GET"], summary="Status of a run", include_in_schema=doc)

    async def get_result(request: Request, slug: str, run_id: str) -> Response:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        return JSONResponse(core.result(core.own_run(served, run_id)).model_dump(mode="json"))

    router.add_api_route(
        "/runs/{run_id}/result",
        get_result,
        methods=["GET"],
        summary="Result of a finished run",
        responses={200: {"model": RunResultOut}, 409: {"description": "Not finished yet"}},
        include_in_schema=doc,
    )

    async def get_events(request: Request, slug: str, run_id: str) -> StreamingResponse:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        run = core.own_run(served, run_id)
        text = request.query_params.get("text", "").lower() in ("1", "true", "yes")
        return await core.events(request, served.slug, run, text=text)

    router.add_api_route(
        "/runs/{run_id}/events",
        get_events,
        methods=["GET"],
        summary="Progress as Server-Sent Events",
        description="A status event first, then progress events until 'end'. Reconnect with "
        "Last-Event-ID to receive what you missed. ?text=true adds the agents' text as it is written.",
        include_in_schema=doc,
    )

    async def cancel(request: Request, slug: str, run_id: str) -> RunStatusOut:
        guard(request)
        _, served = core.access(request, slug, "run", channel=channel)
        run = core.own_run(served, run_id)
        if run.status in ("succeeded", "failed", "cancelled"):
            raise ApiError(409, "run.finished", "The run has already ended", run_status=API_STATUS[run.status])
        core.host.runs.cancel(run.id)
        return core.status(served.slug, core.host.runs.get(run.id) or run)

    router.add_api_route(
        "/runs/{run_id}/cancel", cancel, methods=["POST"], summary="Cancel a run", include_in_schema=doc
    )

    async def list_approvals(request: Request, slug: str, run_id: str) -> list[ApprovalOut]:
        guard(request)
        identity = core.authenticate(request)
        served = core.host.release(slug, None, channel=channel)
        scope: Scope = "approve" if "approve" in identity.scopes else "read"
        core.authorize(identity, served, scope)
        return core.approvals(core.own_run(served, run_id))

    router.add_api_route(
        "/runs/{run_id}/approvals",
        list_approvals,
        methods=["GET"],
        summary="Approvals that wait for a decision, with what to review",
        include_in_schema=doc,
    )

    async def approve(request: Request, slug: str, run_id: str, body: ApprovalDecisionIn) -> RunStatusOut:
        guard(request)
        identity, served = core.access(request, slug, "approve", channel=channel)
        run = core.decide(identity, served, core.own_run(served, run_id), body)
        return core.status(served.slug, run)

    router.add_api_route(
        "/runs/{run_id}/approve",
        approve,
        methods=["POST"],
        summary="Approve or reject a waiting approval",
        description="The run continues on the chosen path. A decision is recorded with the key's id; "
        "a second decision on the same approval answers 409.",
        include_in_schema=doc,
    )

    async def resume(request: Request, slug: str, run_id: str, body: ResumeIn) -> Response:
        guard(request)
        identity, served = core.access(request, slug, "run", channel=channel)
        run = await core.resume(identity, served, core.own_run(served, run_id), body)
        return JSONResponse(core.status(served.slug, run).model_dump(mode="json"), status_code=202)

    router.add_api_route(
        "/runs/{run_id}/resume",
        resume,
        methods=["POST"],
        status_code=202,
        summary="Continue a failed run; successful steps are reused",
        include_in_schema=doc,
    )

    async def list_files(request: Request, slug: str, run_id: str) -> list[dict[str, Any]]:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        run = core.own_run(served, run_id)
        return [core._file_out(entry) for entry in core.host.files.list(run.id)]

    router.add_api_route(
        "/runs/{run_id}/files", list_files, methods=["GET"], summary="Result files of a run", include_in_schema=doc
    )

    async def get_file(request: Request, slug: str, run_id: str, file_id: str) -> Response:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        run = core.own_run(served, run_id)
        try:
            entry, data = core.host.files.content(run.id, file_id)
        except KeyError:
            raise ApiError(404, "file.not_found", "No file with this id") from None
        from urllib.parse import quote

        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(entry.name)}",
            "X-Content-Type-Options": "nosniff",
        }
        return Response(content=data, media_type=entry.media_type, headers=headers)

    router.add_api_route(
        "/runs/{run_id}/files/{file_id}",
        get_file,
        methods=["GET"],
        summary="Download a result file",
        include_in_schema=doc,
    )

    async def usage(request: Request, slug: str) -> UsageSummaryOut:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        days = int_param(request, "days", 30, 1, 365)
        return core.usage_summary(served, days)

    router.add_api_route(
        "/usage", usage, methods=["GET"], summary="Runs, tokens and cost of this API", include_in_schema=doc
    )

    async def openapi(request: Request, slug: str) -> Response:
        guard(request)
        _, served = core.access(request, slug, "read", channel=channel)
        return JSONResponse(openapi_document(core, served))

    router.add_api_route(
        "/openapi.json", openapi, methods=["GET"], summary="OpenAPI description of this API", include_in_schema=False
    )
    return router


def add_schemas(document: dict[str, Any], release: ServedRelease) -> dict[str, Any]:
    """Put the release's request and result schemas into an OpenAPI document."""
    model = input_model(release.workflow)
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    components = document.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in schema.pop("$defs", {}).items():
        components[name] = definition
    components[model.__name__] = schema
    components["WorkflowOutput"] = release.output_schema or {"description": "The workflow's result (text or JSON)"}
    result = components.get("RunResultOut")
    if isinstance(result, dict):
        result.setdefault("properties", {})["output"] = {
            "allOf": [{"$ref": "#/components/schemas/WorkflowOutput"}],
            "description": "The workflow's result exactly as the run produced it",
        }
    # The description is of this one API: its name is part of the paths, not a parameter.
    paths = document.get("paths", {})
    for template in list(paths):
        path = paths.pop(template)
        for operation in path.values():
            if isinstance(operation, dict) and "parameters" in operation:
                operation["parameters"] = [item for item in operation["parameters"] if item.get("name") != "slug"]
                if not operation["parameters"]:
                    del operation["parameters"]
        paths[template.replace("{slug}", release.slug)] = path
    document.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKey"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "A Nodestro API key (ndk_...)",
    }
    document["security"] = [{"ApiKey": []}]
    return document


def openapi_document(core: ServingCore, release: ServedRelease, *, servers: list[str] | None = None) -> dict[str, Any]:
    """The OpenAPI description of one release (generated, not served by an app)."""
    app = FastAPI()
    app.include_router(build_router(core, release=release))
    document = get_openapi(
        title=release.name,
        version=f"{release.number}",
        description=release.description or f"Published Nodestro workflow {release.slug}",
        routes=app.routes,
        servers=[{"url": url} for url in servers] if servers else None,
    )
    return add_schemas(document, release)
