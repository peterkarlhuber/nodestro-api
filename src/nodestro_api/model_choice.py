"""Choosing an agent's model from measured test series results (AO M6).

A test series (:mod:`nodestro_api.benchmarks`) can run the same cases with another model for one agent (Q-2).
This module turns those recorded runs into a traceable recommendation for one agent node of the workflow. It
never invents a quality value: every number comes from a finished test run, and without a measurement the
answer is "keep the configured model".

What counts as a measurement for a model:

- a **finished** test run of the same series,
- whose workflow was the same as the one saved now apart from the models of its agents (``BenchmarkRun.basis``),
- whose cases, checks and judge were the same as the series has now (``BenchmarkRun.suite_basis``),
- in which every *other* agent used the model it has now (``BenchmarkRun.agent_models``),
- with a result for every case of the series.

Older test runs without these records are never used; the answer counts them so the reason is visible. Only
the newest matching run per model is used.

The policy (``ModelPolicy``) is set per request and decides deterministically:

- A candidate must pass at least ``min_pass_rate`` of the cases; by default at least as many as the configured
  model in its own measured run (no regression). Without a measured run of the configured model and without an
  explicit ``min_pass_rate`` there is no recommendation.
- With ``no_new_failures`` (default) a candidate must also pass every case the configured model passed.
- ``objective`` orders the remaining candidates: ``cost`` (cheapest known cost per case; an unknown cost is
  never treated as zero, so such a candidate cannot win on cost), ``duration`` (shortest average time per case)
  or ``quality`` (highest pass rate, then lowest known cost). Ties: higher pass rate, then the model name.
- A model whose provider is not configured (any more) is reported, never recommended.

Nothing changes on its own: the recommendation is shown, and a person applies it by saving the agent with that
model. Policy and evidence (test run ids) are part of the answer, so the choice can be traced.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import check_model
from nodestro_runtime.schemas import Workflow
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .benchmarks import BenchmarkRun, Suite
    from .context import Nodestro

Objective = Literal["cost", "duration", "quality"]

#: Below this many cases the answer warns that the measurement is thin (it still decides by the policy).
FEW_CASES = 10


class ModelChoiceError(NodestroError):
    """A model recommendation cannot be computed for this request."""


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def workflow_basis(workflow: Workflow) -> str:
    """What a test run measured apart from the models: the workflow without names, positions and agent models."""
    data = workflow.model_dump(mode="json", exclude={"id", "name", "description", "created_at", "updated_at"})
    for node in data["nodes"]:
        node.pop("position", None)
        node.pop("label", None)
        if node.get("agent"):
            node["agent"]["model"] = ""
    return _digest(data)


def agent_models(workflow: Workflow) -> dict[str, str]:
    """The model of every agent node."""
    return {node.id: node.agent.model for node in workflow.nodes if node.agent is not None}


def suite_basis(suite: Suite) -> str:
    """The cases (inputs, checks, judge criteria, reference answers) and the judge of a series."""
    cases = [case.model_dump(mode="json", exclude={"name", "origin"}) for case in suite.cases]
    return _digest({"cases": sorted(cases, key=lambda case: case["id"]), "judge_model": suite.judge_model})


class ModelPolicy(BaseModel):
    """How a model is chosen from measurements; nothing in it is a quality value of its own."""

    objective: Objective = "cost"
    #: The least share of passed cases (0..1); ``None``: at least the configured model's measured pass rate.
    min_pass_rate: float | None = Field(default=None, ge=0, le=1)
    #: A candidate must pass every case the configured model passed.
    no_new_failures: bool = True


class Measurement(BaseModel):
    """One model's newest matching test run."""

    model: str
    benchmark_run_id: str
    created_at: Any
    #: Whether this is the model the agent has now.
    current: bool
    cases: int
    passed: int
    pass_rate: float
    #: Average AI judge score (0..1), shown as an AI judgement; not used for the choice.
    judge_score: float | None = None
    #: Known cost per case in USD; ``None`` when any case's cost is unknown.
    cost_per_case_usd: float | None = None
    avg_duration_s: float | None = None
    #: Cases the configured model passed and this one did not.
    new_failures: list[str] = Field(default_factory=list)
    available: bool = True
    #: Why the model cannot be recommended (``model.unavailable``, ``model.pass_rate_low``, ``model.new_failures``,
    #: ``model.cost_unknown``, ``model.duration_unknown``); empty when it is eligible.
    excluded: list[str] = Field(default_factory=list)


class ModelChoice(BaseModel):
    suite_id: str
    workflow_id: str
    node_id: str
    current_model: str
    policy: ModelPolicy
    #: The least pass rate the policy applied (``None``: none, so no recommendation).
    required_pass_rate: float | None = None
    measurements: list[Measurement] = Field(default_factory=list)
    #: The model to use by the policy, or ``None`` (keep the configured model).
    recommended: str | None = None
    #: ``switch`` (another model is better by the policy), ``keep`` (the configured model is best or nothing is
    #: eligible), ``no_baseline`` (the configured model was not measured and no least pass rate was given) or
    #: ``no_measurement`` (no matching test run at all).
    outcome: Literal["switch", "keep", "no_baseline", "no_measurement"]
    #: Finished test runs of the series that were not used, by reason (``older_record``, ``workflow_changed``,
    #: ``cases_changed``, ``other_agents_differ``, ``incomplete``).
    skipped: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _measure(run: BenchmarkRun, model: str, current: str, case_ids: list[str]) -> Measurement:
    by_case = {result.case_id: result for result in run.results}
    results = [by_case[case_id] for case_id in case_ids]
    passed = sum(result.passed is True for result in results)
    scores = [result.judge.score for result in results if result.judge is not None and result.judge.error_code is None]
    costs = [result.cost_usd for result in results]
    durations = [result.duration_s for result in results]
    return Measurement(
        model=model,
        benchmark_run_id=run.id,
        created_at=run.created_at,
        current=model == current,
        cases=len(results),
        passed=passed,
        pass_rate=round(passed / len(results), 4) if results else 0.0,
        judge_score=round(sum(scores) / len(scores), 4) if scores else None,
        cost_per_case_usd=(
            round(sum(cost for cost in costs if cost is not None) / len(costs), 6)
            if costs and all(cost is not None for cost in costs)
            else None
        ),
        avg_duration_s=(
            round(sum(item for item in durations if item is not None) / len(durations), 3)
            if durations and all(item is not None for item in durations)
            else None
        ),
    )


def _passed_cases(run: BenchmarkRun) -> set[str]:
    return {result.case_id for result in run.results if result.passed is True}


def recommend(ctx: Nodestro, suite: Suite, runs: list[BenchmarkRun], node_id: str, policy: ModelPolicy) -> ModelChoice:
    """The recommendation for ``node_id`` of the series' workflow as it is saved now (see the module docstring)."""
    workflow = ctx.store.get("workflows", suite.workflow_id, Workflow)
    if workflow is None:
        raise KeyError(suite.workflow_id)
    node = next((item for item in workflow.nodes if item.id == node_id), None)
    if node is None or node.agent is None:
        raise ModelChoiceError("model_choice.node_unknown", f"The workflow has no agent {node_id!r}", node=node_id)
    current = check_model(node.agent.model)
    basis, cases_basis = workflow_basis(workflow), suite_basis(suite)
    others = {key: value for key, value in agent_models(workflow).items() if key != node_id}
    case_ids = [case.id for case in suite.cases]
    choice = ModelChoice(
        suite_id=suite.id,
        workflow_id=workflow.id,
        node_id=node_id,
        current_model=current,
        policy=policy,
        outcome="no_measurement",
    )

    def skip(reason: str) -> None:
        choice.skipped[reason] = choice.skipped.get(reason, 0) + 1

    newest: dict[str, BenchmarkRun] = {}
    for run in sorted(runs, key=lambda item: item.created_at, reverse=True):
        if run.status != "finished":
            continue
        if run.basis is None or run.suite_basis is None or not run.agent_models:
            skip("older_record")
        elif run.basis != basis:
            skip("workflow_changed")
        elif run.suite_basis != cases_basis:
            skip("cases_changed")
        elif (
            node_id not in run.agent_models
            or {key: value for key, value in run.agent_models.items() if key != node_id} != others
        ):
            skip("other_agents_differ")
        elif not set(case_ids) <= {result.case_id for result in run.results}:
            skip("incomplete")
        else:
            model = check_model(run.agent_models.get(node_id, ""))
            newest.setdefault(model, run)
    if not newest:
        return choice

    measurements = {model: _measure(run, model, current, case_ids) for model, run in newest.items()}
    baseline = newest.get(current)
    if baseline is not None:
        baseline_passed = _passed_cases(baseline)
        for model, measurement in measurements.items():
            if model != current:
                measurement.new_failures = sorted(baseline_passed - _passed_cases(newest[model]))
    required = policy.min_pass_rate
    if required is None and baseline is not None:
        required = measurements[current].pass_rate
    choice.required_pass_rate = required

    for model, measurement in measurements.items():
        measurement.available = _available(ctx, model)
        if not measurement.available:
            measurement.excluded.append("model.unavailable")
        if required is not None and measurement.pass_rate < required:
            measurement.excluded.append("model.pass_rate_low")
        if policy.no_new_failures and measurement.new_failures:
            measurement.excluded.append("model.new_failures")
        if policy.objective == "cost" and measurement.cost_per_case_usd is None:
            measurement.excluded.append("model.cost_unknown")
        if policy.objective == "duration" and measurement.avg_duration_s is None:
            measurement.excluded.append("model.duration_unknown")

    choice.measurements = sorted(measurements.values(), key=lambda item: (not item.current, item.model))
    if len(case_ids) < FEW_CASES:
        choice.warnings.append("model_choice.few_cases")
    if any(item.judge_score is not None for item in choice.measurements):
        choice.warnings.append("model_choice.ai_judged")
    if required is None:
        choice.outcome = "no_baseline"
        return choice

    def rank(item: Measurement) -> tuple[Any, ...]:
        if policy.objective == "cost":
            return (item.cost_per_case_usd, -item.pass_rate, item.model)
        if policy.objective == "duration":
            return (item.avg_duration_s, -item.pass_rate, item.model)
        cost = item.cost_per_case_usd
        return (-item.pass_rate, cost is None, cost or 0.0, item.model)

    eligible = sorted((item for item in choice.measurements if not item.excluded), key=rank)
    if not eligible or eligible[0].current:
        choice.outcome = "keep"
        return choice
    choice.recommended = eligible[0].model
    choice.outcome = "switch"
    return choice


def _available(ctx: Nodestro, model: str) -> bool:
    """Whether the model's provider is configured and reachable under the operator's endpoint rules."""
    try:
        ctx.connect_model(model)
    except NodestroError:
        return False
    return True
