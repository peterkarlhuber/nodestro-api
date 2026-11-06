"""The code examples of a published API are valid Python and quote every shell argument (API2-05)."""

from __future__ import annotations

import ast
from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
from test_published_api import agent_node, edge, publish, save

from nodestro_api.api_examples import examples
from nodestro_api.api_publish import _served

HOSTILE = 'it\'s $(touch PWNED-1) `touch PWNED-2`; touch PWNED-3 #\n"done"\\'


def _workflow(client: TestClient) -> str:
    start = {
        "id": "start",
        "type": "start",
        "start": {
            "fields": [
                {"name": "urgent", "label": "Urgent", "type": "boolean"},
                {"name": "note", "label": "Note", "type": "text", "default": HOSTILE},
                {"name": "count", "label": "Count", "type": "number"},
            ]
        },
    }
    return save(
        client,
        [start, agent_node("writer", "WRITER"), {"id": "out", "type": "output"}],
        [edge("start", "writer"), edge("writer", "out")],
        name="Examples",
        budget_usd=1.0,
    )


def _arguments_of(command: str, tmp_path: Path) -> list[str]:
    """Run ``command`` in a POSIX shell with ``curl`` replaced by a function that prints its arguments."""
    script = 'curl() { for arg in "$@"; do printf "%s\\0" "$arg"; done; }\nNODESTRO_API_KEY=k\n' + command + "\n"
    done = subprocess.run(
        ["sh", "-c", script], cwd=tmp_path, capture_output=True, check=True, env={"PATH": os.environ["PATH"]}
    )
    return done.stdout.decode().split("\0")[:-1]


def test_examples_survive_a_hostile_default(make_app: Callable[..., TestClient], tmp_path: Path) -> None:
    client = make_app({"WRITER": ["ok"] * 3})
    publication = publish(client, _workflow(client))["publication"]
    found = client.get(f"/api/publications/{publication['id']}/docs").json()["examples"]
    request = found["request"]
    assert request["values"]["note"] == HOSTILE and request["values"]["urgent"] is True
    # Python: compiles, and the body it sends is exactly the request (True, not true).
    tree = ast.parse(found["python"])
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "call" and len(node.args) == 3
    )
    assert ast.literal_eval(call.args[2]) == request
    assert not {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} & {"true", "false", "null"}
    # Shell: every example runs nothing it should not, and curl gets the JSON unchanged.
    for name, command in found["curl"].items():
        arguments = _arguments_of(command, tmp_path)
        assert arguments, name
    start = _arguments_of(found["curl"]["start"], tmp_path)
    assert json.loads(start[start.index("-d") + 1]) == request
    assert not list(tmp_path.glob("PWNED*"))


def test_a_hostile_base_url_is_quoted_too(make_app: Callable[..., TestClient], tmp_path: Path) -> None:
    client = make_app({"WRITER": ["ok"] * 3})
    publication = publish(client, _workflow(client))["publication"]
    ctx = client.app.state.nodestro  # type: ignore[attr-defined]
    _, served = _served(ctx, ctx.publications.get(publication["id"]), None)
    base = "http://h$(touch PWNED-4)'\""
    found = examples(served, base)
    ast.parse(found.python)
    path = "/api/published/" + publication["slug"]
    assert f'BASE = os.environ.get("NODESTRO_BASE_URL", {base!r}).rstrip("/") + {path!r}' in found.python
    arguments = _arguments_of(found.curl["status"], tmp_path)
    assert arguments[1] == f"{base}/api/published/{publication['slug']}/runs/RUN_ID"
    assert not list(tmp_path.glob("PWNED*"))


def test_the_python_client_reads_its_base_url_and_reports_the_error_code(
    make_app: Callable[..., TestClient], tmp_path: Path
) -> None:
    """API-053: ``NODESTRO_BASE_URL`` replaces the base URL (default: the one shown), and an HTTP error ends the
    script with the error code of the JSON body instead of a traceback."""
    import http.server
    import sys
    import threading

    client = make_app({"WRITER": ["ok"] * 3})
    publication = publish(client, _workflow(client))["publication"]
    script = client.get(f"/api/publications/{publication['id']}/docs").json()["examples"]["python"]
    assert 'os.environ.get("NODESTRO_BASE_URL", ' in script
    seen: list[str] = []

    class Refusing(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            seen.append(self.path)
            body = json.dumps({"error": {"status": 401, "code": "auth.invalid", "message": "Wrong key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refusing)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        path = tmp_path / "client.py"
        path.write_text(script)
        environment = {
            "PATH": os.environ["PATH"],
            "NODESTRO_API_KEY": "wrong",
            "NODESTRO_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}/",
        }
        done = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, env=environment, timeout=60)
    finally:
        server.shutdown()
    assert done.returncode == 1, done
    assert "HTTP 401 auth.invalid: Wrong key" in done.stderr and "Traceback" not in done.stderr
    assert seen == [f"/api/published/{publication['slug']}/runs"]
