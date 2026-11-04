"""Generated code of an export (backend audit round 3, API3-06): text a person or an AI wrote into a workflow
(its name) is written into generated Python only as escaped string content, never as code."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from test_export import _download, _export, _extract
from test_published_api import form_workflow, publish

from conftest import text


@pytest.mark.parametrize(
    "name",
    [
        '\'"""+__import__("pathlib").Path("MARKER").write_text("x").__str__()+"""',
        'x"""\nimport pathlib; pathlib.Path("MARKER").write_text("x")\n"""',
        'back\\slash \\""" end\\',
        "Angebot für ACME",
    ],
)
def test_the_workflow_name_never_becomes_code_of_the_export(
    make_app: Callable[..., TestClient], tmp_path: Path, name: str
) -> None:
    """The publication's name is written into the generated ``app/main.py`` (API3-06): it must stay text."""

    client = make_app({"WRITER": [text("ok")] * 3})
    workflow_id = form_workflow(client)
    workflow = client.get(f"/api/workflows/{workflow_id}").json()
    workflow["name"] = name
    assert client.put(f"/api/workflows/{workflow_id}", json=workflow).status_code == 200
    publication = publish(client, workflow_id)["publication"]
    assert publication["name"] == name
    project = _extract(_download(client, _export(client, publication["id"])["id"]), tmp_path / "export")
    tree = ast.parse((project / "app" / "main.py").read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree)
    assert docstring is not None and f'"{name}"' in docstring  # the escapes read back as the name
    # Nothing but the docstring, imports and the app's own statements.
    assert isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant)
    assert not any("MARKER" in ast.dump(node) for node in tree.body[1:])
