"""What a published release needs to run elsewhere: its models, local or in the cloud, and its network dependencies.

An export (:mod:`nodestro_api.exporter`, :mod:`nodestro_api.portable_build`) asks this module three things:

- :func:`model_uses`: every model the release's workflows use, with the role it plays (agent, fallback model, quality
  judge, orchestrator, embedding model of the knowledge search) and the nodes that use it. Sub-workflows and the
  teams of an orchestrator's pool are pinned in the bundle and count like the main workflow.
- :func:`local_models`: the local (Ollama) models among them with what this installation knows: installed or not,
  size, digest, the capabilities the model server reports, license and memory need (from the verified catalog).
- :func:`network_needs`: what would need a network connection at run time (cloud models, server-side tools such as
  web search, OpenAI-compatible endpoints, MCP services, AI-generated tools that only run in a Docker sandbox).

Nothing here changes a workflow or a setting, and nothing is downloaded.
"""

from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import TYPE_CHECKING, Literal

from nodestro_runtime.engine import node_label
from nodestro_runtime.local_models import catalog_model
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.providers import PROVIDERS, model_id, provider_of
from nodestro_runtime.release import ReleaseBundle
from nodestro_runtime.tool_spec import builtin_spec
from orkexa.local import is_cloud_model
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .context import Nodestro

logger = logging.getLogger(__name__)

Role = Literal["agent", "fallback", "judge", "orchestrator", "embedding"]
#: Licenses under which model weights may be passed on (with the license text). Anything else needs the person's
#: explicit confirmation before a model is bundled.
REDISTRIBUTABLE = ("Apache-2.0", "MIT")


class ModelUse(BaseModel):
    """One model of a release and where it is used."""

    #: ``provider:model`` (the embedding model of the knowledge search: ``ollama:<tag>``).
    model: str
    provider: str
    #: Runs on this computer's local model server (Ollama), without a cloud service.
    local: bool
    roles: list[Role] = Field(default_factory=list)
    used_by: list[str] = Field(default_factory=list)


class LocalModelInfo(BaseModel):
    """A local model of a release, as this installation knows it."""

    #: The Ollama tag, e.g. ``qwen3.5:4b``.
    name: str
    kind: Literal["chat", "embedding"] = "chat"
    roles: list[Role] = Field(default_factory=list)
    used_by: list[str] = Field(default_factory=list)
    installed: bool = False
    #: Bytes on disk (installed) or the catalog's download size (not installed); ``0``: unknown.
    size: int = 0
    #: The digest of the model's manifest (as ``ollama list`` shows it) when installed.
    digest: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    license: str | None = None
    #: ``permitted``: an open license that allows passing the weights on (with its text); ``unknown``: the person
    #: must confirm the right to pass the model on before it is bundled.
    redistribution: Literal["permitted", "unknown"] = "unknown"
    #: Memory the model needs with a moderate context (catalog estimate), in bytes.
    memory_bytes: int | None = None
    cpu_friendly: bool | None = None
    context: int | None = None
    #: In the verified catalog of Nodestro (digest, license and size checked against the registry).
    catalog: bool = False


class NetworkNeed(BaseModel):
    """Something the release needs a network connection or an outside service for."""

    kind: Literal[
        "cloud_model", "endpoint", "server_tool", "mcp", "docker_sandbox", "ollama_cloud", "builtin_tool", "connection"
    ]
    name: str
    detail: str = ""
    used_by: list[str] = Field(default_factory=list)


def _add(uses: dict[str, ModelUse], model: str, role: Role, label: str) -> None:
    if not model:
        return
    try:
        provider = provider_of(model)
    except NodestroError:
        return
    qualified = model if ":" in model and model.split(":", 1)[0] in PROVIDERS else f"{provider}:{model}"
    use = uses.get(qualified)
    if use is None:
        local = provider == "ollama" and not is_cloud_model(model_id(qualified))
        use = uses[qualified] = ModelUse(model=qualified, provider=provider, local=local)
    if role not in use.roles:
        use.roles.append(role)
    if label not in use.used_by:
        use.used_by.append(label)


def retrieval_modes(bundle: ReleaseBundle, workspace_mode: str = "fts") -> set[str]:
    """The knowledge search modes the release's agents use (``default`` resolved with the workspace setting)."""
    modes: set[str] = set()
    for workflow in bundle.all_workflows():
        for node in workflow.nodes:
            agent = node.agent
            if agent is None or agent.knowledge.mode != "search" or not agent.knowledge.file_ids:
                continue
            mode = agent.knowledge.retrieval
            modes.add(workspace_mode if mode == "default" else mode)
    return modes


def model_uses(
    bundle: ReleaseBundle, *, embedding_model: str | None = None, workspace_mode: str = "fts"
) -> list[ModelUse]:
    """Every model of the release: agents, fallbacks, judges, orchestrators, and the embedding model when an agent
    searches its knowledge semantically (``vector`` or ``hybrid``)."""
    uses: dict[str, ModelUse] = {}
    for workflow in bundle.all_workflows():
        for node in workflow.nodes:
            label = node_label(node)
            if workflow is not bundle.workflow:
                label = f"{workflow.name} / {label}"
            if node.agent is not None:
                _add(uses, node.agent.model, "agent", label)
                for fallback in node.agent.fallbacks:
                    _add(uses, fallback, "fallback", label)
                if node.agent.quality.enabled:
                    _add(uses, node.agent.quality.judge_model, "judge", label)
            if node.orchestrator is not None:
                _add(uses, node.orchestrator.model, "orchestrator", label)
    if embedding_model and retrieval_modes(bundle, workspace_mode) & {"vector", "hybrid"}:
        name = embedding_model if embedding_model.startswith("ollama:") else f"ollama:{embedding_model}"
        users = [
            node_label(node)
            for workflow in bundle.all_workflows()
            for node in workflow.nodes
            if node.agent is not None and node.agent.knowledge.mode == "search" and node.agent.knowledge.file_ids
        ]
        for label in users:
            _add(uses, name, "embedding", label)
    return sorted(uses.values(), key=lambda item: (not item.local, item.model))


def _installed(ctx: Nodestro) -> dict[str, tuple[int, str | None]]:
    """``{tag: (bytes, digest)}`` of the models in this installation's local model folder (no server needed)."""
    from orkexa.local import OllamaError, scan_store

    service = getattr(ctx, "local_models", None)
    if service is None:
        return {}
    folder = service.models_dir()
    if not (folder / "blobs").is_dir():  # no model downloaded yet (a fresh installation)
        return {}
    try:
        stored = scan_store(folder)
    except (OSError, ValueError, OllamaError) as exc:
        logger.warning("The local model folder cannot be read: %s", exc)
        return {}
    return {model.name: (model.size, model.digest) for model in stored}


def _capabilities(ctx: Nodestro, name: str) -> list[str]:
    service = getattr(ctx, "local_models", None)
    if service is None:
        return []
    try:
        overrides = service.capability_overrides(f"ollama:{name}")
    except Exception:  # an unreachable server reports nothing; the catalog is the fallback below
        overrides = {}
    return sorted(key for key, value in overrides.items() if value == "supported")


def local_models(ctx: Nodestro, uses: Iterable[ModelUse]) -> list[LocalModelInfo]:
    """The local models of ``uses`` with installation state, size, license and hardware need."""
    installed = _installed(ctx)
    found: list[LocalModelInfo] = []
    for use in uses:
        if not use.local:
            continue
        name = model_id(use.model)
        entry = catalog_model(name)
        size, digest = installed.get(name, (0, None))
        license_text = entry.license if entry is not None else None
        permitted = license_text is not None and license_text.split(" ", 1)[0] in REDISTRIBUTABLE
        capabilities = _capabilities(ctx, name)
        if not capabilities and entry is not None:
            capabilities = sorted(entry.listed_capabilities)
        found.append(
            LocalModelInfo(
                name=name,
                kind="embedding"
                if "embedding" in use.roles or (entry is not None and entry.kind == "embedding")
                else "chat",
                roles=use.roles,
                used_by=use.used_by,
                installed=name in installed,
                size=size or (entry.download_bytes if entry is not None else 0),
                digest=digest,
                capabilities=capabilities,
                license=license_text,
                redistribution="permitted" if permitted else "unknown",
                memory_bytes=entry.memory_bytes if entry is not None else None,
                cpu_friendly=entry.cpu_friendly if entry is not None else None,
                context=entry.context if entry is not None else None,
                catalog=entry is not None,
            )
        )
    return found


def network_needs(bundle: ReleaseBundle, uses: Iterable[ModelUse]) -> list[NetworkNeed]:
    """What the release would need a network (or a service outside the package) for at run time."""
    needs: list[NetworkNeed] = []
    for use in uses:
        if use.local:
            continue
        info = PROVIDERS.get(use.provider)
        if use.provider == "ollama":
            needs.append(NetworkNeed(kind="ollama_cloud", name=use.model, used_by=use.used_by))
        elif info is not None and info.auth == "endpoint":
            needs.append(NetworkNeed(kind="endpoint", name=use.model, detail=info.label, used_by=use.used_by))
        else:
            needs.append(
                NetworkNeed(
                    kind="cloud_model", name=use.model, detail=info.label if info else use.provider, used_by=use.used_by
                )
            )
    for tool in bundle.server_tools:
        needs.append(NetworkNeed(kind="server_tool", name=tool, detail="Anthropic"))
    for name in sorted(bundle.mcp_tools):
        ref = bundle.mcp_tools[name]
        needs.append(NetworkNeed(kind="mcp", name=name, detail=ref.server_slug))
    for name in bundle.builtin_tools:
        spec = builtin_spec(name)
        if spec is not None and spec.network == "external":
            needs.append(NetworkNeed(kind="builtin_tool", name=name, detail=spec.connection_type or ""))
    for connection_id, wanted in sorted(bundle.required_connections.items()):
        needs.append(NetworkNeed(kind="connection", name=connection_id, detail=wanted))
    untrusted = sorted(name for name in bundle.tools if bundle.tool_trust.get(name) != "user-authored")
    for name in untrusted:
        needs.append(NetworkNeed(kind="docker_sandbox", name=name))
    return needs


__all__ = [
    "REDISTRIBUTABLE",
    "LocalModelInfo",
    "ModelUse",
    "NetworkNeed",
    "local_models",
    "model_uses",
    "network_needs",
    "retrieval_modes",
]
