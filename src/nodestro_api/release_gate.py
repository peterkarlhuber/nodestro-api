"""The opt-in release gate of a publication (evolution plan Q-3, decisions D6 and D6a).

A publication may require test-series evidence before a new release is created. The gate is evaluated on the
server from stored suites and test runs, with the same rules as the studio's test status (Q-1): a run counts for a
version only when it ran that exact version, with the saved models, and the suite has not changed since.

Every gate item has one of three kinds, reported separately:

- ``critical``: deterministic and mandatory. The evidence is missing or does not belong to this version (suite
  missing, never tested, outdated, tested only with other models). A critical failure blocks the release; nothing
  in the UI or the API can override it.
- ``deterministic``: the share of cases whose run succeeded and whose checks all passed, against the gate's
  minimum.
- ``judge``: the AI judge's mean score against the gate's minimum. AI verdicts are probabilistic and labeled so;
  they are never mixed into the deterministic numbers.

A failed ``deterministic`` or ``judge`` item (and no critical one) makes the gate ``overridable``: publishing then
needs a written reason, recorded with the release and in the publication's history (D6a). Publishing requires the
``admin`` permission, so on a server only the tenant roles ``owner`` and ``admin`` can give an override.

Without a gate (``Publication.gate is None``, the default) publishing works exactly as before.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import now
from pydantic import BaseModel, Field, field_validator

from .benchmarks import BenchmarkRun, Suite

if TYPE_CHECKING:
    from .context import Nodestro

MAX_GATE_SUITES = 20
MIN_REASON = 10
MAX_REASON = 500


class ReleaseGate(BaseModel):
    """What a new release of a publication must show (opt-in per publication)."""

    #: Test series whose latest finished run of the saved models must have tested the published version.
    suite_ids: list[str] = Field(min_length=1, max_length=MAX_GATE_SUITES)
    #: Minimum share (0..1) of cases whose run succeeded and whose checks all passed, per suite.
    min_pass_rate: float = Field(default=1.0, ge=0, le=1)
    #: Minimum mean AI judge score (0..1) per suite; ``None``: AI verdicts are reported but not gated.
    judge_min_score: float | None = Field(default=None, ge=0, le=1)


GateKind = Literal["critical", "deterministic", "judge"]


class GateItem(BaseModel):
    kind: GateKind
    #: ``gate.suite_missing``, ``gate.suite_untested``, ``gate.suite_outdated``, ``gate.suite_changed``,
    #: ``gate.suite_variant_only``, ``gate.tested``, ``gate.pass_rate``, ``gate.judge_score``, ``gate.judge_missing``.
    code: str
    passed: bool
    suite_id: str
    suite_name: str = ""
    run_id: str | None = None
    value: float | None = None
    threshold: float | None = None
    #: AI verdicts are probabilistic: always true for ``judge`` items.
    probabilistic: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


GateStatus = Literal["off", "passed", "overridable", "blocked"]


class GateReport(BaseModel):
    status: GateStatus
    workflow_version: int | None = None
    gate: ReleaseGate | None = None
    items: list[GateItem] = Field(default_factory=list)


class GateOverride(BaseModel):
    """The written reason for releasing despite failed non-critical gate items."""

    reason: str = Field(min_length=MIN_REASON, max_length=MAX_REASON)

    @field_validator("reason")
    @classmethod
    def _meaningful(cls, value: str) -> str:
        value = value.strip()
        if len(value) < MIN_REASON:
            raise ValueError(f"The reason needs at least {MIN_REASON} characters")
        return value


class GateRecord(BaseModel):
    """What the gate said when a release was created, and the override if one was given."""

    report: GateReport
    override_reason: str | None = None
    override_by: str | None = None
    override_at: datetime | None = None


def _own_suite(ctx: Nodestro, workflow_id: str, suite_id: str) -> Suite | None:
    try:
        suite = ctx.benchmarks.suite(suite_id)
    except KeyError:
        return None
    return suite if suite.workflow_id == workflow_id else None


def _tested_version(run: BenchmarkRun, versions: list[tuple[int, datetime]]) -> int | None:
    """The version a run tested: its pinned version, or the version saved last before it (``current state``)."""
    if run.workflow_version is not None:
        return run.workflow_version
    before = [number for number, created in versions if created <= run.created_at]
    return max(before) if before else None


def _suite_items(
    suite: Suite, runs: list[BenchmarkRun], version: int | None, gate: ReleaseGate, versions: list[tuple[int, datetime]]
) -> list[GateItem]:
    def item(kind: GateKind, code: str, passed: bool, **extra: Any) -> GateItem:
        return GateItem(kind=kind, code=code, passed=passed, suite_id=suite.id, suite_name=suite.name, **extra)

    finished = sorted((run for run in runs if run.status == "finished"), key=lambda run: run.created_at, reverse=True)
    saved = [run for run in finished if not run.models]
    if not saved:
        code = "gate.suite_variant_only" if finished else "gate.suite_untested"
        return [item("critical", code, False)]
    latest = saved[0]
    tested = _tested_version(latest, versions)
    if version is None or tested != version:
        params = {"tested_version": tested, "version": version}
        return [item("critical", "gate.suite_outdated", False, run_id=latest.id, params=params)]
    if suite.updated_at > latest.created_at:
        return [item("critical", "gate.suite_changed", False, run_id=latest.id)]
    items = [item("critical", "gate.tested", True, run_id=latest.id)]
    results = latest.results
    clean = sum(
        result.run_status == "succeeded" and all(check.passed is True for check in result.checks) for result in results
    )
    rate = clean / len(results) if results else 0.0
    items.append(
        item(
            "deterministic",
            "gate.pass_rate",
            bool(results) and rate >= gate.min_pass_rate,
            run_id=latest.id,
            value=round(rate, 4),
            threshold=gate.min_pass_rate,
            params={"clean": clean, "cases": len(results)},
        )
    )
    if gate.judge_min_score is not None:
        score = latest.summary.judge_score if latest.judge and latest.summary.judged else None
        if score is None:
            items.append(
                item(
                    "judge",
                    "gate.judge_missing",
                    False,
                    run_id=latest.id,
                    threshold=gate.judge_min_score,
                    probabilistic=True,
                )
            )
        else:
            items.append(
                item(
                    "judge",
                    "gate.judge_score",
                    score >= gate.judge_min_score,
                    run_id=latest.id,
                    value=round(score, 4),
                    threshold=gate.judge_min_score,
                    probabilistic=True,
                    params={"judged": latest.summary.judged},
                )
            )
    return items


def evaluate(ctx: Nodestro, workflow_id: str, version: int | None, gate: ReleaseGate | None) -> GateReport:
    """The gate's verdict for one saved version of a workflow (``status="off"`` without a gate)."""
    if gate is None:
        return GateReport(status="off", workflow_version=version)
    versions = [(item.number, item.created_at) for item in ctx.versions.all(workflow_id)]
    items: list[GateItem] = []
    for suite_id in dict.fromkeys(gate.suite_ids):
        suite = _own_suite(ctx, workflow_id, suite_id)
        if suite is None:
            items.append(GateItem(kind="critical", code="gate.suite_missing", passed=False, suite_id=suite_id))
            continue
        items.extend(_suite_items(suite, ctx.benchmarks.runs(suite.id), version, gate, versions))
    failed = [entry for entry in items if not entry.passed]
    status: GateStatus
    if any(entry.kind == "critical" for entry in failed):
        status = "blocked"
    elif failed:
        status = "overridable"
    else:
        status = "passed"
    return GateReport(status=status, workflow_version=version, gate=gate, items=items)


def enforce(report: GateReport, override: GateOverride | None, principal_id: str) -> GateRecord | None:
    """Refuse a release the gate does not allow; return what to record with an allowed one."""
    if report.status == "off":
        return None
    if report.status == "blocked":
        failed = [item.code for item in report.items if item.kind == "critical" and not item.passed]
        raise NodestroError(
            "gate.blocked",
            f"The release gate blocks this version: {', '.join(failed)}",
            count=len(failed),
        )
    if report.status == "overridable":
        if override is None:
            failed = [item.code for item in report.items if not item.passed]
            raise NodestroError(
                "gate.override_required",
                f"The release gate failed ({', '.join(failed)}); publishing needs a written reason",
                count=len(failed),
            )
        return GateRecord(report=report, override_reason=override.reason, override_by=principal_id, override_at=now())
    return GateRecord(report=report)


def validate(ctx: Nodestro, workflow_id: str, gate: ReleaseGate) -> None:
    """A gate may only name test series of its own workflow."""
    for suite_id in gate.suite_ids:
        if _own_suite(ctx, workflow_id, suite_id) is None:
            raise NodestroError("gate.suite_unknown", f"No test series {suite_id} for this workflow", suite=suite_id)
