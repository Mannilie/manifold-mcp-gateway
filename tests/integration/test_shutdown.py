"""Shutdown always finishes, and says so (Phase 6, after the live restore test hung)."""

from __future__ import annotations

import asyncio
import signal
import time

import httpx2
import pytest
import uvicorn

import manifold.toolsets
import tests.fixtures.toolsets
from manifold.__main__ import Server
from manifold.config.settings import Settings
from manifold.gateway import proxy as proxy_mod
from manifold.gateway import shutdown as shutdown_mod
from manifold.gateway.shutdown import ShutdownController
from tests.fake_upstream import API_KEY, make_upstream, running_upstream
from tests.oauth_helpers import ACCESS_HEADER

PACKAGES = (manifold.toolsets, tests.fixtures.toolsets)
MUT = {**ACCESS_HEADER, "X-Manifold-Request": "1"}
UP = "http://upstream.test/mcp"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(proxy_mod, "CONNECT_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(proxy_mod, "STOP_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(shutdown_mod, "SHUTDOWN_TIMEOUT_SECONDS", 1.0)


class Clingy(httpx2.AsyncBaseTransport):
    """Serves the upstream normally but never finishes closing, like a stuck SSE socket."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request):
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await asyncio.Event().wait()


async def test_restart_with_live_proxy_session_completes_within_timeout(env, tmp_path, monkeypatch):
    from manifold.app import create_app

    async with running_upstream(make_upstream()) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: Clingy(transport))
        app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
        app.state.no_restart = True
        started = time.monotonic()
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
            ) as http,
        ):
            cred = {
                "name": "k",
                "auth_kind": "api_key",
                "values": {"key": API_KEY, "header": "x-api-key"},
            }
            cid = (await http.post("/api/credentials", json=cred, headers=MUT)).json()["id"]
            body = {
                "key": "n8n",
                "display_name": "n8n",
                "upstream_url": UP,
                "credential_id": cid,
            }
            r = await http.post("/api/toolsets", json=body, headers=MUT)
            assert r.status_code == 201, r.text
            r = await http.patch("/api/toolsets/n8n", json={"enabled": True}, headers=MUT)
            assert r.status_code == 200 and r.json()["health"]["status"] == "ok", r.text
            r = await http.post("/api/settings/restart", headers=MUT)
            assert r.status_code == 200
            assert app.state.shutdown.shutting_down is False, "no kill in tests"
            stopping = time.monotonic()
        assert time.monotonic() - stopping < 3.0, "teardown waited on the stuck upstream"
        assert time.monotonic() - started < 10.0


async def test_healthz_reports_shutdown(env):
    from manifold.app import create_app

    app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
        ) as http,
    ):
        assert (await http.get("/healthz")).status_code == 200
        app.state.shutdown.begin(watchdog_seconds=None)
        r = await http.get("/healthz")
        assert r.status_code == 503 and r.json() == {"status": "shutting_down"}


async def test_watchdog_forces_exit_when_shutdown_stalls():
    exited: list[int] = []
    ctl = ShutdownController(exit_fn=exited.append)
    ctl.begin(watchdog_seconds=0.05)
    ctl.begin(watchdog_seconds=0.05)  # idempotent, no second timer
    await asyncio.sleep(0.3)
    assert exited == [shutdown_mod.WATCHDOG_EXIT_CODE]


async def test_watchdog_is_cancelled_by_a_clean_finish():
    exited: list[int] = []
    ctl = ShutdownController(exit_fn=exited.append)
    ctl.begin(watchdog_seconds=0.05)
    ctl.cancel_watchdog()
    await asyncio.sleep(0.2)
    assert exited == []


async def test_lifespan_end_begins_shutdown_and_cancels_watchdog(env):
    from manifold.app import create_app

    app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
    exited: list[int] = []
    app.state.shutdown._exit = exited.append
    async with app.router.lifespan_context(app):
        assert not app.state.shutdown.shutting_down
    assert app.state.shutdown.shutting_down
    assert app.state.shutdown._watchdog is None
    assert exited == []


def test_sigterm_from_docker_begins_shutdown(monkeypatch):
    """uvicorn's signal handler is routed through the controller of the current app."""
    from sse_starlette.sse import AppStatus

    exited: list[int] = []
    ctl = ShutdownController(exit_fn=exited.append)
    monkeypatch.setattr(shutdown_mod, "current", ctl)
    server = Server(uvicorn.Config(lambda scope, receive, send: None))
    try:
        server.handle_exit(signal.SIGTERM, None)
        assert ctl.shutting_down and server.should_exit
        # sse_starlette (under the mcp SDK) wraps uvicorn's handler and sets this
        # process-wide flag, which ends every SSE response from then on. Correct in
        # production, poison for the rest of the test run.
        assert AppStatus.should_exit is True
    finally:
        ctl.cancel_watchdog()
        AppStatus.should_exit = False


def test_serve_config_has_graceful_timeout(monkeypatch):
    captured = {}

    class Fake:
        def __init__(self, config):
            captured["config"] = config

        def run(self):
            pass

    monkeypatch.setattr("manifold.__main__.Server", Fake)
    from manifold.__main__ import serve

    serve()
    assert captured["config"].timeout_graceful_shutdown == shutdown_mod.SHUTDOWN_TIMEOUT_SECONDS
