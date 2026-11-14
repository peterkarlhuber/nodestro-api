"""The skill library (NAL-B3): skills in the Agent Skills format with immutable versions, import, export and the
shipped skills.

A skill is a package (``SKILL.md`` plus ``references/``, ``assets/``, ``scripts/``). Reading and checking it is
:mod:`orkexa.skills`; storing it is :mod:`nodestro_runtime.skills`. An import is untrusted content: the ZIP limits of
Orkexa apply (10 MB, 30 MB unpacked, 500 files, no path tricks, links or ZIP bombs), the specification's errors refuse
it, and warnings (unknown keys, ``allowed-tools``, scripts, suspected credentials) are shown before it is saved
(``dry_run``). A skill never grants tools or permissions, and its scripts are never run.

Permissions: ``read`` lists, reads, checks, exports and reads the shipped skills; ``edit`` creates, imports, changes,
copies, activates and disables; ``admin`` archives and deletes. A skill an agent uses cannot be deleted (archive it).
"""

from __future__ import annotations

import base64
import binascii
from datetime import timedelta
from functools import partial
import hashlib
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from nodestro_examples import __version__ as EXAMPLES_VERSION
from nodestro_examples import skills as skill_library
from nodestro_runtime.problems import NodestroError
from nodestro_runtime.schemas import AgentTemplate, Run, Skill, SkillGroup, SkillVersion, Workflow, now
from nodestro_runtime.skill_stats import SkillStats, skill_stats
from nodestro_runtime.skill_tools import script_origin, script_requirement
from nodestro_runtime.skills import Skills, check
from nodestro_runtime.tool_library import ToolJob
from orkexa import UsageTracker
from orkexa.skills import (
    SKILL_FILE,
    SkillIssue,
    SkillLimits,
    SkillPackage,
    SkillPackageError,
    is_text_path,
    parse_skill,
    read_zip,
    write_zip,
)
from pydantic import BaseModel, Field

from . import model_readiness
from .api import Ctx, request_language
from .permissions import ADMIN, EDIT, READ, RUN
from .serving import ApiError

router = APIRouter(prefix="/api")
LIMITS = SkillLimits()
#: The largest file text the API returns for reading in the studio.
MAX_FILE_TEXT = 500_000
_PACKAGE = "nodestro-examples"


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return str(getattr(principal, "id", "") or "")


def _skills(ctx: Ctx) -> Skills:
    return Skills(ctx.store)


def _get(ctx: Ctx, skill_id: str) -> Skill:
    found = _skills(ctx).get(skill_id)
    if found is None:
        raise KeyError(skill_id)
    return found


def _package_error(error: SkillPackageError) -> ApiError:
    status = 413 if error.code in ("skill.too_large", "skill.too_many_files", "skill.zip_bomb") else 422
    # Parameters can carry text of the upload (a rejected path): only a short, bounded part is returned.
    params: dict[str, Any] = {
        key: value[:200] if isinstance(value, str) else value
        for key, value in error.params.items()
        if isinstance(value, str | int)
    }
    return ApiError(status, error.code, str(error), **params)


# ---- models -----------------------------------------------------------------------------------------------------


class SkillFileInfo(BaseModel):
    path: str
    size: int
    #: Text a model may read (``references/*.md`` etc.); others are shown as files only.
    text: bool
    script: bool


class SkillDetail(BaseModel):
    """A skill with one of its versions: the front matter, the instructions and the file list, and the findings."""

    skill: Skill
    version: SkillVersion
    body: str
    files: list[SkillFileInfo]
    issues: list[SkillIssue] = Field(default_factory=list)


def _files(package: SkillPackage) -> list[SkillFileInfo]:
    return [
        SkillFileInfo(
            path=item.path,
            size=item.size,
            text=item.path == SKILL_FILE or is_text_path(item.path),
            script=item.path.startswith("scripts/"),
        )
        for item in package.files
    ]


def _detail(ctx: Ctx, skill: Skill, number: int) -> SkillDetail:
    skills = _skills(ctx)
    package = skills.package(skill.id, number)
    return SkillDetail(
        skill=skill,
        version=skills.version(skill.id, number),
        body=package.body,
        files=_files(package),
        issues=check(package),
    )


class SkillCheck(BaseModel):
    """The result of reading a package without storing it: what the import would store and its findings."""

    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: str | None = None
    body: str
    files: list[SkillFileInfo]
    issues: list[SkillIssue]
    #: A skill of this workspace with the same name (an import would need another name or a new version of it).
    existing: Skill | None = None


def _checked(ctx: Ctx, package: SkillPackage) -> SkillCheck:
    return SkillCheck(
        name=package.name,
        description=package.description.strip(),
        license=package.license,
        compatibility=package.compatibility,
        metadata=package.metadata,
        allowed_tools=package.allowed_tools,
        body=package.body,
        files=_files(package),
        issues=check(package),
        existing=_skills(ctx).by_name(package.name) if package.name else None,
    )


class SkillImportResult(BaseModel):
    skill: Skill | None = None
    check: SkillCheck


# ---- reading ----------------------------------------------------------------------------------------------------


@router.get("/skills", dependencies=READ)
def list_skills(ctx: Ctx) -> list[Skill]:
    """Every skill in its latest version, by name."""
    return _skills(ctx).all()


@router.get("/skills/{skill_id}", dependencies=READ)
def get_skill(skill_id: str, ctx: Ctx, version: int | None = None) -> SkillDetail:
    """A skill in its latest version (or ``version``) with instructions, files and findings."""
    skill = _get(ctx, skill_id)
    return _detail(ctx, skill, version or skill.version)


@router.get("/skills/{skill_id}/versions", dependencies=READ)
def list_skill_versions(skill_id: str, ctx: Ctx) -> list[SkillVersion]:
    """Every version, newest first."""
    _get(ctx, skill_id)
    return list(reversed(_skills(ctx).versions(skill_id)))


class SkillFileText(BaseModel):
    path: str
    size: int
    text: str
    truncated: bool = False


@router.get("/skills/{skill_id}/versions/{version}/file", dependencies=READ)
def read_skill_file(skill_id: str, version: int, path: str, ctx: Ctx) -> SkillFileText:
    """One text file of a version, for reading in the studio (binary files are listed, not shown)."""
    _get(ctx, skill_id)
    found = _skills(ctx).package(skill_id, version).file(path)
    if found is None:
        raise ApiError(404, "skill.file_not_found", f"The skill has no file {path}", path=path[:200])
    if found.path != SKILL_FILE and not is_text_path(found.path):
        raise ApiError(422, "skill.file_not_text", f"{found.path} is not a text file", path=found.path)
    data = found.data[:MAX_FILE_TEXT]
    return SkillFileText(
        path=found.path,
        size=found.size,
        text=data.decode("utf-8", errors="replace"),
        truncated=found.size > MAX_FILE_TEXT,
    )


class SkillUser(BaseModel):
    kind: str
    id: str
    name: str
    #: The agent (node label or template name) that has the skill.
    agent: str
    version: int


@router.get("/skills/{skill_id}/usage", dependencies=READ)
def skill_usage(skill_id: str, ctx: Ctx) -> list[SkillUser]:
    """The workflows and agent templates whose agents have this skill (any version)."""
    _get(ctx, skill_id)
    return _users(ctx, skill_id)


def _users(ctx: Ctx, skill_id: str) -> list[SkillUser]:
    found: list[SkillUser] = []
    for workflow in ctx.store.list("workflows", Workflow):
        for node in workflow.nodes:
            for binding in node.agent.skills if node.agent is not None else []:
                if binding.skill_id == skill_id:
                    found.append(
                        SkillUser(
                            kind="workflow",
                            id=workflow.id,
                            name=workflow.name,
                            agent=node.label or node.id,
                            version=binding.version,
                        )
                    )
    for template in ctx.store.list("templates", AgentTemplate):
        for binding in template.agent.skills:
            if binding.skill_id == skill_id:
                found.append(
                    SkillUser(
                        kind="template",
                        id=template.id,
                        name=template.agent.name,
                        agent=template.agent.name,
                        version=binding.version,
                    )
                )
    return found


@router.get("/skills/{skill_id}/export", dependencies=READ)
def export_skill(skill_id: str, ctx: Ctx, version: int | None = None) -> Response:
    """A version as a standard Agent Skills ZIP (a folder named after the skill, the files' exact bytes)."""
    skill = _get(ctx, skill_id)
    package = _skills(ctx).package(skill_id, version or skill.version)
    return Response(
        content=write_zip(package),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{package.name}.zip"'},
    )


# ---- import and editing -----------------------------------------------------------------------------------------


async def _read_upload(file: UploadFile) -> tuple[SkillPackage, str]:
    """The package of an uploaded ZIP or ``SKILL.md`` and the SHA-256 of the upload."""
    data = await file.read(LIMITS.max_zip_bytes + 1)
    if len(data) > LIMITS.max_zip_bytes:
        raise ApiError(413, "skill.too_large", "A skill file may have at most 10 MB", limit=LIMITS.max_zip_bytes)
    digest = hashlib.sha256(data).hexdigest()
    name = (file.filename or "").lower()

    def parse() -> SkillPackage:
        if data[:4] == b"PK\x03\x04" or name.endswith(".zip"):
            return read_zip(data, limits=LIMITS)
        return parse_skill({SKILL_FILE: data}, limits=LIMITS)

    try:
        # Unpacking and parsing take CPU time: off the event loop, so one import never stalls the server (L1).
        return await run_in_threadpool(parse), digest
    except SkillPackageError as error:
        raise _package_error(error) from None


@router.post("/skills/import", dependencies=EDIT)
async def import_skill(
    request: Request,
    ctx: Ctx,
    file: Annotated[UploadFile, File()],
    dry_run: Annotated[bool, Form()] = False,
    skill_id: Annotated[str | None, Form()] = None,
) -> SkillImportResult:
    """Read a ZIP or a ``SKILL.md``. With ``dry_run`` nothing is stored: the answer is the validation report and the
    preview. Otherwise the package becomes a new skill (origin ``import:<sha256>``) or, with ``skill_id``, the next
    version of that skill. Errors refuse it (``skill.invalid``); warnings do not."""
    package, digest = await _read_upload(file)
    checked = await run_in_threadpool(_checked, ctx, package)
    if dry_run:
        return SkillImportResult(check=checked)
    skills = _skills(ctx)
    origin = f"import:{digest}"
    if skill_id:
        _get(ctx, skill_id)
        # Imported content marks the skill as imported, whatever its origin was (internal review M2).
        save = partial(skills.save, skill_id, package, origin=origin, note="import", created_by=_actor(request))
    else:
        save = partial(skills.create, package, origin=origin, note="import", created_by=_actor(request))
    return SkillImportResult(skill=await run_in_threadpool(save), check=checked)


class SkillFileIn(BaseModel):
    """One file of an edited package: text, base64 bytes, or ``keep`` (the bytes of the skill's latest version)."""

    path: str = Field(min_length=1, max_length=300)
    text: str | None = None
    base64: str | None = None
    keep: bool = False


class SkillIn(BaseModel):
    files: list[SkillFileIn] = Field(min_length=1, max_length=500)
    #: A note for the version this save makes ("clearer steps").
    note: str = Field(default="", max_length=200)


def _package_from(body: SkillIn, previous: SkillPackage | None) -> SkillPackage:
    files: dict[str, bytes] = {}
    for item in body.files:
        if item.keep:
            kept = previous.file(item.path) if previous is not None else None
            if kept is None:
                raise ApiError(422, "skill.file_not_found", f"There is no file {item.path} to keep", path=item.path)
            files[item.path] = kept.data
        elif item.text is not None:
            files[item.path] = item.text.encode("utf-8")
        elif item.base64 is not None:
            try:
                files[item.path] = base64.b64decode(item.base64, validate=True)
            except (binascii.Error, ValueError):
                raise ApiError(422, "skill.file_invalid", f"{item.path} is not valid base64", path=item.path) from None
        else:
            raise ApiError(422, "skill.file_invalid", f"{item.path} has no content", path=item.path)
    try:
        return parse_skill(files, limits=LIMITS)
    except SkillPackageError as error:
        raise _package_error(error) from None


@router.post("/skills/check", dependencies=READ)
def check_skill(body: SkillIn, ctx: Ctx, skill_id: str | None = None) -> SkillCheck:
    """The findings for an edited package, without storing it (the editor's live check)."""
    previous = None
    if skill_id:
        skill = _get(ctx, skill_id)
        previous = _skills(ctx).package(skill_id, skill.version)
    return _checked(ctx, _package_from(body, previous))


@router.post("/skills", status_code=201, dependencies=EDIT)
def create_skill(body: SkillIn, request: Request, ctx: Ctx) -> Skill:
    """A new skill made in the studio (version 1, origin ``user``)."""
    return _skills(ctx).create(_package_from(body, None), note=body.note, created_by=_actor(request))


@router.put("/skills/{skill_id}", dependencies=EDIT)
def update_skill(skill_id: str, body: SkillIn, request: Request, ctx: Ctx) -> Skill:
    """Store an edited package as the next version (unchanged files make none); the origin stays."""
    skill = _get(ctx, skill_id)
    previous = _skills(ctx).package(skill_id, skill.version)
    return _skills(ctx).save(skill_id, _package_from(body, previous), note=body.note, created_by=_actor(request))


class SkillCopyIn(BaseModel):
    #: The name of the copy (lowercase letters, digits, hyphens); it replaces ``name`` in ``SKILL.md``.
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _renamed(package: SkillPackage, name: str) -> SkillPackage:
    """The package with another ``name`` line in its front matter; every other byte stays."""
    raw = package.file(SKILL_FILE)
    assert raw is not None
    text = raw.data.decode("utf-8")
    head, separator, rest = text.partition(f"\nname: {package.name}")
    if not separator:
        raise ApiError(422, "skill.rename_unsupported", "The name of this skill cannot be changed here")
    files = {item.path: item.data for item in package.files}
    files[SKILL_FILE] = f"{head}\nname: {name}{rest}".encode()
    return parse_skill(files, limits=LIMITS)


@router.post("/skills/{skill_id}/copy", status_code=201, dependencies=EDIT)
def copy_skill(skill_id: str, body: SkillCopyIn, request: Request, ctx: Ctx) -> Skill:
    """A new skill from the latest version under another name (e.g. a copy in another language to translate)."""
    skill = _get(ctx, skill_id)
    package = _renamed(_skills(ctx).package(skill_id, skill.version), body.name)
    # The copy keeps the source's origin: an imported skill stays marked as imported (internal review M2).
    note = f"Copied from {skill.name}"
    return _skills(ctx).create(package, origin=skill.origin, note=note, created_by=_actor(request))


class SkillStatusIn(BaseModel):
    status: Literal["active", "disabled"]


@router.post("/skills/{skill_id}/status", dependencies=EDIT)
def set_skill_status(skill_id: str, body: SkillStatusIn, ctx: Ctx) -> Skill:
    """Activate or disable a skill; a disabled skill cannot run. Archiving needs ``admin`` (``.../archive``)."""
    _get(ctx, skill_id)
    return _skills(ctx).set_status(skill_id, body.status)


@router.post("/skills/{skill_id}/archive", dependencies=ADMIN)
def archive_skill(skill_id: str, ctx: Ctx) -> Skill:
    """Archive a skill (it cannot run or be assigned; it stays for history). Activating it again is ``status``."""
    _get(ctx, skill_id)
    return _skills(ctx).set_status(skill_id, "archived")


@router.delete("/skills/{skill_id}", status_code=204, dependencies=ADMIN)
def delete_skill(skill_id: str, ctx: Ctx) -> Response:
    """Delete a skill with all its versions; refused while an agent uses it (``skill.in_use``: archive it instead).
    Published releases keep their frozen copy."""
    _get(ctx, skill_id)
    users = _users(ctx, skill_id)
    if users:
        raise ApiError(409, "skill.in_use", "The skill is used by agents; archive it instead", count=len(users))
    _skills(ctx).delete(skill_id)
    return Response(status_code=204)


# ---- groups (NAL-B4) --------------------------------------------------------------------------------------------


class SkillGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    skill_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=100)


@router.get("/skill-groups", dependencies=READ)
def list_skill_groups(ctx: Ctx) -> list[SkillGroup]:
    return _skills(ctx).groups()


@router.post("/skill-groups", status_code=201, dependencies=EDIT)
def create_skill_group(body: SkillGroupIn, ctx: Ctx) -> SkillGroup:
    """A named set of skills of the library; assigning it to an agent adds its skills one by one (the editor does
    that, each in its current version). Names are unique (``skill.group_name_taken``)."""
    return _skills(ctx).save_group(SkillGroup(**body.model_dump()))


@router.put("/skill-groups/{group_id}", dependencies=EDIT)
def update_skill_group(group_id: str, body: SkillGroupIn, ctx: Ctx) -> SkillGroup:
    """Change a group. Agents keep the skills they were given with it."""
    found = _skills(ctx).group(group_id)
    if found is None:
        raise KeyError(group_id)
    return _skills(ctx).save_group(found.model_copy(update=body.model_dump()))


@router.delete("/skill-groups/{group_id}", status_code=204, dependencies=EDIT)
def delete_skill_group(group_id: str, ctx: Ctx) -> Response:
    """Delete a group; no skill and no agent changes."""
    if not _skills(ctx).delete_group(group_id):
        raise KeyError(group_id)
    return Response(status_code=204)


# ---- usage (NAL-B4) ---------------------------------------------------------------------------------------------

#: The most runs the usage looks at (newest first).
MAX_STATS_RUNS = 2000


@router.get("/skill-stats", dependencies=READ)
def get_skill_stats(ctx: Ctx, days: Annotated[int, Query(ge=1, le=365)] = 30) -> list[SkillStats]:
    """How skills were used in the runs of the last ``days`` days (at most the newest 2000 runs): offered, loaded,
    files read, runs, outcomes of the steps that loaded them and quality checks passed."""
    # One keyset page of the creation index (API2-04): never more than MAX_STATS_RUNS runs are read, and none before
    # the period. Team runs count too (their agents load the skills).
    since = now() - timedelta(days=days)
    runs, _more = ctx.store.list_page("runs", Run, limit=MAX_STATS_RUNS, since=since)
    return skill_stats(runs)


# ---- scripts as tool drafts (NAL-B4) ----------------------------------------------------------------------------


class ScriptDraftIn(BaseModel):
    path: str = Field(min_length=1, max_length=255)
    #: What the person wants the tool for (optional, at most 1000 characters are used).
    note: str = Field(default="", max_length=2000)


@router.post("/skills/{skill_id}/versions/{version}/script-draft", status_code=201, dependencies=RUN)
def draft_tool_from_script(skill_id: str, version: int, body: ScriptDraftIn, ctx: Ctx, request: Request) -> ToolJob:
    """Start a tool job that turns one script of a skill version into a tool draft (NAL-B4).

    The Coding Agent writes a tool from the script's text (a description, never instructions), and the draft goes
    through the unchanged tool chain: static check, tests in the isolating sandbox (without one:
    ``sandbox.isolation_required``), and a person's approval in the job. Until then the script is text that is
    never run. The job's ``origin`` is ``skill:<name>@<version>/<path>``. Same permission and model as any tool job.
    """
    skill = _get(ctx, skill_id)
    if skill.status != "active":
        raise NodestroError(
            "skill.inactive", f"The skill {skill.name} is {skill.status}", name=skill.name, status=skill.status
        )
    package = _skills(ctx).package(skill_id, version)
    try:
        requirement = script_requirement(package, version, body.path, body.note)
    except ValueError as exc:
        code = str(exc)
        status = 404 if code == "skill.script_not_found" else 422
        raise ApiError(status, code, "The script cannot become a tool") from None
    model = ctx.settings().assistant_model
    model_readiness.require_local_now(ctx, request, model, "agent_design")
    connection = ctx.connect_model(model)

    def record(job: ToolJob, tracker: UsageTracker) -> None:
        ctx.usage_log.record("tool_generation", tracker.records, label=job.origin or "", ref=job.id, tracker=tracker)

    return ctx.tools.start_job(
        requirement,
        connection=connection,
        model=model,
        usage_tracker=ctx.tracker(),
        on_finished=record,
        origin=script_origin(package.name, version, body.path.strip().lstrip("/")),
    )


# ---- the shipped skills -----------------------------------------------------------------------------------------


class LibrarySkill(BaseModel):
    name: str
    family: str
    language: str
    description: str
    version: str
    files: list[SkillFileInfo]


class LibrarySkillDetail(LibrarySkill):
    body: str


def _library_package(name: str) -> SkillPackage:
    try:
        return parse_skill(skill_library.skill_files(name), limits=LIMITS)
    except KeyError:
        raise ApiError(404, "skill.library_not_found", f"There is no shipped skill {name}", name=name[:80]) from None


def _library_version(package: SkillPackage) -> str:
    return f"{EXAMPLES_VERSION}+r{package.metadata.get('revision', '1')}"


def _library_entry(package: SkillPackage) -> dict[str, Any]:
    return {
        "name": package.name,
        "family": package.metadata.get("family", package.name),
        "language": package.metadata.get("language", ""),
        "description": package.description.strip(),
        "version": _library_version(package),
        "files": _files(package),
    }


@router.get("/library/skills", dependencies=READ)
def list_library_skills(request: Request, language: str | None = None) -> list[LibrarySkill]:
    """The shipped skills in one language (default: the interface language; English where one is missing)."""
    lang = language or request_language(request)
    return [
        LibrarySkill(**_library_entry(_library_package(skill_library.skill_for(family, lang))))
        for family in skill_library.SKILL_FAMILIES
    ]


@router.get("/library/skills/{name}", dependencies=READ)
def get_library_skill(name: str) -> LibrarySkillDetail:
    package = _library_package(name)
    return LibrarySkillDetail(**_library_entry(package), body=package.body)


@router.post("/library/skills/{name}/copy", status_code=201, dependencies=EDIT)
def copy_library_skill(name: str, request: Request, ctx: Ctx) -> Skill:
    """Store a shipped skill as a skill of this workspace (version 1, origin ``builtin:...``); a newer package
    never changes the copy. A skill of the same name already here is ``skill.name_taken``."""
    package = _library_package(name)
    origin = f"builtin:{_PACKAGE}@{_library_version(package)}/{package.name}"
    return _skills(ctx).create(package, origin=origin, note="library", created_by=_actor(request))
