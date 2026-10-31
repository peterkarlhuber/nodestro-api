"""The API of batches, workflow test series and schedules (``/api/batches``, ``/api/benchmarks``,
``/api/schedules``). All three start ordinary runs through the one engine."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile
from nodestro_runtime.schemas import Workflow
from pydantic import BaseModel

from .api import Ctx, _quote
from .batches import (
    Batch,
    BatchError,
    BatchIn,
    BatchPlan,
    BatchSummary,
    ParsedRows,
    export_csv,
    export_rows,
    parse_table,
    suggest_mapping,
)
from .benchmarks import BenchmarkPlan, BenchmarkRun, BenchmarkStart, Comparison, Suite, SuiteIn, compare
from .permissions import EDIT, READ, RUN
from .schedules import Activation, Schedule, ScheduleIn, time_zones, upcoming

router = APIRouter(prefix="/api")

_MAX_TABLE_BYTES = 10 * 1024 * 1024


# ---- batches ---------------------------------------------------------------------------


@router.post("/batches/parse", dependencies=READ)
async def parse_batch_file(
    ctx: Ctx, file: Annotated[UploadFile, File()], workflow_id: Annotated[str, Form()]
) -> ParsedRows:
    """Rows of a CSV, XLSX or JSON file and a suggested mapping to the workflow's form; nothing starts."""
    workflow = ctx.store.get("workflows", workflow_id, Workflow)
    if workflow is None:
        raise KeyError(workflow_id)
    data = await file.read()
    if len(data) > _MAX_TABLE_BYTES:
        raise BatchError("batch.file_too_large", "The file is larger than 10 MB", limit_mb=10)
    rows = parse_table(data, file.filename or "rows.csv")
    if not rows:
        raise BatchError("batch.file_empty", "The file contains no rows")
    if len(rows) > 1000:
        raise BatchError("batch.too_many_rows", "A batch takes at most 1000 rows", limit=1000)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    return ParsedRows(columns=columns, rows=rows, mapping=suggest_mapping(columns, workflow))


@router.post("/batches/plan", dependencies=READ)
def plan_batch(body: BatchIn, ctx: Ctx) -> BatchPlan:
    """Counts valid and invalid rows and estimates the cost; nothing starts."""
    return ctx.batches.plan(body)


@router.post("/batches", status_code=201, dependencies=RUN)
async def create_batch(body: BatchIn, ctx: Ctx) -> Batch:
    return ctx.batches.create(body)


@router.get("/batches", dependencies=READ)
def list_batches(ctx: Ctx) -> list[BatchSummary]:
    return ctx.batches.list()


@router.get("/batches/{batch_id}", dependencies=READ)
def get_batch(batch_id: str, ctx: Ctx) -> Batch:
    return ctx.batches.get(batch_id)


@router.post("/batches/{batch_id}/pause", dependencies=RUN)
async def pause_batch(batch_id: str, ctx: Ctx) -> Batch:
    return ctx.batches.pause(batch_id)


@router.post("/batches/{batch_id}/resume", dependencies=RUN)
async def resume_batch(batch_id: str, ctx: Ctx) -> Batch:
    return ctx.batches.resume(batch_id)


@router.post("/batches/{batch_id}/cancel", dependencies=RUN)
async def cancel_batch(batch_id: str, ctx: Ctx) -> Batch:
    return await ctx.batches.cancel(batch_id)


@router.delete("/batches/{batch_id}", status_code=204, dependencies=EDIT)
def delete_batch(batch_id: str, ctx: Ctx) -> Response:
    ctx.batches.delete(batch_id)
    return Response(status_code=204)


@router.get("/batches/{batch_id}/export", dependencies=READ)
def export_batch(batch_id: str, ctx: Ctx, format: str = "csv") -> Response:
    batch = ctx.batches.get(batch_id)
    stem = f"batch_{batch.name or batch.id}"
    if format == "json":
        data = json.dumps(export_rows(batch), ensure_ascii=False, indent=2, default=str).encode("utf-8")
        media, name = "application/json", f"{stem}.json"
    else:
        data, media, name = export_csv(batch), "text/csv; charset=utf-8", f"{stem}.csv"
    return Response(
        content=data, media_type=media, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(name)}"}
    )


# ---- test series -----------------------------------------------------------------------


@router.get("/benchmarks", dependencies=READ)
def list_suites(ctx: Ctx, workflow_id: str | None = None) -> list[Suite]:
    return ctx.benchmarks.suites(workflow_id)


@router.post("/benchmarks", status_code=201, dependencies=EDIT)
def create_suite(body: SuiteIn, ctx: Ctx) -> Suite:
    return ctx.benchmarks.save_suite(body)


@router.get("/benchmarks/{suite_id}", dependencies=READ)
def get_suite(suite_id: str, ctx: Ctx) -> Suite:
    return ctx.benchmarks.suite(suite_id)


@router.put("/benchmarks/{suite_id}", dependencies=EDIT)
def update_suite(suite_id: str, body: SuiteIn, ctx: Ctx) -> Suite:
    return ctx.benchmarks.save_suite(body, suite_id)


@router.delete("/benchmarks/{suite_id}", status_code=204, dependencies=EDIT)
def delete_suite(suite_id: str, ctx: Ctx) -> Response:
    ctx.benchmarks.delete_suite(suite_id)
    return Response(status_code=204)


@router.post("/benchmarks/{suite_id}/plan", dependencies=READ)
def plan_benchmark(suite_id: str, body: BenchmarkStart, ctx: Ctx) -> BenchmarkPlan:
    """Cost estimate, external tools and approval steps of a test run; nothing starts."""
    return ctx.benchmarks.plan(suite_id, body)


@router.post("/benchmarks/{suite_id}/runs", status_code=201, dependencies=RUN)
async def start_benchmark(suite_id: str, body: BenchmarkStart, ctx: Ctx) -> BenchmarkRun:
    return ctx.benchmarks.start(suite_id, body)


@router.get("/benchmarks/{suite_id}/runs", dependencies=READ)
def list_benchmark_runs(suite_id: str, ctx: Ctx) -> list[BenchmarkRun]:
    return ctx.benchmarks.runs(suite_id)


@router.get("/benchmark-runs/compare", dependencies=READ)
def compare_benchmark_runs(a: str, b: str, ctx: Ctx) -> Comparison:
    """Two test runs of a series case by case (e.g. two workflow versions)."""
    return compare(ctx.benchmarks.run(a), ctx.benchmarks.run(b))


@router.get("/benchmark-runs/{run_id}", dependencies=READ)
def get_benchmark_run(run_id: str, ctx: Ctx) -> BenchmarkRun:
    return ctx.benchmarks.run(run_id)


@router.post("/benchmark-runs/{run_id}/cancel", dependencies=RUN)
async def cancel_benchmark_run(run_id: str, ctx: Ctx) -> BenchmarkRun:
    return await ctx.benchmarks.cancel(run_id)


@router.delete("/benchmark-runs/{run_id}", status_code=204, dependencies=EDIT)
def delete_benchmark_run(run_id: str, ctx: Ctx) -> Response:
    ctx.benchmarks.delete_run(run_id)
    return Response(status_code=204)


# ---- schedules ---------------------------------------------------------------------------


class ScheduleState(BaseModel):
    schedule: Schedule
    activation: Activation | None = None


@router.get("/schedules/time-zones", dependencies=READ)
def list_time_zones() -> list[str]:
    return time_zones()


@router.post("/schedules/preview", dependencies=READ)
def preview_schedule(body: ScheduleIn, ctx: Ctx) -> Activation:
    """The next slots and everything that would keep the schedule off; nothing is saved."""
    result = ctx.schedules.check(body)
    if not result.next_runs:
        result.next_runs = upcoming(body, ctx.schedules.clock())
    return result


@router.get("/schedules", dependencies=READ)
def list_schedules(ctx: Ctx) -> list[Schedule]:
    return ctx.schedules.list()


@router.post("/schedules", status_code=201, dependencies=RUN)
def create_schedule(body: ScheduleIn, ctx: Ctx) -> Schedule:
    return ctx.schedules.create(body)


@router.get("/schedules/{schedule_id}", dependencies=READ)
def get_schedule(schedule_id: str, ctx: Ctx) -> Schedule:
    return ctx.schedules.get(schedule_id)


@router.put("/schedules/{schedule_id}", dependencies=RUN)
def update_schedule(schedule_id: str, body: ScheduleIn, ctx: Ctx) -> Schedule:
    return ctx.schedules.update(schedule_id, body)


@router.delete("/schedules/{schedule_id}", status_code=204, dependencies=RUN)
def delete_schedule(schedule_id: str, ctx: Ctx) -> Response:
    ctx.schedules.delete(schedule_id)
    return Response(status_code=204)


@router.post("/schedules/{schedule_id}/enable", dependencies=RUN)
def enable_schedule(schedule_id: str, ctx: Ctx) -> ScheduleState:
    """Switch on after checking everything; a schedule that does not pass stays off."""
    schedule, activation = ctx.schedules.enable(schedule_id)
    return ScheduleState(schedule=schedule, activation=activation)


@router.post("/schedules/{schedule_id}/disable", dependencies=RUN)
def disable_schedule(schedule_id: str, ctx: Ctx) -> ScheduleState:
    return ScheduleState(schedule=ctx.schedules.disable(schedule_id))
