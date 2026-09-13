"""Static UI serving (DECISIONS.md, Phase 3 gate 1): SPA fallthrough and cache headers."""

from __future__ import annotations

import httpx2
import pytest

import manifold.toolsets
import tests.fixtures.toolsets
from manifold.config.settings import Settings

PACKAGES = (manifold.toolsets, tests.fixtures.toolsets)


@pytest.fixture
async def ui(env, tmp_path):
    from manifold.app import create_app

    dist = tmp_path / "dist"
    (dist / "_astro").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Manifold</title><div id=app></div>")
    (dist / "_astro" / "app.abc123.js").write_text("console.log('hi')")
    (dist / "favicon.svg").write_text("<svg/>")
    env["MANIFOLD_DATA_DIR"] = str(tmp_path / "data")
    app = create_app(
        Settings.from_env(env), reload_poll_seconds=100, ui_dir=dist, toolset_packages=PACKAGES
    )
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http


async def test_index_is_no_store(ui):
    r = await ui.get("/")
    assert r.status_code == 200 and "Manifold" in r.text
    assert r.headers["cache-control"] == "no-store"


async def test_hashed_assets_are_immutable(ui):
    r = await ui.get("/_astro/app.abc123.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "path", ["/toolsets/sheets", "/credentials/3", "/audit", "/settings", "/add-proxy"]
)
async def test_client_routes_fall_through_to_index(ui, path):
    r = await ui.get(path)
    assert r.status_code == 200
    assert "<div id=app>" in r.text
    assert r.headers["cache-control"] == "no-store"


async def test_missing_asset_is_404_not_index(ui):
    assert (await ui.get("/_astro/missing.js")).status_code == 404
    assert (await ui.get("/robots.txt")).status_code == 404


async def test_toolset_and_reserved_paths_still_win(ui):
    assert (await ui.post("/manifold")).status_code == 401
    assert (await ui.get("/api")).status_code == 404
    assert (await ui.get("/healthz")).status_code == 200


async def test_never_redirects(ui):
    for path in ("/", "/settings", "/settings/", "/toolsets/x/", "/_astro/", "/index.html"):
        r = await ui.get(path)
        assert not 300 <= r.status_code < 400, (path, r.status_code)
