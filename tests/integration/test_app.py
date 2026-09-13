"""End to end over real uvicorn: raw HTTP for routing, the SDK client for MCP semantics."""

from __future__ import annotations

import httpx2
import pytest

from tests.oauth_helpers import obtain_tokens

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
async def http(live_server):
    async with httpx2.AsyncClient(base_url=live_server, follow_redirects=False) as c:
        yield c


@pytest.fixture
def bearer_for(http):
    async def _bearer(key: str) -> dict[str, str]:
        _, tokens = await obtain_tokens(http, key)
        return {**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}

    return _bearer


async def test_healthz(http):
    r = await http.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["toolsets"] == ["manifold", "ping-b"]


@pytest.mark.parametrize("key", ["manifold", "ping-b"])
async def test_toolset_healthz(http, key):
    r = await http.get(f"/{key}/healthz")
    assert r.status_code == 200
    assert r.json()["toolset"] == key
    assert r.json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/manifold", "/manifold/", "/ping-b", "/ping-b/"])
async def test_initialize_on_exact_and_slashed_paths(http, bearer_for, path):
    r = await http.post(path, json=INIT, headers=await bearer_for(path.strip("/")))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["result"]["serverInfo"]["name"] in ("manifold", "ping-b")


async def test_nothing_redirects(http):
    paths = [
        "/",
        "/healthz",
        "/healthz/",
        "/manifold",
        "/manifold/",
        "/manifold/healthz",
        "/manifold/other",
        "/ping-b",
        "/ping-b/",
        "/api",
        "/api/",
        "/oauth",
        "/unknown",
    ]
    for path in paths:
        for method in ("GET", "POST"):
            try:
                r = await http.request(method, path, headers=MCP_HEADERS, timeout=3)
            except httpx2.ReadTimeout:
                pytest.fail(f"{method} {path} hung")
            assert not 300 <= r.status_code < 400, (method, path, r.status_code)


@pytest.mark.parametrize("key", ["manifold", "ping-b"])
async def test_get_on_endpoint_is_405_not_a_hanging_stream(http, bearer_for, key):
    r = await http.get(f"/{key}", headers=await bearer_for(key), timeout=3)
    assert r.status_code == 405
    assert r.headers["allow"] == "POST, DELETE"


async def test_unknown_path_falls_through_to_ui(http):
    r = await http.get("/some-page")
    assert r.status_code == 200
    assert "Manifold" in r.text


async def test_reserved_and_wrong_paths_are_404(http):
    for path in ("/api", "/oauth/callback", "/manifold/other", "/static/x"):
        assert (await http.get(path)).status_code == 404, path
