"""Schedules: start a workflow once or regularly, while Nodestro is running.

A schedule names a workflow, its input (form values, free text, files) and when to run:
once at a date and time, every few hours, daily, on chosen weekdays or monthly. Times
are wall-clock times in the schedule's time zone (IANA name such as ``Europe/Berlin``),
so a daily 08:00 stays 08:00 across daylight saving changes:

- a time that does not exist on the day clocks spring forward (02:30) runs one hour
  later (03:30);
- a time that exists twice when clocks fall back runs once, at its first occurrence;
- a monthly day that a month does not have (31) runs on the month's last day.

Schedules are stored locally and survive restarts. They only run while Nodestro runs:
a slot that passed while it was not running (or was busy for longer than
:data:`GRACE`) is *missed*. Missed slots are listed; the policy ``skip`` ignores them,
``run_once`` runs the workflow once to catch up, however many slots were missed.

A schedule is created switched off. Switching it on checks everything a run needs (the
workflow check, the form values, a budget limit per run, agreement to own tools that may
act outside Nodestro); a schedule that does not pass stays off. Every start goes through
:func:`nodestro_runtime.launcher.launch` with ``trigger="schedule"``: same engine, same budget
per run, same usage, and approval steps wait for a person as always.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from nodestro_runtime.engine import start_fields
from nodestro_runtime.forms import check_values, file_ids_of
from nodestro_runtime.input_files import is_input_id
from nodestro_runtime.launcher import approval_steps, blocking_problems, external_tools, launch
from nodestro_runtime.preflight import agent_models
from nodestro_runtime.problems import NodestroError, Problem, problem
from nodestro_runtime.providers import provider_of
from nodestro_runtime.schemas import RunIn, Workflow, new_id, now
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .context import Nodestro

logger = logging.getLogger(__name__)

COLLECTION = "schedules"
#: A slot is run when Nodestro notices it within this time; later it counts as missed.
GRACE = timedelta(minutes=10)
TICK_SECONDS = 20.0
_MAX_MISSED = 50

ScheduleKind = Literal["once", "hourly", "daily", "weekly", "monthly"]


class ScheduleError(NodestroError):
    """A schedule cannot be saved or switched on."""


class ScheduleIn(BaseModel):
    name: str = ""
    workflow_id: str
    kind: ScheduleKind = "daily"
    #: Local time ``HH:MM`` (``once``, ``daily``, ``weekly``, ``monthly``; minute for ``hourly``).
    at: str = "08:00"
    #: ``once``: the date; others: the first day the schedule may run (empty: today).
    start_date: str = ""
    #: ``weekly``: weekdays, 0 = Monday … 6 = Sunday.
    weekdays: list[int] = Field(default_factory=lambda: [0])
    #: ``monthly``: day of the month (a month without it runs on its last day).
    day_of_month: int = Field(default=1, ge=1, le=31)
    #: ``hourly``: every how many hours.
    every_hours: int = Field(default=1, ge=1, le=24)
    time_zone: str = "UTC"
    input: str = ""
    values: dict[str, Any] = Field(default_factory=dict)
    file_ids: list[str] = Field(default_factory=list)
    missed: Literal["skip", "run_once"] = "skip"
    #: The workflow has own tools that may act outside Nodestro; the user agreed that they run unattended.
    confirm_external: bool = False


class Schedule(ScheduleIn):
    id: str = Field(default_factory=lambda: new_id("schedule"))
    enabled: bool = False
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    #: The next slot in UTC (``None``: off, or a one-off schedule that has run).
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_id: str | None = None
    last_error_code: str | None = None
    last_error: str | None = None
    last_error_params: dict[str, Any] = Field(default_factory=dict)
    runs: int = 0
    #: Slots that passed while Nodestro was not running (newest last, at most 50).
    missed_slots: list[datetime] = Field(default_factory=list)
    missed_total: int = 0
    #: Set when a one-off schedule has run.
    completed: bool = False


class Activation(BaseModel):
    """The result of checking a schedule before it is switched on."""

    ok: bool
    problems: list[Problem] = Field(default_factory=list)
    #: Hints that do not prevent switching on (approval steps, the number of runs …).
    notes: list[Problem] = Field(default_factory=list)
    external_tools: list[str] = Field(default_factory=list)
    next_runs: list[datetime] = Field(default_factory=list)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError("schedule.time_zone", f"Unknown time zone {name!r}", zone=name) from None


def time_zones() -> list[str]:
    return sorted(available_timezones())


def _clock(value: str) -> time:
    try:
        hours, minutes = value.strip().split(":")[:2]
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        raise ScheduleError("schedule.time_invalid", f"{value!r} is not a time (HH:MM)", value=value) from None


def _day(value: str, fallback: date) -> date:
    if not value.strip():
        return fallback
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise ScheduleError("schedule.date_invalid", f"{value!r} is not a date (YYYY-MM-DD)", value=value) from None


def _at(day: date, clock: time, tz: ZoneInfo) -> datetime:
    """The UTC instant of a wall-clock time; skipped times move forward, repeated ones take the first."""
    # fold=0: a repeated time is its first occurrence; a skipped time is read with the offset
    # before the change, which lands one hour later on the clock (02:30 -> 03:30).
    return datetime.combine(day, clock, tzinfo=tz).astimezone(timezone.utc)


def _last_day(year: int, month: int) -> int:
    following = date(year + month // 12, month % 12 + 1, 1)
    return (following - timedelta(days=1)).day


def next_slot(schedule: ScheduleIn, after: datetime) -> datetime | None:
    """The first slot strictly after ``after`` (UTC), or ``None`` when there is none."""
    tz = zone(schedule.time_zone)
    clock = _clock(schedule.at)
    local_after = after.astimezone(tz)
    first_day = _day(schedule.start_date, local_after.date())
    if schedule.kind == "once":
        slot = _at(first_day, clock, tz)
        return slot if slot > after else None
    day = max(first_day, local_after.date() - timedelta(days=1))
    if schedule.kind == "hourly":
        start = _at(first_day, time(0, clock.minute), tz)
        if start > after:
            return start
        steps = int((after - start).total_seconds() // (3600 * schedule.every_hours)) + 1
        return start + timedelta(hours=schedule.every_hours * steps)
    weekdays = sorted({item % 7 for item in schedule.weekdays}) or [0]
    for _ in range(400):
        candidate: datetime | None = None
        if schedule.kind == "daily":
            candidate = _at(day, clock, tz)
        elif schedule.kind == "weekly":
            if day.weekday() in weekdays:
                candidate = _at(day, clock, tz)
        elif schedule.kind == "monthly":
            wanted = min(schedule.day_of_month, _last_day(day.year, day.month))
            if day.day == wanted:
                candidate = _at(day, clock, tz)
        if candidate is not None and candidate > after:
            return candidate
        day += timedelta(days=1)
    return None


def upcoming(schedule: ScheduleIn, after: datetime, count: int = 5) -> list[datetime]:
    slots: list[datetime] = []
    current = after
    while len(slots) < count:
        slot = next_slot(schedule, current)
        if slot is None:
            break
        slots.append(slot)
        current = slot
    return slots


class Schedules:
    """Stores schedules and starts their runs while the app runs."""

    def __init__(self, ctx: Nodestro, *, clock: Any = None) -> None:
        self.ctx = ctx
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ---- storage -------------------------------------------------------------

    def list(self) -> list[Schedule]:
        return sorted(self.ctx.store.list(COLLECTION, Schedule, limit=1000), key=lambda item: item.created_at)

    def get(self, schedule_id: str) -> Schedule:
        found = self.ctx.store.get(COLLECTION, schedule_id, Schedule)
        if found is None:
            raise KeyError(schedule_id)
        return found

    def _save(self, schedule: Schedule) -> None:
        schedule.updated_at = now()
        self.ctx.store.put(COLLECTION, schedule.id, schedule)

    def _validate_timing(self, data: ScheduleIn) -> None:
        zone(data.time_zone)
        _clock(data.at)
        _day(data.start_date, date.today())
        if data.kind == "weekly" and not data.weekdays:
            raise ScheduleError("schedule.no_weekdays", "Choose at least one weekday")

    def create(self, data: ScheduleIn) -> Schedule:
        if self.ctx.store.get_raw("workflows", data.workflow_id) is None:
            raise KeyError(data.workflow_id)
        self._validate_timing(data)
        schedule = Schedule(**data.model_dump())
        self._save(schedule)
        return schedule

    def update(self, schedule_id: str, data: ScheduleIn) -> Schedule:
        """Change a schedule; an active schedule is checked again and stays on only if it passes."""
        current = self.get(schedule_id)
        self._validate_timing(data)
        changed = current.model_copy(update=data.model_dump())
        changed.completed = False
        if current.enabled:
            check = self.check(changed)
            if not check.ok:
                changed.enabled, changed.next_run_at = False, None
            else:
                changed.next_run_at = next_slot(changed, self.clock())
        self._save(changed)
        return changed

    def delete(self, schedule_id: str) -> None:
        if not self.ctx.store.delete(COLLECTION, schedule_id):
            raise KeyError(schedule_id)

    # ---- switching on and off ------------------------------------------------

    def _missing_providers(self, workflow: Workflow) -> builtins.list[str]:
        configured = {item.id for item in self.ctx.settings().providers if item.configured}
        missing: builtins.list[str] = []
        for model in agent_models(workflow):
            try:
                provider = provider_of(model)
            except NodestroError:
                continue  # reported by the workflow check
            if provider not in configured and provider not in missing:
                missing.append(provider)
        return sorted(missing)

    def check(self, schedule: ScheduleIn) -> Activation:
        """Everything that would stop the schedule's runs, found before it is switched on."""
        found: builtins.list[Problem] = []
        notes: builtins.list[Problem] = []
        workflow = self.ctx.store.get("workflows", schedule.workflow_id, Workflow)
        tools: builtins.list[str] = []
        if workflow is None:
            found.append(problem("schedule.workflow_missing", "The workflow no longer exists"))
        else:
            try:
                blocking_problems(self.ctx, workflow)
            except NodestroError as error:
                found.append(error.problem)
            fields = start_fields(workflow)
            known = self.ctx.file_catalog()
            if fields:
                _, errors = check_values(fields, schedule.values, known_files=known)
                found += [problem(error.code, error.message, field=error.field, **error.params) for error in errors]
            elif not schedule.input.strip():
                found.append(problem("schedule.input_empty", "The schedule has no input for the workflow"))
            missing = [item for item in schedule.file_ids if item not in known]
            if missing:
                found.append(problem("form.file_missing", f"{len(missing)} files no longer exist", count=len(missing)))
            tools = external_tools(self.ctx, workflow)
            if tools and not schedule.confirm_external:
                found.append(
                    problem(
                        "schedule.confirm_external",
                        f"Own tools may act outside Nodestro: {', '.join(tools)}",
                        tools=", ".join(tools),
                    )
                )
            stored_inputs = [
                item for item in [*schedule.file_ids, *file_ids_of(fields, schedule.values)] if is_input_id(item)
            ]
            if stored_inputs:
                # Uploaded files stay stored for the schedule; files of earlier runs are never taken.
                notes.append(
                    problem(
                        "schedule.input_files_kept",
                        "The uploaded files are kept for this schedule and used by every run",
                        count=len(set(stored_inputs)),
                    )
                )
            approvals = approval_steps(self.ctx, workflow)
            if approvals:
                notes.append(
                    problem(
                        "schedule.approvals",
                        "Runs stop at approval steps until a person decides",
                        count=approvals,
                    )
                )
        if self.ctx.settings().default_budget_usd is None:
            found.append(problem("schedule.no_budget", "Scheduled runs need a budget limit per run (settings)"))
        if workflow is not None:
            # Every provider the agents use must be configured; runs fail until it is.
            for provider in self._missing_providers(workflow):
                notes.append(
                    problem(
                        "schedule.api_key_missing",
                        f"{provider} is not configured; runs will fail until it is",
                        provider=provider,
                    )
                )
        slots: builtins.list[datetime] = []
        try:
            self._validate_timing(schedule)
            slots = upcoming(schedule, self.clock())
            if not slots:
                found.append(problem("schedule.in_past", "The time lies in the past"))
        except ScheduleError as error:
            found.append(error.problem)
        return Activation(ok=not found, problems=found, notes=notes, external_tools=tools, next_runs=slots)

    def enable(self, schedule_id: str) -> tuple[Schedule, Activation]:
        schedule = self.get(schedule_id)
        result = self.check(schedule)
        if result.ok:
            schedule.enabled = True
            schedule.completed = False
            schedule.next_run_at = next_slot(schedule, self.clock())
            schedule.last_error_code = schedule.last_error = None
            self._save(schedule)
        return schedule, result

    def disable(self, schedule_id: str) -> Schedule:
        schedule = self.get(schedule_id)
        schedule.enabled, schedule.next_run_at = False, None
        self._save(schedule)
        return schedule

    # ---- running -------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # pragma: no cover - the loop must survive one bad schedule
                logger.exception("Schedule tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> builtins.list[str]:
        """Start every due schedule; returns the ids of the started runs."""
        started: builtins.list[str] = []
        async with self._lock:
            current = self.clock()
            for schedule in self.list():
                if not schedule.enabled or schedule.next_run_at is None or schedule.next_run_at > current:
                    continue
                run_id = await self._due(schedule, current)
                if run_id:
                    started.append(run_id)
        return started

    async def _due(self, schedule: Schedule, current: datetime) -> str | None:
        assert schedule.next_run_at is not None
        due: builtins.list[datetime] = []
        slot: datetime | None = schedule.next_run_at
        while slot is not None and slot <= current and len(due) < 10_000:
            due.append(slot)
            slot = next_slot(schedule, slot)
        on_time = [item for item in due if current - item <= GRACE]
        missed = [item for item in due if current - item > GRACE]
        if missed:
            schedule.missed_total += len(missed)
            schedule.missed_slots = (schedule.missed_slots + missed)[-_MAX_MISSED:]
        run_now = bool(on_time) or (bool(missed) and schedule.missed == "run_once")
        schedule.next_run_at = slot
        if schedule.kind == "once" and slot is None:
            schedule.enabled, schedule.completed = False, True
        run_id = None
        if run_now:
            run_id = await self._run(schedule, current)
        self._save(schedule)
        return run_id

    async def _run(self, schedule: Schedule, current: datetime) -> str | None:
        schedule.last_run_at = current
        workflow = self.ctx.store.get("workflows", schedule.workflow_id, Workflow)
        try:
            if workflow is None:
                raise ScheduleError("schedule.workflow_missing", "The workflow no longer exists")
            if self.ctx.settings().default_budget_usd is None:
                raise ScheduleError("schedule.no_budget", "Scheduled runs need a budget limit per run (settings)")
            tools = external_tools(self.ctx, workflow)
            if tools and not schedule.confirm_external:
                raise ScheduleError(
                    "schedule.confirm_external",
                    f"Own tools may act outside Nodestro: {', '.join(tools)}",
                    tools=", ".join(tools),
                )
            run = await launch(
                self.ctx,
                workflow,
                RunIn(input=schedule.input, values=schedule.values, file_ids=schedule.file_ids),
                trigger="schedule",
                trigger_ref=schedule.id,
            )
        except NodestroError as error:
            schedule.last_error_code, schedule.last_error = error.code, str(error)
            schedule.last_error_params = dict(error.params)
            return None
        schedule.runs += 1
        schedule.last_run_id = run.id
        schedule.last_error_code = schedule.last_error = None
        schedule.last_error_params = {}
        return run.id
