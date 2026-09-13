"""Dispatcher contract, exercised in isolation with fake ASGI apps (DECISIONS.md, mounting gate)."""

from __future__ import annotations

import json

import httpx2
import pytest

from manifold.gateway.dispatcher import ToolsetDispatcher, ToolsetRoute
from manifold.gateway.manifest import RESERVED_KEYS


def _json_app(tag: str):
    calls: list[dict] = []

    async def app(scope, receive, send):
        calls.append({"path": scope["path"], "method": scope["method"]})
        body = json.dumps({"app": tag}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    app.calls = calls
    return app


@pytest.fixture
def parts():
    mcp_a, mcp_b, fallback = _json_app("mcp-a"), _json_app("mcp-b"), _json_app("ui")

    async def health_ok():
        return {"toolset": "alpha", "status": "ok"}

    async def health_down():
        return {"toolset": "beta", "status": "down", "detail": "upstream unreachable"}

    routes = {
        "alpha": ToolsetRoute(mcp_app=mcp_a, health=health_ok),
        "beta": ToolsetRoute(mcp_app=mcp_b, health=health_down),
    }
    dispatcher = ToolsetDispatcher(routes, fallback=fallback)
    return dispatcher, routes, mcp_a, mcp_b, fallback


@pytest.fixture
async def client(parts):
    dispatcher = parts[0]
    transport = httpx2.ASGITransport(app=dispatcher)
    async with httpx2.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_exact_key_post_reaches_mcp_app(client, parts):
    r = await client.post("/alpha", json={})
    assert r.status_code == 200
    assert r.json() == {"app": "mcp-a"}
    assert parts[2].calls[-1]["path"] == "/alpha"


async def test_trailing_slash_reaches_same_mcp_app(client, parts):
    r = await client.post("/alpha/", json={})
    assert r.status_code == 200
    assert r.json() == {"app": "mcp-a"}


async def test_second_toolset_is_independent(client, parts):
    r = await client.post("/beta", json={})
    assert r.json() == {"app": "mcp-b"}
    assert parts[2].calls == []


async def test_healthz_ok(client):
    r = await client.get("/alpha/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_healthz_reports_503_when_not_ok(client):
    r = await client.get("/beta/healthz")
    assert r.status_code == 503
    assert r.json()["detail"] == "upstream unreachable"


async def test_healthz_rejects_post(client):
    r = await client.post("/alpha/healthz")
    assert r.status_code == 405
    assert r.headers["allow"] == "GET, HEAD"


async def test_anything_else_under_key_is_404(client, parts):
    for path in ("/alpha/anything-else", "/alpha/mcp", "/alpha/healthz/extra", "/alpha//x"):
        r = await client.post(path, json={})
        assert r.status_code == 404, path
    assert parts[2].calls == []
    assert parts[4].calls == []


@pytest.mark.parametrize("key", sorted(RESERVED_KEYS))
async def test_reserved_keys_are_404_not_fallthrough(client, parts, key):
    for path in (f"/{key}", f"/{key}/", f"/{key}/deeper"):
        r = await client.get(path)
        assert r.status_code == 404, path
    assert parts[4].calls == []


async def test_unknown_key_falls_through_to_ui(client, parts):
    for path in ("/", "/index.html", "/gamma", "/gamma/deep/path"):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert r.json() == {"app": "ui"}
    assert [c["path"] for c in parts[4].calls] == ["/", "/index.html", "/gamma", "/gamma/deep/path"]


async def test_never_redirects(client):
    paths = [
        "/alpha",
        "/alpha/",
        "/alpha/healthz",
        "/alpha/healthz/",
        "/alpha/x",
        "/beta",
        "/beta/",
        "/api",
        "/api/",
        "/healthz/",
        "/gamma",
        "/gamma/",
        "/",
        "",
    ]
    for path in paths:
        for method in ("GET", "POST"):
            r = await client.request(method, path)
            assert not 300 <= r.status_code < 400, (method, path, r.status_code)
            assert "location" not in r.headers, (method, path)


async def test_honours_root_path_when_mounted(parts):
    dispatcher = parts[0]

    async def wrapped(scope, receive, send):
        scope = dict(scope, root_path="/prefix")
        await dispatcher(scope, receive, send)

    transport = httpx2.ASGITransport(app=wrapped)
    async with httpx2.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/prefix/alpha")
    assert r.status_code == 200
    assert r.json() == {"app": "mcp-a"}


async def test_live_mapping_is_read_on_every_request(parts):
    """Phase 2 hot reload swaps entries in the mapping; the dispatcher must not copy it."""
    dispatcher, routes = parts[0], parts[1]
    transport = httpx2.ASGITransport(app=dispatcher)
    async with httpx2.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.post("/gamma")).json() == {"app": "ui"}
        routes["gamma"] = ToolsetRoute(mcp_app=_json_app("mcp-g"), health=parts[1]["alpha"].health)
        assert (await c.post("/gamma")).json() == {"app": "mcp-g"}
        del routes["gamma"]
        assert (await c.post("/gamma")).json() == {"app": "ui"}
