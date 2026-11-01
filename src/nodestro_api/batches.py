"""Batch processing: one workflow, many inputs, one run per input.

A batch takes rows (from a CSV or Excel file, or JSON records), maps their columns to
the fields of the workflow's input form (or to the free-text input) and starts one
ordinary run per row through :func:`nodestro_runtime.launcher.launch`, with
``trigger="batch"``. There is no separate engine: every item is a normal run in the
history, with its own trace, budget and usage; the batch only collects them.

- Before the start, :func:`plan` checks every row against the form and counts valid
  and invalid rows, so the user sees what will run and what it will probably cost.
- A batch runs at most ``concurrency`` runs at a time; it can be paused (no new runs
  start, running ones finish), continued and cancelled (running items are cancelled).
- A failed item never stops the others. Optional limits stop the batch: total cost
  (``max_cost_usd``) and number of failed items (``max_failures``).
- An item that waits for an approval does not block a slot; it finishes when a person
  decides.
- After a restart, a batch that was running is ``interrupted``; continuing it starts the
  items that did not finish (failed runs of the interruption are run again).
- Results can be exported as CSV or JSON.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import csv
from datetime import datetime
import io
import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from nodestro_runtime.conditions import parse_result
from nodestro_runtime.documents import rows_of_xlsx
from nodestro_runtime.engine import start_fields
from nodestro_runtime.forms import FieldError, check_values, field_label
from nodestro_runtime.launcher import CostEstimate, approval_steps, estimate_cost, external_tools, launch
from nodestro_runtime.problems import NodestroError, Problem, problem
from nodestro_runtime.schemas import Run, RunIn, RunStatus, RunTrigger, Workflow, new_id, now, total_cost
from nodestro_runtime.tool_preflight import UnattendedTrigger, unattended_tool_issues
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .context import Nodestro

logger = logging.getLogger(__name__)

MAX_ITEMS = 1000
MAX_CONCURRENCY = 8
COLLECTION = "batches"

ItemStatus = Literal["pending", "invalid", "running", "paused", "succeeded", "failed", "cancelled", "skipped"]
BatchStatus = Literal["running", "paused", "finished", "cancelled", "stopped", "interrupted"]
_DONE: frozenset[str] = frozenset({"succeeded", "failed", "cancelled", "skipped", "invalid"})


class BatchError(NodestroError):
    """A batch cannot be created or changed."""


class BatchRow(BaseModel):
    """One input: form values and/or free text."""

    values: dict[str, Any] = Field(default_factory=dict)
    input: str = ""
    #: Knowledge files of this row (in addition to the batch's files).
    file_ids: list[str] = Field(default_factory=list)
    #: An identifier of the row's origin, e.g. the test case.
    ref: str | None = None


class BatchItem(BaseModel):
    index: int
    values: dict[str, Any] = Field(default_factory=dict)
    input: str = ""
    file_ids: list[str] = Field(default_factory=list)
    ref: str | None = None
    status: ItemStatus = "pending"
    run_id: str | None = None
    #: Earlier runs of this item (e.g. interrupted by a restart).
    previous_run_ids: list[str] = Field(default_factory=list)
    errors: list[FieldError] = Field(default_factory=list)
    error_code: str | None = None
    error: str | None = None
    error_params: dict[str, Any] = Field(default_factory=dict)
    #: ``None``: the cost is unknown (a model without known prices).
    cost_usd: float | None = 0.0
    tokens: int = 0
    duration_s: float | None = None
    output: Any = None


def tool_warnings(
    ctx: Nodestro, workflow: Workflow, *, trigger: UnattendedTrigger, trigger_ref: str | None = None
) -> list[Problem]:
    """The tool calls that runs started by ``trigger`` would have refused (risk R1 of the S-2 acceptance), from the
    runtime's own policy (:func:`~nodestro_runtime.tool_preflight.unattended_tool_issues`); empty while per-call
    approvals are off. Only a warning: nothing is granted, and the runtime still refuses at every call."""
    return [
        problem(item.code, item.message, **item.params)
        for item in unattended_tool_issues(ctx, workflow, trigger=trigger, trigger_ref=trigger_ref)
    ]


def with_models(workflow: Workflow, models: dict[str, str]) -> Workflow:
    """A copy of ``workflow`` whose agent nodes named in ``models`` use that model; the stored workflow is unchanged."""
    if not models:
        return workflow
    nodes = [
        node.model_copy(update={"agent": node.agent.model_copy(update={"model": models[node.id]})})
        if node.agent is not None and node.id in models
        else node
        for node in workflow.nodes
    ]
    return workflow.model_copy(update={"nodes": nodes})


class BatchIn(BaseModel):
    workflow_id: str
    name: str = ""
    rows: list[BatchRow] = Field(max_length=MAX_ITEMS)
    #: Knowledge files every run receives.
    file_ids: list[str] = Field(default_factory=list)
    concurrency: int = Field(default=2, ge=1, le=MAX_CONCURRENCY)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_failures: int | None = Field(default=None, ge=1)
    #: The workflow has own tools that may act outside Nodestro; the user agreed to run them for every row.
    confirm_external: bool = False


class BatchCounts(BaseModel):
    total: int = 0
    pending: int = 0
    running: int = 0
    paused: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0
    invalid: int = 0


class Batch(BaseModel):
    id: str = Field(default_factory=lambda: new_id("batch"))
    kind: Literal["batch", "benchmark"] = "batch"
    #: The test series of a benchmark batch.
    ref: str | None = None
    workflow_id: str
    workflow_name: str
    workflow_version: int | None = None
    name: str = ""
    status: BatchStatus = "running"
    stop_code: str | None = None
    stop_params: dict[str, Any] = Field(default_factory=dict)
    items: list[BatchItem] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)
    concurrency: int = 2
    max_cost_usd: float | None = None
    max_failures: int | None = None
    #: Agent node id -> model every run uses instead of the saved one (a test series comparing models, Q-2).
    model_overrides: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    cost_usd: float | None = 0.0
    tokens: int = 0
    counts: BatchCounts = Field(default_factory=BatchCounts)


class BatchSummary(BaseModel):
    id: str
    kind: Literal["batch", "benchmark"] = "batch"
    ref: str | None = None
    workflow_id: str
    workflow_name: str
    workflow_version: int | None = None
    name: str = ""
    status: BatchStatus
    stop_code: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    cost_usd: float | None = 0.0
    counts: BatchCounts = Field(default_factory=BatchCounts)


class BatchPlan(BaseModel):
    """What a batch would do; nothing starts."""

    total: int
    valid: int
    invalid: int
    items: list[BatchItem]
    estimate: CostEstimate
    external_tools: list[str] = Field(default_factory=list)
    #: Tool calls the per-call approval rules would refuse in these runs (only while they are switched on).
    tool_warnings: list[Problem] = Field(default_factory=list)
    approvals: int = 0
    fields: list[str] = Field(default_factory=list)


class ParsedRows(BaseModel):
    columns: list[str]
    rows: list[dict[str, str]]
    #: Suggested mapping column -> form field (``input`` for the free text).
    mapping: dict[str, str]


# ---- reading rows ----------------------------------------------------------------------


def parse_table(data: bytes, name: str) -> list[dict[str, str]]:
    """Records from a CSV (comma or semicolon, UTF-8 or Windows-1252), XLSX or JSON file."""
    lower = name.lower()
    if lower.endswith(".xlsx"):
        return rows_of_xlsx(data, name)
    if lower.endswith(".json"):
        try:
            value = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise BatchError("batch.file_unreadable", f"{name} is not valid JSON", name=name) from None
        if isinstance(value, dict):
            value = next((item for item in value.values() if isinstance(item, list)), [value])
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise BatchError("batch.file_unreadable", f"{name} must contain a list of objects", name=name)
        return [{str(key): _cell(item) for key, item in record.items()} for record in value]
    if not lower.endswith((".csv", ".tsv", ".txt")):
        raise BatchError("batch.file_type", f"{name}: use a CSV, XLSX or JSON file", name=name)
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = data.decode("cp1252", errors="replace")
    try:
        dialect: Any = csv.Sniffer().sniff(content[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    rows = []
    for record in reader:
        cleaned = {str(key).strip(): (value or "").strip() for key, value in record.items() if key is not None}
        if any(cleaned.values()):
            rows.append(cleaned)
    return rows


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def suggest_mapping(columns: list[str], workflow: Workflow) -> dict[str, str]:
    """Columns named like a form field (name or label, any case) map to it; ``input`` takes the rest."""
    fields = start_fields(workflow)
    names = {field.name.casefold(): field.name for field in fields}
    names.update({field_label(field).casefold(): field.name for field in fields})
    mapping: dict[str, str] = {}
    for column in columns:
        key = column.strip().casefold()
        if key in names:
            mapping[column] = names[key]
        elif key in ("input", "eingabe", "text", "anfrage", "entrada", "entrée", "testo") and not fields:
            mapping[column] = "input"
    if not fields and not mapping and len(columns) == 1:
        mapping[columns[0]] = "input"
    return mapping


def rows_from_records(records: list[dict[str, str]], mapping: dict[str, str]) -> list[BatchRow]:
    rows = []
    for record in records:
        row = BatchRow()
        extra: list[str] = []
        for column, value in record.items():
            target = mapping.get(column)
            if target == "input":
                row.input = f"{row.input}\n{value}".strip() if row.input else value
            elif target:
                row.values[target] = value
            elif value and not mapping:
                extra.append(f"{column}: {value}")
        if extra:
            row.input = "\n".join(extra)
        rows.append(row)
    return rows


# ---- the manager -------------------------------------------------------------------------


def _summary(batch: Batch) -> BatchSummary:
    return BatchSummary(**batch.model_dump(exclude={"items", "file_ids"}))


class Batches:
    """Creates, runs and remembers batches."""

    def __init__(self, ctx: Nodestro) -> None:
        self.ctx = ctx
        self._live: dict[str, Batch] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._done: dict[str, asyncio.Event] = {}
        ctx.runs.listeners.append(self._run_changed)
        self._recover()

    # ---- storage ------------------------------------------------------------

    def get(self, batch_id: str) -> Batch:
        batch = self._live.get(batch_id) or self.ctx.store.get(COLLECTION, batch_id, Batch)
        if batch is None:
            raise KeyError(batch_id)
        return batch

    def list(self, *, kind: str | None = "batch", ref: str | None = None) -> list[BatchSummary]:
        found = []
        for batch in self.ctx.store.list(COLLECTION, Batch, limit=500):
            current = self._live.get(batch.id, batch)
            if (kind is None or current.kind == kind) and (ref is None or current.ref == ref):
                found.append(_summary(current))
        return sorted(found, key=lambda item: item.created_at, reverse=True)

    def _save(self, batch: Batch) -> None:
        self._count(batch)
        self.ctx.store.put(COLLECTION, batch.id, batch)

    def delete(self, batch_id: str) -> None:
        batch = self.get(batch_id)
        if batch.status in ("running", "paused") and batch_id in self._tasks:
            raise BatchError("batch.delete_running", "Cancel the batch before deleting it")
        self.ctx.store.delete(COLLECTION, batch_id)
        self._live.pop(batch_id, None)

    def _recover(self) -> None:
        for batch in self.ctx.store.list(COLLECTION, Batch, limit=500):
            if batch.status == "running":
                batch.status = "interrupted"
                batch.stop_code = "batch.interrupted"
                for item in batch.items:
                    self._sync_item(item)
                self._save(batch)

    @staticmethod
    def _count(batch: Batch) -> None:
        counts = BatchCounts(total=len(batch.items))
        for item in batch.items:
            setattr(counts, item.status, getattr(counts, item.status) + 1)
        batch.counts = counts
        batch.cost_usd = total_cost(item.cost_usd for item in batch.items)
        batch.tokens = sum(item.tokens for item in batch.items)

    # ---- planning ------------------------------------------------------------

    def _workflow(self, workflow_id: str, version: int | None = None) -> tuple[Workflow, int | None]:
        """The workflow as saved (``version`` ``None``) or in one of its versions."""
        if version is not None:
            return self.ctx.resolve_workflow(workflow_id, version)
        workflow = self.ctx.store.get("workflows", workflow_id, Workflow)
        if workflow is None:
            raise KeyError(workflow_id)
        return workflow, self.ctx.current_version(workflow)

    def _items(self, workflow: Workflow, rows: builtins.list[BatchRow]) -> builtins.list[BatchItem]:
        fields = start_fields(workflow)
        known = self.ctx.file_catalog()
        items = []
        for index, row in enumerate(rows):
            item = BatchItem(
                index=index, values=dict(row.values), input=row.input, file_ids=list(row.file_ids), ref=row.ref
            )
            missing = [file_id for file_id in row.file_ids if file_id not in known]
            if fields:
                _, errors = check_values(fields, row.values, known_files=known)
                if errors:
                    item.status, item.errors = "invalid", errors
            elif not row.input.strip() and not row.file_ids:
                item.status = "invalid"
                item.errors = [FieldError(field="input", code="batch.input_empty", message="The row has no input")]
            if missing:
                item.status = "invalid"
                item.errors.append(
                    FieldError(
                        field="files",
                        code="form.file_missing",
                        message=f"{len(missing)} files no longer exist",
                        params={"count": len(missing)},
                    )
                )
            items.append(item)
        return items

    def plan(self, request: BatchIn) -> BatchPlan:
        workflow, _ = self._workflow(request.workflow_id)
        if not request.rows:
            raise BatchError("batch.empty", "The batch has no rows")
        items = self._items(workflow, request.rows)
        valid = sum(item.status != "invalid" for item in items)
        return BatchPlan(
            total=len(items),
            valid=valid,
            invalid=len(items) - valid,
            items=items,
            estimate=estimate_cost(self.ctx, workflow.id, valid),
            external_tools=external_tools(self.ctx, workflow),
            tool_warnings=tool_warnings(self.ctx, workflow, trigger="batch"),
            approvals=approval_steps(self.ctx, workflow),
            fields=[field.name for field in start_fields(workflow)],
        )

    # ---- running -------------------------------------------------------------

    def create(
        self,
        request: BatchIn,
        *,
        kind: Literal["batch", "benchmark"] = "batch",
        ref: str | None = None,
        version: int | None = None,
        models: dict[str, str] | None = None,
    ) -> Batch:
        workflow, version = self._workflow(request.workflow_id, version)
        if not request.rows:
            raise BatchError("batch.empty", "The batch has no rows")
        tools = external_tools(self.ctx, workflow)
        if tools and not request.confirm_external:
            raise BatchError(
                "batch.confirm_external",
                f"The workflow uses own tools that may act outside Nodestro: {', '.join(tools)}",
                tools=", ".join(tools),
            )
        items = self._items(workflow, request.rows)
        if not any(item.status != "invalid" for item in items):
            raise BatchError("batch.no_valid_rows", "No row fits the input form")
        batch = Batch(
            kind=kind,
            ref=ref,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            workflow_version=version,
            name=request.name.strip() or workflow.name,
            items=items,
            file_ids=request.file_ids,
            concurrency=request.concurrency,
            max_cost_usd=request.max_cost_usd,
            max_failures=request.max_failures,
            model_overrides=dict(models or {}),
        )
        self._save(batch)
        self._start(batch)
        return batch

    def _start(self, batch: Batch) -> None:
        batch.status = "running"
        batch.stop_code, batch.stop_params, batch.finished_at = None, {}, None
        self._live[batch.id] = batch
        self._wake[batch.id] = asyncio.Event()
        self._done.setdefault(batch.id, asyncio.Event()).clear()
        self._save(batch)
        self._tasks[batch.id] = asyncio.get_running_loop().create_task(self._drive(batch))

    def pause(self, batch_id: str) -> Batch:
        batch = self.get(batch_id)
        if batch.status != "running":
            raise BatchError("batch.not_running", "The batch is not running")
        batch.status = "paused"
        self._save(batch)
        self._poke(batch_id)
        return batch

    def resume(self, batch_id: str) -> Batch:
        batch = self.get(batch_id)
        if batch.status not in ("paused", "interrupted", "stopped"):
            raise BatchError("batch.not_paused", "Only a paused, interrupted or stopped batch can be continued")
        for item in batch.items:
            if item.status == "failed" and item.error_code == "run.interrupted" and item.run_id:
                item.previous_run_ids.append(item.run_id)
                item.run_id, item.status, item.error_code, item.error = None, "pending", None, None
        if batch.id in self._tasks and not self._tasks[batch.id].done():
            batch.status = "running"
            self._save(batch)
            self._poke(batch_id)
        else:
            self._start(batch)
        return batch

    async def cancel(self, batch_id: str) -> Batch:
        batch = self.get(batch_id)
        if batch.status in ("finished", "cancelled"):
            return batch
        batch.status = "cancelled"
        batch.stop_code = "batch.cancelled"
        for item in batch.items:
            if item.status == "pending":
                item.status = "skipped"
        self._save(batch)
        for item in batch.items:
            if item.run_id and item.status in ("running", "paused"):
                with contextlib.suppress(KeyError, NodestroError):
                    self.ctx.runs.cancel(item.run_id)
        self._poke(batch_id)
        if batch_id not in self._tasks or self._tasks[batch_id].done():
            self._finish(batch)
        return batch

    async def wait(self, batch_id: str) -> Batch:
        task = self._tasks.get(batch_id)
        if task is not None:
            await asyncio.shield(task)
        return self.get(batch_id)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _poke(self, batch_id: str) -> None:
        event = self._wake.get(batch_id)
        if event is not None:
            event.set()

    def _limit_reached(self, batch: Batch) -> tuple[str, dict[str, Any]] | None:
        self._count(batch)
        # Unknown costs cannot be limited: the known part counts (like Orkexa's budget, which warns).
        known = sum(item.cost_usd for item in batch.items if item.cost_usd is not None)
        if batch.max_cost_usd is not None and known >= batch.max_cost_usd:
            return "batch.cost_limit", {"limit": batch.max_cost_usd, "cost": round(known, 6)}
        if batch.max_failures is not None and batch.counts.failed >= batch.max_failures:
            return "batch.failure_limit", {"limit": batch.max_failures}
        return None

    async def _drive(self, batch: Batch) -> None:
        try:
            workflow = self._pinned(batch)
            while True:
                wake = self._wake[batch.id]
                wake.clear()
                if batch.status == "cancelled":
                    break
                limit = self._limit_reached(batch)
                if limit is not None and batch.status == "running":
                    batch.status, (batch.stop_code, batch.stop_params) = "stopped", limit
                    for item in batch.items:
                        if item.status == "pending":
                            item.status = "skipped"
                    self._save(batch)
                active = sum(item.status == "running" for item in batch.items)
                waiting = [item for item in batch.items if item.status == "pending"]
                if batch.status == "running":
                    for item in waiting[: max(batch.concurrency - active, 0)]:
                        stop = await self._launch(batch, workflow, item)
                        if stop is not None:
                            batch.status, (batch.stop_code, batch.stop_params) = "stopped", stop
                            for rest in batch.items:
                                if rest.status == "pending":
                                    rest.status = "skipped"
                            break
                    self._save(batch)
                unsettled = [item for item in batch.items if item.status not in _DONE]
                if not unsettled:
                    break
                busy = any(item.status in ("running", "paused") for item in batch.items)
                if batch.status in ("stopped", "cancelled") and not busy:
                    break
                # Wait for a run to change, or for the user to continue, pause or cancel.
                await wake.wait()
        except asyncio.CancelledError:
            batch.status = "interrupted"
            batch.stop_code = "batch.interrupted"
            self._save(batch)
            raise
        except Exception as error:  # pragma: no cover - defensive: the batch must not hang
            logger.exception("Batch %s failed", batch.id)
            batch.status, batch.stop_code, batch.stop_params = "stopped", "batch.error", {"error": str(error)}
        self._finish(batch)

    def _finish(self, batch: Batch) -> None:
        if batch.status == "running":
            batch.status = "finished"
        if batch.status in ("finished", "cancelled", "stopped"):
            batch.finished_at = now()
        self._save(batch)
        self._live.pop(batch.id, None)
        self._done.setdefault(batch.id, asyncio.Event()).set()

    def _pinned(self, batch: Batch) -> Workflow:
        """The workflow every item runs: the saved version the batch started with, with its model overrides."""
        workflow: Workflow | None = None
        if batch.workflow_version is not None:
            with contextlib.suppress(KeyError):
                workflow, _ = self.ctx.resolve_workflow(batch.workflow_id, batch.workflow_version)
        if workflow is None:
            workflow, _ = self._workflow(batch.workflow_id)
        return with_models(workflow, batch.model_overrides)

    async def _launch(self, batch: Batch, workflow: Workflow, item: BatchItem) -> tuple[str, dict[str, Any]] | None:
        """Start one item; returns a reason to stop the whole batch (missing key, invalid workflow)."""
        files = list(dict.fromkeys([*batch.file_ids, *item.file_ids]))
        request = RunIn(input=item.input, values=item.values, file_ids=files)
        trigger: RunTrigger = "benchmark" if batch.kind == "benchmark" else "batch"
        try:
            run = await launch(
                self.ctx,
                workflow,
                request,
                trigger=trigger,
                trigger_ref=batch.id,
                version=batch.workflow_version,
            )
        except NodestroError as error:
            if error.code.startswith("form."):
                item.status, item.error_code, item.error = "invalid", error.code, str(error)
                item.error_params = dict(error.params)
                return None
            item.status, item.error_code, item.error = "failed", error.code, str(error)
            item.error_params = dict(error.params)
            return error.code, dict(error.params)
        item.run_id, item.status = run.id, "running"
        if run.status not in ("queued", "running"):
            self._apply(item, run)
        return None

    # ---- following runs ------------------------------------------------------

    def _run_changed(self, run: Run) -> None:
        if run.trigger not in ("batch", "benchmark") or not run.trigger_ref:
            return
        batch = self._live.get(run.trigger_ref)
        if batch is None:
            stored = self.ctx.store.get(COLLECTION, run.trigger_ref, Batch)
            if stored is None:
                return
            batch = stored
        item = next((item for item in batch.items if item.run_id == run.id), None)
        if item is None:
            return
        self._apply(item, run)
        self._save(batch)
        self._poke(batch.id)

    def _sync_item(self, item: BatchItem) -> None:
        if item.run_id and item.status in ("running", "paused"):
            run = self.ctx.store.get("runs", item.run_id, Run)
            if run is not None:
                self._apply(item, run)

    @staticmethod
    def _apply(item: BatchItem, run: Run) -> None:
        status: RunStatus = run.status
        mapped: dict[str, ItemStatus] = {
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "paused": "paused",
            "queued": "running",
            "running": "running",
        }
        item.status = mapped.get(status, "failed")
        item.error_code, item.error, item.error_params = run.error_code, run.error, dict(run.error_params)
        item.cost_usd = run.usage.estimated_cost_usd
        item.tokens = run.usage.total_tokens
        if run.started_at and run.finished_at:
            item.duration_s = round((run.finished_at - run.started_at).total_seconds(), 3)
        item.output = run.output if status == "succeeded" else None


# ---- export ------------------------------------------------------------------------------


def export_rows(batch: Batch) -> list[dict[str, Any]]:
    fields = list(dict.fromkeys(name for item in batch.items for name in item.values))
    rows = []
    for item in batch.items:
        row: dict[str, Any] = {"row": item.index + 1}
        row.update({name: item.values.get(name, "") for name in fields})
        if any(entry.input for entry in batch.items):
            row["input"] = item.input
        output = parse_result(item.output)
        if isinstance(output, dict) and all(not isinstance(value, (dict, list)) for value in output.values()):
            row.update({f"result.{key}": value for key, value in output.items()})
        else:
            row["result"] = (
                output if isinstance(output, str) or output is None else json.dumps(output, ensure_ascii=False)
            )
        row.update(
            {
                "status": item.status,
                "error": item.error_code or "",
                "cost_usd": round(item.cost_usd, 6) if item.cost_usd is not None else "",
                "tokens": item.tokens,
                "duration_s": item.duration_s if item.duration_s is not None else "",
                "run_id": item.run_id or "",
            }
        )
        rows.append(row)
    return rows


def export_csv(batch: Batch) -> bytes:
    rows = export_rows(batch)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: ("'" + value if isinstance(value, str) and value[:1] in ("=", "+", "@") else value)
                for key, value in row.items()
            }
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")
