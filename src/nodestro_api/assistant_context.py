"""Nodestro objects as context for the Nodestro Assistant.

The user attaches an object to a conversation (a workflow, an agent, a tool, a memory
space, a knowledge file or a run). Nodestro then takes a *snapshot*: a structured copy
built from the existing data models, with

- **secrets masked** (:class:`nodestro_runtime.sanitize.Sanitizer`: API keys, tokens, passwords,
  secret-named JSON keys, and the configured Anthropic key wherever it appears),
- **long texts shortened** to a size limit, with a note of what was shortened, so the
  assistant can say that it only saw part of the data,
- a **fingerprint** of the object's content, so a later change (or deletion) is detected
  without storing anything twice.

Snapshots never change. A message records which snapshots it was sent with, and
"refresh context" only takes a new snapshot for the following messages. Nothing here
calls the API, runs a workflow or changes an object.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from nodestro_runtime.engine import node_label
from nodestro_runtime.problems import NodestroError, Problem, problem
from nodestro_runtime.python_deps import analyze as analyze_dependencies
from nodestro_runtime.python_envs import scope_of
from nodestro_runtime.sanitize import Sanitizer
from nodestro_runtime.schemas import AgentConfig, AgentTemplate, Run, Workflow, WorkflowIn, new_id, now
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .context import Nodestro

AttachmentKind = Literal["workflow", "agent", "tool", "memory", "knowledge", "run"]
Variant = Literal["saved", "draft"]

#: Characters of one object in the model's context; longer texts are shortened.
SNAPSHOT_LIMIT = 60_000
#: Characters of all attached objects of one request together.
REQUEST_LIMIT = 160_000
#: Content of one memory file or knowledge file that is included at most.
_FILE_LIMIT = 40_000
_MAX_MEMORY_FILES = 20


class AttachRequest(BaseModel):
    kind: AttachmentKind
    #: Workflow id, agent template id or ``<workflow id>/<node id>``, tool name, memory space name,
    #: knowledge file id or run id.
    ref_id: str = Field(min_length=1, max_length=300)
    #: ``draft``: the editor's unsaved workflow (``workflow`` carries it) instead of the saved one.
    variant: Variant = "saved"
    workflow: WorkflowIn | None = None
    #: Memory: the files whose content the user selected (paths); without them only the list is shared.
    paths: list[str] = Field(default_factory=list, max_length=_MAX_MEMORY_FILES)
    #: Knowledge: share the file's text as well, not only its metadata.
    include_content: bool = False


class Snapshot(BaseModel):
    """What the assistant gets of one object at one point in time; never changed afterwards."""

    id: str = Field(default_factory=lambda: new_id("snap"))
    conversation_id: str = ""
    kind: AttachmentKind
    ref_id: str
    label: str
    variant: Variant = "saved"
    #: A readable version, e.g. ``v4`` of a workflow or ``v2`` of a tool.
    version: str | None = None
    #: The hash of the object's content when the snapshot was taken.
    fingerprint: str
    captured_at: datetime = Field(default_factory=now)
    data: dict[str, Any]
    #: What was left out or changed: shortened texts, masked secrets, missing references.
    notes: list[Problem] = Field(default_factory=list)
    chars: int = 0
    truncated: bool = False
    #: Number of masked credentials.
    secrets: int = 0
    #: The options of the attachment (memory paths, knowledge content).
    paths: list[str] = Field(default_factory=list)
    include_content: bool = False


def fingerprint(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _workflow_content(workflow: WorkflowIn) -> dict[str, Any]:
    return WorkflowIn.model_validate(workflow.model_dump()).model_dump(mode="json")


# ---- shortening ------------------------------------------------------------------------


def _strings(value: Any) -> list[int]:
    if isinstance(value, str):
        return [len(value)]
    if isinstance(value, dict):
        return [size for item in value.values() for size in _strings(item)]
    if isinstance(value, list):
        return [size for item in value for size in _strings(item)]
    return []


def _cut(value: Any, cap: int, counter: list[int]) -> Any:
    if isinstance(value, str) and len(value) > cap:
        counter[0] += 1
        return value[:cap] + f" … [shortened: {cap} of {len(value)} characters]"
    if isinstance(value, dict):
        return {key: _cut(item, cap, counter) for key, item in value.items()}
    if isinstance(value, list):
        return [_cut(item, cap, counter) for item in value]
    return value


def fit(data: dict[str, Any], limit: int) -> tuple[dict[str, Any], int]:
    """``data`` within ``limit`` characters of JSON: the longest texts are shortened first.

    Returns the data and the number of shortened texts.
    """
    if len(json.dumps(data, ensure_ascii=False)) <= limit:
        return data, 0
    sizes = sorted(_strings(data), reverse=True)
    cap = max(sizes[0] // 2, 200) if sizes else 200
    result = data
    while True:
        counter = [0]
        result = _cut(data, cap, counter)
        if len(json.dumps(result, ensure_ascii=False)) <= limit or cap <= 200:
            return result, counter[0]
        cap = max(cap // 2, 200)


# ---- the objects ------------------------------------------------------------------------


def _levels(workflow: WorkflowIn) -> list[list[str]]:
    """The order of execution: each level runs after the previous one, its nodes in parallel."""
    incoming: dict[str, set[str]] = {node.id: set() for node in workflow.nodes}
    for edge in workflow.edges:
        if edge.target in incoming and edge.source in incoming:
            incoming[edge.target].add(edge.source)
    labels = {node.id: node_label(node) for node in workflow.nodes}
    done: set[str] = set()
    levels: list[list[str]] = []
    while len(done) < len(incoming):
        ready = [node_id for node_id, sources in incoming.items() if node_id not in done and sources <= done]
        if not ready:  # a cycle; the check reports it
            break
        levels.append([labels[node_id] for node_id in ready])
        done.update(ready)
    return levels


def _agent_data(nodestro: Nodestro, config: AgentConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    tools = {info.name: info for info in nodestro.tools.all()}
    data["tools"] = [
        {"name": name, "description": tools[name].description if name in tools else None, "exists": name in tools}
        for name in config.tools
    ]
    files = {entry.id: entry for entry in nodestro.knowledge.store.list()}
    data["knowledge"]["files"] = [
        {"id": file_id, "name": files[file_id].name if file_id in files else None, "exists": file_id in files}
        for file_id in config.knowledge.file_ids
    ]
    data["knowledge"].pop("file_ids", None)
    data["check"] = [issue.model_dump(mode="json") for issue in nodestro.builder.validate(config)]
    return data


def _workflow_data(nodestro: Nodestro, workflow: WorkflowIn, workflow_id: str | None) -> dict[str, Any]:
    from nodestro_runtime.preflight import preflight

    labels = {node.id: node_label(node) for node in workflow.nodes}
    nodes = []
    for node in workflow.nodes:
        entry: dict[str, Any] = {"id": node.id, "type": node.type, "label": labels[node.id]}
        if node.agent is not None:
            entry["agent"] = _agent_data(nodestro, node.agent)
        if node.start is not None:
            entry["start"] = node.start.model_dump(mode="json")
        if node.condition is not None:
            entry["decision"] = node.condition.model_dump(mode="json")
        if node.approval is not None:
            entry["approval"] = node.approval.model_dump(mode="json")
        if node.subworkflow is not None:
            data = node.subworkflow.model_dump(mode="json")
            child = nodestro.store.get("workflows", node.subworkflow.workflow_id, Workflow)
            data["workflow_name"] = child.name if child is not None else None
            entry["subworkflow"] = data
        if node.orchestrator is not None:
            data = node.orchestrator.model_dump(mode="json")
            for member in data["members"]:
                team = nodestro.store.get("workflows", member["workflow_id"], Workflow)
                member["workflow_name"] = team.name if team is not None else None
            entry["orchestrator"] = data
        if node.output is not None and node.output.files:
            entry["result_files"] = node.output.model_dump(mode="json")["files"]
        nodes.append(entry)
    base = WorkflowIn.model_validate(workflow.model_dump())
    check = preflight(nodestro, Workflow(**base.model_dump(), id=workflow_id or "draft"))
    return {
        "name": workflow.name,
        "description": workflow.description,
        "budget_usd": workflow.budget_usd,
        "nodes": nodes,
        "connections": [
            {
                "from": labels.get(edge.source, edge.source),
                "to": labels.get(edge.target, edge.target),
                **({"branch": _branch(workflow, edge.source, edge.source_handle)} if edge.source_handle else {}),
            }
            for edge in workflow.edges
        ],
        "execution_order": _levels(workflow),
        "check": {
            "ok": check.ok,
            "problems": [
                {
                    "level": item.level,
                    "code": item.code,
                    "message": item.message,
                    "node": labels.get(item.node_id or ""),
                }
                for item in check.problems
            ],
            "agents": {
                labels.get(node_id, node_id): [issue.message for issue in issues]
                for node_id, issues in check.agents.items()
                if issues
            },
        },
    }


def _branch(workflow: WorkflowIn, source: str, handle: str | None) -> str:
    from nodestro_runtime.graph import port_label

    node = next((item for item in workflow.nodes if item.id == source), None)
    return port_label(node, handle) if node is not None else (handle or "")


def _version_label(nodestro: Nodestro, workflow: Workflow) -> str | None:
    current = next((item for item in nodestro.versions.summaries(workflow) if item.current), None)
    return current.label if current else None


def _run_data(run: Run) -> dict[str, Any]:
    labels = {node.id: node_label(node) for node in run.workflow.nodes}
    models = {node.id: node.agent.model for node in run.workflow.nodes if node.agent is not None}

    def seconds(start: datetime | None, end: datetime | None) -> float | None:
        return round((end - start).total_seconds(), 1) if start and end else None

    nodes = []
    for node_id, state in run.nodes.items():
        nodes.append(
            {
                "node": labels.get(node_id, node_id),
                "model": models.get(node_id),
                "status": state.status,
                "error": {"code": state.error_code, "message": state.error} if state.error else None,
                "duration_s": seconds(state.started_at, state.finished_at),
                "usage": state.usage.model_dump(mode="json"),
                "prompt": state.prompt,
                "output": state.output,
                "tool_calls": [
                    {
                        "name": call.name,
                        "input": call.input,
                        "output": call.output,
                        "is_error": call.is_error,
                        "duration_s": seconds(call.started_at, call.finished_at),
                    }
                    for call in state.tool_calls
                ],
                "evaluation": state.evaluation.model_dump(mode="json") if state.evaluation else None,
                "decision": state.decision.model_dump(mode="json") if state.decision else None,
                "approval": state.approval.model_dump(mode="json") if state.approval else None,
                "sub_workflow_run": state.child_run_id,
                "reused_from_run": state.reused_from,
            }
        )
    return {
        "id": run.id,
        "workflow": {"id": run.workflow_id, "name": run.workflow_name},
        "status": run.status,
        "error": {"code": run.error_code, "message": run.error} if run.error else None,
        "input": run.input,
        "output": run.output,
        "created_at": run.created_at.isoformat(),
        "duration_s": seconds(run.started_at, run.finished_at),
        "usage": run.usage.model_dump(mode="json"),
        "imported": run.imported_at is not None,
        "form_values": run.values,
        "trigger": run.trigger,
        "continued_from_run": run.resumed_from,
        "parent_run": run.parent_run_id,
        "sanitized_export": run.sanitized is not None,
        "nodes": nodes,
        "agents_configuration": {
            labels[node.id]: {
                "model": node.agent.model,
                "system_prompt": node.agent.system_prompt,
                "task_template": node.agent.task_template,
                "tools": node.agent.tools,
                "server_tools": node.agent.server_tools,
                "max_tokens": node.agent.max_tokens,
                "effort": node.agent.effort,
            }
            for node in run.workflow.nodes
            if node.agent is not None
        },
    }


def _used_by(nodestro: Nodestro, matches: Callable[[AgentConfig], bool]) -> list[str]:
    """``workflow / agent`` names of the saved agents that use something."""
    found = []
    for workflow in nodestro.store.list("workflows", Workflow):
        found += [
            f"{workflow.name} / {node_label(node)}"
            for node in workflow.nodes
            if node.agent is not None and matches(node.agent)
        ]
    return found[:50]


class _Built(BaseModel):
    label: str
    version: str | None = None
    #: The content the fingerprint is computed from (the stored object, not the description).
    content: Any
    data: dict[str, Any]
    notes: list[Problem] = Field(default_factory=list)


def _not_found(kind: str, ref_id: str) -> NodestroError:
    return NodestroError(
        "assistant.object_missing", f"The {kind} {ref_id!r} does not exist (any more)", kind=kind, name=ref_id
    )


def _workflow(nodestro: Nodestro, workflow_id: str) -> Workflow:
    workflow = nodestro.store.get("workflows", workflow_id, Workflow)
    if workflow is None:
        raise _not_found("workflow", workflow_id)
    return workflow


def _build(nodestro: Nodestro, request: AttachRequest) -> _Built:
    kind, ref = request.kind, request.ref_id
    if kind == "workflow":
        stored = _workflow(nodestro, ref)
        if request.variant == "draft":
            if request.workflow is None:
                raise NodestroError("assistant.draft_missing", "An unsaved draft needs the editor's workflow")
            draft = request.workflow
            return _Built(
                label=draft.name,
                version=_version_label(nodestro, stored),
                content=_workflow_content(draft),
                data=_workflow_data(nodestro, draft, ref),
            )
        return _Built(
            label=stored.name,
            version=_version_label(nodestro, stored),
            content=_workflow_content(stored),
            data=_workflow_data(nodestro, stored, ref),
        )
    if kind == "agent":
        if "/" in ref:
            workflow_id, _, node_id = ref.partition("/")
            stored = _workflow(nodestro, workflow_id)
            source: WorkflowIn = stored
            if request.variant == "draft":
                if request.workflow is None:
                    raise NodestroError("assistant.draft_missing", "An unsaved draft needs the editor's workflow")
                source = request.workflow
            node = next((item for item in source.nodes if item.id == node_id and item.agent is not None), None)
            if node is None or node.agent is None:
                raise _not_found("agent", ref)
            data = _agent_data(nodestro, node.agent)
            data["workflow"] = stored.name
            return _Built(
                label=node_label(node),
                version=_version_label(nodestro, stored),
                content=node.agent.model_dump(mode="json"),
                data=data,
            )
        template = nodestro.store.get("templates", ref, AgentTemplate)
        if template is None:
            raise _not_found("agent", ref)
        data = _agent_data(nodestro, template.agent)
        data["library_template"] = True
        return _Built(label=template.agent.name, content=template.agent.model_dump(mode="json"), data=data)
    if kind == "tool":
        if ref not in nodestro.tools:
            raise _not_found("tool", ref)
        info = nodestro.tools.get(ref)
        data = info.model_dump(mode="json")
        if info.code:
            found = analyze_dependencies(info.code, info.tests or "")
            data["dependencies"] = {
                "requirements": [item.line() for item in found.requirements],
                "imports": [f"{item.name} ({item.kind})" for item in found.modules],
                "issues": [item.message for item in found.issues],
            }
            environment = nodestro.python.get(scope_of("tool", ref))
            if environment is not None:
                data["dependencies"]["approved_versions"] = [
                    f"{item.name}=={item.version}" for item in environment.packages if item.direct
                ]
                data["dependencies"]["installed"] = environment.install.status
        data["used_by"] = _used_by(nodestro, lambda config: ref in config.tools)
        return _Built(
            label=ref,
            version=f"v{info.version}",
            content=info.model_dump(mode="json"),
            data=data,
        )
    if kind == "memory":
        space = nodestro.memory.open(ref)
        files = space.entries()
        if not files and ref not in {info.name for info in nodestro.memory.all()}:
            raise _not_found("memory", ref)
        known = {item.path for item in files}
        contents: dict[str, str] = {}
        notes: list[Problem] = []
        for path in request.paths:
            if path not in known:
                notes.append(problem("assistant.memory_file_missing", f"{path} does not exist", path=path))
                continue
            contents[path] = space.read(path)[:_FILE_LIMIT]
        data = {
            "space": ref,
            "files": [item.model_dump(mode="json") for item in files],
            "selected_contents": contents,
            "contents_shared": "only the selected files" if contents else "none (only the file list)",
            "used_by": _used_by(
                nodestro, lambda config: config.memory.mode == "persistent" and config.memory.space == ref
            ),
        }
        return _Built(
            label=ref,
            content={"files": [item.model_dump(mode="json") for item in files], "contents": contents},
            data=data,
            notes=notes,
        )
    if kind == "knowledge":
        if ref not in nodestro.knowledge.store:
            raise _not_found("knowledge", ref)
        entry = nodestro.knowledge.get(ref)
        data = {**entry.model_dump(mode="json"), "used_by": _used_by(nodestro, lambda c: ref in c.knowledge.file_ids)}
        if request.include_content:
            data["content"] = _knowledge_text(nodestro, ref)[:_FILE_LIMIT] or None
        else:
            data["content"] = "not shared (only the metadata)"
        return _Built(label=entry.name, content=entry.model_dump(mode="json"), data=data)
    if kind == "run":
        run = nodestro.store.get("runs", ref, Run)
        if run is None:
            raise _not_found("run", ref)
        return _Built(
            label=f"{run.workflow_name} · {run.created_at:%Y-%m-%d %H:%M}",
            content={"status": run.status, "finished_at": str(run.finished_at), "nodes": len(run.nodes)},
            data={
                **_run_data(run),
                "trigger_ref": run.trigger_ref,
                "result_files": [
                    {"name": entry.name, "format": entry.format, "error": entry.error_code}
                    for entry in nodestro.files.list(run.id)
                ],
                "input_files": _run_inputs(nodestro, run),
            },
        )
    raise NodestroError("assistant.kind_unknown", f"Unknown object kind {kind!r}", kind=kind)


def _run_inputs(nodestro: Nodestro, run: Run) -> list[dict[str, Any]]:
    """The files a run received (names and formats only, never their content)."""
    from nodestro_runtime.input_files import is_input_id

    found: list[dict[str, Any]] = []
    for file_id in run.file_ids:
        if is_input_id(file_id):
            item = nodestro.inputs.find(file_id)
            found.append(
                {"source": "upload", "name": item.name if item else None, "format": item.format if item else None}
                if item
                else {"source": "upload", "available": False}
            )
        else:
            found.append({"source": "knowledge", "id": file_id})
    return found


def _knowledge_text(nodestro: Nodestro, file_id: str) -> str:
    """The readable text: converted DOCX/XLSX, OCR text of scans (uncertain words marked) or indexed text."""
    return nodestro.knowledge.text_of(file_id)


def take_snapshot(nodestro: Nodestro, request: AttachRequest, *, conversation_id: str = "") -> Snapshot:
    """A sanitized, size-limited snapshot of one object; raises if it does not exist."""
    built = _build(nodestro, request)
    sanitizer = Sanitizer(personal_data=False)
    for key in nodestro.secret_keys():
        sanitizer._placeholder("api_key", key)
        sanitizer.counts["api_key"] -= 1  # registered, not found
    data = sanitizer.known(sanitizer.value(built.data))
    secrets = sum(sanitizer.counts.values())
    notes = list(built.notes)
    if secrets:
        notes.append(problem("assistant.secrets_masked", f"{secrets} secret value(s) masked", count=secrets))
    data, shortened = fit(data, SNAPSHOT_LIMIT)
    if shortened:
        notes.append(problem("assistant.shortened", f"{shortened} long text(s) shortened", count=shortened))
    return Snapshot(
        conversation_id=conversation_id,
        kind=request.kind,
        ref_id=request.ref_id,
        label=built.label[:120] or request.ref_id,
        variant=request.variant,
        version=built.version,
        fingerprint=fingerprint(built.content),
        data=data,
        notes=notes,
        chars=len(json.dumps(data, ensure_ascii=False)),
        truncated=bool(shortened),
        secrets=secrets,
        paths=list(request.paths),
        include_content=request.include_content,
    )


def current_fingerprint(nodestro: Nodestro, snapshot: Snapshot) -> str | None:
    """The fingerprint of the object as it is now (saved state); ``None`` if it is gone."""
    try:
        built = _build(
            nodestro,
            AttachRequest(
                kind=snapshot.kind,
                ref_id=snapshot.ref_id,
                paths=snapshot.paths,
                include_content=snapshot.include_content,
            ),
        )
    except (NodestroError, KeyError):
        return None
    return fingerprint(built.content)


def render(snapshots: list[tuple[str, Snapshot]], *, limit: int = REQUEST_LIMIT) -> tuple[str, dict[str, bool]]:
    """The attached objects as one block for the model, within ``limit`` characters.

    ``snapshots`` are ``(attachment id, snapshot)`` pairs. Returns the text and, per
    attachment id, whether this request saw a shortened copy.
    """
    if not snapshots:
        return "", {}
    share = max(limit // len(snapshots), 4000)
    parts = []
    truncated: dict[str, bool] = {}
    for attachment_id, snapshot in snapshots:
        data, shortened = fit(snapshot.data, share) if snapshot.chars > share else (snapshot.data, 0)
        truncated[attachment_id] = snapshot.truncated or bool(shortened)
        header = {
            "attachment": attachment_id,
            "kind": snapshot.kind,
            "label": snapshot.label,
            "version": "unsaved editor draft" if snapshot.variant == "draft" else snapshot.version,
            "captured_at": snapshot.captured_at.isoformat(timespec="seconds"),
            "partial": truncated[attachment_id],
            "notes": [note.message for note in snapshot.notes]
            + ([f"{shortened} more text(s) shortened"] if shortened else []),
        }
        parts.append(
            f"<attached_object {json.dumps(header, ensure_ascii=False)}>\n"
            f"{json.dumps(data, ensure_ascii=False, indent=1, default=str)}\n</attached_object>"
        )
    return "\n\n".join(parts), truncated
