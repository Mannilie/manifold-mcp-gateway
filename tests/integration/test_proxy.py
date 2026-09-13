"""Proxy toolsets against an in-process upstream (DECISIONS.md, Phase 5 gate 2)."""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest

from manifold.config.settings import Settings
from manifold.gateway import proxy as proxy_mod
from manifold.gateway.manifest import Credentials
from manifold.gateway.proxy import (
    ProxyConnection,
    ProxyServer,
    SnapshotTool,
    UpstreamUnreachable,
    apply_lists,
)
from tests.fake_upstream import API_KEY, make_upstream, running_upstream
from tests.oauth_helpers import ACCESS_HEADER, obtain_tokens

MUT = {**ACCESS_HEADER, "X-Manifold-Request": "1"}
UP = "http://upstream.test/mcp"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(proxy_mod, "CALL_BUDGET_SECONDS", 3.0)
    monkeypatch.setattr(proxy_mod, "CONNECT_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(proxy_mod, "BACKOFF_BASE_SECONDS", 0.05)
    monkeypatch.setattr(proxy_mod, "BACKOFF_MAX_SECONDS", 0.2)


class Dead(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx2.ConnectError("connection refused")


def api_key_creds() -> Credentials:
    return Credentials(kind="api_key", values={"key": API_KEY, "header": "x-api-key"})


# -- connection and server, in isolation ------------------------------------------------


async def test_connection_lists_and_calls_with_auth_header(monkeypatch):
    async with running_upstream(make_upstream()) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: transport)
        conn = ProxyConnection("n8n", UP, api_key_creds())
        await conn.start()
        try:
            assert conn.connected
            names = [t.name for t in await conn.list_tools()]
            assert names == ["echo", "fail", "secret"]
            result = await conn.call_tool("echo", {"text": "hi", "times": 2})
            assert result.content[0].text == "hihi" and not result.is_error
        finally:
            await conn.stop()


async def test_wrong_credential_never_connects(monkeypatch):
    async with running_upstream(make_upstream()) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: transport)
        conn = ProxyConnection("n8n", UP, Credentials(kind="api_key", values={"key": "nope"}))
        await conn.start(wait=0.5)
        try:
            assert not conn.connected and conn.last_error
            with pytest.raises(UpstreamUnreachable, match="upstream n8n is unreachable"):
                await conn.call_tool("echo", {"text": "x"})
        finally:
            await conn.stop()


async def test_dead_upstream_returns_readable_error_within_budget(monkeypatch):
    monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: Dead())
    conn = ProxyConnection("n8n", UP, api_key_creds())
    await conn.start(wait=0.3)
    try:
        started = asyncio.get_event_loop().time()
        with pytest.raises(UpstreamUnreachable, match="upstream n8n is unreachable: ConnectError"):
            await conn.call_tool("echo", {"text": "x"})
        assert asyncio.get_event_loop().time() - started < 3.0
        assert conn.attempts >= 2, "it kept trying with backoff"
    finally:
        await conn.stop()


async def test_reconnects_after_upstream_returns(monkeypatch):
    upstream = make_upstream()
    state = {"transport": Dead()}
    monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: state["transport"])
    conn = ProxyConnection("n8n", UP, api_key_creds())
    await conn.start(wait=0.3)
    try:
        assert not conn.connected
        async with running_upstream(upstream) as transport:
            state["transport"] = transport
            for _ in range(40):
                if conn.connected:
                    break
                await asyncio.sleep(0.1)
            assert conn.connected
            assert (await conn.call_tool("echo", {"text": "back"})).content[0].text == "back"
    finally:
        await conn.stop()


async def test_proxy_server_prefix_description_passthrough_and_errors(monkeypatch):
    async with running_upstream(make_upstream()) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: transport)
        conn = ProxyConnection("n8n", UP, api_key_creds())
        await conn.start()
        try:
            snapshot = [SnapshotTool.from_tool(t) for t in await conn.list_tools()]
            exported = apply_lists(snapshot, allow=(), deny=("secret",))
            server = ProxyServer("n8n", "n8n", conn, exported, "n8n_")
            tools = await server.list_tools()
            assert [t.name for t in tools] == ["n8n_echo", "n8n_fail"]
            assert tools[0].description.startswith("Via n8n: Echo the text back")
            assert tools[0].input_schema["properties"]["times"]["default"] == 1, (
                "schema passed through"
            )
            assert server.aliases == {"n8n_echo": "echo", "n8n_fail": "fail"}
            from mcp import types

            ok = await server._on_call_tool(
                None, types.CallToolRequestParams(name="n8n_echo", arguments={"text": "a"})
            )
            assert ok.content[0].text == "a"
            err = await server._on_call_tool(
                None, types.CallToolRequestParams(name="n8n_fail", arguments={})
            )
            assert err.is_error and "upstream boom: the widget is on fire" in err.content[0].text
            unknown = await server._on_call_tool(
                None, types.CallToolRequestParams(name="n8n_secret", arguments={})
            )
            assert unknown.is_error and "unknown tool" in unknown.content[0].text
        finally:
            await conn.stop()


def test_apply_lists():
    snap = [SnapshotTool(n, "", {}) for n in ("a", "b", "c")]
    assert [t.name for t in apply_lists(snap, (), ())] == ["a", "b", "c"]
    assert [t.name for t in apply_lists(snap, (), ("b",))] == ["a", "c"]
    assert [t.name for t in apply_lists(snap, ("a", "b"), ("b",))] == ["a"]


# -- through the whole app ----------------------------------------------------------------


@pytest.fixture
async def app_rig(env, tmp_path, monkeypatch):
    from manifold.app import create_app

    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    env["MANIFOLD_BASE_URL"] = "http://testserver"
    app = create_app(Settings.from_env(env), reload_poll_seconds=100)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            r = await http.post(
                "/api/credentials",
                json={
                    "name": "n8n key",
                    "auth_kind": "api_key",
                    "values": {"key": API_KEY, "header": "x-api-key"},
                },
                headers=MUT,
            )
            cid = r.json()["id"]
            r = await http.post(
                "/api/toolsets",
                json={
                    "key": "n8n",
                    "display_name": "n8n",
                    "upstream_url": UP,
                    "prefix": "n8n_",
                    "credential_id": cid,
                    "deny": ["secret"],
                },
                headers=MUT,
            )
            assert r.status_code == 201, r.text
            yield app, http


async def mcp_call(http, key, name, args):
    _, tokens = await obtain_tokens(http, key)
    hdr = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {tokens['access_token']}",
    }
    listing = await http.post(
        f"/{key}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=hdr
    )
    call = await http.post(
        f"/{key}",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        headers=hdr,
    )
    return listing.json()["result"]["tools"], call.json()["result"]


async def test_proxy_end_to_end_with_snapshot_and_audit(app_rig, monkeypatch):
    app, http = app_rig
    async with running_upstream(make_upstream()) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: transport)
        r = await http.patch("/api/toolsets/n8n", json={"enabled": True}, headers=MUT)
        assert r.status_code == 200, r.text
        detail = r.json()
        assert detail["mounted"] is True and detail["health"]["status"] == "ok"
        assert [t["name"] for t in detail["tools"]] == ["n8n_echo", "n8n_fail"]
        assert detail["tools"][0]["description"].startswith("Via n8n:")
        row = await app.state.repos["toolsets"].get("n8n")
        assert [t["name"] for t in row.upstream.tools] == ["echo", "fail", "secret"], (
            "snapshot stored"
        )
        tools, result = await mcp_call(http, "n8n", "n8n_echo", {"text": "yo", "times": 3})
        assert [t["name"] for t in tools] == ["n8n_echo", "n8n_fail"]
        assert result["content"][0]["text"] == "yoyoyo"
        _, err = await mcp_call(http, "n8n", "n8n_fail", {})
        assert err["isError"] is True and "widget is on fire" in err["content"][0]["text"]
        entries = await app.state.repos["audit"].recent(toolset_key="n8n")
        assert {(e.tool_name, e.upstream_tool) for e in entries} >= {
            ("n8n_echo", "echo"),
            ("n8n_fail", "fail"),
        }
        r = await http.get("/api/audit?toolset=n8n", headers=ACCESS_HEADER)
        assert r.json()[0]["upstream_tool"] in ("echo", "fail")


async def test_new_upstream_tool_is_reported_then_denied_on_reload(app_rig, monkeypatch):
    app, http = app_rig
    async with running_upstream(make_upstream()) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: transport)
        await http.patch("/api/toolsets/n8n", json={"enabled": True}, headers=MUT)
    async with running_upstream(make_upstream(("echo", "fail", "secret", "later"))) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: transport)
        # The first probe finds the old session dead and kicks a reconnect; poll until the
        # upstream answers, then the drift shows as degraded and flags a rebuild.
        r = None
        for _ in range(60):
            r = await http.post("/api/toolsets/n8n/test", headers=MUT)
            if r.json()["status"] == "degraded":
                break
            await asyncio.sleep(0.1)
        assert r is not None and r.json()["status"] == "degraded", r.json()
        assert "upstream added 1 tool (later)" in r.json()["detail"]
        # a plain reload re-snapshots; the new tool lands in the deny list
        r = await http.post("/api/reload", headers=MUT)
        assert "n8n" in r.json()["rebuilt"], r.json()
        row = await app.state.repos["toolsets"].get("n8n")
        assert "later" in row.upstream.deny and "later" in [t["name"] for t in row.upstream.tools]
        r = await http.get("/api/toolsets/n8n", headers=ACCESS_HEADER)
        assert [t["name"] for t in r.json()["tools"]] == ["n8n_echo", "n8n_fail"], (
            "still not exported"
        )
        assert (await http.post("/api/toolsets/n8n/test", headers=MUT)).json()["status"] == "ok"
        # allow it from the UI
        r = await http.patch(
            "/api/toolsets/n8n",
            json={
                "upstream": {"upstream_url": UP, "prefix": "n8n_", "allow": [], "deny": ["secret"]}
            },
            headers=MUT,
        )
        assert r.status_code == 200, r.text
        r = await http.get("/api/toolsets/n8n", headers=ACCESS_HEADER)
        assert "n8n_later" in [t["name"] for t in r.json()["tools"]]


async def test_upstream_down_at_build_serves_snapshot_and_reports_down(app_rig, monkeypatch):
    _, http = app_rig
    async with running_upstream(make_upstream()) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: transport)
        await http.patch("/api/toolsets/n8n", json={"enabled": True}, headers=MUT)
    monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: Dead())
    # A prefix change is a real config change, so this rebuilds with the upstream dead.
    r = await http.patch(
        "/api/toolsets/n8n",
        json={"upstream": {"upstream_url": UP, "prefix": "up_", "allow": [], "deny": ["secret"]}},
        headers=MUT,
    )
    assert r.status_code == 200, r.text
    assert r.json()["mounted"] is True, "rebuilt from the stored snapshot"
    assert [t["name"] for t in r.json()["tools"]] == ["up_echo", "up_fail"]
    assert (await http.post("/api/toolsets/n8n/test", headers=MUT)).json()["status"] == "down"
    _, result = await mcp_call(http, "n8n", "up_echo", {"text": "x"})
    assert result["isError"] is True
    assert "upstream n8n is unreachable" in result["content"][0]["text"]


async def test_upstream_down_with_no_snapshot_is_a_build_failure(app_rig, monkeypatch):
    _, http = app_rig
    monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: Dead())
    r = await http.patch("/api/toolsets/n8n", json={"enabled": True}, headers=MUT)
    assert r.json()["mounted"] is False and "unreachable" in (r.json()["error"] or "")
    assert r.json()["health"]["status"] == "down"


async def test_oauth2_credential_drives_bearer_header(app_rig, monkeypatch):
    app, http = app_rig
    r = await http.post(
        "/api/credentials",
        json={
            "name": "HH",
            "auth_kind": "oauth2",
            "provider": "generic",
            "auth_url": "https://hh.test/authorize",
            "token_url": "https://hh.test/token",
            "scopes": ["x"],
            "values": {"client_id": "c", "client_secret": "s"},
        },
        headers=MUT,
    )
    cid = r.json()["id"]
    creds = app.state.repos["credentials"]
    await creds.update_values(
        cid,
        {
            "client_secret": "s",
            "refresh_token": "rt",
            "access_token": "oauth-up",
            "expires_at": 9999999999,
        },
    )
    await creds.set_status(cid, "ok")
    await http.patch("/api/toolsets/n8n", json={"credential_id": cid}, headers=MUT)
    async with running_upstream(make_upstream()) as transport:
        monkeypatch.setattr(proxy_mod, "transport_factory", lambda url: transport)
        r = await http.patch("/api/toolsets/n8n", json={"enabled": True}, headers=MUT)
        assert r.json()["mounted"] is True and r.json()["health"]["status"] == "ok", json.dumps(
            r.json()
        )
