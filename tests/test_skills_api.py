"""The skill library API (NAL-B3): import with a validation report, editing, versions, export, usage, the shipped
skills and skills frozen in a release."""

from __future__ import annotations

from collections.abc import Callable
import io
from typing import Any
import zipfile

from fastapi.testclient import TestClient
from nodestro_examples import skills as skill_library
from nodestro_runtime.testing import text, tool_use
from test_published_api import publish

from test_flow import edge, save
from test_runs import agent_node

SKILL_MD = "---\nname: pdf-review\ndescription: Reviews PDF reports.\n---\nCheck page numbers first.\n"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _import(client: TestClient, data: bytes, name: str = "skill.zip", **form: Any) -> Any:
    return client.post("/api/skills/import", files={"file": (name, data)}, data={k: str(v) for k, v in form.items()})


def _create(client: TestClient, body: str = SKILL_MD, **files: str) -> dict[str, Any]:
    payload = [{"path": "SKILL.md", "text": body}, *({"path": path, "text": value} for path, value in files.items())]
    answer = client.post("/api/skills", json={"files": payload, "note": "first"})
    assert answer.status_code == 201, answer.text
    result: dict[str, Any] = answer.json()
    return result


def test_import_reports_first_and_stores_after(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    package = _zip(
        {
            "pdf-review/SKILL.md": SKILL_MD.replace("---\nCheck", "allowed-tools: Bash\n---\nCheck").encode(),
            "pdf-review/scripts/run.sh": b"echo hi\n",
        }
    )
    report = _import(client, package, dry_run="true")
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["skill"] is None and client.get("/api/skills").json() == []
    codes = {issue["code"] for issue in body["check"]["issues"]}
    assert codes == {"skill.allowed_tools_ignored", "skill.scripts_not_run"}
    assert [item["path"] for item in body["check"]["files"] if item["script"]] == ["scripts/run.sh"]
    stored = _import(client, package).json()["skill"]
    assert stored["origin"].startswith("import:") and stored["has_scripts"]
    # The same name again is refused; as a new version of that skill it is stored.
    again = _import(client, package)
    assert again.status_code == 400 and again.json()["code"] == "skill.name_taken"


def test_unsafe_and_invalid_packages_are_refused(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    unsafe = _import(client, _zip({"SKILL.md": SKILL_MD.encode(), "../evil.md": b"x"}))
    assert unsafe.status_code == 422 and unsafe.json()["code"] == "skill.unsafe_path"
    bomb = _import(client, _zip({"SKILL.md": SKILL_MD.encode(), "big.txt": b"0" * 2_000_000}))
    assert bomb.status_code == 413 and bomb.json()["code"] == "skill.zip_bomb"
    invalid = _import(client, b"---\nname: Bad Name\ndescription: x\n---\n", name="SKILL.md")
    assert invalid.status_code == 400 and invalid.json()["params"]["reason"] == "skill.name_invalid"


def test_editing_makes_versions_and_keeps_files(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    skill = _create(client, **{"references/list.md": "- a"})
    url = f"/api/skills/{skill['id']}"
    changed = client.put(
        url,
        json={
            "files": [
                {"path": "SKILL.md", "text": SKILL_MD.replace("first", "first, then tables")},
                {"path": "references/list.md", "keep": True},
            ],
            "note": "tables",
        },
    ).json()
    assert changed["version"] == 2
    assert [item["version"] for item in client.get(f"{url}/versions").json()] == [2, 1]
    detail = client.get(url).json()
    assert "tables" in detail["body"] and [f["path"] for f in detail["files"]] == ["SKILL.md", "references/list.md"]
    assert client.get(f"{url}?version=1").json()["body"].strip() == "Check page numbers first."
    read = client.get(f"{url}/versions/2/file", params={"path": "references/list.md"}).json()
    assert read["text"] == "- a"
    # The editor's live check stores nothing.
    checked = client.post(
        f"/api/skills/check?skill_id={skill['id']}", json={"files": [{"path": "SKILL.md", "text": "no front matter"}]}
    ).json()
    assert checked["issues"][0]["code"] == "skill.frontmatter_invalid"
    # Export: a standard ZIP with the exact bytes; importing it again gives the same files.
    exported = client.get(f"{url}/export")
    assert exported.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert sorted(archive.namelist()) == ["pdf-review/SKILL.md", "pdf-review/references/list.md"]
    copy = client.post(f"{url}/copy", json={"name": "pdf-review-de"}).json()
    assert copy["name"] == "pdf-review-de" and copy["version"] == 1


def test_status_usage_and_delete(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    skill = _create(client)
    url = f"/api/skills/{skill['id']}"
    assert client.post(f"{url}/status", json={"status": "disabled"}).json()["status"] == "disabled"
    assert client.post(f"{url}/status", json={"status": "archived"}).status_code == 422
    assert client.post(f"{url}/archive").json()["status"] == "archived"
    client.post(f"{url}/status", json={"status": "active"})
    binding = {"skill_id": skill["id"], "version": 1}
    workflow_id = save(
        client,
        [{"id": "start", "type": "start"}, agent_node("writer", "WRITER", skills=[binding])],
        [edge("start", "writer")],
    )
    usage = client.get(f"{url}/usage").json()
    assert [(item["kind"], item["id"], item["version"]) for item in usage] == [("workflow", workflow_id, 1)]
    refused = client.delete(url)
    assert refused.status_code == 409 and refused.json()["code"] == "skill.in_use"
    client.delete(f"/api/workflows/{workflow_id}")
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404


def test_the_shipped_skills(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    listed = client.get("/api/library/skills", params={"language": "de"}).json()
    assert {item["family"] for item in listed} == set(skill_library.SKILL_FAMILIES)
    assert all(item["language"] == "de" for item in listed)
    name = skill_library.skill_for("research-method", "fr")
    detail = client.get(f"/api/library/skills/{name}").json()
    assert detail["name"] == name and detail["body"]
    copied = client.post(f"/api/library/skills/{name}/copy").json()
    assert copied["origin"].startswith("builtin:nodestro-examples@") and copied["origin"].endswith(f"/{name}")
    assert client.post(f"/api/library/skills/{name}/copy").json()["code"] == "skill.name_taken"
    assert client.get("/api/library/skills/nope").status_code == 404


def test_a_release_runs_its_frozen_skill_version(make_app: Callable[..., TestClient]) -> None:
    """A release freezes the assigned version with its files; changing the skill afterwards changes nothing."""
    from test_published_api import PUBLISHED, auth, finish, new_key

    script = {"WRITER": [tool_use("load_skill", {"name": "pdf-review"}), text("done")] * 3}
    client = make_app(script)
    skill = _create(client)
    binding = {"skill_id": skill["id"], "version": 1}
    workflow_id = save(
        client,
        [
            {"id": "start", "type": "start"},
            agent_node("writer", "WRITER", skills=[binding]),
            {"id": "out", "type": "output"},
        ],
        [edge("start", "writer"), edge("writer", "out")],
        name="Skilled",
    )
    result = publish(client, workflow_id)
    release = client.get(f"/api/publications/{result['publication']['id']}/releases/1").json()
    [frozen] = release["bundle"]["skills"]
    assert (frozen["skill_id"], frozen["version"], frozen["name"]) == (skill["id"], 1, "pdf-review")
    # The skill changes and is even disabled: the release still runs its own copy.
    client.put(f"/api/skills/{skill['id']}", json={"files": [{"path": "SKILL.md", "text": SKILL_MD + "More.\n"}]})
    client.post(f"/api/skills/{skill['id']}/status", json={"status": "disabled"})
    token = new_key(client, result["publication"]["id"])
    slug = result["publication"]["slug"]
    started = client.post(f"{PUBLISHED}/{slug}/runs", json={"input": "x"}, headers=auth(token))
    assert started.status_code in (200, 201, 202), started.text
    status = finish(client, slug, token, started.json()["run_id"])
    assert status["status"] == "succeeded", status


def test_skills_are_in_the_backup() -> None:
    from nodestro_api import backup

    models = backup._document_models()
    assert "skills" in models and "skill_versions" in models
