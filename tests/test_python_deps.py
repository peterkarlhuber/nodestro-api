"""Python dependencies of tools: detection, approval, environments, integration."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil
import time
from typing import Any

from fastapi.testclient import TestClient
from nodestro_runtime.python_deps import analyze, parse_requirement
from nodestro_runtime.python_envs import explain_uv_error, parse_lock
import pytest

from conftest import FakeUv, isolate, parsed, text, tool_use
from test_runs import agent_node, create, start


def _index() -> dict[str, list[tuple[str, list[str], str]]]:
    return {
        "tabulate": [
            ("0.8.0", ["wcwidth"], "tabulate\nVERSION = '0.8'\n\ndef tabulate(rows):\n    return '|'.join(rows)\n"),
            ("0.9.0", ["wcwidth"], "tabulate\nVERSION = '0.9'\n\ndef tabulate(rows):\n    return ' | '.join(rows)\n"),
        ],
        "wcwidth": [("0.2.0", [], "wcwidth\nWIDTH = 1\n")],
        "pillow": [("10.0.0", [], "PIL\nNAME = 'pil'\n")],
    }


TOOL = '''\
# /// script
# dependencies = ["tabulate>=0.8"]
# ///
import tabulate


def join_cells(cells: list[str]) -> str:
    """Join table cells.

    Args:
        cells: The cells.

    Returns:
        The joined row.
    """
    return tabulate.tabulate(cells) + " v" + tabulate.VERSION
'''

TESTS = """\
from tool import join_cells


def test_join():
    assert join_cells(["a", "b"]).startswith("a")
"""

PLAIN = '''\
import math


def circle_area(radius: float) -> float:
    """Area of a circle.

    Args:
        radius: The radius.

    Returns:
        The area.
    """
    return math.pi * radius**2
'''


# ---- detection -----------------------------------------------------------------------


def test_standard_library_local_and_runtime_modules_need_no_package() -> None:
    found = analyze("import math\nimport json, os.path\nfrom pydantic import BaseModel\n", "from tool import f\n")
    assert found.requirements == [] and found.issues == []
    assert {module.kind for module in found.modules} == {"stdlib", "runtime", "local"}


def test_module_names_map_to_package_names() -> None:
    found = analyze("import numpy as np\nfrom PIL import Image\nimport sklearn.linear_model\nimport yaml\n")
    assert [item.name for item in found.requirements] == ["numpy", "Pillow", "PyYAML", "scikit-learn"]
    assert found.import_names() == {"numpy", "PIL", "sklearn", "yaml"}
    pillow = next(item for item in found.requirements if item.key == "pillow")
    assert pillow.modules == ["PIL"] and pillow.source == "import"


def test_header_declares_versions_and_extra_packages() -> None:
    code = '# /// script\n# dependencies = ["pandas>=2.2,<3", "openpyxl"]\n# ///\nimport pandas as pd\n'
    found = analyze(code)
    by_key = {item.key: item for item in found.requirements}
    assert by_key["pandas"].spec == ">=2.2,<3" and by_key["pandas"].modules == ["pandas"]
    assert by_key["openpyxl"].source == "header" and by_key["openpyxl"].modules == []
    assert found.issues == []


def test_unknown_modules_are_reported_not_guessed() -> None:
    found = analyze("import fancy_lib\n")
    assert found.requirements == []
    assert [issue.code for issue in found.issues] == ["deps.module_unknown"]
    assert found.issues[0].params == {"module": "fancy_lib"}
    declared = analyze('# /// script\n# dependencies = ["fancy-lib"]\n# ///\nimport fancy_lib\n')
    assert declared.issues == [] and declared.requirements[0].modules == ["fancy_lib"]
    other = analyze('# /// script\n# dependencies = ["fancy-distribution"]\n# ///\nimport fancy_lib\n')
    assert [issue.code for issue in other.issues] == ["deps.module_assumed"]


def test_dynamic_optional_and_invalid_imports() -> None:
    found = analyze("import importlib\nmod = importlib.import_module('x')\n")
    assert "deps.dynamic_import" in [issue.code for issue in found.issues]
    optional = analyze("try:\n    import ujson as json\nexcept ImportError:\n    import json\n")
    assert optional.requirements == [] and optional.issues == []
    invalid = analyze('# /// script\n# dependencies = ["evil @ https://x.example/evil.whl", "ok>=1"]\n# ///\n')
    assert [issue.code for issue in invalid.issues] == ["deps.requirement_invalid"]
    assert [item.key for item in invalid.requirements] == ["ok"]
    broken = analyze("# /// script\n# dependencies = [\n# ///\n")
    assert [issue.code for issue in broken.issues] == ["deps.header_invalid"]


def test_requirements_and_lock_files() -> None:
    requirement = parse_requirement("Pandas[excel] >= 2.2 ; python_version >= '3.11'")
    assert requirement is not None
    assert requirement.key == "pandas" and requirement.extras == ["excel"] and requirement.spec == ">=2.2"
    assert requirement.line() == "Pandas[excel]>=2.2; python_version >= '3.11'"
    assert parse_requirement("git+https://example.com/x.git") is None
    lock = (
        "colorama==0.4.6 ; sys_platform == 'win32'\n    # via tqdm\n"
        "numpy==2.5.4\n    # via\n    #   -r requirements.in\n    #   pandas\n"
        "pandas==3.1.0\n    # via -r requirements.in\n"
    )
    packages = {item.key: item for item in parse_lock(lock)}
    assert packages["colorama"].marker == "sys_platform == 'win32'" and packages["colorama"].required_by == ["tqdm"]
    assert packages["numpy"].direct and packages["numpy"].required_by == ["pandas"]
    assert packages["pandas"].direct and packages["pandas"].version == "3.1.0"


def test_uv_errors_get_codes() -> None:
    assert explain_uv_error("Because nope was not found in the package registry").code == "deps.not_found"
    assert explain_uv_error("No solution found ... there is no version of x==9 ...").params["name"] == "x"
    assert explain_uv_error("No solution found when resolving dependencies: unsatisfiable").code == "deps.conflict"
    assert explain_uv_error("error: Failed to fetch: https://pypi.org/simple/x/").code == "deps.network"


# ---- the flow through the API ----------------------------------------------------------


def _wait_env(client: TestClient, kind: str, ref: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        env = client.get(f"/api/python/environments/{kind}/{ref}").json()
        if env["install"]["status"] != "installing":
            return env  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("installation did not finish")


def _approve_tool(client: TestClient, name: str = "join_cells", code: str = TOOL) -> dict[str, Any]:
    plan = client.post(f"/api/python/tools/{name}/plan", json={"code": code, "tests": TESTS}).json()
    assert plan["error"] is None, plan
    approved = client.post(f"/api/python/plans/{plan['id']}/approve")
    assert approved.status_code == 200, approved.text
    return _wait_env(client, "tool", name)


@pytest.fixture
def uv() -> FakeUv:
    return FakeUv(_index())


def test_tool_with_a_library_needs_approval_before_tests(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app(python_runner=uv)
    check = client.post("/api/tools/check", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS}).json()
    assert not check["ok"] and check["dependencies_pending"]
    assert [error["code"] for error in check["errors"]] == ["deps.approval_needed"]
    assert uv.syncs == 0  # nothing installed without approval
    saved = client.post("/api/tools", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS}).json()
    assert saved["tool"] is None

    plan = client.post("/api/python/tools/join_cells/plan", json={"code": TOOL, "tests": TESTS}).json()
    packages = {item["key"]: item for item in plan["packages"]}
    assert plan["needs_approval"]
    assert packages["tabulate"]["status"] == "new" and packages["tabulate"]["version"] == "0.9.0"
    assert packages["tabulate"]["direct"] and packages["tabulate"]["required_by"] == ["join_cells"]
    assert packages["wcwidth"]["status"] == "new" and not packages["wcwidth"]["direct"]
    assert packages["wcwidth"]["required_by"] == ["tabulate"]
    assert packages["pydantic"]["status"] == "runtime"
    assert uv.syncs == 0

    env = _approve_tool(client)
    assert env["install"]["status"] == "installed", env
    assert [change["kind"] for change in env["history"]] == ["approved", "installed"]
    check = client.post("/api/tools/check", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS}).json()
    assert check["ok"], check
    saved = client.post("/api/tools", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS}).json()
    assert saved["tool"]["name"] == "join_cells"
    result = client.post("/api/tools/join_cells/run", json={"input": {"cells": ["x", "y"]}}).json()
    assert result == {"output": "x | y v0.9", "is_error": False}


def test_workflow_needs_its_own_approval_and_runs_in_its_environment(
    make_app: Callable[..., TestClient], uv: FakeUv
) -> None:
    client = make_app({"TABLE": [tool_use("join_cells", {"cells": ["a", "b"]}), text("fertig")]}, python_runner=uv)
    _approve_tool(client)
    assert client.post("/api/tools", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS}).json()["tool"]
    workflow_id = create(client, [agent_node("table", "TABLE", tools=["join_cells"])], [])
    workflow = client.get(f"/api/workflows/{workflow_id}").json()

    check = client.post(f"/api/workflows/check?workflow_id={workflow_id}", json=workflow).json()
    codes = {item["code"]: item for item in check["problems"]}
    assert codes["deps.approval_needed"]["level"] == "error" and not check["ok"]
    assert codes["deps.approval_needed"]["params"]["packages"] == "tabulate"
    blocked = client.post(f"/api/workflows/{workflow_id}/runs", json={"input": "x"})
    assert blocked.status_code == 400

    plan = client.post(f"/api/python/workflows/{workflow_id}/plan").json()
    tabulate = next(item for item in plan["packages"] if item["key"] == "tabulate")
    assert tabulate["status"] == "new" and tabulate["approved_elsewhere"] == ["join_cells"]
    client.post(f"/api/python/plans/{plan['id']}/approve")
    assert _wait_env(client, "workflow", workflow_id)["install"]["status"] == "installed"
    status = client.post(f"/api/python/workflows/{workflow_id}/status").json()
    assert status["state"] == "ready" and status["needed"][0]["required_by"] == ["join_cells"]

    run = start(client, workflow_id)
    assert run["status"] == "succeeded", run
    assert run["nodes"]["table"]["tool_calls"][0]["output"] == "a | b v0.9"

    # Approved and unchanged: no new approval, the plan has nothing new.
    again = client.post(f"/api/python/workflows/{workflow_id}/plan").json()
    assert not again["needs_approval"]
    assert {item["status"] for item in again["packages"]} <= {"approved", "runtime"}


def test_approved_versions_never_change_silently(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    uv.index["tabulate"] = uv.index["tabulate"][:1]  # only 0.8.0 exists when approving
    client = make_app(python_runner=uv)
    approved = {item["key"]: item["version"] for item in _approve_tool(client)["packages"]}
    assert approved["tabulate"] == "0.8.0"
    uv.index["tabulate"] = _index()["tabulate"]  # 0.9.0 is released
    status = client.post("/api/python/tools/join_cells/status", json={"code": TOOL, "tests": TESTS}).json()
    assert status["state"] == "ready"
    plan = client.post("/api/python/tools/join_cells/plan", json={"code": TOOL, "tests": TESTS}).json()
    assert next(item for item in plan["packages"] if item["key"] == "tabulate")["version"] == "0.8.0"

    # A changed requirement must be checked and confirmed again.
    newer = TOOL.replace("tabulate>=0.8", "tabulate>=0.9")
    status = client.post("/api/python/tools/join_cells/status", json={"code": newer, "tests": TESTS}).json()
    assert status["state"] == "approval_needed" and status["reasons"] == ["changed"]
    plan = client.post("/api/python/tools/join_cells/plan", json={"code": newer, "tests": TESTS}).json()
    tabulate = next(item for item in plan["packages"] if item["key"] == "tabulate")
    assert (
        tabulate["status"] == "changed" and tabulate["previous_version"] == "0.8.0" and tabulate["version"] == "0.9.0"
    )


def test_conflicts_and_unknown_packages_are_explained(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app(python_runner=uv)
    code = TOOL.replace('"tabulate>=0.8"', '"tabulate>=5"')
    plan = client.post("/api/python/tools/join_cells/plan", json={"code": code, "tests": TESTS}).json()
    assert plan["error"]["code"] == "deps.no_version" and plan["packages"] == []
    unknown = '# /// script\n# dependencies = ["nowhere-lib"]\n# ///\nimport nowhere_lib\n' + PLAIN
    plan = client.post("/api/python/tools/circle_area/plan", json={"code": unknown, "tests": ""}).json()
    assert plan["error"]["code"] == "deps.not_found" and plan["error"]["params"]["name"] == "nowhere-lib"
    unresolved = "import mystery\n" + PLAIN
    plan = client.post("/api/python/tools/circle_area/plan", json={"code": unresolved, "tests": ""}).json()
    assert plan["error"]["code"] == "deps.unresolved" and plan["issues"][0]["code"] == "deps.module_unknown"
    assert uv.syncs == 0


def test_failed_installation_keeps_the_old_environment(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app(python_runner=uv)
    assert _approve_tool(client)["install"]["status"] == "installed"
    uv.fail_sync = "error: Failed to fetch: https://pypi.org/simple/tabulate/"
    client.post("/api/python/environments/tool/join_cells/install")
    env = _wait_env(client, "tool", "join_cells")
    assert env["install"]["status"] == "failed" and env["install"]["error"]["code"] == "deps.network"
    # The previous environment is still there and still works.
    uv.fail_sync = None
    result = client.post("/api/tools/check", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS})
    assert result.json()["ok"]


def test_export_import_needs_new_approval_and_restores(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app(python_runner=uv)
    _approve_tool(client)
    client.post("/api/tools", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS})
    workflow_id = create(client, [agent_node("table", "TABLE", tools=["join_cells"])], [])
    plan = client.post(f"/api/python/workflows/{workflow_id}/plan").json()
    client.post(f"/api/python/plans/{plan['id']}/approve")
    _wait_env(client, "workflow", workflow_id)

    definition = client.get(f"/api/python/workflows/{workflow_id}/export").json()
    assert "approvals" not in definition and "tabulate==0.9.0" in definition["lock"]
    exported = {**client.get(f"/api/workflows/{workflow_id}").json(), "python": definition}
    imported = client.post("/api/workflows/import", json=exported).json()
    status = client.post(f"/api/python/workflows/{imported['id']}/status").json()
    assert status["state"] == "approval_needed"
    env = client.get(f"/api/python/environments/workflow/{imported['id']}").json()
    assert env["approvals"] == [] and env["history"][0]["kind"] == "imported"
    # The stored lock is the preference: the same versions come back.
    plan = client.post(f"/api/python/workflows/{imported['id']}/plan").json()
    assert next(item for item in plan["packages"] if item["key"] == "tabulate")["version"] == "0.9.0"

    copy = client.post(f"/api/workflows/{workflow_id}/duplicate").json()
    assert client.post(f"/api/python/workflows/{copy['id']}/status").json()["state"] == "approval_needed"


def test_approvals_belong_to_this_installation(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app(python_runner=uv)
    _approve_tool(client)
    data_dir = Path(client.app.state.nodestro.config.data_dir)  # type: ignore[attr-defined]
    (data_dir / "python" / "installation-id").write_text("another-computer", encoding="utf-8")
    status = client.post("/api/python/tools/join_cells/status", json={"code": TOOL, "tests": TESTS}).json()
    assert status["state"] == "approval_needed" and "other_installation" in status["reasons"]


def test_missing_environment_is_rebuilt_from_the_approved_lock(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app({"TABLE": [tool_use("join_cells", {"cells": ["a"]}), text("ok")]}, python_runner=uv)
    _approve_tool(client)
    client.post("/api/tools", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS})
    workflow_id = create(client, [agent_node("table", "TABLE", tools=["join_cells"])], [])
    plan = client.post(f"/api/python/workflows/{workflow_id}/plan").json()
    client.post(f"/api/python/plans/{plan['id']}/approve")
    _wait_env(client, "workflow", workflow_id)
    assert client.delete(f"/api/python/environments/workflow/{workflow_id}").status_code == 204
    status = client.post(f"/api/python/workflows/{workflow_id}/status").json()
    assert status["state"] == "install_needed"
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    check = client.post(f"/api/workflows/check?workflow_id={workflow_id}", json=workflow).json()
    assert any(item["code"] == "deps.install_pending" and item["level"] == "warning" for item in check["problems"])
    syncs = uv.syncs
    run = start(client, workflow_id)
    assert run["status"] == "succeeded" and uv.syncs == syncs + 1
    assert len(uv.compiles) == 2  # no new resolution: the approved lock was installed


def test_workflows_without_libraries_are_unchanged(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app({"CALC": [tool_use("calculator", {"expression": "6*7"}), text("42")]}, python_runner=uv)
    saved = client.post(
        "/api/tools",
        json={
            "function_name": "circle_area",
            "code": PLAIN,
            "tests": "from tool import circle_area\n\ndef test_a():\n    assert circle_area(1) > 3\n",
        },
    ).json()
    assert saved["tool"] is not None
    workflow_id = create(client, [agent_node("calc", "CALC", tools=["calculator", "circle_area"])], [])
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    check = client.post(f"/api/workflows/check?workflow_id={workflow_id}", json=workflow).json()
    assert not [item for item in check["problems"] if item["code"].startswith("deps.")]
    assert start(client, workflow_id)["status"] == "succeeded"
    assert uv.compiles == [] and uv.syncs == 0
    assert not (Path(client.app.state.nodestro.config.data_dir) / "python" / "envs").exists()  # type: ignore[attr-defined]


def test_docker_sandbox_and_missing_uv_are_reported(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app(python_runner=uv)
    client.put("/api/settings", json={"sandbox": "docker"})
    status = client.post("/api/python/tools/join_cells/status", json={"code": TOOL, "tests": TESTS}).json()
    assert status["state"] == "unsupported" and status["problem"]["code"] == "deps.docker_unsupported"
    client.put("/api/settings", json={"sandbox": "subprocess"})
    uv.binary = None
    status = client.post("/api/python/tools/join_cells/status", json={"code": TOOL, "tests": TESTS}).json()
    assert status["problem"]["code"] == "deps.uv_missing"


def test_maintenance_lists_and_cleans_orphaned_environments(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    client = make_app(python_runner=uv)
    _approve_tool(client)
    info = client.get("/api/python").json()
    assert info["uv_available"] and len(info["environments"]) == 1
    environment = info["environments"][0]
    assert environment["scope"] == "tool:join_cells" and environment["orphaned"]  # the tool was never saved
    assert environment["size_bytes"] > 0
    assert client.post("/api/python/cleanup").json() == {"removed": ["tool:join_cells"]}
    assert client.get("/api/python").json()["environments"] == []


# ---- tool generation and the architect ----------------------------------------------


def _proposal(code: str, tests: str = TESTS) -> Any:
    return parsed(lambda model: model(function_name="join_cells", description="Joins cells.", code=code, tests=tests))


def _wait_job(client: TestClient, job_id: str, *states: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/tools/jobs/{job_id}").json()
        if job["status"] in states:
            return job  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {job['status']}")


def test_generated_tool_never_gets_libraries_and_is_rewritten_without_them(
    make_app: Callable[..., TestClient], uv: FakeUv
) -> None:
    """Generated code is untrusted (ADR-0006) and the library environments are host processes: a tool job never
    resolves, approves or installs libraries; the Coding Agent is asked for a standard-library version."""
    plain_tool = TOOL.split("import tabulate\n")[1].replace(
        'tabulate.tabulate(cells) + " v" + tabulate.VERSION', '" | ".join(cells)'
    )
    client = make_app({"expert Python developer": [_proposal(TOOL), _proposal(plain_tool)]}, python_runner=uv)
    container = isolate(client)
    job = client.post("/api/tools/jobs", json={"requirement": "Zellen verbinden"}).json()
    job = _wait_job(client, job["id"], "awaiting_dependencies", "awaiting_approval", "failed")
    assert job["status"] == "awaiting_approval", job
    assert job["dependency_plan"] is None and job["approval"]["dependencies"] == []
    feedback = client.fake.messages.calls[-1]["messages"][-1]["content"]  # type: ignore[attr-defined]
    assert "Use only the standard library" in feedback
    assert uv.syncs == 0 and uv.builds == []
    assert container.calls == [("validate", "join_cells")]  # only the standard-library version ran
    client.post(f"/api/tools/jobs/{job['id']}/decision", json={"approved": True})
    assert _wait_job(client, job["id"], "approved", "failed")["status"] == "approved"


def test_untrusted_code_with_libraries_is_refused_before_anything_is_installed(
    make_app: Callable[..., TestClient], uv: FakeUv
) -> None:
    client = make_app(python_runner=uv)
    isolate(client)
    nodestro = client.app.state.nodestro  # type: ignore[attr-defined]
    from nodestro_runtime.python_tools import check_with_dependencies

    result = check_with_dependencies(nodestro.tools, nodestro.python, TOOL, TESTS, "join_cells", trust="untrusted")
    assert not result.ok and result.errors[-1].code == "sandbox.libraries_need_isolation"
    assert uv.syncs == 0 and uv.builds == []
    assert client.get("/api/python").json()["environments"] == []


def test_architect_drafts_may_use_libraries_without_installing(uv: FakeUv) -> None:
    from nodestro_api.architect import _static_errors

    assert _static_errors(TOOL, TESTS, "join_cells", set()) == []
    errors = _static_errors("import mystery\n" + PLAIN, "", "circle_area", set())
    assert "deps.module_unknown" in [item.code for item in errors]
    assert uv.syncs == 0


@pytest.mark.skipif(not os.environ.get("NODESTRO_NETWORK_TESTS"), reason="set NODESTRO_NETWORK_TESTS=1 (uses PyPI)")
def test_real_uv_installs_a_package_from_pypi(make_app: Callable[..., TestClient]) -> None:  # pragma: no cover
    assert shutil.which("uv") or __import__("uv")
    client = make_app()
    code = '# /// script\n# dependencies = ["six==1.17.0"]\n# ///\nimport six\n' + PLAIN.replace(
        "return math.pi", "return 0.0 * len(six.PY3 * 'x') + math.pi"
    )
    tests = "from tool import circle_area\n\ndef test_a():\n    assert circle_area(1) > 3\n"
    plan = client.post("/api/python/tools/circle_area/plan", json={"code": code, "tests": tests}).json()
    assert plan["error"] is None, plan
    assert any(item["key"] == "six" and item["version"] == "1.17.0" for item in plan["packages"])
    client.post(f"/api/python/plans/{plan['id']}/approve")
    assert _wait_env(client, "tool", "circle_area")["install"]["status"] == "installed"
    check = client.post("/api/tools/check", json={"function_name": "circle_area", "code": code, "tests": tests})
    assert check.json()["ok"], check.json()


def test_published_api_and_export_keep_the_approved_libraries(make_app: Callable[..., TestClient], uv: FakeUv) -> None:
    from test_published_api import PUBLISHED, auth, finish, new_key

    client = make_app({"TABLE": [tool_use("join_cells", {"cells": ["a", "b"]}), text("fertig")]}, python_runner=uv)
    _approve_tool(client)
    client.post("/api/tools", json={"function_name": "join_cells", "code": TOOL, "tests": TESTS})
    workflow_id = create(client, [agent_node("table", "TABLE", tools=["join_cells"])], [], name="Tabelle")
    blocked = client.post(f"/api/workflows/{workflow_id}/api/check", json={}).json()
    assert not blocked["ok"] and "deps.approval_needed" in {item["code"] for item in blocked["problems"]}
    plan = client.post(f"/api/python/workflows/{workflow_id}/plan").json()
    client.post(f"/api/python/plans/{plan['id']}/approve")
    _wait_env(client, "workflow", workflow_id)
    published = client.post(f"/api/workflows/{workflow_id}/api/publish", json={}).json()
    publication = published["publication"]
    bundle = client.get(f"/api/publications/{publication['id']}/releases/1").json()["bundle"]
    assert bundle["python"][0]["requirements"] == ["tabulate>=0.8"]
    assert "tabulate==0.9.0" in bundle["python"][0]["lock"]
    token = new_key(client, publication["id"])
    run_id = client.post(f"{PUBLISHED}/tabelle/runs", json={"input": "x"}, headers=auth(token)).json()["run_id"]
    assert finish(client, "tabelle", token, run_id)["status"] == "succeeded"
    assert client.get(f"/api/runs/{run_id}").json()["nodes"]["table"]["tool_calls"][0]["output"] == "a | b v0.9"
    check = client.post(f"/api/publications/{publication['id']}/export/check", json={"options": {}}).json()
    assert check["tool_libraries"] == ["tabulate==0.9.0", "wcwidth==0.2.0"]
    assert "requirements-tools.lock" in check["files"]
    assert {"tabulate==0.9.0", "wcwidth==0.2.0"} <= set(check["dependencies"])
