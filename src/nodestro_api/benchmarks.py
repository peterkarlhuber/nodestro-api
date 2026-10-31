"""Workflow test series: fixed test cases, run against a workflow version, measured.

A test series (``Suite``) belongs to a workflow and holds test cases: an input (form
values, free text, files), deterministic checks on the result and, optionally, criteria
for an AI judge. Running a series (``BenchmarkRun``) starts one ordinary run per case
through the batch machinery (:mod:`nodestro_api.batches`, ``trigger="benchmark"``), so the
same engine, budgets, approvals and usage apply. Nothing about a test run is special:
approval steps stop and wait for a person, and own tools that may act outside Nodestro
must be confirmed before the series starts.

After the runs, every case is evaluated:

- **Checks** use the operators of decision nodes (:mod:`nodestro_runtime.conditions`) on the
  result, e.g. ``category equals complaint`` or ``total gt 100``. They are exact.
- **AI judge** (optional): a separate AI model (any configured provider) judges the result against the case's
  criteria (one per line) and the expected answer, if given. Its verdicts are marked as
  AI judgements and cost money, which is recorded in the usage ledger (``benchmark_judge``).

A case passes when its run succeeded, every check holds and, if judged, the judge
passed it. The summary aggregates success, checks, judge quality, tokens, cost and time;
two runs of a series (e.g. version 3 against version 4) can be compared case by case.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from nodestro_runtime.conditions import evaluate, parse_result, rule_problems
from nodestro_runtime.engine import usage_of
from nodestro_runtime.launcher import CostEstimate, approval_steps, estimate_cost, external_tools
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import check_model, model_id
from nodestro_runtime.schemas import ConditionRule, Usage, Workflow, new_id, now, total_cost
from orkexa import Criterion, Evaluator
from orkexa.dynamic import BudgetTracker
from orkexa.usage import summarize_usage
from pydantic import BaseModel, Field

from .batches import BatchIn, BatchRow

if TYPE_CHECKING:
    from .context import Nodestro

logger = logging.getLogger(__name__)

SUITES = "benchmark_suites"
RUNS = "benchmark_runs"
MAX_CASES = 200


class BenchmarkError(NodestroError):
    """A test series cannot be saved or run."""


class TestCase(BaseModel):
    __test__ = False  # not a pytest class

    id: str = Field(default_factory=lambda: new_id("case"))
    name: str = ""
    input: str = ""
    values: dict[str, Any] = Field(default_factory=dict)
    file_ids: list[str] = Field(default_factory=list)
    checks: list[ConditionRule] = Field(default_factory=list)
    #: Criteria for the AI judge, one per line (empty: no judge for this case).
    judge_criteria: str = ""
    #: A reference answer the judge compares with (optional).
    expected: str = ""


class SuiteIn(BaseModel):
    name: str = ""
    description: str = ""
    workflow_id: str
    cases: list[TestCase] = Field(default_factory=list, max_length=MAX_CASES)
    #: The judge model (``None``: the assistant model of the settings).
    judge_model: str | None = None


class Suite(SuiteIn):
    id: str = Field(default_factory=lambda: new_id("suite"))
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class BenchmarkStart(BaseModel):
    #: The saved workflow version to test (``None``: the workflow as it is saved now).
    version: int | None = None
    concurrency: int = Field(default=2, ge=1, le=8)
    #: Stop starting cases once the runs cost this much (USD).
    max_cost_usd: float | None = Field(default=None, ge=0)
    #: Stop after this many failed runs.
    max_failures: int | None = Field(default=None, ge=1)
    judge: bool = True
    confirm_external: bool = False


class CheckResult(BaseModel):
    rule_id: str
    label: str
    #: ``None``: the check could not be evaluated (missing field, wrong type) and counts as failed.
    passed: bool | None


class JudgeVerdict(BaseModel):
    """The verdict of the AI judge; always shown as an AI judgement."""

    ai: Literal[True] = True
    model: str
    passed: bool
    #: Share of criteria passed (0..1).
    score: float
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    #: ``None``: unknown (a judge model without known prices).
    cost_usd: float | None = 0.0
    error_code: str | None = None
    error: str | None = None


class CaseResult(BaseModel):
    case_id: str
    name: str
    run_id: str | None = None
    run_status: str
    passed: bool | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    judge: JudgeVerdict | None = None
    cost_usd: float | None = 0.0
    tokens: int = 0
    duration_s: float | None = None
    output: Any = None
    error_code: str | None = None


class BenchmarkSummary(BaseModel):
    cases: int = 0
    passed: int = 0
    failed: int = 0
    #: The AI judge could not decide (error) and every check passed.
    undecided: int = 0
    #: Runs that did not succeed (failed, cancelled, invalid input, not started).
    errors: int = 0
    pass_rate: float = 0.0
    checks_passed: int = 0
    checks_total: int = 0
    judged: int = 0
    judge_score: float | None = None
    cost_usd: float | None = 0.0
    judge_cost_usd: float | None = 0.0
    tokens: int = 0
    avg_duration_s: float | None = None


class BenchmarkRun(BaseModel):
    id: str = Field(default_factory=lambda: new_id("bench"))
    suite_id: str
    suite_name: str
    workflow_id: str
    workflow_version: int | None = None
    batch_id: str | None = None
    status: Literal["running", "evaluating", "finished", "stopped", "cancelled", "interrupted"] = "running"
    stop_code: str | None = None
    judge: bool = True
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    results: list[CaseResult] = Field(default_factory=list)
    summary: BenchmarkSummary = Field(default_factory=BenchmarkSummary)
    judge_usage: Usage = Field(default_factory=Usage)


class BenchmarkPlan(BaseModel):
    cases: int
    estimate: CostEstimate
    judged_cases: int
    external_tools: list[str] = Field(default_factory=list)
    approvals: int = 0
    problems: list[dict[str, Any]] = Field(default_factory=list)


class CaseComparison(BaseModel):
    case_id: str
    name: str
    a: CaseResult | None = None
    b: CaseResult | None = None
    #: ``improved``, ``worse``, ``same`` or ``changed`` (same verdict, different result).
    change: str


class Comparison(BaseModel):
    a: BenchmarkRun
    b: BenchmarkRun
    cases: list[CaseComparison]


def _criteria(text: str) -> list[Criterion]:
    lines = [line.strip(" -*\t") for line in text.splitlines() if line.strip(" -*\t")]
    return [Criterion(name=f"c{index}", description=line) for index, line in enumerate(lines, start=1)]


def check_case(case: TestCase, output: Any) -> list[CheckResult]:
    parsed = parse_result(output)
    return [
        CheckResult(rule_id=rule.id, label=rule.label or rule.field or rule.operator, passed=evaluate(rule, parsed))
        for rule in case.checks
    ]


def summarize(results: list[CaseResult]) -> BenchmarkSummary:
    summary = BenchmarkSummary(cases=len(results))
    durations = []
    scores = []
    for result in results:
        if result.passed:
            summary.passed += 1
        elif result.passed is None:
            summary.undecided += 1
        else:
            summary.failed += 1
        if result.run_status != "succeeded":
            summary.errors += 1
        summary.checks_total += len(result.checks)
        summary.checks_passed += sum(check.passed is True for check in result.checks)
        if result.judge is not None and result.judge.error_code is None:
            summary.judged += 1
            scores.append(result.judge.score)
        summary.tokens += result.tokens
        if result.duration_s is not None:
            durations.append(result.duration_s)
    summary.pass_rate = round(summary.passed / summary.cases, 4) if summary.cases else 0.0
    summary.judge_score = round(sum(scores) / len(scores), 4) if scores else None
    summary.avg_duration_s = round(sum(durations) / len(durations), 3) if durations else None
    summary.cost_usd = total_cost(result.cost_usd for result in results)
    summary.judge_cost_usd = total_cost(result.judge.cost_usd for result in results if result.judge is not None)
    return summary


def compare(a: BenchmarkRun, b: BenchmarkRun) -> Comparison:
    by_a = {result.case_id: result for result in a.results}
    by_b = {result.case_id: result for result in b.results}
    cases = []
    for case_id in dict.fromkeys([*by_a, *by_b]):
        left, right = by_a.get(case_id), by_b.get(case_id)
        if left is None or right is None:
            change = "changed"
        elif bool(left.passed) != bool(right.passed):
            change = "improved" if right.passed else "worse"
        elif json.dumps(left.output, sort_keys=True, default=str) != json.dumps(
            right.output, sort_keys=True, default=str
        ):
            change = "changed"
        else:
            change = "same"
        shown = right or left
        name = shown.name if shown is not None else case_id
        cases.append(CaseComparison(case_id=case_id, name=name, a=left, b=right, change=change))
    return Comparison(a=a, b=b, cases=cases)


class Benchmarks:
    def __init__(self, ctx: Nodestro) -> None:
        self.ctx = ctx
        self._tasks: dict[str, asyncio.Task[None]] = {}
        for stored in ctx.store.list(RUNS, BenchmarkRun, limit=500):
            if stored.status in ("running", "evaluating"):
                stored.status, stored.stop_code = "interrupted", "benchmark.interrupted"
                ctx.store.put(RUNS, stored.id, stored)

    # ---- suites ----------------------------------------------------------------

    def suites(self, workflow_id: str | None = None) -> list[Suite]:
        found = self.ctx.store.list(SUITES, Suite, limit=1000)
        return [suite for suite in found if workflow_id is None or suite.workflow_id == workflow_id]

    def suite(self, suite_id: str) -> Suite:
        found = self.ctx.store.get(SUITES, suite_id, Suite)
        if found is None:
            raise KeyError(suite_id)
        return found

    def _validate(self, data: SuiteIn) -> None:
        if self.ctx.store.get_raw("workflows", data.workflow_id) is None:
            raise KeyError(data.workflow_id)
        for case in data.cases:
            for rule in case.checks:
                found = rule_problems(rule)
                if found:
                    code, message = found[0]
                    raise BenchmarkError(code, f"{case.name or case.id}: {message}", case=case.name or case.id)

    def save_suite(self, data: SuiteIn, suite_id: str | None = None) -> Suite:
        self._validate(data)
        if suite_id is None:
            suite = Suite(**data.model_dump())
        else:
            suite = self.suite(suite_id).model_copy(update=data.model_dump())
            suite.updated_at = now()
        for index, case in enumerate(suite.cases, start=1):
            if not case.name.strip():
                case.name = f"#{index}"
        self.ctx.store.put(SUITES, suite.id, suite)
        return suite

    def delete_suite(self, suite_id: str) -> None:
        if not self.ctx.store.delete(SUITES, suite_id):
            raise KeyError(suite_id)

    # ---- runs ------------------------------------------------------------------

    def runs(self, suite_id: str) -> list[BenchmarkRun]:
        found = [run for run in self.ctx.store.list(RUNS, BenchmarkRun, limit=500) if run.suite_id == suite_id]
        return sorted(found, key=lambda item: item.created_at, reverse=True)

    def run(self, run_id: str) -> BenchmarkRun:
        found = self.ctx.store.get(RUNS, run_id, BenchmarkRun)
        if found is None:
            raise KeyError(run_id)
        return found

    def delete_run(self, run_id: str) -> None:
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise BenchmarkError("benchmark.delete_running", "Cancel the test run before deleting it")
        if not self.ctx.store.delete(RUNS, run_id):
            raise KeyError(run_id)

    def _workflow(self, suite: Suite, version: int | None) -> Workflow:
        if version is not None:
            return self.ctx.resolve_workflow(suite.workflow_id, version)[0]
        stored = self.ctx.store.get("workflows", suite.workflow_id, Workflow)
        if stored is None:
            raise KeyError(suite.workflow_id)
        return stored

    def plan(self, suite_id: str, start: BenchmarkStart) -> BenchmarkPlan:
        suite = self.suite(suite_id)
        workflow = self._workflow(suite, start.version)
        batch_plan = self.ctx.batches.plan(self._batch_request(suite, start))
        problems = [
            {"case": suite.cases[item.index].name, "errors": [error.model_dump() for error in item.errors]}
            for item in batch_plan.items
            if item.status == "invalid"
        ]
        return BenchmarkPlan(
            cases=len(suite.cases),
            estimate=estimate_cost(self.ctx, workflow.id, len(suite.cases)),
            judged_cases=sum(bool(_criteria(case.judge_criteria)) for case in suite.cases) if start.judge else 0,
            external_tools=external_tools(self.ctx, workflow),
            approvals=approval_steps(self.ctx, workflow),
            problems=problems,
        )

    @staticmethod
    def _batch_request(suite: Suite, start: BenchmarkStart) -> BatchIn:
        return BatchIn(
            workflow_id=suite.workflow_id,
            name=suite.name,
            rows=[
                BatchRow(values=case.values, input=case.input, file_ids=case.file_ids, ref=case.id)
                for case in suite.cases
            ],
            concurrency=start.concurrency,
            max_cost_usd=start.max_cost_usd,
            max_failures=start.max_failures,
            confirm_external=start.confirm_external,
        )

    def start(self, suite_id: str, start: BenchmarkStart) -> BenchmarkRun:
        suite = self.suite(suite_id)
        if not suite.cases:
            raise BenchmarkError("benchmark.no_cases", "The test series has no test cases")
        if start.version is not None:
            self._workflow(suite, start.version)
        bench = BenchmarkRun(suite_id=suite.id, suite_name=suite.name, workflow_id=suite.workflow_id, judge=start.judge)
        batch = self.ctx.batches.create(
            self._batch_request(suite, start), kind="benchmark", ref=bench.id, version=start.version
        )
        bench.batch_id = batch.id
        bench.workflow_version = batch.workflow_version
        self.ctx.store.put(RUNS, bench.id, bench)
        self._tasks[bench.id] = asyncio.get_running_loop().create_task(self._follow(bench, suite))
        return bench

    async def cancel(self, run_id: str) -> BenchmarkRun:
        bench = self.run(run_id)
        if bench.batch_id and bench.status == "running":
            await self.ctx.batches.cancel(bench.batch_id)
        return self.run(run_id)

    async def wait(self, run_id: str) -> BenchmarkRun:
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)
        return self.run(run_id)

    async def _follow(self, bench: BenchmarkRun, suite: Suite) -> None:
        assert bench.batch_id is not None
        batch = await self.ctx.batches.wait(bench.batch_id)
        bench.status = "evaluating"
        self.ctx.store.put(RUNS, bench.id, bench)
        cases = {case.id: case for case in suite.cases}
        budget = self.ctx.settings().default_budget_usd
        judge_tracker = self.ctx.tracker(budget)
        results = []
        for item in batch.items:
            case = cases.get(item.ref or "")
            if case is None:
                continue
            status = {"invalid": "invalid", "skipped": "not_started", "pending": "not_started"}.get(
                item.status, item.status
            )
            result = CaseResult(
                case_id=case.id,
                name=case.name,
                run_id=item.run_id,
                run_status=status,
                cost_usd=item.cost_usd,
                tokens=item.tokens,
                duration_s=item.duration_s,
                output=item.output,
                error_code=item.error_code or (item.errors[0].code if item.errors else None),
            )
            if item.status == "succeeded":
                result.checks = check_case(case, item.output)
                if bench.judge and _criteria(case.judge_criteria):
                    result.judge = await self._judge(bench, suite, case, item.output, judge_tracker)
                checks_ok = all(check.passed is True for check in result.checks)
                if result.judge is not None and result.judge.error_code is not None:
                    # The judge could not decide: the case is open unless a check already failed.
                    result.passed = None if checks_ok else False
                else:
                    result.passed = checks_ok and (result.judge is None or result.judge.passed)
            else:
                result.passed = False
            results.append(result)
        bench.results = results
        bench.summary = summarize(results)
        bench.judge_usage = usage_of(
            judge_tracker.usage, judge_tracker.records, stale_requests=judge_tracker.stale_requests()
        )
        ended: dict[str, Literal["cancelled", "stopped", "interrupted"]] = {
            "cancelled": "cancelled",
            "stopped": "stopped",
            "interrupted": "interrupted",
        }
        bench.status = ended.get(batch.status, "finished")
        bench.stop_code = batch.stop_code if bench.status != "finished" else None
        bench.finished_at = now()
        self.ctx.store.put(RUNS, bench.id, bench)

    async def _judge(
        self, bench: BenchmarkRun, suite: Suite, case: TestCase, output: Any, tracker: BudgetTracker
    ) -> JudgeVerdict:
        model = suite.judge_model or self.ctx.settings().assistant_model
        before = len(tracker.records)
        task = "Process this input as the workflow intends.\n\n" + (
            case.input or json.dumps(case.values, ensure_ascii=False)
        )
        context = f"Expected answer (reference):\n{case.expected}" if case.expected.strip() else None
        verdict = JudgeVerdict(model=model, passed=False, score=0.0)
        try:
            judge = check_model(model)
            connection = self.ctx.connect_model(judge)
            evaluator = Evaluator(
                model=model_id(judge),
                options=connection.options(),
                usage_tracker=tracker,
                name="Judge",
                **connection.agent_kwargs(),
            )
            evaluation = await asyncio.to_thread(
                evaluator.evaluate, task, output, criteria=_criteria(case.judge_criteria), context=context
            )
        except Exception as error:
            verdict.error_code = getattr(error, "code", None) or "benchmark.judge_failed"
            verdict.error = str(error)
        else:
            criteria = _criteria(case.judge_criteria)
            by_name = {result.name: result for result in evaluation.results}
            verdict.criteria = [
                {
                    "criterion": criterion.description,
                    "passed": by_name[criterion.name].passed if criterion.name in by_name else None,
                    "reasoning": by_name[criterion.name].reasoning if criterion.name in by_name else "",
                }
                for criterion in criteria
            ]
            passed = sum(item["passed"] is True for item in verdict.criteria)
            verdict.passed = evaluation.passed
            verdict.score = round(passed / len(criteria), 4) if criteria else 0.0
            verdict.summary = evaluation.summary
        records = tracker.records[before:]
        verdict.cost_usd = usage_of(summarize_usage(records), records).estimated_cost_usd
        if records:
            self.ctx.usage_log.record(
                "benchmark_judge",
                records,
                label=f"{suite.name}: {case.name}",
                ref=bench.id,
                request=case.id,
                tracker=tracker,
            )
        return verdict
