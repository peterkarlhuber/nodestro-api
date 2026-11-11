"""AC-3 (TABRION QA-18): one blocked-module test per optional edge of nodestro-api's desktop path (K-33, document 13).

Document 07 (AC-3): "the nodestro-api desktop path without axentra/psycopg". The ``server`` extra brings
``nodestro-runtime[postgres]`` (with Axentra) and psycopg for ``nodestro-server``; the desktop and Nodestro Local
Docker never install it. So the desktop path (``nodestro_api.cli``, ``nodestro_api.local_container`` and the app of
``nodestro_api.app.create_app``, started and asked for ``/api/health``) must work with either module blocked:

* ``axentra``: the platform services of the server profile;
* ``psycopg``: the PostgreSQL driver (``psycopg``, ``psycopg_pool``).

Each edge runs once in a fresh ``python -I`` that blocks its modules (``sys.modules[name] = None``). The probe adds a
top-level import of the blocked module to a copy of the package: the same check must then fail. The meta test keeps
the edge list equal to these edges and to the ``server`` extra. (``nodestro-api → tabrion`` is a dev-only edge, no
optional one, so it has no blocked-module test here; AC-1/AC-2 check that kind of edge.)
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

#: Edge -> the modules it blocks.
EDGES: dict[str, tuple[str, ...]] = {
    "axentra": ("axentra",),
    "psycopg": ("psycopg", "psycopg_binary", "psycopg_c", "psycopg_pool"),
}
#: The optional edges of AC-3 for this repository.
AC3_EDGES = frozenset({"axentra", "psycopg"})
#: ``(file below src/nodestro_api, line)`` per edge: the mutation probe's change to a copy of the package.
PROBES = {"axentra": ("app.py", "import axentra"), "psycopg": ("app.py", "import psycopg")}


def check_source(blocked: tuple[str, ...]) -> str:
    """Block the modules, import the desktop entry points, start the desktop app and ask for its health."""
    tops = tuple(sorted({b.partition(".")[0] for b in blocked}))
    return (
        "import sys, tempfile\n"
        f"for _n in {blocked!r}:\n"
        "    sys.modules[_n] = None\n"
        "from pathlib import Path\n"
        "import nodestro_api.cli, nodestro_api.local_container\n"
        "from fastapi.testclient import TestClient\n"
        "from nodestro_api.app import create_app\n"
        "from nodestro_api.context import AppConfig\n"
        "with tempfile.TemporaryDirectory() as _d:\n"
        "    _app = create_app(AppConfig(data_dir=Path(_d)), integrations=[], scheduler=False)\n"
        "    with TestClient(_app, base_url='http://127.0.0.1') as _c:\n"
        "        assert _c.get('/api/health').json() == {'status': 'ok'}\n"
        f"_loaded = sorted(n for n in sys.modules if n.partition('.')[0] in {tops!r} and sys.modules[n])\n"
        "assert not _loaded, _loaded\n"
        "import nodestro_api\n"
        "print('desktop ok', nodestro_api.__file__)\n"
    )


def run_python(code: str, path: Path) -> subprocess.CompletedProcess[str]:
    """``python -I -c code`` (no PYTHONPATH, no cwd on sys.path), with ``path`` first on ``sys.path``."""
    code = f"import sys\nsys.path.insert(0, {str(path)!r})\n" + code
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "NODESTRO_"))}
    return subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=300, check=False
    )


@pytest.mark.parametrize("edge", sorted(EDGES))
def test_the_desktop_path_works_without_the_edge(edge: str) -> None:
    result = run_python(check_source(EDGES[edge]), SRC)
    assert result.returncode == 0, result.stderr[-4000:]
    assert f"desktop ok {SRC / 'nodestro_api' / '__init__.py'}" in result.stdout  # this checkout's package


def test_every_optional_edge_has_exactly_one_test() -> None:
    assert set(EDGES) == AC3_EDGES == set(PROBES)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    server = " ".join(project["optional-dependencies"]["server"]).replace(" ", "")
    base = " ".join(project["dependencies"]).replace(" ", "")
    # Axentra arrives with nodestro-runtime[postgres], psycopg directly; neither is a base dependency.
    assert "nodestro-runtime[postgres]" in server and "psycopg" in server
    assert "[postgres]" not in base and not re.search(r"(^|\W)(psycopg|axentra)\W", base)
    for file, _ in PROBES.values():
        assert (SRC / "nodestro_api" / file).is_file()


@pytest.mark.parametrize("edge", sorted(EDGES))
def test_a_top_level_import_turns_the_edge_red(tmp_path: Path, edge: str) -> None:
    """Mutation probe: a copy with the edge's module imported at the top of ``app.py`` fails the check."""
    target = tmp_path / "src"
    shutil.copytree(SRC / "nodestro_api", target / "nodestro_api", ignore=shutil.ignore_patterns("__pycache__"))
    file, line = PROBES[edge]
    path = target / "nodestro_api" / file
    anchor = "from __future__ import annotations\n"
    text = path.read_text(encoding="utf-8")
    assert anchor in text
    path.write_text(text.replace(anchor, anchor + "\n" + line + "\n", 1), encoding="utf-8")
    result = run_python(check_source(EDGES[edge]), target)
    assert result.returncode != 0, f"the probe {line!r} went unnoticed"
    assert "halted; None in sys.modules" in result.stderr  # failed on the block itself, not for another reason
    assert str(target) in result.stderr  # the copy was imported (the edge test on SRC is the positive control)
