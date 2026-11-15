"""NAL-C3: the platform pre-check of a portable package's libraries and the support matrix."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from fastapi.testclient import TestClient
import pytest
from test_portable import published  # noqa: F401 - the fixture

from nodestro_api import portable_build

ROOT = Path(__file__).resolve().parent.parent
#: The job names of .github/workflows/portable.yml and the platform each builds and starts.
SMOKE_JOBS = {
    "Linux x64": "linux-x86_64",
    "Linux ARM64": "linux-arm64",
    "Windows x64": "windows-x86_64",
    "Windows ARM64": "windows-arm64",
    "macOS Apple Silicon": "macos-arm64",
    "macOS Intel": "macos-x86_64",
}
#: uv 0.11's answer for a library without a wheel for the platform (wrapped as uv prints it).
NO_WHEEL = """  x No solution found when resolving dependencies:
  `-> Because cryptography==46.0.5 has no usable wheels and you require
      cryptography==46.0.5, we can conclude that your requirements are unsatisfiable.

hint: Wheels are required for `cryptography` because building from source is disabled for all packages"""
OFFLINE = """  `-> Because cryptography was not found in the cache and you require
      cryptography==46.0.5, we can conclude that your requirements are
      unsatisfiable.

hint: Packages were unavailable because the network was disabled."""


def test_every_platform_has_a_row_and_ci_does_what_the_matrix_says() -> None:
    support = portable_build.platform_support()
    assert set(support) == set(portable_build.PLATFORMS)
    workflow = (ROOT / ".github" / "workflows" / "portable.yml").read_text("utf-8")
    jobs = set(re.findall(r'^\s+- \{ name: "([^"]+)"', workflow, re.MULTILINE))
    assert jobs == set(SMOKE_JOBS), "name the new smoke job in SMOKE_JOBS and platform-support.json"
    smoked = {SMOKE_JOBS[name] for name in jobs}
    assert {key for key, row in support.items() if row.ci == "built_and_started"} == smoked


def test_the_docs_explain_every_limit() -> None:
    docs = (ROOT / "docs" / "portable-export.md").read_text("utf-8")
    limits = {limit for row in portable_build.platform_support().values() for limit in row.limits}
    for limit in limits:
        assert f"`{limit}`" in docs, limit


class _Uv:
    """Stands in for ``uv pip compile``: answers with ``returncode`` and ``output``, counts the calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, returncode: int = 0, output: str = "") -> None:
        self.returncode, self.output, self.calls = returncode, output, []
        monkeypatch.setattr("nodestro_runtime.python_envs.find_uv", lambda: "uv")
        monkeypatch.setattr(portable_build.subprocess, "run", self.run)

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, self.returncode, "", self.output)


def _check(client: TestClient, publication: dict[str, Any], **options: Any) -> dict[str, Any]:
    body = {"options": {"kind": "portable", "platform": "macos-x86_64", "confirm_distribution": True, **options}}
    return client.post(f"/api/publications/{publication['id']}/export/check", json=body).json()


def _precheck(client: TestClient, publication: dict[str, Any], **options: Any) -> Any:
    body = {"options": {"kind": "portable", "platform": "macos-x86_64", **options}}
    return client.post(f"/api/publications/{publication['id']}/export/libraries", json=body)


def test_a_missing_wheel_is_reported_before_the_build(
    published: Callable[..., tuple[TestClient, dict[str, Any]]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studio, publication = published()
    assert _check(studio, publication)["portable"]["libraries"]["status"] == "unchecked"
    uv = _Uv(monkeypatch, 1, NO_WHEEL)
    answer = _precheck(studio, publication)
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "unavailable" and answer.json()["libraries"] == ["cryptography"]
    command, env = uv.calls[0]
    assert command[command.index("--python-platform") + 1] == "x86_64-apple-darwin"
    assert ":all:" in command and "UV_OFFLINE" not in env
    # Remembered: the check reports it as an error, and a second pre-check does not run uv again.
    checked = _check(studio, publication)
    problem = next(item for item in checked["problems"] if item["code"] == "export.libraries_unavailable")
    assert problem["level"] == "error"
    assert problem["params"] == {"platform": "macos-x86_64", "libraries": "cryptography"}
    assert not checked["ok"]
    assert _precheck(studio, publication).json()["status"] == "unavailable" and len(uv.calls) == 1
    # Another platform is its own question.
    uv.returncode, uv.output = 0, ""
    assert _precheck(studio, publication, platform="linux-x86_64").json()["status"] == "ok"
    linux = _check(studio, publication, platform="linux-x86_64")
    assert linux["portable"]["libraries"]["status"] == "ok"
    assert "export.libraries_unavailable" not in {item["code"] for item in linux["problems"]}


def test_a_check_that_cannot_decide_is_not_remembered(
    published: Callable[..., tuple[TestClient, dict[str, Any]]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without downloads uv reads only its cache: a library that is not there says nothing about the platform."""
    studio, publication = published()
    uv = _Uv(monkeypatch, 1, OFFLINE)
    answer = _precheck(studio, publication, allow_downloads=False).json()
    assert answer["status"] == "unchecked" and "network" in answer["detail"]
    assert uv.calls[0][1]["UV_OFFLINE"] == "1"
    assert _precheck(studio, publication, allow_downloads=False).json()["status"] == "unchecked"
    assert len(uv.calls) == 2
    assert "export.libraries_unavailable" not in {item["code"] for item in _check(studio, publication)["problems"]}


def test_without_uv_or_in_time_the_check_stays_open(
    published: Callable[..., tuple[TestClient, dict[str, Any]]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studio, publication = published()
    monkeypatch.setattr("nodestro_runtime.python_envs.find_uv", lambda: None)
    assert _precheck(studio, publication).json()["status"] == "unchecked"
    monkeypatch.setattr("nodestro_runtime.python_envs.find_uv", lambda: "uv")

    def slow(command: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(portable_build.subprocess, "run", slow)
    assert _precheck(studio, publication).json()["status"] == "unchecked"


def test_the_pre_check_needs_a_known_platform(
    published: Callable[..., tuple[TestClient, dict[str, Any]]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studio, publication = published()
    _Uv(monkeypatch)
    answer = _precheck(studio, publication, platform="plan9-mips")
    assert answer.status_code == 400 and answer.json()["code"] == "export.platform_unknown"


def test_the_check_names_the_support_of_every_platform(
    published: Callable[..., tuple[TestClient, dict[str, Any]]],  # noqa: F811
) -> None:
    studio, publication = published()
    platforms = {item["id"]: item for item in _check(studio, publication)["portable"]["platforms"]}
    assert platforms["windows-arm64"]["support"]["limits"] == ["unsigned", "gemini", "vector_search", "self_signed_tls"]
    assert platforms["linux-x86_64"]["support"] == {
        "ci": "built_and_started",
        "local_models": "checked",
        "human": False,
        "limits": [],
    }


def test_the_real_uv_tells_a_missing_wheel_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The words the pre-check reads in uv's answer: a library with source packages only, from a local index."""
    from nodestro_runtime.python_envs import find_uv

    if find_uv() is None:
        pytest.skip("uv is not available")
    index = tmp_path / "index"
    index.mkdir()
    (index / "onlysource-1.0.tar.gz").write_bytes(b"")
    work = tmp_path / "work"
    work.mkdir()
    (work / "requirements.in").write_text("onlysource==1.0\n", encoding="utf-8")
    done = subprocess.run(
        [
            str(find_uv()),
            "pip",
            "compile",
            "requirements.in",
            "--no-index",
            "--find-links",
            str(index),
            "--python-platform",
            "x86_64-apple-darwin",
            "--python-version",
            portable_build.python_minor(),
            "--only-binary",
            ":all:",
            "--quiet",
        ],
        cwd=work,
        capture_output=True,
        text=True,
        env={
            "UV_NO_CONFIG": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_CACHE_DIR": str(tmp_path / "cache"),
            "PATH": os.environ.get("PATH", ""),
        },
        check=False,
    )
    output = " ".join((done.stdout + done.stderr).split())
    assert done.returncode != 0
    assert any(words in output for words in portable_build._NO_WHEEL), output
    assert portable_build._NAMED.findall(output) == ["onlysource"], output
