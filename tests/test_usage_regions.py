"""Provider regions in the usage report (API-021): ``by_region`` and ``UsageBucket.regions`` from the regions the
runtime records (``UsageEntry.region``, ``Usage.regions`` of a run and of each of its steps)."""

from __future__ import annotations

from typing import Any

from nodestro_runtime.schemas import AgentConfig, Node, NodeRun, OrchestratorConfig, Run, Usage, Workflow
from nodestro_runtime.usage_log import UsageEntry
from test_reliability import _ctx, _store_runs

BEDROCK = "bedrock:anthropic.claude-sonnet-5-5"


def _usage(usd: float, *regions: str, requests: int = 1) -> Usage:
    return Usage(
        requests=requests,
        total_tokens=10 * requests,
        estimated_cost_usd=usd,
        known_cost_usd=usd,
        cost_complete=True,
        regions=list(regions),
    )


def _agent(node_id: str, model: str) -> Node:
    return Node(id=node_id, type="agent", agent=AgentConfig(name=node_id, model=model))


def _run(run_id: str, nodes: list[tuple[Node, NodeRun]], **fields: Any) -> Run:
    """A finished run whose usage is the sum of its steps' (regions merged), as the engine stores it."""
    usage = Usage()
    regions: set[str] = set()
    for _node, state in nodes:
        for part in (state.usage, state.decision_usage):
            if part is not None:
                usage.requests += part.requests
                usage.total_tokens += part.total_tokens
                usage.estimated_cost_usd = (usage.estimated_cost_usd or 0) + (part.estimated_cost_usd or 0)
                usage.known_cost_usd = (usage.known_cost_usd or 0) + (part.known_cost_usd or 0)
                regions.update(part.regions)
    usage.cost_complete = True
    usage.regions = sorted(regions)
    return Run(
        id=run_id,
        workflow_id=fields.pop("workflow_id", "wf_regions"),
        workflow_name="Regions",
        workflow=Workflow(id="wf_regions", name="Regions", nodes=[node for node, _state in nodes]),
        status="succeeded",
        nodes={node.id: state for node, state in nodes},
        usage=usage,
        **fields,
    )


def _entry(usd: float, region: str | None, model: str = BEDROCK) -> UsageEntry:
    return UsageEntry(source="assistant", model=model, region=region, usage=_usage(usd, *([region] if region else [])))


def _put_entries(ctx: Any, entries: list[UsageEntry]) -> None:
    with ctx.store.transaction():
        for entry in entries:
            ctx.store.put("usage_log", entry.id, entry)


def _by_region(client: Any) -> dict[str, dict[str, Any]]:
    return {bucket["key"]: bucket for bucket in client.get("/api/usage").json()["by_region"]}


def test_a_report_without_regions_has_an_empty_by_region(make_app: Any) -> None:
    """Usage of other providers, and usage recorded before the runtime recorded the region, is in no region."""
    client = make_app()
    ctx = _ctx(client)
    _store_runs(ctx, [_run("run_plain", [(_agent("a", "anthropic:claude-sonnet-5-5"), NodeRun(usage=_usage(0.5)))])])
    _put_entries(ctx, [_entry(0.25, None, model="openai:gpt-5.5")])
    report = client.get("/api/usage").json()
    assert report["by_region"] == []
    assert report["total"]["requests"] == 2 and report["total"]["regions"] == []
    assert all(bucket["regions"] == [] for bucket in report["by_model"])


def test_runs_and_operations_are_split_by_region(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    eu, us = "eu-central-1", "us-east-1"
    child = _run("run_child", [(_agent("c", BEDROCK), NodeRun(usage=_usage(0.4, us)))], parent_run_id="run_mixed")
    team = _run("run_team", [(_agent("t", BEDROCK), NodeRun(usage=_usage(0.1, eu)))], parent_run_id="run_mixed")
    sub = Node(id="sub", type="subworkflow")
    orchestrator = Node(id="orc", type="orchestrator", orchestrator=OrchestratorConfig(model=BEDROCK))
    mixed = _run(
        "run_mixed",
        [
            # A Bedrock step in eu-central-1 and a step of another provider (no region) in the same run.
            (_agent("a", BEDROCK), NodeRun(usage=_usage(1.0, eu))),
            (_agent("b", "anthropic:claude-sonnet-5-5"), NodeRun(usage=_usage(2.0))),
            # A sub-workflow whose agent ran in us-east-1 (the parent's step carries the child's usage).
            (sub, NodeRun(usage=_usage(0.4, us), child_run_id="run_child")),
            # An orchestrator: its own decisions in eu-central-1, its team's agent too.
            (
                orchestrator,
                NodeRun(usage=_usage(0.3, eu, requests=2), decision_usage=_usage(0.2, eu), child_run_ids=["run_team"]),
            ),
        ],
    )
    _store_runs(ctx, [child, team, mixed])
    _put_entries(ctx, [_entry(0.05, eu), _entry(0.5, None, model="openai:gpt-5.5")])

    report = client.get("/api/usage").json()
    regions = {bucket["key"]: bucket for bucket in report["by_region"]}
    assert set(regions) == {eu, us}
    # eu-central-1: step a (1.0), the orchestrator's decisions (0.2) and its team (0.1), plus one assistant entry.
    assert regions[eu]["runs"] == 1 and regions[eu]["operations"] == 1
    assert regions[eu]["requests"] == 4
    assert regions[eu]["known_cost_usd"] == regions[eu]["estimated_cost_usd"] == 1.35
    assert regions[eu]["cost_state"] == "known" and regions[eu]["regions"] == [eu]
    assert regions[us]["runs"] == 1 and regions[us]["operations"] == 0 and regions[us]["known_cost_usd"] == 0.4
    # The most expensive region first; the step without a region (2.0) and the OpenAI entry are in none.
    assert [bucket["key"] for bucket in report["by_region"]] == [eu, us]
    assert report["total"]["regions"] == [eu, us]
    by_model = {bucket["key"]: bucket for bucket in report["by_model"]}
    assert by_model[BEDROCK]["regions"] == [eu, us]
    assert by_model["anthropic:claude-sonnet-5-5"]["regions"] == []


def test_entries_with_a_region_only_in_their_usage_count_too(make_app: Any) -> None:
    """``UsageEntry.region`` and the entry's ``usage.regions`` name the same region; either is enough."""
    client = make_app()
    ctx = _ctx(client)
    only_usage = UsageEntry(source="agent_test", model=BEDROCK, usage=_usage(0.2, "eu-west-1"))
    only_field = UsageEntry(source="agent_test", model=BEDROCK, region="eu-west-1", usage=_usage(0.3))
    _put_entries(ctx, [only_usage, only_field])
    regions = _by_region(client)
    assert list(regions) == ["eu-west-1"]
    assert regions["eu-west-1"]["operations"] == 2 and regions["eu-west-1"]["known_cost_usd"] == 0.5


def test_a_step_with_several_regions_is_counted_once(make_app: Any) -> None:
    client = make_app()
    ctx = _ctx(client)
    _store_runs(
        ctx, [_run("run_two", [(_agent("a", BEDROCK), NodeRun(usage=_usage(0.6, "us-west-2", "eu-central-1")))])]
    )
    regions = _by_region(client)
    assert list(regions) == ["eu-central-1+us-west-2"]
    assert regions["eu-central-1+us-west-2"]["known_cost_usd"] == 0.6
