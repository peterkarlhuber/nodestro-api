"""The example library: completeness in every language, valid workflows, copies and fake runs."""

from __future__ import annotations

from collections.abc import Callable
import json
import re
import time
import types
from typing import Any

from fastapi.testclient import TestClient
import nodestro_examples as library
from nodestro_runtime.schemas import LANGUAGES
from nodestro_runtime.testing import AnswerAll, AnswerAllClient, sample_instance
import pytest

from conftest import Parsed, text

IDS = library.template_ids()
#: The marker every prepared test input with sample data starts with.
SYNTHETIC = {
    "de": "SYNTHETISCHE BEISPIELDATEN",
    "en": "SYNTHETIC SAMPLE DATA",
    "es": "DATOS DE EJEMPLO SINTÉTICOS",
    "fr": "DONNÉES D'EXEMPLE SYNTHÉTIQUES",
    "pt-BR": "DADOS DE EXEMPLO SINTÉTICOS",
    "it": "DATI DI ESEMPIO SINTETICI",
}
#: The agent icons the editor can show (frontend/src/components/icons.ts).
ICONS = {
    *("bot", "search", "calculator", "check", "pen", "list", "users", "brain", "code", "globe"),
    *("book", "file", "mail", "chat", "shield", "scale", "target", "idea", "sparkles"),
}
MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def wait_run(client: TestClient, run_id: str, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run  # type: ignore[no-any-return]
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} did not finish")


def shape(value: Any) -> Any:
    """The structure of a sample file's content: sheets, rows and columns, or the number of files."""
    if isinstance(value, dict):
        return sorted(shape(item) for item in value.values())
    if isinstance(value, list):
        return [len(value), *({len(row) for row in value if isinstance(row, list)} or {0})]
    return "text" if isinstance(value, str) else type(value).__name__


def keys_of(value: Any, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        return {key for name, item in value.items() for key in keys_of(item, f"{prefix}{name}.")} | {prefix}
    return {prefix}


class Teams(AnswerAll):
    """Like :class:`AnswerAll`, and orchestrators decide like a person would test them: staff the first team when
    the orchestrator staffs its teams, give it one task, then finish with a summary."""

    def _answer(self, request: dict[str, Any]) -> Any:
        model = request.get("output_format")
        if model is None or model.__name__ != "Decision":
            return super()._answer(request)
        with self._lock:
            self.calls.append({key: value for key, value in request.items() if key != "output_format"})
        # The task lists the pool ("id: wf_..."), and what happened so far in the run.
        seen = json.dumps(request.get("messages", []), default=str)
        team = next(iter(re.findall(r"id: (wf_[A-Za-z0-9_]+)", seen)), "")
        if "Delegated to" in seen:
            decision: dict[str, Any] = {"action": "finish", "answer": "Summary of the teams.", "reason": "done"}
        elif "Team now: nobody yet" in seen:
            decision = {"action": "staff", "add": [team]}
        else:
            decision = {"action": "delegate", "member": team, "task": "Please work on this."}
        optional = {name: None for name, field in model.model_fields.items() if not field.is_required()}
        return Parsed(text("{}"), model(**{**optional, **decision}))


class TeamsClient(AnswerAllClient):
    def __init__(self) -> None:
        self.messages = Teams()
        self.beta = types.SimpleNamespace(messages=self.messages)


# ---- the files ------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_templates_pass_the_check_before_a_run(make_app: Callable[..., TestClient], language: str) -> None:
    client = make_app()
    for template_id in IDS:
        # A copy, so sub-workflow nodes point at the copied parts.
        workflow = client.post(f"/api/library/{template_id}/copy", json={"language": language}).json()
        check = client.post(f"/api/workflows/check?workflow_id={workflow['id']}", json=workflow).json()
        codes = [problem["code"] for problem in check["problems"] if problem["level"] != "info"]
        # Without an API key the check reports exactly that; the template itself has nothing to fix.
        assert codes == ["check.api_key_missing"], (language, template_id, check["problems"])
        assert all(not issues for issues in check["agents"].values()), (language, template_id, check["agents"])


# ---- the API --------------------------------------------------------------------


def test_listing_and_preview_store_nothing_and_call_no_model(make_app: Callable[..., TestClient]) -> None:
    fake = AnswerAllClient()
    client = make_app(client=fake)
    listing = client.get("/api/library", headers={"Accept-Language": "fr"}).json()
    assert len(listing) == 24 and {item["language"] for item in listing} == {"fr"}
    assert listing[4]["id"] == "financial-analysis" and listing[4]["name"] == "Analyse financière"
    italian = client.get("/api/library?language=it", headers={"Accept-Language": "fr"}).json()
    assert italian[0]["language"] == "it"
    detail = client.get("/api/library/quality-8d", headers={"Accept-Language": "de"}).json()
    assert detail["name"] == "Reklamation mit 8D untersuchen" and detail["agents"] == 5
    first = "Problembeschreibung D1\u2013D3"
    assert detail["agent_details"][0]["name"] == first
    # The root cause analysis reads the complaint itself (text or files) and the result of D1-D3.
    assert detail["agent_details"][1]["receives"] == [first, "Reklamation"]
    assert detail["guide"]["adapt"] and detail["input"].startswith(SYNTHETIC["de"])
    assert len(detail["workflow"]["nodes"]) == 9
    assert {"decision", "approval", "structured"} <= set(detail["features"]) and detail["pattern"] == "branching"
    # Nothing is installed or stored: neither on first visit nor by looking at templates.
    assert client.get("/api/workflows").json() == []
    assert fake.messages.calls == []


def test_unknown_templates_are_not_found(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    assert client.get("/api/library/does-not-exist").status_code == 404
    assert client.get("/api/library/..%2Fsecrets").status_code == 404
    assert client.post("/api/library/does-not-exist/copy", json={}).status_code == 404
    assert client.get("/api/library?language=xx").status_code == 422


def test_a_copy_is_an_independent_workflow(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    german = {"Accept-Language": "de"}
    first = client.post("/api/library/blog-article/copy", json={}, headers=german).json()
    assert first["name"] == "Blogartikel" and first["id"].startswith("wf_")
    names = [node.label for node in library.build("blog-article", "de").nodes if node.type == "agent"]
    assert [node["label"] for node in first["nodes"] if node["type"] == "agent"] == names
    assert len(client.get(f"/api/workflows/{first['id']}/versions").json()) == 1

    # Edit the copy: name, prompt, a node removed. The template and later copies stay as they were.
    changed = {**first, "name": "Mein Blog"}
    changed["nodes"] = [node for node in first["nodes"] if node["id"] != "editor"]
    changed["edges"] = [edge for edge in first["edges"] if "editor" not in (edge["source"], edge["target"])]
    changed["nodes"][1]["agent"]["system_prompt"] = "Geändert"
    client.put(f"/api/workflows/{first['id']}", json=changed)
    second = client.post("/api/library/blog-article/copy", json={"language": "de"}).json()
    assert second["id"] != first["id"] and second["name"] == "Blogartikel"
    template = library.build("blog-article", "de").model_dump(mode="json")
    for key in ("nodes", "edges", "description"):
        assert second[key] == template[key]
    assert client.get(f"/api/workflows/{first['id']}").json()["name"] == "Mein Blog"

    # The language of a copy is chosen once; a later language switch never changes it.
    spanish = client.post("/api/library/blog-article/copy", json={"language": "es"}, headers=german).json()
    assert spanish["name"] == "Artículo de blog"
    client.put("/api/settings", json={"language": "it"})
    assert client.get(f"/api/workflows/{first['id']}", headers={"Accept-Language": "it"}).json()["name"] == "Mein Blog"
    assert client.get(f"/api/workflows/{spanish['id']}").json()["name"] == "Artículo de blog"
    assert len(client.get("/api/workflows").json()) == 3


def test_existing_example_workflows_are_kept(make_app: Callable[..., TestClient]) -> None:
    from nodestro_runtime.schemas import Workflow

    client = make_app()
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    nodestro.store.put("workflows", "wf_example_finance", Workflow(id="wf_example_finance", name="Finanzanalyse"))
    nodestro.store.put_raw("settings", "flags", {"examples_installed": True})
    names = [workflow["name"] for workflow in client.get("/api/workflows").json()]
    assert names == ["Finanzanalyse"]


def test_the_global_search_finds_examples_in_the_interface_language(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    hits = client.get("/api/search?q=8D", headers={"Accept-Language": "de"}).json()
    assert hits[0]["kind"] == "example" and hits[0]["id"] == "quality-8d"
    assert hits[0]["subtitle"] == "Qualitätsmanagement"
    english = client.get("/api/search?q=household", headers={"Accept-Language": "en"}).json()
    assert [hit["id"] for hit in english if hit["kind"] == "example"][:1] == ["household-budget"]
    # A workflow of the user ranks before an example with the same match.
    client.post("/api/library/quality-8d/copy", json={"language": "de"})
    hits = client.get("/api/search?q=8D", headers={"Accept-Language": "de"}).json()
    kinds = [hit["kind"] for hit in hits]
    assert kinds.index("workflow") < kinds.index("example")


# ---- fake runs ------------------------------------------------------------------


@pytest.mark.parametrize("template_id", IDS)
def test_every_template_runs_end_to_end_with_a_fake_client(
    make_app: Callable[..., TestClient], template_id: str
) -> None:
    # German plus one more language per template, so all six languages run.
    for language in ("de", LANGUAGES[1 + IDS.index(template_id) % (len(LANGUAGES) - 1)]):
        fake = TeamsClient()
        client = make_app(client=fake)
        workflow = client.post(f"/api/library/{template_id}/copy", json={"language": language}).json()
        start = next(node for node in workflow["nodes"] if node["type"] == "start")
        run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": start["start"]["default_input"]})
        assert run.status_code in (200, 201), run.text
        result = wait_run(client, run.json()["id"])
        # Approval steps wait for a person; the test approves them.
        while result["status"] == "paused":
            for pending in client.get("/api/approvals").json():
                decided = client.post(
                    f"/api/runs/{pending['run_id']}/approvals/{pending['node_id']}", json={"decision": "approved"}
                )
                assert decided.status_code == 200, decided.text
            result = wait_run(client, run.json()["id"])
        assert result["status"] == "succeeded", (language, template_id, result.get("error"))
        agents = [node for node in workflow["nodes"] if node["type"] == "agent"]
        incoming: dict[str, list[Any]] = {}
        for edge in workflow["edges"]:
            incoming.setdefault(edge["target"], []).append(edge.get("source_handle"))
        # Agents that only run after a rejection are skipped when everything is approved (an agent a rejection
        # loops back to, e.g. the reply writer of customer-service, runs on its first way in as well).
        rejected_only = {target for target, handles in incoming.items() if set(handles) == {"rejected"}}
        # Agents behind a decision run or are skipped, depending on the branch the fake answers open.
        branch_only = {target for target, handles in incoming.items() if all(handles)} - rejected_only
        for node in agents:
            state = result["nodes"][node["id"]]
            if node["id"] in rejected_only:
                assert state["status"] == "skipped", (language, template_id, node["id"])
                continue
            if node["id"] in branch_only and state["status"] == "skipped":
                continue
            assert state["status"] == "succeeded", (language, template_id, node["id"])
            prompt = state["prompt"]
            assert "{{" not in prompt and "# Eingaben" not in prompt, (language, template_id, node["id"], prompt)
            if node["agent"]["output"]["mode"] == "structured":
                names = {field["name"] for field in node["agent"]["output"]["fields"]}
                assert set(state["output"]) == names
        systems = {str(call.get("system", "")) for call in fake.messages.calls}
        for node in [item for item in agents if result["nodes"][item["id"]]["status"] == "succeeded"]:
            assert any(node["agent"]["system_prompt"][:60] in system for system in systems), node["id"]
        assert result["output"] is not None


def test_copying_a_template_with_a_sub_workflow_copies_its_part(make_app: Callable[..., TestClient]) -> None:
    fake = AnswerAllClient()
    client = make_app(client=fake)
    workflow = client.post("/api/library/production-downtime/copy", json={"language": "en"}).json()
    node = next(item for item in workflow["nodes"] if item["type"] == "subworkflow")
    child_id = node["subworkflow"]["workflow_id"]
    child = client.get(f"/api/workflows/{child_id}").json()
    assert child["name"] == "Quality assurance of measures (building block)"
    assert node["subworkflow"]["version"] == 1
    assert len(client.get("/api/workflows").json()) == 2
    # The template keeps naming its part; the copy is independent of it.
    assert library.build("production-downtime", "en").nodes[-2].subworkflow.workflow_id == "library-part:quality-check"
    start = next(item for item in workflow["nodes"] if item["type"] == "start")
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": start["start"]["default_input"]}).json()
    result = wait_run(client, run["id"])
    assert result["status"] == "succeeded", result.get("error")
    child_run = result["nodes"][node["id"]]["child_run_id"]
    assert client.get(f"/api/runs/{child_run}").json()["workflow_id"] == child_id
    # Only the top-level run counts in the usage report; the child's cost is part of it.
    usage = client.get("/api/usage").json()
    assert usage["total"]["runs"] == 1


def test_the_travel_form_decides_the_documents_branch(make_app: Callable[..., TestClient]) -> None:
    client = make_app(client=AnswerAllClient())
    workflow = client.post("/api/library/travel-plan/copy", json={"language": "de"}).json()
    run = client.post(
        f"/api/workflows/{workflow['id']}/runs",
        json={
            "values": {"destination": "Hamburg", "arrival": "2026-07-06", "departure": "2026-07-08", "abroad": False}
        },
    ).json()
    result = wait_run(client, run["id"])
    assert result["status"] == "succeeded"
    assert result["nodes"]["documents"]["status"] == "skipped"
    assert result["nodes"]["gate"]["decision"]["branches"] == ["default"]
    assert "Reiseziel: Hamburg" in result["input"]
    invalid = client.post(f"/api/workflows/{workflow['id']}/runs", json={"values": {"arrival": "bald"}})
    assert invalid.status_code == 400 and invalid.json()["code"] == "form.invalid"
    assert invalid.json()["params"]["errors"][0]["code"] == "form.not_date"


# ---- file inputs, sample files, result files and test series ---------------------------------


class Override(AnswerAll):
    """Like :class:`AnswerAll`, with chosen values for named fields of structured results."""

    def __init__(self, **values: Any) -> None:
        super().__init__()
        self.values = values

    def _answer(self, request: dict[str, Any]) -> Any:
        model = request.get("output_format")
        if model is not None and model.__name__ != "_Verdict":
            with self._lock:
                self.calls.append({key: value for key, value in request.items() if key != "output_format"})
            sample = sample_instance(model, request).model_dump()
            sample.update({key: value for key, value in self.values.items() if key in sample})
            return Parsed(text("{}"), model(**sample))
        return super()._answer(request)


class OverrideClient(AnswerAllClient):
    def __init__(self, **values: Any) -> None:
        self.messages = Override(**values)
        self.beta = types.SimpleNamespace(messages=self.messages)


def file_fields(template_id: str) -> list[Any]:
    return [field for node in library.structure(template_id).nodes for field in node.form if field.type == "file"]


def upload(client: TestClient, name: str, data: bytes) -> dict[str, Any]:
    response = client.post("/api/inputs", files=[("files", (name, data))])
    assert response.status_code == 201, response.text
    [result] = response.json()
    assert result["file"] is not None, result
    return result["file"]  # type: ignore[no-any-return]


def decide_approvals(client: TestClient, run_id: str, decision: str = "approved", comment: str = "") -> dict[str, Any]:
    result = wait_run(client, run_id)
    while result["status"] == "paused":
        for pending in client.get("/api/approvals").json():
            decided = client.post(
                f"/api/runs/{pending['run_id']}/approvals/{pending['node_id']}",
                json={"decision": decision, "comment": comment},
            )
            assert decided.status_code == 200, decided.text
        result = wait_run(client, run_id)
    return result


def test_sample_files_can_be_downloaded_from_the_gallery(make_app: Callable[..., TestClient]) -> None:
    client = make_app()
    detail = client.get("/api/library/contract-review?language=en").json()
    [sample] = detail["samples"]
    assert sample["field"] == "contract_file" and sample["file_name"].endswith(".docx")
    assert sample["field_label"] and sample["description"]
    assert detail["tests"] and "documents" in detail["features"] and "proposals" in detail["features"]
    response = client.get(f"/api/library/contract-review/samples/{sample['id']}?language=en")
    assert response.status_code == 200 and response.content[:2] == b"PK"
    assert "attachment" in response.headers["content-disposition"]
    assert client.get("/api/library/contract-review/samples/nope").status_code == 404
    image = client.get("/api/library/real-estate/samples/floor_plan")
    assert image.headers["content-type"] == "image/png" and image.content[:4] == b"\x89PNG"
    # Looking at samples stores nothing.
    assert client.get("/api/workflows").json() == []
    assert client.app.state.nodestro.inputs.all() == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("template_id", IDS)
def test_a_copy_can_take_over_the_test_cases_with_their_sample_files(
    make_app: Callable[..., TestClient], template_id: str
) -> None:
    from nodestro_runtime.conditions import evaluate
    from nodestro_runtime.schemas import ConditionRule

    language = LANGUAGES[IDS.index(template_id) % len(LANGUAGES)]
    client = make_app(client=TeamsClient())
    plain = client.post(f"/api/library/{template_id}/copy", json={"language": language}).json()
    assert client.get(f"/api/benchmarks?workflow_id={plain['id']}").json() == []
    copy = client.post(f"/api/library/{template_id}/copy", json={"language": language, "tests": True}).json()
    [suite] = client.get(f"/api/benchmarks?workflow_id={copy['id']}").json()
    spec = library.structure(template_id)
    assert len(suite["cases"]) == len(spec.tests) >= 1
    assert client.get("/api/runs").json() == [], "copying never starts a run"
    # The sample files are input files of the copy, kept as long as the test cases reference them.
    stored = {item.id: item for item in client.app.state.nodestro.inputs.all()}  # type: ignore[attr-defined]
    assert len(stored) == len({sample for case in spec.tests for sample in case.samples})
    alternative_paths = 0
    for case, case_spec in zip(suite["cases"], spec.tests, strict=True):
        assert case["name"].strip() and case["checks"]
        run = client.post(f"/api/workflows/{copy['id']}/runs", json={"input": case["input"], "values": case["values"]})
        assert run.status_code in (200, 201), (template_id, case["name"], run.text)
        result = decide_approvals(client, run.json()["id"])
        assert result["status"] == "succeeded", (template_id, case["name"], result.get("error"))
        for check in case["checks"]:
            verdict = evaluate(ConditionRule(**check), result["output"])
            # Every check names a field the result has (a fake answers every field).
            assert verdict is not None, (template_id, case["name"], check)
        if case_spec.samples:
            ids = [item for value in case["values"].values() if isinstance(value, list) for item in value]
            assert ids and set(ids) <= set(stored)
            receivers = [state for state in result["nodes"].values() if state.get("files")]
            assert receivers, "the agents fed by the start node received the sample files"
        alternative_paths += case_spec.input == "own" or bool(case_spec.values)
    if any(node.type == "condition" and node.condition and node.condition.source == "start" for node in spec.nodes):
        assert alternative_paths, "a decision on the form has a test case for its other path"


def test_production_downtime_checks_tell_both_branches_apart(make_app: Callable[..., TestClient]) -> None:
    from nodestro_api.benchmarks import TestCase as Case
    from nodestro_api.benchmarks import check_case

    for share, quality in ((4.6, "succeeded"), (1.4, "skipped")):
        client = make_app(client=OverrideClient(downtime_share_percent=share))
        copy = client.post("/api/library/production-downtime/copy", json={"language": "de", "tests": True}).json()
        [suite] = client.get(f"/api/benchmarks?workflow_id={copy['id']}").json()
        above, within = suite["cases"][0], suite["cases"][1]
        case = above if share > 3 else within
        run = client.post(f"/api/workflows/{copy['id']}/runs", json={"input": case["input"]}).json()
        result = wait_run(client, run["id"])
        assert result["nodes"]["quality"]["status"] == quality
        assert all(check.passed for check in check_case(Case(**case), result["output"]))
        other = within if share > 3 else above
        assert not all(check.passed for check in check_case(Case(**other), result["output"]))


def test_contract_review_reads_an_uploaded_contract(make_app: Callable[..., TestClient]) -> None:
    from nodestro_examples import samples

    client = make_app(client=AnswerAllClient())
    workflow = client.post("/api/library/contract-review/copy", json={"language": "de"}).json()
    name, _, data = samples.render("contract-review", "contract", "de")
    uploaded = upload(client, name, data)
    run = client.post(
        f"/api/workflows/{workflow['id']}/runs", json={"values": {"contract_file": [uploaded["id"]]}}
    ).json()
    result = wait_run(client, run["id"])
    assert result["status"] == "succeeded", result.get("error")
    nodes = result["nodes"]
    # Every agent that works on the contract itself gets the file, not only the structurer.
    for node_id in ("structure", "explain", "checkpoints"):
        assert [item["name"] for item in nodes[node_id]["files"]] == [name], node_id
    assert nodes["report"]["files"] == []
    # Result files: PDF and Word, named in the copy's language and bound to this run.
    files = client.get(f"/api/runs/{run['id']}/files").json()
    assert sorted(item["name"] for item in files) == ["Vertragsübersicht.docx", "Vertragsübersicht.pdf"]
    for item in files:
        download = client.get(f"/api/runs/{run['id']}/files/{item['id']}")
        assert download.status_code == 200 and download.content[:4] in (b"%PDF", b"PK\x03\x04")
    # The structurer proposes the contract's key data; nothing reaches knowledge without a confirmation.
    [proposal] = nodes["structure"]["proposals"]
    assert proposal["content_kind"] == "extracted" and proposal["adopted_file_id"] is None
    # The knowledge base holds only the copy's own knowledge file (the playbook), no proposal.
    [playbook] = client.get("/api/knowledge").json()
    assert playbook["collection"].startswith("Beispiele: ") and playbook["origin"] == "upload"
    # Wrong file types and a second file are refused before the run starts.
    other = upload(client, "data.csv", b"a,b\n1,2\n")
    wrong = client.post(f"/api/workflows/{workflow['id']}/runs", json={"values": {"contract_file": [other["id"]]}})
    assert wrong.status_code == 400 and wrong.json()["params"]["errors"][0]["code"] == "form.file_type"
    two = client.post(
        f"/api/workflows/{workflow['id']}/runs",
        json={"values": {"contract_file": [uploaded["id"], uploaded["id"] + "x"]}},
    )
    assert two.status_code == 400


def test_code_review_reads_a_zip_project_as_overview_and_never_runs_it(make_app: Callable[..., TestClient]) -> None:
    from nodestro_examples import samples

    client = make_app(client=AnswerAllClient())
    workflow = client.post("/api/library/code-review/copy", json={"language": "en"}).json()
    name, _, data = samples.render("code-review", "project", "en")
    uploaded = upload(client, name, data)
    run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"values": {"code_files": [uploaded["id"]]}}).json()
    result = wait_run(client, run["id"])
    assert result["status"] == "succeeded"
    for node_id in ("bugs", "security", "fix"):
        [provided] = result["nodes"][node_id]["files"]
        assert provided["mode"] == "overview", node_id
    tools = {tool["name"] for call in client.fake.messages.calls for tool in call.get("tools", [])}  # type: ignore[attr-defined]
    assert "input_files" in tools


def test_hr_recruiting_revises_only_after_a_failed_fairness_check(make_app: Callable[..., TestClient]) -> None:
    for approved, status in ((False, "succeeded"), (True, "skipped")):
        client = make_app(client=OverrideClient(approved=approved))
        workflow = client.post("/api/library/hr-recruiting/copy", json={"language": "en"}).json()
        start = next(node for node in workflow["nodes"] if node["type"] == "start")
        run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": start["start"]["default_input"]})
        # The release step waits for a person; the test approves it.
        result = decide_approvals(client, run.json()["id"])
        assert result["status"] == "succeeded"
        assert result["nodes"]["revise"]["status"] == status
        if not approved:
            prompt = result["nodes"]["revise"]["prompt"]
            labels = {node["id"]: node["label"] for node in workflow["nodes"]}
            # The revision sees the documents and the check through the decision.
            assert prompt.count("Ergebnis des Agenten.") == 2 and "approved" in prompt, labels


def test_construction_report_files_wait_for_the_approval(make_app: Callable[..., TestClient]) -> None:
    for decision, status in (("rejected", "cancelled"), ("approved", "succeeded")):
        client = make_app(client=TeamsClient())
        workflow = client.post("/api/library/construction-delay/copy", json={"language": "fr"}).json()
        start = next(node for node in workflow["nodes"] if node["type"] == "start")
        run = client.post(f"/api/workflows/{workflow['id']}/runs", json={"input": start["start"]["default_input"]})
        result = decide_approvals(client, run.json()["id"], decision, comment="Termine prüfen")
        assert result["status"] == status
        formats = sorted(item["format"] for item in client.get(f"/api/runs/{result['id']}/files").json())
        assert formats == ([] if decision == "rejected" else ["docx", "pdf"])


def test_learning_plan_works_out_the_chosen_week(make_app: Callable[..., TestClient]) -> None:
    client = make_app(client=AnswerAllClient())
    workflow = client.post("/api/library/learning-plan/copy", json={"language": "de"}).json()
    start = next(node for node in workflow["nodes"] if node["type"] == "start")
    run = client.post(
        f"/api/workflows/{workflow['id']}/runs", json={"input": start["start"]["default_input"], "values": {"week": 3}}
    ).json()
    result = wait_run(client, run["id"])
    assert "Woche 3" in result["nodes"]["tutor"]["prompt"] and "Woche 3" in result["nodes"]["quiz"]["prompt"]
    default = client.post(
        f"/api/workflows/{workflow['id']}/runs", json={"input": start["start"]["default_input"]}
    ).json()
    assert "Woche 1" in wait_run(client, default["id"])["nodes"]["quiz"]["prompt"]


@pytest.mark.parametrize("language", ["de", "pt-BR"])
def test_every_example_can_be_checked_for_publishing_as_an_api(
    make_app: Callable[..., TestClient], language: str
) -> None:
    client = make_app()
    for template_id in IDS:
        workflow = client.post(f"/api/library/{template_id}/copy", json={"language": language}).json()
        check = client.post(f"/api/workflows/{workflow['id']}/api/check", json={}).json()
        errors = [item for item in check["problems"] if item["level"] == "error"]
        assert check["ok"] and not errors, (template_id, errors)
        properties = json.dumps(check["input_schema"])
        for field in file_fields(template_id):
            assert field.name in properties, (template_id, field.name)
        # Approvals stay approvals behind the API, also those of the teams and sub-workflows.
        nodes = [
            *library.structure(template_id).nodes,
            *(n for p in library.part_ids(template_id) for n in library.part(p).nodes),
        ]
        approvals = sum(node.type == "approval" for node in nodes)
        assert check["approvals"] == approvals


def test_a_published_example_takes_a_sample_file_and_returns_its_result_files(
    make_app: Callable[..., TestClient],
) -> None:
    from nodestro_examples import samples
    from test_published_api import PUBLISHED, auth, finish, new_key, publish

    client = make_app(client=AnswerAllClient())
    workflow = client.post("/api/library/financial-analysis/copy", json={"language": "en"}).json()
    workflow["budget_usd"] = 1.0
    client.put(f"/api/workflows/{workflow['id']}", json=workflow)
    publication = publish(client, workflow["id"], note="v1")["publication"]
    token, slug = new_key(client, publication["id"]), publication["slug"]
    name, _, data = samples.render("financial-analysis", "figures", "en")
    started = client.post(
        f"{PUBLISHED}/{slug}/runs",
        headers=auth(token),
        data={"input": "Analyse the figures in the attached workbook."},
        files=[("figures", (name, data))],
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    assert finish(client, slug, token, run_id)["status"] == "succeeded"
    files = client.get(f"{PUBLISHED}/{slug}/runs/{run_id}/files", headers=auth(token)).json()
    listed = files["files"] if isinstance(files, dict) else files
    assert sorted(item["format"] for item in listed) == ["pdf", "xlsx"]
