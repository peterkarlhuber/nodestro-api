"""How the built studio is served next to the API (AX-21): gzip for its text files, long caching for hashed
assets, ``no-cache`` for ``index.html`` and the single-page fallback; API responses and event streams untouched.

The server profile (``create_tenant_app``) case needs ``NODESTRO_TEST_POSTGRES``; with
``NODESTRO_REQUIRE_POSTGRES=1`` (CI) a missing database fails instead of skipping.
"""

from __future__ import annotations

from collections.abc import Iterator
import gzip
import os
from pathlib import Path
import uuid

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import httpx
import pytest

from nodestro_api.frontend import CACHE_HASHED, CACHE_REVALIDATE, mount_frontend

GZIP = {"Accept-Encoding": "gzip"}
BUNDLE = "index-Abc123.js"
DSN = os.environ.get("NODESTRO_TEST_POSTGRES", "")


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    """A small stand-in for the studio's ``npm run build``: index.html, hashed assets and a public file."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        '<!doctype html><html><head><script type="module" src="/assets/index-Abc123.js"></script></head>'
        + "<body><div id='root'></div></body></html>\n" * 40
    )
    (static / "assets" / BUNDLE).write_text("export const greeting = 'Hello, world';\n" * 20_000)
    (static / "assets" / "index-Def456.css").write_text("body { margin: 0; color: #222; }\n" * 500)
    (static / "assets" / "tiny-Ghi789.js").write_text("export {};\n")
    (static / "assets" / "logo-Jkl012.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(4096))
    (static / "manifest.webmanifest").write_text('{"name": "Nodestro", "icons": []}' + " " * 2048)
    return static


@pytest.fixture
def client(static_dir: Path) -> Iterator[TestClient]:
    """An app with a large JSON route, an event stream and the studio mounted after them (as the profiles do)."""
    app = FastAPI()

    @app.get("/api/items")
    def items() -> list[dict[str, str]]:
        return [{"id": f"item_{number}", "name": "An item with a name long enough"} for number in range(200)]

    @app.get("/api/events")
    def events() -> StreamingResponse:
        def stream() -> Iterator[str]:
            for number in range(50):
                yield f'event: delta\ndata: {{"n": {number}, "text": "{"x" * 100}"}}\n\n'
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    mount_frontend(app, static_dir)
    with TestClient(app) as test_client:
        yield test_client


def test_a_large_bundle_is_gzipped_and_cached_for_a_year(client: TestClient, static_dir: Path) -> None:
    response = client.get(f"/assets/{BUNDLE}", headers=GZIP)
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["vary"]
    assert response.headers["cache-control"] == CACHE_HASHED == "public, max-age=31536000, immutable"
    assert response.content == (static_dir / "assets" / BUNDLE).read_bytes()
    assert response.num_bytes_downloaded < len(response.content) / 10
    css = client.get("/assets/index-Def456.css", headers=GZIP)
    assert css.headers["content-encoding"] == "gzip" and css.headers["cache-control"] == CACHE_HASHED
    # A client without gzip gets the file as it is, with the same caching.
    plain = client.get(f"/assets/{BUNDLE}", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.headers["cache-control"] == CACHE_HASHED
    assert int(plain.headers["content-length"]) == (static_dir / "assets" / BUNDLE).stat().st_size


def test_small_and_binary_assets_are_not_compressed(client: TestClient) -> None:
    for name in ("tiny-Ghi789.js", "logo-Jkl012.png"):
        response = client.get(f"/assets/{name}", headers=GZIP)
        assert response.status_code == 200
        assert "content-encoding" not in response.headers, name
        assert response.headers["cache-control"] == CACHE_HASHED


def test_a_revalidated_asset_keeps_its_caching_and_a_missing_one_gets_none(client: TestClient) -> None:
    etag = client.get(f"/assets/{BUNDLE}").headers["etag"]
    not_modified = client.get(f"/assets/{BUNDLE}", headers={**GZIP, "If-None-Match": etag})
    assert not_modified.status_code == 304 and not_modified.headers["cache-control"] == CACHE_HASHED
    # A chunk of an older build must not be cached for a year as a 404.
    missing = client.get("/assets/index-Old000.js", headers=GZIP)
    assert missing.status_code == 404 and "cache-control" not in missing.headers


@pytest.mark.parametrize("path", ["/", "/index.html", "/workflows/flow_1/edit", "/settings"])
def test_index_html_and_the_spa_fallback_are_revalidated(client: TestClient, static_dir: Path, path: str) -> None:
    response = client.get(path, headers=GZIP)
    assert response.status_code == 200
    assert response.headers["cache-control"] == CACHE_REVALIDATE == "no-cache"
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-encoding"] == "gzip"
    assert response.content == (static_dir / "index.html").read_bytes()
    assert "etag" in response.headers


def test_other_files_of_the_studio_are_compressed_and_revalidated(client: TestClient) -> None:
    response = client.get("/manifest.webmanifest", headers=GZIP)
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip" and response.headers["cache-control"] == CACHE_REVALIDATE


def test_api_responses_are_untouched(client: TestClient) -> None:
    response = client.get("/api/items", headers=GZIP)
    assert response.status_code == 200 and len(response.content) > 5_000
    assert "content-encoding" not in response.headers and "cache-control" not in response.headers
    assert "vary" not in response.headers
    for path in ("/api", "/api/nothing-here"):
        unknown = client.get(path, headers=GZIP)
        assert unknown.status_code == 404 and unknown.json() == {"detail": "Not found"}, path
        assert "cache-control" not in unknown.headers and "content-encoding" not in unknown.headers
    # Only GET (and HEAD) reach the studio, as before.
    assert client.head("/").status_code == 200
    assert client.post("/workflows").status_code == 405


def test_event_streams_are_never_compressed(client: TestClient) -> None:
    """gzip buffers and would hold back the events of a live stream (text/event-stream)."""
    with client.stream("GET", "/api/events", headers=GZIP) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in response.headers
        assert response.headers["cache-control"] == "no-cache"
        body = b"".join(response.iter_raw())
    assert body.startswith(b"event: delta") and body.endswith(b"event: end\ndata: {}\n\n")
    with pytest.raises(gzip.BadGzipFile):
        gzip.decompress(body)


def test_the_server_profile_serves_the_studio_compressed_behind_its_login(tmp_path: Path, static_dir: Path) -> None:
    """``create_tenant_app``: the same headers; logins, the JSON 404 and the same-origin check stay as they were."""
    if not DSN:
        if os.environ.get("NODESTRO_REQUIRE_POSTGRES") == "1":
            pytest.fail("NODESTRO_REQUIRE_POSTGRES=1 but NODESTRO_TEST_POSTGRES names no database")
        pytest.skip("NODESTRO_TEST_POSTGRES is not set")
    pytest.importorskip("psycopg")
    pytest.importorskip("jwt")
    from nodestro_runtime.identity_store import IdentityStore
    from nodestro_runtime.storage_postgres import PostgresDatabase
    import psycopg
    from psycopg import sql
    import stub_oidc

    from nodestro_api.identity import OidcClient, OidcSettings
    from nodestro_api.server_app import TenantComposition, create_tenant_app

    schema = f"nodestro_test_{uuid.uuid4().hex[:16]}"
    database = PostgresDatabase(DSN, schema=schema)
    try:
        database.migrate()
        provider = stub_oidc.StubProvider()
        oidc = OidcClient(
            OidcSettings(
                issuer=stub_oidc.ISSUER,
                client_id=stub_oidc.CLIENT_ID,
                client_secret=stub_oidc.CLIENT_SECRET,
                redirect_uri=stub_oidc.REDIRECT_URI,
            ),
            http=httpx.Client(transport=provider.transport()),
        )
        app = create_tenant_app(
            TenantComposition(
                database=database,
                identity=IdentityStore(database),
                oidc=oidc,
                origin=stub_oidc.ORIGIN,
                data_root=tmp_path / "server",
                integrations=[],
                static_dir=static_dir,
            )
        )
        with TestClient(app, base_url=stub_oidc.ORIGIN) as browser:
            browser.headers["Origin"] = stub_oidc.ORIGIN
            bundle = browser.get(f"/assets/{BUNDLE}", headers=GZIP)
            assert bundle.status_code == 200 and bundle.headers["content-encoding"] == "gzip"
            assert bundle.headers["cache-control"] == CACHE_HASHED
            page = browser.get("/workflows", headers=GZIP)
            assert page.status_code == 200 and page.headers["content-encoding"] == "gzip"
            assert page.headers["cache-control"] == CACHE_REVALIDATE
            assert page.content == (static_dir / "index.html").read_bytes()
            # The API keeps its login, its JSON answers and its headers.
            refused = browser.get("/api/workflows", headers=GZIP)
            assert refused.status_code == 401 and refused.json()["code"] == "access.login_required"
            assert "content-encoding" not in refused.headers and "cache-control" not in refused.headers
            assert browser.get("/api/nothing-here").status_code in (401, 404)
            health = browser.get("/api/health", headers=GZIP)
            assert health.status_code == 200 and "content-encoding" not in health.headers
            # The same-origin check still runs in front of everything.
            cross_site = browser.post("/api/workflows", json={}, headers={"Origin": "https://evil.example"})
            assert cross_site.status_code == 403 and cross_site.json()["code"] == "access.cross_site"
    finally:
        database.close()
        with psycopg.connect(DSN, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
