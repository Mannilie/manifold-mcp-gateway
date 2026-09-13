"""The registration guard on the real /oauth/register route, in process with tiny limits."""

from __future__ import annotations

import httpx2
import pytest
from starlette.applications import Starlette

from manifold.auth.provider import ManifoldOAuthProvider
from manifold.auth.routes import build_oauth_routes
from manifold.auth.store import InMemoryTokenStore

BODY = {"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}


def make_app(max_per_window: int, max_clients: int) -> Starlette:
    provider = ManifoldOAuthProvider(
        InMemoryTokenStore(), frozenset({"a@b.c"}), "https://mcp.example", lambda k: True
    )
    routes = build_oauth_routes(
        provider,
        "https://mcp.example",
        frozenset({"a@b.c"}),
        max_registrations_per_window=max_per_window,
        registration_window_seconds=600,
        max_clients=max_clients,
    )
    return Starlette(routes=routes)


@pytest.fixture
def client_factory():
    def make(max_per_window: int = 3, max_clients: int = 100) -> httpx2.AsyncClient:
        transport = httpx2.ASGITransport(app=make_app(max_per_window, max_clients))
        return httpx2.AsyncClient(transport=transport, base_url="https://mcp.example")

    return make


async def test_rate_limit_trips_after_window_fills(client_factory):
    async with client_factory(max_per_window=3) as c:
        for _ in range(3):
            assert (await c.post("/oauth/register", json=BODY)).status_code == 201
        r = await c.post("/oauth/register", json=BODY)
        assert r.status_code == 429
        assert r.json()["error"] == "too_many_registrations"
        assert int(r.headers["retry-after"]) > 0


async def test_failed_registrations_still_count(client_factory):
    async with client_factory(max_per_window=2) as c:
        bad = {"redirect_uris": ["https://evil.example/cb"]}
        assert (await c.post("/oauth/register", json=bad)).status_code == 400
        assert (await c.post("/oauth/register", json=bad)).status_code == 400
        assert (await c.post("/oauth/register", json=BODY)).status_code == 429


async def test_client_cap_refuses_new_clients(client_factory):
    async with client_factory(max_per_window=100, max_clients=2) as c:
        assert (await c.post("/oauth/register", json=BODY)).status_code == 201
        assert (await c.post("/oauth/register", json=BODY)).status_code == 201
        r = await c.post("/oauth/register", json=BODY)
        assert r.status_code == 429
        assert r.json()["error"] == "client_cap_reached"


async def test_options_preflight_is_not_counted(client_factory):
    async with client_factory(max_per_window=1) as c:
        for _ in range(3):
            r = await c.options(
                "/oauth/register",
                headers={"Origin": "https://x", "Access-Control-Request-Method": "POST"},
            )
            assert r.status_code == 200
        assert (await c.post("/oauth/register", json=BODY)).status_code == 201
