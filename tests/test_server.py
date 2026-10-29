"""The API on its own: started without the studio, with or without documentation."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient

from nodestro_api.app import create_app
from nodestro_api.cli import main, parser
from nodestro_api.context import AppConfig


def test_the_api_serves_no_interface(tmp_path: Path) -> None:
    app = create_app(AppConfig(data_dir=tmp_path / "data"), integrations=[], scheduler=False)
    with TestClient(app) as client:
        assert client.get("/api/meta").status_code == 200
        assert client.get("/api/docs").status_code == 200
        assert client.get("/").status_code == 404
        assert client.get("/workflows").status_code == 404


def test_the_assistant_reads_the_documentation_it_is_given(tmp_path: Path) -> None:
    chapters = tmp_path / "docs" / "en"
    chapters.mkdir(parents=True)
    (chapters / "start.md").write_text(
        "# Getting started\n\nIntro.\n\n## Approvals {#approvals}\n\nA person decides before the run goes on.\n",
        encoding="utf-8",
    )
    config = AppConfig(data_dir=tmp_path / "data", docs_dir=tmp_path / "docs")
    with TestClient(create_app(config, integrations=[], scheduler=False)) as client:
        hits = client.get("/api/assistant/docs/search", params={"q": "approvals", "language": "en"}).json()
        assert [hit["link"] for hit in hits] == ["/docs/start#approvals"]
    with TestClient(create_app(AppConfig(data_dir=tmp_path / "other"), integrations=[], scheduler=False)) as client:
        assert client.get("/api/assistant/docs/search", params={"q": "approvals", "language": "en"}).json() == []


def test_the_command_line_has_the_options_of_nodestro() -> None:
    args = parser("nodestro-api", "").parse_args(["--host", "0.0.0.0", "--port", "9000", "--data-dir", "x", "--reload"])
    assert (args.host, args.port, args.data_dir, args.reload) == ("0.0.0.0", 9000, "x", True)
    shown = subprocess.run(
        [sys.executable, "-c", "from nodestro_api.cli import main; main(['--help'])"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert shown.returncode == 0 and "--data-dir" in shown.stdout
    assert callable(main)


def test_the_api_does_not_import_the_studio() -> None:
    code = (
        "import sys, nodestro_api.app\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in ('nodestro_studio', 'nodestro', 'claude_agent')]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
