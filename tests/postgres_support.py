"""PostgreSQL for the tests through ``tabrion.testing`` (TABRION MIG-NOD-1).

``NODESTRO_TEST_POSTGRES`` names the test database (a libpq connection string). Without it the PostgreSQL cases are
skipped, unless ``NODESTRO_REQUIRE_POSTGRES=1`` (CI), which turns the missing database into a failure. That rule
comes from ``tabrion.testing``: the plugin is active in ``conftest.py`` (``pytest_plugins``), :func:`configure_tabrion`
points it at Nodestro's variables, and :func:`require_postgres` resolves its session fixture ``tabrion_postgres_url``.
nodestro-runtime's ``tests/postgres_support.py`` has the same three names with the same behavior; keep them alike.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib.util
import os

import pytest

#: Whether ``tabrion.testing`` is installed (the ``dev`` extra). CI's platform jobs install without it and run no
#: PostgreSQL case; there :func:`require_postgres` skips (fails under ``NODESTRO_REQUIRE_POSTGRES=1``) by itself.
TABRION_TESTING = (
    importlib.util.find_spec("tabrion") is not None and importlib.util.find_spec("tabrion.testing") is not None
)

#: The pytest session's fixture request (:func:`tabrion_session`), through which :func:`require_postgres` resolves
#: ``tabrion.testing``'s session fixtures from plain functions.
_SESSION: pytest.FixtureRequest | None = None


def configure_tabrion() -> None:
    """Point ``tabrion.testing`` at Nodestro's variables (called by ``conftest.py`` in ``pytest_configure``).

    ``NODESTRO_TEST_POSTGRES``, ``NODESTRO_REQUIRE_POSTGRES`` and ``NODESTRO_TEST_RUNTIME_ROLE`` keep their names
    and meaning, so the CI jobs are unchanged. Anything the plugin's own fixtures create starts with
    ``nodestro_test_``, like the schemas of the tests. Imported here, not at module level, so the plugin module is
    first imported by pytest itself (``pytest_plugins``) and its assertions are rewritten.
    """
    if not TABRION_TESTING:
        return
    from tabrion.testing import configure

    configure(env_prefix="NODESTRO", schema_prefix="nodestro_test_")


@pytest.fixture(scope="session", autouse=True)
def tabrion_session(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keep the session's fixture request for :func:`require_postgres` (resolves nothing by itself)."""
    global _SESSION
    _SESSION = request
    try:
        yield
    finally:
        _SESSION = None


def require_postgres() -> str:
    """The test database, or skip (fail when PostgreSQL is required).

    Through ``tabrion.testing``'s session fixture ``tabrion_postgres_url``: without ``NODESTRO_TEST_POSTGRES`` the
    calling test skips, under ``NODESTRO_REQUIRE_POSTGRES=1`` it fails; the outcome is resolved once per session.
    """
    if not TABRION_TESTING:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but tabrion[testing] (the dev extra) is not installed")
        pytest.skip("PostgreSQL tests need the dev extra (tabrion[testing])")
    if _SESSION is None:
        raise RuntimeError("require_postgres() runs only inside a pytest session (conftest.py loads tabrion_session)")
    return str(_SESSION.getfixturevalue("tabrion_postgres_url"))
