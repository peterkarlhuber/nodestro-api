"""Ready-to-use examples for a published API: curl, Python, requests and answers.

Generated deterministically from a release (no AI request). The studio shows them in the
*API & Export* dialog; an exported project gets the same ones in its README and ``examples/``.
"""

from __future__ import annotations

import json
from typing import Any

from nodestro_runtime.engine import start_fields
from nodestro_runtime.file_formats import accept_extensions
from pydantic import BaseModel

from .serving import PREFIX, ServedRelease, example_values

KEY_VARIABLE = "NODESTRO_API_KEY"


class ApiExamples(BaseModel):
    base_url: str
    endpoints: list[dict[str, str]]
    request: dict[str, Any]
    curl: dict[str, str]
    python: str
    responses: dict[str, Any]


def endpoints(slug: str) -> list[dict[str, str]]:
    base = f"{PREFIX}/{slug}"
    return [
        {"method": "GET", "path": base, "scope": "read", "purpose": "info"},
        {"method": "POST", "path": f"{base}/runs", "scope": "run", "purpose": "start"},
        {"method": "POST", "path": f"{base}/versions/{{number}}/runs", "scope": "run", "purpose": "start_version"},
        {"method": "GET", "path": f"{base}/runs", "scope": "read", "purpose": "list"},
        {"method": "GET", "path": f"{base}/runs/{{run_id}}", "scope": "read", "purpose": "status"},
        {"method": "GET", "path": f"{base}/runs/{{run_id}}/result", "scope": "read", "purpose": "result"},
        {"method": "GET", "path": f"{base}/runs/{{run_id}}/events", "scope": "read", "purpose": "events"},
        {"method": "POST", "path": f"{base}/runs/{{run_id}}/cancel", "scope": "run", "purpose": "cancel"},
        {"method": "GET", "path": f"{base}/runs/{{run_id}}/approvals", "scope": "read", "purpose": "approvals"},
        {"method": "POST", "path": f"{base}/runs/{{run_id}}/approve", "scope": "approve", "purpose": "approve"},
        {"method": "POST", "path": f"{base}/runs/{{run_id}}/resume", "scope": "run", "purpose": "resume"},
        {"method": "GET", "path": f"{base}/runs/{{run_id}}/files", "scope": "read", "purpose": "files"},
        {"method": "GET", "path": f"{base}/runs/{{run_id}}/files/{{file_id}}", "scope": "read", "purpose": "file"},
        {"method": "GET", "path": f"{base}/usage", "scope": "read", "purpose": "usage"},
        {"method": "GET", "path": f"{base}/openapi.json", "scope": "read", "purpose": "openapi"},
    ]


def python_client(base_url: str, slug: str, request: dict[str, Any]) -> str:
    body = json.dumps(request, indent=4, ensure_ascii=False).replace("\n", "\n    ")
    return f'''"""Start a run, wait for it (deciding approvals if asked) and print the result."""

import json
import os
import time
import urllib.request

BASE = "{base_url}{PREFIX}/{slug}"
KEY = os.environ["{KEY_VARIABLE}"]


def call(method, path, body=None):
    request = urllib.request.Request(
        BASE + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={{"Authorization": f"Bearer {{KEY}}", "Content-Type": "application/json"}},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


run = call(
    "POST",
    "/runs",
    {body},
)
print("started", run["run_id"], run["status"])
while run["status"] in ("queued", "running", "awaiting_approval"):
    if run["status"] == "awaiting_approval":
        for approval in run["approvals"]:
            print("waiting for approval:", approval["step"], approval["review"])
            # Decide here (needs a key with the "approve" permission) or in Nodestro:
            # call("POST", f"/runs/{{run['run_id']}}/approve",
            #      {{"approval_id": approval["approval_id"], "decision": "approved", "comment": "ok"}})
    time.sleep(2)
    run = call("GET", f"/runs/{{run['run_id']}}")
result = call("GET", f"/runs/{{run['run_id']}}/result")
print(result["status"], json.dumps(result["output"], indent=2, ensure_ascii=False))
print("cost (USD):", result["usage"]["estimated_cost_usd"])
'''


def _multipart_example(release: ServedRelease, url: str, auth: str, request: dict[str, Any]) -> str | None:
    """Starting a run with files as ``multipart/form-data`` (only for forms with file fields)."""
    file_fields = [field for field in start_fields(release.workflow) if field.type == "file"]
    if not file_fields:
        return None
    others = {
        key: value for key, value in request.get("values", {}).items() if key not in {f.name for f in file_fields}
    }
    parts = [f"  -F 'values={json.dumps(others, ensure_ascii=False)}'"] if others else []
    for field in file_fields:
        extensions = accept_extensions(field.accept) if field.accept else []
        extension = ".pdf" if not extensions or ".pdf" in extensions else extensions[0]
        count = 2 if field.multiple else 1
        parts += [f'  -F "{field.name}=@{field.name}_{index + 1}{extension}"' for index in range(count)]
    return f"curl -s -X POST {url}/runs \\\n  {auth} \\\n" + " \\\n".join(parts)


def examples(release: ServedRelease, base_url: str) -> ApiExamples:
    slug = release.slug
    request = example_values(release.workflow)
    url = f"{base_url}{PREFIX}/{slug}"
    data = json.dumps(request, ensure_ascii=False)
    auth = f'-H "Authorization: Bearer ${KEY_VARIABLE}"'
    curl = {
        "start": f'curl -s -X POST {url}/runs \\\n  {auth} \\\n  -H "Content-Type: application/json" \\\n'
        f"  -H \"Idempotency-Key: order-4711\" \\\n  -d '{data}'",
        "status": f"curl -s {url}/runs/RUN_ID \\\n  {auth}",
        "result": f"curl -s {url}/runs/RUN_ID/result \\\n  {auth}",
        "events": f"curl -N {url}/runs/RUN_ID/events \\\n  {auth}",
        "approve": f"curl -s -X POST {url}/runs/RUN_ID/approve \\\n  {auth} \\\n"
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"decision": "approved", "comment": "Checked."}\'',
        "cancel": f"curl -s -X POST {url}/runs/RUN_ID/cancel \\\n  {auth}",
    }
    upload = _multipart_example(release, url, auth, request)
    if upload:
        curl["upload"] = upload
    if release.limits.sync_enabled:
        curl["sync"] = (
            f'curl -s -X POST "{url}/runs?wait={release.limits.sync_timeout_seconds}" \\\n  {auth} \\\n'
            f"  -H \"Content-Type: application/json\" \\\n  -d '{data}'"
        )
    started = {
        "run_id": "run_3f2a9c1b7d4e",
        "status": "queued",
        "publication": slug,
        "version": release.number,
        "created_at": "2026-01-31T09:00:00Z",
        "usage": {"requests": 0, "total_tokens": 0, "estimated_cost_usd": 0.0},
        "links": {"self": f"{PREFIX}/{slug}/runs/run_3f2a9c1b7d4e"},
    }
    responses = {
        "started": started,
        "awaiting_approval": {
            **started,
            "status": "awaiting_approval",
            "approvals": [
                {
                    "approval_id": "run_3f2a9c1b7d4e:node_review",
                    "step": "Review",
                    "instructions": "Check the draft before it is sent.",
                    "review": [{"label": "Writer", "value": "..."}],
                }
            ],
        },
        "result": {
            "run_id": "run_3f2a9c1b7d4e",
            "status": "succeeded",
            "output": "...",
            "files": [],
            "usage": {"requests": 3, "total_tokens": 5120, "estimated_cost_usd": 0.0213},
        },
        "invalid": {
            "error": {
                "status": 422,
                "code": "request.invalid",
                "message": "The request does not fit the workflow's input",
                "params": {"errors": [{"field": "values.customer", "code": "missing", "message": "Field required"}]},
            }
        },
        "unauthorized": {
            "error": {
                "status": 401,
                "code": "auth.invalid",
                "message": "The API key is invalid or was revoked",
                "params": {},
            }
        },
        "limited": {
            "error": {
                "status": 429,
                "code": "limit.concurrent",
                "message": "Too many runs at the same time; try again when one has finished",
                "params": {"limit": release.limits.max_concurrent_runs},
            }
        },
        "events": 'id: 4\nevent: node_finished\ndata: {"type": "node_finished", "node_id": "node_writer", '
        '"status": "succeeded"}\n\nevent: end\ndata: {"status": "succeeded"}',
    }
    return ApiExamples(
        base_url=base_url,
        endpoints=endpoints(slug),
        request=request,
        curl=curl,
        python=python_client(base_url, slug, request),
        responses=responses,
    )
