"""An exported workflow with the portable web interface and a scripted model, for the browser test of the result folder.

Usage: python tests/webui_output_server.py --dir DIR [--port 9910] [--opener]

Builds the project like the tests do (Nodestro with the deterministic double, a workflow whose result node writes a
Markdown file, an export with the web interface), then serves it like a portable package's launcher: the operator's
one-time link, the settings page and the result folder (:class:`nodestro_api.output_folder.OutputFolder`). Prints one
JSON line (``launch``, ``project``, ``data``, ``default``) and serves until stopped. ``--opener`` records "Open folder"
in ``DIR/opened.txt`` instead of starting a file manager (a test computer has none). Nothing calls a provider.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import secrets
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient
from nodestro_runtime.testing import ScriptedClient, text
import uvicorn

from nodestro_api.app import create_app as create_studio
from nodestro_api.context import AppConfig

ANSWER = "# Bericht\n\nDie Ergebnisse liegen im gewählten Ordner."


def build(work: Path) -> Path:
    """Export a small workflow with a result file and the web interface into ``work/export``."""
    from nodestro_runtime.schemas import DEFAULT_AGENT_MODEL, DEFAULT_ASSISTANT_MODEL, SettingsIn

    fake = ScriptedClient({"WRITER": [text(ANSWER)] * 5})
    studio = create_studio(
        AppConfig(data_dir=work / "studio"), client_factory=lambda _p: fake, integrations=[], scheduler=False
    )
    studio.state.nodestro.update_settings(
        SettingsIn(default_model=DEFAULT_AGENT_MODEL, assistant_model=DEFAULT_ASSISTANT_MODEL)
    )
    with TestClient(studio, base_url="http://127.0.0.1") as client:
        nodes = [
            {"id": "start", "type": "start"},
            {
                "id": "writer",
                "type": "agent",
                "label": "Writer",
                "agent": {"name": "Writer", "system_prompt": "WRITER"},
            },
            {"id": "out", "type": "output", "output": {"files": [{"format": "md", "name": "Bericht"}]}},
        ]
        edges = [
            {"id": "e1", "source": "start", "target": "writer"},
            {"id": "e2", "source": "writer", "target": "out"},
        ]
        saved = client.post("/api/workflows", json={"name": "Quartalsbericht", "nodes": nodes, "edges": edges})
        assert saved.status_code in (200, 201), saved.text
        workflow_id = saved.json()["id"]
        published = client.post(f"/api/workflows/{workflow_id}/api/publish", json={})
        assert published.status_code == 201, published.text
        publication = published.json()["publication"]
        exported = client.post(
            f"/api/publications/{publication['id']}/exports", json={"options": {"web_ui": True, "language": "de"}}
        )
        assert exported.status_code == 201, exported.text
        archive = client.get(f"/api/exports/{exported.json()['id']}/download").content
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        root = package.namelist()[0].split("/")[0]
        package.extractall(work / "export")
    return work / "export" / root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True)
    parser.add_argument("--port", type=int, default=9910)
    parser.add_argument("--opener", action="store_true")
    args = parser.parse_args()
    work = Path(args.dir).resolve()
    project = build(work)

    from nodestro_api.output_folder import OutputFolder
    from nodestro_api.standalone import StandaloneSettings, create_app
    from nodestro_api.webui_server import UiOptions, WebUi

    data = project / "data"
    launch = secrets.token_urlsafe(16)

    def record(folder: Path) -> None:
        with (work / "opened.txt").open("a", encoding="utf-8") as log:
            log.write(f"{folder}\n")

    def web_ui(host: object, _core: object) -> WebUi:
        output = OutputFolder(
            host,  # type: ignore[arg-type]
            data_dir=data,
            project_dir=project,
            opener=record if args.opener else None,
        )
        ui = WebUi(host, UiOptions(language="de", control_token="control"), output=output)  # type: ignore[arg-type]
        ui.sessions.add_launch_token(launch)
        return ui

    fake = ScriptedClient({"WRITER": [text(ANSWER)] * 50})
    settings = StandaloneSettings.from_env({"DATA_DIR": str(data), "OUTPUT_DIR": "results", "API_KEYS": ""})
    app = create_app(project, settings=settings, client_factory=lambda _p: fake, web_ui=web_ui)
    base = f"http://127.0.0.1:{args.port}"
    info = {
        "launch": f"{base}/ui/launch?token={launch}",
        "base": base,
        "project": str(project),
        "data": str(data),
        "default": str((project / "results").resolve()),
    }
    print(json.dumps(info), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
