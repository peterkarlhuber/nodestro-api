"""The studio's API for published workflows (the *API & Export* dialog).

``/api/workflows/{id}/api``   overview, check and publishing of a saved version
``/api/publications/...``    settings, switching on/off, versions, docs, tests, runs, exports, guide
``/api/publication-overview`` every publication with its workflow, active release and exports (read only)
``/api/api-keys``            API keys (hashed; the complete key is shown once)
``/api/api-network``         the optional network listener
``/api/exports/{id}``        downloading a generated project

The published endpoints themselves are :mod:`nodestro_api.serving` (``/api/published/...``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse
from nodestro_runtime.engine import start_fields
from nodestro_runtime.forms import check_values, input_text
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import LANGUAGES, RunSummary, Workflow
from pydantic import BaseModel, Field

from . import access_audit, api_guide, exporter, portable_build
from .api import Ctx, _quote
from .api_examples import ApiExamples, examples
from .api_guide import ApiGuide
from .api_keys import ApiKeyOut, NewApiKey
from .network import NetworkConfig, NetworkStatus
from .permissions import ADMIN, EDIT, INSTALL, READ, RUN
from .publishing import RELEASES, ApiCheck, Publication, Release, ReleaseSummary, api_runs
from .release_gate import GateOverride, GateReport, ReleaseGate
from .serving import SCOPES, ApiLimits, KeyIdentity, ServedRelease, openapi_document

router = APIRouter(prefix="/api")


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _workflow(ctx: Ctx, workflow_id: str) -> Workflow:
    workflow = ctx.store.get("workflows", workflow_id, Workflow)
    if workflow is None:
        raise KeyError(workflow_id)
    return workflow


def _served(ctx: Ctx, publication: Publication, number: int | None) -> tuple[Release, ServedRelease]:
    wanted = number or publication.active_release
    if wanted is None:
        raise NodestroError("api.not_published", "No version of this workflow is published yet")
    release = ctx.publications.release(publication.id, wanted)
    return release, ctx.publications.served(publication, release)


# ---- overview, check, publish ----------------------------------------------------------


class ApiOverview(BaseModel):
    workflow_id: str
    #: The saved version the editor shows (what "publish" publishes by default).
    current_version: int | None
    publication: Publication | None = None
    releases: list[ReleaseSummary] = Field(default_factory=list)
    #: The API's address in this studio and, when switched on, in the network.
    local_url: str | None = None
    network_urls: list[str] = Field(default_factory=list)
    network: NetworkStatus
    keys: int = 0
    runs: list[RunSummary] = Field(default_factory=list)
    pending_approvals: int = 0
    missing_resources: list[str] = Field(default_factory=list)


@router.get("/workflows/{workflow_id}/api", dependencies=READ)
def api_overview(workflow_id: str, request: Request, ctx: Ctx) -> ApiOverview:
    workflow = _workflow(ctx, workflow_id)
    publication = ctx.publications.for_workflow(workflow_id)
    network = ctx.network.status()
    overview = ApiOverview(workflow_id=workflow_id, current_version=ctx.current_version(workflow), network=network)
    if publication is None:
        return overview
    overview.publication = publication
    overview.releases = ctx.publications.summaries(publication)
    path = f"/api/published/{publication.slug}"
    overview.local_url = _base_url(request) + path
    if network.running and publication.network:
        overview.network_urls = [url + path for url in network.urls]
    overview.keys = sum(publication.id in key.publications and key.revoked_at is None for key in ctx.api_keys.all())
    runs = api_runs(ctx, publication.id, limit=None)
    overview.runs = runs[:20]
    overview.pending_approvals = sum(run.status == "paused" for run in runs)
    if publication.active_release is not None:
        _, served = _served(ctx, publication, None)
        overview.missing_resources = served.missing_resources
    return overview


class CheckIn(BaseModel):
    #: A saved version of the workflow; ``None``: the version the editor shows.
    version: int | None = None


@router.post("/workflows/{workflow_id}/api/check", dependencies=READ)
def api_check(workflow_id: str, body: CheckIn, ctx: Ctx) -> ApiCheck:
    return ctx.publications.check(workflow_id, body.version)


class PublishIn(BaseModel):
    version: int | None = None
    note: str = Field(default="", max_length=200)
    slug: str | None = Field(default=None, max_length=48)
    #: Make the new version the one ``/runs`` uses (otherwise only ``/versions/{n}/runs`` reaches it).
    activate: bool = True
    #: Switch the release gate on (or change it) with this release; ``None``: keep the publication's gate.
    gate: ReleaseGate | None = None
    #: The written reason for releasing although non-critical gate items failed (never for critical ones).
    override: GateOverride | None = None


class PublishOut(BaseModel):
    publication: Publication
    release: ReleaseSummary


class GateCheckIn(BaseModel):
    #: A saved version of the workflow; ``None``: the version the editor shows.
    version: int | None = None
    #: A gate to try before switching it on; ``None``: the publication's gate.
    gate: ReleaseGate | None = None


@router.post("/workflows/{workflow_id}/api/gate", dependencies=READ)
def gate_report(workflow_id: str, body: GateCheckIn, ctx: Ctx) -> GateReport:
    """What the release gate says about a saved version (``status="off"`` without a gate)."""
    _workflow(ctx, workflow_id)
    return ctx.publications.gate_report(workflow_id, body.version, body.gate)


@router.post("/workflows/{workflow_id}/api/publish", status_code=201, dependencies=ADMIN)
def publish(workflow_id: str, body: PublishIn, request: Request, ctx: Ctx) -> PublishOut:
    principal = getattr(request.state, "principal", None)
    publication, release = ctx.publications.publish(
        workflow_id,
        body.version,
        note=body.note,
        slug=body.slug,
        activate=body.activate,
        gate=body.gate,
        override=body.override,
        principal_id=principal.id if principal is not None else "local",
    )
    summary = next(item for item in ctx.publications.summaries(publication) if item.id == release.id)
    return PublishOut(publication=publication, release=summary)


# ---- one publication ---------------------------------------------------------------------


class PublicationListItem(BaseModel):
    id: str
    slug: str
    name: str
    workflow_id: str
    enabled: bool
    network: bool
    active_release: int | None


@router.get("/publications", dependencies=READ)
def list_publications(ctx: Ctx) -> list[PublicationListItem]:
    return [
        PublicationListItem(**item.model_dump(include=set(PublicationListItem.model_fields)))
        for item in sorted(ctx.publications.all(), key=lambda item: item.name.lower())
    ]


class PublicationOverviewItem(PublicationListItem):
    """One publication on the studio's *Publications* page: the stored publication, its workflow, the active
    release and the export history. Read only; everything comes from the stored publications, releases and
    export records (nothing is stored for the page)."""

    #: The workflow's current name (``None``: the workflow no longer exists).
    workflow_name: str | None = None
    created_at: datetime
    updated_at: datetime
    #: The saved workflow version of the active release.
    active_version: int | None = None
    #: When the active release was published.
    published_at: datetime | None = None
    #: The publication's exports, newest first (as ``GET /api/publications/{id}/exports``).
    exports: list[exporter.ExportRecord] = Field(default_factory=list)
    #: Portable export jobs still being built, newest first (as ``GET /api/publications/{id}/export-jobs``).
    export_jobs: list[portable_build.ExportJob] = Field(default_factory=list)


class _ReleaseHead(BaseModel):
    """The two fields of a stored release the overview shows; its bundle is not validated (API-063)."""

    workflow_version: int | None = None
    created_at: datetime


@router.get("/publication-overview", dependencies=READ)
def publication_overview(ctx: Ctx) -> list[PublicationOverviewItem]:
    """All publications of this installation (on a server: of the tenant) with their workflow, active release and
    exports, for the studio's *Publications* page (one request instead of one per publication)."""
    exports = exporter.records_by_publication(ctx)
    jobs: dict[str, list[portable_build.ExportJob]] = {}
    for job in sorted(ctx.store.list(portable_build.JOBS, portable_build.ExportJob), key=lambda job: job.created_at):
        if job.state == "running":
            jobs.setdefault(job.publication_id, []).insert(0, job)
    found = []
    for publication in sorted(ctx.publications.all(), key=lambda item: item.name.lower()):
        workflow = ctx.store.get("workflows", publication.workflow_id, Workflow)
        release: _ReleaseHead | None = None
        if publication.active_release is not None:
            release = ctx.store.get(RELEASES, f"{publication.id}:{publication.active_release:06d}", _ReleaseHead)
        found.append(
            PublicationOverviewItem(
                **publication.model_dump(include=set(PublicationListItem.model_fields)),
                workflow_name=workflow.name if workflow is not None else None,
                created_at=publication.created_at,
                updated_at=publication.updated_at,
                active_version=release.workflow_version if release is not None else None,
                published_at=release.created_at if release is not None else None,
                exports=exports.get(publication.id, []),
                export_jobs=jobs.get(publication.id, []),
            )
        )
    return found


class PublicationPatch(BaseModel):
    slug: str | None = Field(default=None, max_length=48)
    description: str | None = Field(default=None, max_length=2000)
    limits: ApiLimits | None = None
    network: bool | None = None
    #: Offer the API as a tool of Nodestro's MCP server (WP-11; also needs the operator's NODESTRO_MCP_SERVER=1).
    mcp_tool: bool | None = None


@router.patch("/publications/{publication_id}", dependencies=ADMIN)
def update_publication(publication_id: str, body: PublicationPatch, ctx: Ctx) -> Publication:
    return ctx.publications.update(
        publication_id,
        slug=body.slug,
        description=body.description,
        limits=body.limits,
        network=body.network,
        mcp_tool=body.mcp_tool,
    )


class EnableIn(BaseModel):
    enabled: bool
    #: When switching off: also cancel runs that are running or wait for an approval.
    cancel_running: bool = False


class EnableOut(BaseModel):
    publication: Publication
    cancelled: int


class GateIn(BaseModel):
    #: ``None`` switches the release gate off.
    gate: ReleaseGate | None = None


@router.put("/publications/{publication_id}/gate", dependencies=ADMIN)
def set_gate(publication_id: str, body: GateIn, ctx: Ctx) -> Publication:
    """Switch the release gate of a publication on, change it, or switch it off (recorded in the history)."""
    return ctx.publications.set_gate(publication_id, body.gate)


@router.post("/publications/{publication_id}/enabled", dependencies=ADMIN)
def set_enabled(publication_id: str, body: EnableIn, ctx: Ctx) -> EnableOut:
    publication, cancelled = ctx.publications.set_enabled(
        publication_id, body.enabled, cancel_running=body.cancel_running
    )
    return EnableOut(publication=publication, cancelled=cancelled)


class ActivateIn(BaseModel):
    number: int


@router.post("/publications/{publication_id}/activate", dependencies=ADMIN)
def activate(publication_id: str, body: ActivateIn, ctx: Ctx) -> Publication:
    """Make an earlier (or later) published version the active one: rollback without republishing."""
    return ctx.publications.activate(publication_id, body.number)


@router.delete("/publications/{publication_id}", status_code=204, dependencies=ADMIN)
def delete_publication(publication_id: str, ctx: Ctx) -> Response:
    publication = ctx.publications.get(publication_id)
    live = [run for run in api_runs(ctx, publication.id, limit=None) if run.status in ("queued", "running", "paused")]
    if live:
        raise NodestroError(
            "api.runs_active",
            f"{len(live)} runs of this API have not finished; switch it off and cancel them first",
            count=len(live),
        )
    ctx.publications.delete(publication.id)
    ctx.api_keys.forget_publication(publication.id)
    return Response(status_code=204)


@router.get("/publications/{publication_id}/releases/{number}", dependencies=READ)
def get_release(publication_id: str, number: int, ctx: Ctx) -> Release:
    """The complete, immutable snapshot of a published version."""
    return ctx.publications.release(publication_id, number)


class ApiDocs(BaseModel):
    number: int
    openapi: dict[str, Any]
    examples: ApiExamples
    scopes: list[str]


@router.get("/publications/{publication_id}/docs", dependencies=READ)
def api_docs(publication_id: str, request: Request, ctx: Ctx, number: int | None = None) -> ApiDocs:
    publication = ctx.publications.get(publication_id)
    release, served = _served(ctx, publication, number)
    base = _base_url(request)
    return ApiDocs(
        number=release.number,
        openapi=openapi_document(ctx.serving, served, servers=[base]),
        examples=examples(served, base),
        scopes=list(SCOPES),
    )


# ---- testing inside the studio ------------------------------------------------------------


def _studio_identity(publication: Publication) -> KeyIdentity:
    """Who a test from the studio calls as: Nodestro itself, for this one publication."""
    return KeyIdentity(id="studio", name="Nodestro", scopes=list(SCOPES), publications=[publication.id])


class TestIn(BaseModel):
    #: The request body exactly as an API client would send it.
    body: dict[str, Any] = Field(default_factory=dict)
    #: A published version; ``None``: the active one.
    number: int | None = None
    #: Only validate the body (no run, no cost).
    dry_run: bool = False


class ValidationOut(BaseModel):
    ok: bool
    #: What the run would receive (values after defaults; files by name).
    request: dict[str, Any] = Field(default_factory=dict)
    #: The input text the first agents receive.
    input_text: str = ""


@router.post("/publications/{publication_id}/test", dependencies=RUN)
async def test_api(publication_id: str, body: TestIn, ctx: Ctx) -> Any:
    """Validate a request body like the API does, or start a real run through the same path.

    The run is an ordinary API run (it counts towards the limits and shows up in the history),
    started as "Nodestro" instead of with a key. A switched-off API refuses it like any client.
    """
    publication = ctx.publications.get(publication_id)
    _, served = _served(ctx, publication, body.number)
    parsed = ctx.serving.parse_body(served, body.body)
    if body.dry_run:
        data = parsed.model_dump(mode="json")
        for item in data.get("files") or []:
            item.pop("content_base64", None)
        fields = start_fields(served.workflow)
        values = data.get("values") or {}
        files = {field.name for field in fields if field.type == "file"}
        typed, _ = check_values([field for field in fields if field.name not in files], values)
        for key in files & set(values):
            items = values[key] if isinstance(values[key], list) else [values[key]]
            typed[key] = [item.get("name") for item in items if isinstance(item, dict)]
        data["values"] = typed
        text = input_text(fields, typed, str(data.get("input") or ""), {})
        return ValidationOut(ok=True, request=data, input_text=text)
    return await ctx.serving.launch(_studio_identity(publication), served, body.body)


@router.get("/publications/{publication_id}/runs", dependencies=READ)
def publication_runs(publication_id: str, ctx: Ctx, limit: Annotated[int, Query(ge=1)] = 50) -> list[RunSummary]:
    ctx.publications.get(publication_id)
    return api_runs(ctx, publication_id, limit=min(max(limit, 1), 500))


# ---- export --------------------------------------------------------------------------------


class ExportIn(BaseModel):
    number: int | None = None
    options: exporter.ExportOptions = Field(default_factory=exporter.ExportOptions)


@router.post("/publications/{publication_id}/export/check", dependencies=READ)
def export_check(publication_id: str, body: ExportIn, ctx: Ctx) -> exporter.ExportCheck:
    publication = ctx.publications.get(publication_id)
    release, _ = _served(ctx, publication, body.number)
    return exporter.check(ctx, publication, release, body.options)


@router.post("/publications/{publication_id}/exports", status_code=201, dependencies=EDIT)
def create_export(publication_id: str, body: ExportIn, ctx: Ctx) -> exporter.ExportRecord:
    publication = ctx.publications.get(publication_id)
    release, _ = _served(ctx, publication, body.number)
    return exporter.export(ctx, publication, release, body.options)


@router.get("/publications/{publication_id}/exports", dependencies=READ)
def list_exports(publication_id: str, ctx: Ctx) -> list[exporter.ExportRecord]:
    ctx.publications.get(publication_id)
    return exporter.records(ctx, publication_id)


@router.get("/exports/{export_id}/download", dependencies=READ)
def download_export(export_id: str, ctx: Ctx) -> Response:
    headers = {"Content-Disposition": "attachment; filename*=UTF-8''{name}"}
    record, path = exporter.content_path(ctx, export_id)
    headers["Content-Disposition"] = headers["Content-Disposition"].format(name=_quote(record.file_name))
    if record.size <= 256 * 1024 * 1024:
        # Small exports: the SHA-256 is checked on every download (a changed file is refused).
        record, data = exporter.content(ctx, export_id)
        return Response(data, media_type="application/zip", headers=headers)
    # Portable packages with models are streamed from disk (their size was checked).
    return FileResponse(path, media_type="application/zip", headers=headers)


# ---- portable packages (background jobs) ---------------------------------------------------


@router.post("/publications/{publication_id}/export-jobs", status_code=202, dependencies=EDIT)
def start_export_job(publication_id: str, body: ExportIn, ctx: Ctx) -> portable_build.ExportJob:
    """Build a portable package in the background (``options.kind`` must be ``portable``).

    On a server this answers ``403 export.server_unsupported`` unless its operator enabled export jobs
    (``NODESTRO_SERVER_EXPORT_JOBS=1``); then one package is built at a time on the whole server
    (``export.busy`` otherwise) and a package larger than ``NODESTRO_SERVER_EXPORT_MAX_BYTES`` (default 2 GiB) is
    refused (``403 export.server_too_large`` from the estimate, or the job fails with that code)."""
    publication = ctx.publications.get(publication_id)
    release, _ = _served(ctx, publication, body.number)
    options = body.options
    if options.kind != "portable":
        raise NodestroError("export.not_portable", "Only portable packages are built as jobs")
    found = exporter.check(ctx, publication, release, options)
    # On a server only with the operator's opt-in, one build at a time and a size limit (API3-07); its refusal
    # answers 403 before the check's own copy of it (API-053) would answer 400.
    portable_build.server_admission(ctx, found.portable.estimated_bytes if found.portable else 0)
    if not found.ok:
        first = next(item for item in found.problems if item.level == "error")
        raise NodestroError(first.code, first.message, **first.params)
    return portable_build.runner(ctx).start(publication, release, options)


@router.get("/publications/{publication_id}/export-jobs", dependencies=READ)
def list_export_jobs(publication_id: str, ctx: Ctx) -> list[portable_build.ExportJob]:
    ctx.publications.get(publication_id)
    return portable_build.runner(ctx).for_publication(publication_id)


@router.get("/export-jobs/{job_id}", dependencies=READ)
def get_export_job(job_id: str, ctx: Ctx) -> portable_build.ExportJob:
    return portable_build.runner(ctx).get(job_id)


@router.post("/export-jobs/{job_id}/cancel", dependencies=EDIT)
def cancel_export_job(job_id: str, ctx: Ctx) -> portable_build.ExportJob:
    return portable_build.runner(ctx).cancel(job_id)


# ---- the optional AI guide ---------------------------------------------------------------


class GuideIn(BaseModel):
    number: int | None = None
    language: str = "en"


@router.post("/publications/{publication_id}/guide", status_code=201, dependencies=RUN)
def create_guide(publication_id: str, body: GuideIn, request: Request, ctx: Ctx) -> ApiGuide:
    """Costs one request of the assistant model (recorded in the usage ledger); only on request."""
    if body.language not in LANGUAGES:
        raise ValueError(f"Unknown language: {body.language}")
    publication = ctx.publications.get(publication_id)
    release, served = _served(ctx, publication, body.number)
    return api_guide.generate(ctx, publication, release, examples(served, _base_url(request)), body.language)


@router.get("/publications/{publication_id}/guides", dependencies=READ)
def list_guides(publication_id: str, ctx: Ctx) -> list[ApiGuide]:
    ctx.publications.get(publication_id)  # an unknown publication is 404, not an empty list (API2-07)
    return api_guide.for_publication(ctx, publication_id)


class GuideDecision(BaseModel):
    decision: Literal["accept", "discard"]


@router.post("/api-guides/{guide_id}/decision", dependencies=EDIT)
def decide_guide(guide_id: str, body: GuideDecision, ctx: Ctx) -> ApiGuide | None:
    return api_guide.decide(ctx, guide_id, body.decision)


# ---- keys -----------------------------------------------------------------------------------


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[Literal["run", "read", "approve"]] = Field(min_length=1)
    publications: list[str] = Field(min_length=1)


@router.get("/api-keys", dependencies=ADMIN)
def list_keys(ctx: Ctx) -> list[ApiKeyOut]:
    return ctx.api_keys.all()


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.id if principal is not None else "local"


def _key_event(ctx: Ctx, request: Request, kind: access_audit.AccessKind, key: ApiKeyOut, **details: Any) -> None:
    """API-004: who created, revoked, rotated or deleted which key (its id, name and prefix; never the key)."""
    access_audit.record(
        ctx.store,
        access_audit.AccessEvent(
            kind=kind,
            actor=_actor(request),
            target_type="api_key",
            target_id=key.id,
            details={"name": key.name, "prefix": key.prefix, "scopes": list(key.scopes), **details},
        ),
    )


@router.post("/api-keys", status_code=201, dependencies=ADMIN)
def create_key(body: KeyIn, request: Request, ctx: Ctx) -> NewApiKey:
    created = ctx.api_keys.create(body.name, list(body.scopes), body.publications)
    _key_event(ctx, request, "api_key_created", created.key)
    return created


@router.post("/api-keys/{key_id}/revoke", dependencies=ADMIN)
def revoke_key(key_id: str, request: Request, ctx: Ctx) -> ApiKeyOut:
    revoked = ctx.api_keys.revoke(key_id)
    _key_event(ctx, request, "api_key_revoked", revoked)
    return revoked


@router.post("/api-keys/{key_id}/rotate", dependencies=ADMIN)
def rotate_key(key_id: str, request: Request, ctx: Ctx) -> NewApiKey:
    """A new key with the same name, permissions and APIs; the old one is revoked at once."""
    rotated = ctx.api_keys.rotate(key_id)
    _key_event(ctx, request, "api_key_rotated", rotated.key, replaces=key_id)
    return rotated


@router.delete("/api-keys/{key_id}", status_code=204, dependencies=ADMIN)
def delete_key(key_id: str, request: Request, ctx: Ctx) -> Response:
    found = next((key for key in ctx.api_keys.all() if key.id == key_id), None)
    ctx.api_keys.delete(key_id)
    if found is not None:
        _key_event(ctx, request, "api_key_deleted", found)
    return Response(status_code=204)


@router.get("/access-audit", dependencies=ADMIN)
def access_audit_events(
    ctx: Ctx,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    kind: access_audit.AccessKind | None = None,
) -> list[access_audit.AccessEvent]:
    """The access audit trail of this workspace (on a server: of the tenant), newest first (API-004): logins and
    refused logins, tenant switches and logouts, API key changes, and the operator's tenant and member commands.
    Read only; it never holds a secret."""
    return access_audit.events(ctx.store, limit=limit, kind=kind)


# ---- network -----------------------------------------------------------------------------


@router.get("/api-network", dependencies=INSTALL)
def network_status(ctx: Ctx) -> NetworkStatus:
    return ctx.network.status()


@router.put("/api-network", dependencies=INSTALL)
async def configure_network(body: NetworkConfig, ctx: Ctx) -> NetworkStatus:
    return await ctx.network.apply(body)
