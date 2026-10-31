"""Publishing workflows as APIs in Nodestro: publications, releases and the direct API.

A **publication** is the API of one workflow: its name in the URL (``slug``), whether it is
switched on, whether it is reachable in the network, its limits and which release is active.
A **release** is an explicitly published, saved workflow version, frozen as an immutable
:class:`~nodestro_runtime.release.ReleaseBundle`. Saving the workflow again never changes a release;
only publishing again creates the next one. Any earlier release can be made active again
(rollback), and ``/versions/{n}/runs`` calls one directly.

Runs keep the release they started with: a run records ``release_id`` and the engine executes
it with the release's environment (its tools, its sub-workflow versions), also when it continues
after an approval or a restart. Switching an API off refuses new runs; runs that already started
finish (or can be cancelled together with switching off).

:class:`StudioHost` connects the shared API layer (:mod:`nodestro_api.serving`) to this
installation: the engine, the result files, the keys (:mod:`nodestro_api.api_keys`) and the
Python environments. Nothing here is a second engine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import re
import shutil
import threading
from typing import TYPE_CHECKING, Any, Literal
import unicodedata

from nodestro_runtime import __version__ as runtime_version
from nodestro_runtime.agent_builder import AgentBuilder
from nodestro_runtime.engine import MAX_DEPTH, RunEnvironment, node_label
from nodestro_runtime.preflight import PROVIDER_CHECKS, preflight
from nodestro_runtime.problems import CheckIssue, NodestroError
from nodestro_runtime.providers import PROVIDERS, MissingApiKeyError
from nodestro_runtime.python_envs import scope_of
from nodestro_runtime.release import (
    KnowledgeRef,
    MemoryRef,
    PythonLock,
    ReleaseBundle,
    SubworkflowSnapshot,
    current_runtime,
    materialize_tools,
    tool_record,
)
from nodestro_runtime.schemas import RunSummary, Workflow, new_id, now
from nodestro_runtime.tool_legacy import LegacyBinding, VersionOf, stamp_workflow
from nodestro_runtime.tool_library import ToolLibrary, make_sandbox
from orkexa import __version__ as library_version
from pydantic import BaseModel, Field

from .release_gate import GateOverride, GateRecord, GateReport, ReleaseGate, enforce, evaluate
from .release_gate import validate as validate_gate
from .serving import (
    SLUG,
    ApiError,
    ApiLimits,
    KeyIdentity,
    ServedRelease,
    api_run_summaries,
    input_model,
    output_schema,
)

if TYPE_CHECKING:
    from nodestro_runtime.schemas import Run
    from orkexa.dynamic import Sandbox

    from .context import Nodestro

PUBLICATIONS = "publications"
RELEASES = "api_releases"

EventKind = Literal[
    "published",
    "activated",
    "enabled",
    "disabled",
    "network_on",
    "network_off",
    "settings",
    "renamed",
    "exported",
    "gate_on",
    "gate_changed",
    "gate_off",
    "gate_override",
    "legacy_stamped",
]


class PublicationEvent(BaseModel):
    at: datetime = Field(default_factory=now)
    kind: EventKind
    #: e.g. the release number, the new name.
    details: dict[str, Any] = Field(default_factory=dict)


class Publication(BaseModel):
    id: str = Field(default_factory=lambda: new_id("pub"))
    workflow_id: str
    slug: str
    name: str
    description: str = ""
    #: Whether new runs may start (runs that started keep going either way).
    enabled: bool = True
    active_release: int | None = None
    #: Reachable through the network listener (when that is switched on, see :mod:`nodestro_api.network`).
    network: bool = False
    limits: ApiLimits = Field(default_factory=ApiLimits)
    #: The opt-in release gate (Q-3); ``None``: new releases need no test evidence (as before).
    gate: ReleaseGate | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    history: list[PublicationEvent] = Field(default_factory=list)


class ReleaseCheckSummary(BaseModel):
    errors: int = 0
    warnings: int = 0
    infos: int = 0


class Release(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rel"))
    publication_id: str
    workflow_id: str
    number: int
    workflow_version: int | None = None
    note: str = ""
    created_at: datetime = Field(default_factory=now)
    digest: str
    check: ReleaseCheckSummary = Field(default_factory=ReleaseCheckSummary)
    #: What the release gate said, and the override if one was given; ``None``: released without a gate.
    gate: GateRecord | None = None
    bundle: ReleaseBundle


class ReleaseSummary(BaseModel):
    id: str
    number: int
    workflow_version: int | None
    note: str
    created_at: datetime
    digest: str
    active: bool
    agents: int
    approvals: int
    tools: list[str]
    runs: int = 0
    #: ``passed`` or ``overridden`` (Q-3); ``None``: released without a gate.
    gate: Literal["passed", "overridden"] | None = None


class ApiCheck(BaseModel):
    """Whether a saved workflow version can be published, and what to know about it."""

    ok: bool
    workflow_version: int | None
    problems: list[CheckIssue] = Field(default_factory=list)
    #: Per agent: configuration problems (as in the editor's check).
    agents: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    approvals: int = 0
    subworkflows: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    libraries: list[str] = Field(default_factory=list)
    knowledge: list[KnowledgeRef] = Field(default_factory=list)
    memory: list[MemoryRef] = Field(default_factory=list)
    errors: int = 0
    warnings: int = 0


def slugify(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:48].strip("-")
    return slug or "workflow"


def _issue(code: str, message: str, level: Literal["error", "warning", "info"] = "error", **params: Any) -> CheckIssue:
    node_id = params.pop("node_id", None)
    return CheckIssue(code=code, message=message, level=level, params=params, node_id=node_id)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Publications:
    """Publications and releases of this installation."""

    def __init__(self, ctx: Nodestro) -> None:
        self.ctx = ctx
        self.store = ctx.store
        self._environments: dict[str, RunEnvironment] = {}
        self._lock = threading.RLock()

    # ---- storage ------------------------------------------------------------------

    def get(self, publication_id: str) -> Publication:
        found = self.store.get(PUBLICATIONS, publication_id, Publication)
        if found is None:
            raise KeyError(publication_id)
        return found

    def all(self) -> list[Publication]:
        return self.store.list(PUBLICATIONS, Publication)

    def for_workflow(self, workflow_id: str) -> Publication | None:
        return next((item for item in self.all() if item.workflow_id == workflow_id), None)

    def by_slug(self, slug: str) -> Publication | None:
        return next((item for item in self.all() if item.slug == slug), None)

    def _put(self, publication: Publication) -> None:
        publication.updated_at = now()
        publication.history = publication.history[-200:]
        self.store.put(PUBLICATIONS, publication.id, publication)

    def normalize_stored(self) -> None:
        """Keep only the current runtime components in stored releases (idempotent).

        The digest covers the runtime, so it is recomputed for a release that changes.
        """
        for key, release in self.store.items(RELEASES, Release):
            runtime = current_runtime(release.bundle.runtime)
            if runtime != release.bundle.runtime:
                release.bundle.runtime = runtime
                release.digest = release.bundle.digest()
                self.store.put(RELEASES, key, release)
        self.forget_environments()

    def stamp_legacy_tool_bindings(self, version_of: VersionOf) -> list[LegacyBinding]:
        """The activation step of per-call tool approvals for published releases (S-2, D10b/D10c): every tool binding
        of every stored release becomes a visible legacy exemption bound to the tool version the release runs with,
        so activating approvals never changes a published API silently. Each changed release gets a new digest, and
        its publication records the change (``legacy_stamped``) with the old and the new digest. Idempotent."""
        found: list[LegacyBinding] = []
        with self._lock, self.store.transaction():
            changed: dict[str, list[dict[str, Any]]] = {}
            for key, release in self.store.items(RELEASES, Release):
                bundle = release.bundle

                def pinned(name: str, bundle: ReleaseBundle = bundle) -> int | None:
                    record = bundle.tools.get(name)
                    return int(record.get("version", 1)) if record is not None else version_of(name)

                workflow, stamped = stamp_workflow(bundle.workflow, pinned)
                subworkflows = []
                for item in bundle.subworkflows:
                    child, more = stamp_workflow(item.workflow, pinned)
                    subworkflows.append(item.model_copy(update={"workflow": child}) if more else item)
                    stamped += [entry.model_copy(update={"workflow_id": item.workflow_id}) for entry in more]
                if not stamped:
                    continue
                before = release.digest
                bundle.workflow = workflow  # type: ignore[assignment]
                bundle.subworkflows = subworkflows
                release.digest = bundle.digest()
                self.store.put(RELEASES, key, release)
                entries = [
                    entry.model_copy(
                        update={
                            "workflow_id": entry.workflow_id or release.workflow_id,
                            "workflow_version": entry.workflow_version or release.workflow_version,
                            "release": f"{release.publication_id}:{release.number}",
                        }
                    )
                    for entry in stamped
                ]
                found.extend(entries)
                changed.setdefault(release.publication_id, []).append(
                    {
                        "release": release.number,
                        "bindings": len(entries),
                        "digest_before": before,
                        "digest": release.digest,
                    }
                )
            for publication_id, releases in changed.items():
                publication = self.store.get(PUBLICATIONS, publication_id, Publication)
                if publication is None:
                    continue
                publication.history.append(PublicationEvent(kind="legacy_stamped", details={"releases": releases}))
                self._put(publication)
        self.forget_environments()
        return found

    def releases(self, publication_id: str) -> list[Release]:
        return self.store.list_prefix(RELEASES, f"{publication_id}:", Release)

    def release(self, publication_id: str, number: int) -> Release:
        found = self.store.get(RELEASES, f"{publication_id}:{number:06d}", Release)
        if found is None:
            raise KeyError(f"v{number}")
        return found

    def release_by_id(self, release_id: str) -> Release | None:
        return next((item for item in self.store.list(RELEASES, Release) if item.id == release_id), None)

    def summaries(self, publication: Publication) -> list[ReleaseSummary]:
        counts: dict[str, int] = {}
        for summary in api_run_summaries(self.store, publication.id):
            if summary.release_id:
                counts[summary.release_id] = counts.get(summary.release_id, 0) + 1
        return [
            ReleaseSummary(
                id=item.id,
                number=item.number,
                workflow_version=item.workflow_version,
                note=item.note,
                created_at=item.created_at,
                digest=item.digest,
                active=item.number == publication.active_release,
                agents=sum(node.type == "agent" for node in item.bundle.workflow.nodes),
                approvals=item.bundle.approvals(),
                tools=sorted([*item.bundle.tools, *item.bundle.builtin_tools]),
                runs=counts.get(item.id, 0),
                gate=None if item.gate is None else ("overridden" if item.gate.override_reason else "passed"),
            )
            for item in reversed(self.releases(publication.id))
        ]

    # ---- checks -------------------------------------------------------------------

    def _versioned(self, workflow_id: str, version: int | None) -> tuple[Workflow, int | None]:
        if version is None:
            current = self.store.get("workflows", workflow_id, Workflow)
            if current is None:
                raise KeyError(workflow_id)
            version = self.ctx.current_version(current)
            if version is None:
                raise NodestroError("api.unsaved", "Save the workflow first; only saved versions can be published")
        workflow, number = self.ctx.resolve_workflow(workflow_id, version)
        return workflow, number

    def _pin_all(self, workflow: Workflow) -> tuple[Workflow, list[SubworkflowSnapshot], list[CheckIssue]]:
        """The workflow with every sub-workflow (also nested ones) pinned, and the pinned versions."""
        snapshots: dict[tuple[str, int], SubworkflowSnapshot] = {}
        problems: list[CheckIssue] = []

        def pin(item: Workflow, depth: int) -> Workflow:
            pinned = self.ctx.runs.pin_subworkflows(item)
            for node in pinned.nodes:
                config = node.subworkflow
                if node.type != "subworkflow" or config is None or not config.workflow_id:
                    continue
                if config.version is None:
                    problems.append(
                        _issue(
                            "api.subworkflow_unsaved",
                            f"{node_label(node)}: the sub-workflow has no saved version",
                            node_id=node.id,
                            name=node_label(node),
                        )
                    )
                    continue
                key = (config.workflow_id, config.version)
                if key in snapshots or depth >= MAX_DEPTH:
                    continue
                try:
                    child, number = self.ctx.resolve_workflow(config.workflow_id, config.version)
                except KeyError:
                    continue  # reported by the workflow check
                snapshots[key] = SubworkflowSnapshot(
                    workflow_id=config.workflow_id, version=number or 0, workflow=child
                )
                snapshots[key].workflow = pin(child, depth + 1)
            return pinned

        root = pin(workflow, 0)
        return root, list(snapshots.values()), problems

    def check(self, workflow_id: str, version: int | None) -> ApiCheck:
        workflow, number = self._versioned(workflow_id, version)
        root, children, problems = self._pin_all(workflow)
        everything = [root, *(item.workflow for item in children)]
        result = ApiCheck(ok=False, workflow_version=number)
        base = preflight(self.ctx, root)
        for item in base.problems:
            if item.code in PROVIDER_CHECKS:
                provider = str(item.params.get("provider") or "")
                label = PROVIDERS[provider].label if provider in PROVIDERS else provider
                problems.append(
                    _issue(
                        "api.model_key_missing",
                        f"{label} is not configured: published runs will answer 503 until it is",
                        level="warning",
                        provider=provider,
                    )
                )
            elif item.code == "check.knowledge_missing":
                problems.append(item.model_copy(update={"level": "error"}))
            else:
                problems.append(item)
        result.agents = {
            node_id: [issue.model_dump() for issue in issues] for node_id, issues in base.agents.items() if issues
        }
        agent_errors = sum(issue.level == "error" for issues in base.agents.values() for issue in issues)
        # Sub-workflows: their agents and libraries are part of the release, too.
        for child in children:
            child_check = preflight(self.ctx, child.workflow)
            for node_id, issues in child_check.agents.items():
                for issue in issues:
                    if issue.level == "error":
                        agent_errors += 1
                        problems.append(
                            _issue(
                                "api.subworkflow_agent_invalid",
                                f"{child.workflow.name}: {issue.message}",
                                workflow=child.workflow.name,
                                detail=issue.code,
                                node=node_id,
                            )
                        )
            for item in child_check.problems:
                if item.code.startswith("deps.") and item.level == "error":
                    problems.append(
                        item.model_copy(update={"params": {**item.params, "workflow": child.workflow.name}})
                    )
        tools: set[str] = set()
        server_tools: set[str] = set()
        for flow in everything:
            for node in flow.nodes:
                if node.agent is not None:
                    tools.update(node.agent.tools)
                    server_tools.update(node.agent.server_tools)
        result.tools = sorted(tools)
        result.subworkflows = [f"{item.workflow.name} v{item.version}" for item in children]
        result.approvals = sum(node.type == "approval" for item in everything for node in item.nodes)
        if result.approvals:
            problems.append(
                _issue(
                    "api.approvals",
                    f"Runs stop at {result.approvals} approval steps until someone decides in Nodestro or via the API",
                    level="info",
                    count=result.approvals,
                )
            )
        if server_tools:
            problems.append(
                _issue(
                    "api.server_tools",
                    f"Agents use server tools ({', '.join(sorted(server_tools))}); they cost extra per call",
                    level="info",
                    tools=", ".join(sorted(server_tools)),
                )
            )
        result.knowledge = self._knowledge_refs(everything)
        result.memory = self._memory_refs(everything)
        if result.knowledge:
            problems.append(
                _issue(
                    "api.knowledge_live",
                    f"{len(result.knowledge)} knowledge files are used as they are at run time (not frozen)",
                    level="info",
                    count=len(result.knowledge),
                )
            )
        if result.memory:
            spaces = ", ".join(item.space for item in result.memory)
            problems.append(
                _issue(
                    "api.memory_shared",
                    f"Persistent memory ({spaces}) is shared with the studio and every API run",
                    level="warning",
                    spaces=spaces,
                )
            )
        for flow in everything:
            needed = self.ctx.python.needed_for_workflow(flow)
            result.libraries += [requirement.line() for requirement in needed.requirements]
        result.libraries = sorted(set(result.libraries))
        budget = root.budget_usd if root.budget_usd is not None else self.ctx.settings().default_budget_usd
        if budget is None:
            problems.append(
                _issue(
                    "api.no_budget",
                    "Runs have no cost limit; set a budget for the workflow or a run limit for the API",
                    level="warning",
                )
            )
        result.output_schema = output_schema(root, lambda wid, ver: self._child(children, wid, ver))
        result.input_schema = input_model(root).model_json_schema()
        order = {"error": 0, "warning": 1, "info": 2}
        problems.sort(key=lambda item: order[item.level])
        result.problems = problems
        result.errors = sum(item.level == "error" for item in problems) + agent_errors
        result.warnings = sum(item.level == "warning" for item in problems)
        result.ok = result.errors == 0
        return result

    @staticmethod
    def _child(children: list[SubworkflowSnapshot], workflow_id: str, version: int | None) -> Workflow:
        for item in children:
            if item.workflow_id == workflow_id and (version is None or item.version == version):
                return item.workflow
        raise KeyError(workflow_id)

    def _knowledge_refs(self, workflows: list[Workflow]) -> list[KnowledgeRef]:
        refs: dict[str, KnowledgeRef] = {}
        store = self.ctx.knowledge.store
        for workflow in workflows:
            for node in workflow.nodes:
                if node.agent is None:
                    continue
                for file_id in node.agent.knowledge.file_ids:
                    ref = refs.get(file_id)
                    if ref is None:
                        if file_id not in store:
                            continue
                        entry = store.info(file_id)
                        ref = refs[file_id] = KnowledgeRef(
                            id=file_id,
                            name=entry.name,
                            media_type=entry.media_type,
                            size=entry.size,
                            sha256=entry.sha256,
                        )
                    ref.used_by.append(node_label(node))
                    if node.agent.knowledge.mode not in ref.modes:
                        ref.modes.append(node.agent.knowledge.mode)
            start = next((node for node in workflow.nodes if node.type == "start"), None)
            for file_id in start.start.file_ids if start is not None and start.start is not None else []:
                if file_id in store and file_id not in refs:
                    entry = store.info(file_id)
                    refs[file_id] = KnowledgeRef(
                        id=file_id,
                        name=entry.name,
                        media_type=entry.media_type,
                        size=entry.size,
                        sha256=entry.sha256,
                        used_by=["Start"],
                    )
        return sorted(refs.values(), key=lambda item: item.name.lower())

    @staticmethod
    def _memory_refs(workflows: list[Workflow]) -> list[MemoryRef]:
        refs: dict[str, MemoryRef] = {}
        for workflow in workflows:
            for node in workflow.nodes:
                if node.agent is not None and node.agent.memory.mode == "persistent" and node.agent.memory.space:
                    refs.setdefault(node.agent.memory.space, MemoryRef(space=node.agent.memory.space)).used_by.append(
                        node_label(node)
                    )
        return sorted(refs.values(), key=lambda item: item.space.lower())

    # ---- publishing ---------------------------------------------------------------

    def _bundle(self, workflow_id: str, version: int | None) -> tuple[ReleaseBundle, ApiCheck]:
        check = self.check(workflow_id, version)
        if not check.ok:
            raise NodestroError(
                "api.publish_blocked",
                f"The workflow cannot be published: {check.errors} problems",
                count=check.errors,
            )
        workflow, number = self._versioned(workflow_id, version)
        root, children, _ = self._pin_all(workflow)
        everything = [root, *(item.workflow for item in children)]
        tools: dict[str, dict[str, Any]] = {}
        builtin: set[str] = set()
        server_tools: set[str] = set()
        models: set[str] = set()
        for item in everything:
            for node in item.nodes:
                if node.agent is None:
                    continue
                models.update({node.agent.model, *node.agent.fallbacks})
                if node.agent.quality.enabled:
                    models.add(node.agent.quality.judge_model)
                server_tools.update(node.agent.server_tools)
                for name in node.agent.tools:
                    if name in self.ctx.tools.builtin:
                        builtin.add(name)
                    elif name not in tools:
                        tools[name] = tool_record(self.ctx.tools.registry.record(name))
        python: list[PythonLock] = []
        for item in everything:
            env = self.ctx.python.get(scope_of("workflow", item.id))
            needed = self.ctx.python.needed_for_workflow(item)
            if needed.requirements and env is not None and env.lock:
                python.append(
                    PythonLock(
                        workflow_id=item.id,
                        requirements=[requirement.line() for requirement in needed.requirements],
                        lock=env.lock,
                        lock_sha=_sha(env.lock),
                        python_version=env.python_version,
                    )
                )
        bundle = ReleaseBundle(
            workflow_id=workflow_id,
            workflow_version=number,
            workflow=root,
            subworkflows=children,
            tools=tools,
            tool_trust={name: self.ctx.tools.trust(name) for name in tools},
            builtin_tools=sorted(builtin),
            server_tools=sorted(server_tools),
            models=sorted(models),
            python=python,
            knowledge=check.knowledge,
            memory=check.memory,
            output_schema=check.output_schema,
            runtime={"nodestro": runtime_version, "orkexa": library_version},
        )
        return bundle, check

    def gate_report(self, workflow_id: str, version: int | None, gate: ReleaseGate | None = None) -> GateReport:
        """The release gate's verdict for a saved version: ``gate``, or else the publication's own gate."""
        _, number = self._versioned(workflow_id, version)
        if gate is None:
            publication = self.for_workflow(workflow_id)
            gate = publication.gate if publication else None
        else:
            validate_gate(self.ctx, workflow_id, gate)
        return evaluate(self.ctx, workflow_id, number, gate)

    def set_gate(self, publication_id: str, gate: ReleaseGate | None) -> Publication:
        """Switch the release gate on, change it, or switch it off; every change is in the history."""
        with self._lock:
            publication = self.get(publication_id)
            if gate is not None:
                validate_gate(self.ctx, publication.workflow_id, gate)
            if gate == publication.gate:
                return publication
            kind: EventKind = (
                "gate_off" if gate is None else ("gate_on" if publication.gate is None else "gate_changed")
            )
            details = {} if gate is None else gate.model_dump()
            publication.gate = gate
            publication.history.append(PublicationEvent(kind=kind, details=details))
            self._put(publication)
            return publication

    def publish(
        self,
        workflow_id: str,
        version: int | None,
        *,
        note: str = "",
        slug: str | None = None,
        activate: bool = True,
        gate: ReleaseGate | None = None,
        override: GateOverride | None = None,
        principal_id: str = "local",
    ) -> tuple[Publication, Release]:
        """Create a release. ``gate`` switches the release gate on (or changes it) before it is evaluated."""
        with self._lock:
            bundle, check = self._bundle(workflow_id, version)
            publication = self.for_workflow(workflow_id)
            wanted_gate = gate if gate is not None else (publication.gate if publication else None)
            if gate is not None:
                validate_gate(self.ctx, workflow_id, gate)
            report = evaluate(self.ctx, workflow_id, bundle.workflow_version, wanted_gate)
            record = enforce(report, override, principal_id)
            if publication is None:
                wanted = slugify(slug or bundle.workflow.name)
                publication = Publication(
                    workflow_id=workflow_id,
                    slug=self._free_slug(wanted, None),
                    name=bundle.workflow.name,
                    description=bundle.workflow.description,
                )
            elif slug:
                self.check_slug(slug, publication.id)
                publication.slug = slug
            existing = self.releases(publication.id)
            number = (existing[-1].number if existing else 0) + 1
            release = Release(
                publication_id=publication.id,
                workflow_id=workflow_id,
                number=number,
                workflow_version=bundle.workflow_version,
                note=note.strip()[:200],
                digest=bundle.digest(),
                check=ReleaseCheckSummary(
                    errors=check.errors,
                    warnings=check.warnings,
                    infos=sum(item.level == "info" for item in check.problems),
                ),
                gate=record,
                bundle=bundle,
            )
            if gate is not None and gate != publication.gate:
                changed: EventKind = "gate_on" if publication.gate is None else "gate_changed"
                publication.gate = gate
                publication.history.append(PublicationEvent(kind=changed, details=gate.model_dump()))
            self.store.put(RELEASES, f"{publication.id}:{number:06d}", release)
            publication.name = bundle.workflow.name
            publication.history.append(
                PublicationEvent(kind="published", details={"release": number, "version": bundle.workflow_version})
            )
            if record is not None and record.override_reason:
                failed = [item.code for item in record.report.items if not item.passed]
                publication.history.append(
                    PublicationEvent(
                        kind="gate_override",
                        details={
                            "release": number,
                            "reason": record.override_reason,
                            "by": record.override_by,
                            "failed": failed,
                        },
                    )
                )
            if activate or publication.active_release is None:
                publication.active_release = number
                publication.history.append(PublicationEvent(kind="activated", details={"release": number}))
            self._put(publication)
            return publication, release

    def check_slug(self, slug: str, publication_id: str | None) -> None:
        if not SLUG.match(slug):
            raise NodestroError(
                "api.slug_invalid", "Use lower-case letters, digits and hyphens (up to 48 characters)", slug=slug
            )
        other = self.by_slug(slug)
        if other is not None and other.id != publication_id:
            raise NodestroError("api.slug_taken", f"Another API is already called {slug}", slug=slug)

    def _free_slug(self, wanted: str, publication_id: str | None) -> str:
        candidate, counter = wanted, 2
        while (other := self.by_slug(candidate)) is not None and other.id != publication_id:
            candidate = f"{wanted[:44]}-{counter}"
            counter += 1
        return candidate

    def activate(self, publication_id: str, number: int) -> Publication:
        with self._lock:
            publication = self.get(publication_id)
            self.release(publication_id, number)
            publication.active_release = number
            publication.history.append(PublicationEvent(kind="activated", details={"release": number}))
            self._put(publication)
        return publication

    def set_enabled(
        self, publication_id: str, enabled: bool, *, cancel_running: bool = False
    ) -> tuple[Publication, int]:
        with self._lock:
            publication = self.get(publication_id)
            publication.enabled = enabled
            publication.history.append(PublicationEvent(kind="enabled" if enabled else "disabled"))
            self._put(publication)
        cancelled = 0
        if not enabled and cancel_running:
            for run in self.ctx.runs.live_runs():
                if run.trigger == "api" and run.trigger_ref == publication_id and run.parent_run_id is None:
                    cancelled += int(self.ctx.runs.cancel(run.id))
            for summary in api_run_summaries(self.store, publication_id):
                if summary.status == "paused":
                    cancelled += int(self.ctx.runs.cancel(summary.id))
        return publication, cancelled

    def update(
        self,
        publication_id: str,
        *,
        slug: str | None = None,
        description: str | None = None,
        limits: ApiLimits | None = None,
        network: bool | None = None,
    ) -> Publication:
        with self._lock:
            return self._update(publication_id, slug=slug, description=description, limits=limits, network=network)

    def _update(
        self,
        publication_id: str,
        *,
        slug: str | None,
        description: str | None,
        limits: ApiLimits | None,
        network: bool | None,
    ) -> Publication:
        publication = self.get(publication_id)
        if slug is not None and slug != publication.slug:
            self.check_slug(slug, publication.id)
            publication.history.append(PublicationEvent(kind="renamed", details={"from": publication.slug, "to": slug}))
            publication.slug = slug
        if description is not None:
            publication.description = description.strip()[:2000]
        if limits is not None:
            if limits.sync_enabled and publication.active_release is not None:
                active = self.release(publication.id, publication.active_release)
                if active.bundle.approvals():
                    raise NodestroError(
                        "api.sync_with_approvals",
                        "Waiting for the result is not possible for a workflow with approval steps",
                    )
            publication.limits = limits
            publication.history.append(PublicationEvent(kind="settings"))
        if network is not None and network != publication.network:
            publication.network = network
            publication.history.append(PublicationEvent(kind="network_on" if network else "network_off"))
        self._put(publication)
        return publication

    def delete(self, publication_id: str) -> None:
        with self._lock:
            publication = self.get(publication_id)
            for release in self.releases(publication.id):
                self.forget_environment(release.id)
            self.store.delete_prefix(RELEASES, f"{publication.id}:")
            self.store.delete(PUBLICATIONS, publication.id)

    def forget_workflow(self, workflow_id: str) -> list[str]:
        removed = []
        for publication in self.all():
            if publication.workflow_id == workflow_id:
                self.delete(publication.id)
                removed.append(publication.id)
        return removed

    # ---- execution environment ----------------------------------------------------

    def _release_dir(self, release_id: str) -> Any:
        return self.ctx.config.data_dir / "releases" / release_id

    def forget_environments(self) -> None:
        with self._lock:
            self._environments.clear()

    def forget_environment(self, release_id: str) -> None:
        with self._lock:
            self._environments.pop(release_id, None)
        shutil.rmtree(self._release_dir(release_id), ignore_errors=True)

    def environment(self, release_id: str) -> RunEnvironment | None:
        """The engine environment of a release: its own tool registry and sub-workflow versions."""
        with self._lock:
            cached = self._environments.get(release_id)
            if cached is not None:
                return cached
            release = self.release_by_id(release_id)
            if release is None:
                return None
            bundle = release.bundle
            # The studio's provenance decides the trust class of the frozen tools, not the bundle's claim.
            tools = ToolLibrary(
                materialize_tools(bundle, self._release_dir(release_id) / "tools"),
                sandbox=make_sandbox(self.ctx._stored("sandbox") or "subprocess", self.ctx.sandbox_policy),
                policy=self.ctx.sandbox_policy,
                provenance=self.ctx.tools.linked_provenance(),
            )
            builder = AgentBuilder(tools, self.ctx.knowledge, self.ctx.memory)
            builder.declared = self.ctx.builder.declared
            locks = {item.workflow_id for item in bundle.python}

            def sandbox(workflow_id: str) -> Sandbox | None:
                return self.ctx.python.workflow_sandbox(workflow_id) if workflow_id in locks else None

            environment = RunEnvironment(
                builder=builder,
                resolve_workflow=bundle.resolver(),
                tool_sandbox=sandbox,
                tool_marker=bundle.tool_marker,
            )
            self._environments[release_id] = environment
            return environment

    def served(self, publication: Publication, release: Release) -> ServedRelease:
        return ServedRelease(
            publication_id=publication.id,
            slug=publication.slug,
            name=publication.name,
            description=publication.description,
            release_id=release.id,
            number=release.number,
            workflow_version=release.workflow_version,
            workflow=release.bundle.workflow,
            limits=publication.limits,
            enabled=publication.enabled,
            active=publication.active_release == release.number,
            network=publication.network,
            published_at=release.created_at,
            approvals=release.bundle.approvals(),
            output_schema=release.bundle.output_schema,
            missing_resources=[
                f"knowledge:{ref.name}" for ref in release.bundle.knowledge if ref.id not in self.ctx.knowledge.store
            ],
        )


class StudioHost:
    """The direct API of this Nodestro installation (see :class:`nodestro_api.serving.ServingHost`)."""

    def __init__(self, ctx: Nodestro) -> None:
        self.ctx = ctx
        self.runs = ctx.runs
        self.store = ctx.store
        self.files = ctx.files

    def release(self, slug: str, number: int | None, *, channel: str) -> ServedRelease:
        publication = self.ctx.publications.by_slug(slug)
        if (
            publication is None
            or publication.active_release is None
            or (channel == "network" and not publication.network)
        ):
            raise ApiError(404, "publication.not_found", "No published workflow with this name")
        try:
            release = self.ctx.publications.release(publication.id, number or publication.active_release)
        except KeyError:
            raise ApiError(404, "version.not_found", "This version was not published", version=number) from None
        return self.ctx.publications.served(publication, release)

    def versions(self, slug: str) -> list[int]:
        publication = self.ctx.publications.by_slug(slug)
        return [item.number for item in self.ctx.publications.releases(publication.id)] if publication else []

    def release_of_run(self, run: Run) -> ServedRelease | None:
        if not run.release_id or not run.trigger_ref:
            return None
        release = self.ctx.publications.release_by_id(run.release_id)
        try:
            publication = self.ctx.publications.get(run.trigger_ref)
        except KeyError:
            return None
        return self.ctx.publications.served(publication, release) if release is not None else None

    def verify(self, key_id: str, secret: str) -> KeyIdentity | None:
        return self.ctx.api_keys.verify(key_id, secret)

    async def prepare_start(self, release: ServedRelease) -> None:
        stored = self.ctx.publications.release_by_id(release.release_id)
        if stored is None:
            raise ApiError(404, "version.not_found", "This version was not published")
        bundle = stored.bundle
        # Every provider the release's agents use must be configured (bare model ids of older
        # releases are Anthropic's); no other provider stands in for a missing one.
        try:
            self.ctx.require_providers(set(bundle.models))
        except MissingApiKeyError as exc:
            provider = str(exc.params.get("provider") or "")
            raise ApiError(
                503,
                "model.unavailable",
                f"Nodestro has no settings for the provider {provider}; runs cannot start",
                headers={"Retry-After": "300"},
                provider=provider,
            ) from None
        for lock in bundle.python:
            env = self.ctx.python.get(scope_of("workflow", lock.workflow_id))
            if env is None or _sha(env.lock) != lock.lock_sha:
                raise ApiError(
                    503,
                    "publication.dependencies_changed",
                    "The approved Python libraries changed since publishing; publish again",
                )
            workflow = next((item for item in bundle.all_workflows() if item.id == lock.workflow_id), None)
            if workflow is None:
                continue
            needed = self.ctx.python.needed_for_sources(bundle.tool_sources(workflow))
            status = await asyncio.to_thread(self.ctx.python.ensure, "workflow", lock.workflow_id, needed)
            if status.state != "ready":
                raise ApiError(
                    503,
                    "publication.dependencies_unavailable",
                    "The Python libraries of this API are not ready",
                    state=status.state,
                )

    def add_upload(self, release: ServedRelease, name: str, data: bytes, media_type: str | None) -> str:
        # An input file of the run, not knowledge (see input_files.py).
        limit = release.limits.max_upload_mb
        return self.ctx.inputs.add(data, name=name, media_type=media_type, origin="api", limit_mb=limit).id

    def builtin_tools(self) -> set[str]:
        return set(self.ctx.tools.builtin)


def api_runs(ctx: Nodestro, publication_id: str, limit: int | None = 50) -> list[RunSummary]:
    """The newest top-level runs of a publication (``limit=None``: all of them)."""
    return api_run_summaries(ctx.store, publication_id)[:limit]
