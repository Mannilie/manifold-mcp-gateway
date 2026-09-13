"""Admin API, in process. Auth guards, CSRF header, and never a credential value."""

from __future__ import annotations

import json

import httpx2
import pytest

import manifold.toolsets
import tests.fixtures.toolsets
from manifold.config.settings import Settings
from tests.oauth_helpers import ACCESS_HEADER

MUT = {**ACCESS_HEADER, "X-Manifold-Request": "1"}
SA_JSON = json.dumps(
    {
        "type": "service_account",
        "client_email": "bot@proj.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nSECRET\n",
        "project_id": "proj",
    }
)

PACKAGES = (manifold.toolsets, tests.fixtures.toolsets)


@pytest.fixture
async def api(env, tmp_path):
    from manifold.app import create_app

    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    env["MANIFOLD_BASE_URL"] = "https://mcp.example.test"
    app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="https://mcp.example.test"
        ) as http:
            yield app, http


# -- guards --------------------------------------------------------------------------


async def test_api_requires_access_identity(api):
    _, http = api
    assert (await http.get("/api/toolsets")).status_code == 401
    r = await http.get("/api/toolsets", headers={"Cf-Access-Authenticated-User-Email": "x@y.z"})
    assert r.status_code == 403


async def test_mutations_require_csrf_header(api):
    _, http = api
    r = await http.patch("/api/toolsets/ping-b", json={"enabled": True}, headers=ACCESS_HEADER)
    assert r.status_code == 403 and "x-manifold-request" in r.text
    r = await http.patch("/api/toolsets/ping-b", json={"enabled": True}, headers=MUT)
    assert r.status_code == 200


async def test_api_responses_are_no_store(api):
    _, http = api
    r = await http.get("/api/me", headers=ACCESS_HEADER)
    assert r.json() == {"email": "manny@example.com"}
    assert r.headers["cache-control"] == "no-store"
    assert (await http.get("/healthz")).headers["cache-control"] == "no-store"


# -- toolsets ------------------------------------------------------------------------


async def test_list_and_detail(api):
    _, http = api
    r = await http.get("/api/toolsets", headers=ACCESS_HEADER)
    assert r.status_code == 200
    by_key = {t["key"]: t for t in r.json()}
    assert by_key["manifold"]["enabled"] and by_key["manifold"]["mounted"]
    assert by_key["manifold"]["health"]["status"] == "ok"
    assert by_key["manifold"]["tool_count"] == 1
    assert by_key["manifold"]["cloudflare"] == {
        "connector_url": "https://mcp.example.test/manifold",
        "bypass_path": "mcp.example.test/manifold",
        "covers": ["/manifold", "/manifold/healthz"],
    }
    assert by_key["ping-b"]["enabled"] is False and by_key["ping-b"]["mounted"] is False
    assert by_key["ping-b"]["health"]["status"] == "disabled"
    d = (await http.get("/api/toolsets/ping-b", headers=ACCESS_HEADER)).json()
    assert d["tools"] == [
        {"name": "ping", "description": d["tools"][0]["description"], "enabled": True}
    ]
    assert d["settings_schema"]["type"] == "object"
    assert (await http.get("/api/toolsets/nope", headers=ACCESS_HEADER)).status_code == 404


async def test_enable_mounts_and_disable_unmounts(api):
    app, http = api
    r = await http.patch("/api/toolsets/ping-b", json={"enabled": True}, headers=MUT)
    assert r.status_code == 200 and r.json()["mounted"] is True
    assert "ping-b" in app.state.registry.routes
    r = await http.patch("/api/toolsets/ping-b", json={"enabled": False}, headers=MUT)
    assert r.json()["mounted"] is False
    assert "ping-b" not in app.state.registry.routes


async def test_manifold_cannot_be_disabled_or_deleted(api):
    _, http = api
    assert (
        await http.patch("/api/toolsets/manifold", json={"enabled": False}, headers=MUT)
    ).status_code == 409
    assert (await http.delete("/api/toolsets/manifold", headers=MUT)).status_code == 409


async def test_settings_are_validated_server_side(api):
    _, http = api
    r = await http.patch("/api/toolsets/ping-b", json={"settings": {"bogus": 1}}, headers=MUT)
    assert r.status_code == 422
    assert "bogus" in json.dumps(r.json())


async def test_disable_a_tool_hides_it_and_refuses_calls(api):
    _, http = api
    from tests.oauth_helpers import obtain_tokens

    await http.patch(
        "/api/toolsets/ping-b", json={"enabled": True, "disabled_tools": ["ping"]}, headers=MUT
    )
    d = (await http.get("/api/toolsets/ping-b", headers=ACCESS_HEADER)).json()
    assert d["tools"][0]["enabled"] is False and d["tool_count"] == 0
    _, tokens = await obtain_tokens(http, "ping-b")
    hdr = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {tokens['access_token']}",
    }
    listing = await http.post(
        "/ping-b", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=hdr
    )
    assert listing.json()["result"]["tools"] == []
    call = await http.post(
        "/ping-b",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        },
        headers=hdr,
    )
    assert "disabled" in json.dumps(call.json())
    await http.patch("/api/toolsets/ping-b", json={"disabled_tools": []}, headers=MUT)
    listing = await http.post(
        "/ping-b", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, headers=hdr
    )
    assert [t["name"] for t in listing.json()["result"]["tools"]] == ["ping"]


async def test_test_connection_bypasses_cache(api):
    _, http = api
    r = await http.post("/api/toolsets/manifold/test", headers=MUT)
    assert r.json()["status"] == "ok"


async def test_rename_moves_endpoint(api):
    app, http = api
    await http.patch("/api/toolsets/ping-b", json={"enabled": True}, headers=MUT)
    r = await http.post("/api/toolsets/ping-b/rename", json={"new_key": "ping-c"}, headers=MUT)
    assert r.status_code == 200, r.text
    assert r.json()["key"] == "ping-c" and r.json()["endpoint_url"].endswith("/ping-c")
    assert "ping-c" not in app.state.registry.routes, (
        "no code exists for ping-c, so it is not mounted"
    )
    assert (
        await http.post("/api/toolsets/ping-c/rename", json={"new_key": "api"}, headers=MUT)
    ).status_code == 422
    assert (
        await http.post("/api/toolsets/manifold/rename", json={"new_key": "x"}, headers=MUT)
    ).status_code == 422


async def test_create_proxy_and_delete(api):
    _, http = api
    body = {
        "key": "homeassist",
        "display_name": "Home Assistant",
        "upstream_url": "http://homeassistant:8123/mcp",
        "prefix": "ha_",
        "deny": ["exec"],
    }
    r = await http.post("/api/toolsets", json=body, headers=MUT)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "proxy" and r.json()["upstream"]["deny"] == ["exec"]
    assert (await http.post("/api/toolsets", json=body, headers=MUT)).status_code == 409
    assert (
        await http.post("/api/toolsets", json={**body, "key": "healthz"}, headers=MUT)
    ).status_code == 422
    assert (await http.delete("/api/toolsets/homeassist", headers=MUT)).status_code == 204
    assert (await http.get("/api/toolsets/homeassist", headers=ACCESS_HEADER)).status_code == 404


# -- credentials ---------------------------------------------------------------------


async def test_credentials_never_expose_values(api):
    _, http = api
    r = await http.post(
        "/api/credentials",
        json={
            "name": "Google (Manny)",
            "auth_kind": "service_account",
            "values": {"json": SA_JSON},
        },
        headers=MUT,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["meta"]["client_email"] == "bot@proj.iam.gserviceaccount.com"
    assert "SECRET" not in r.text and "private_key" not in r.text
    listing = await http.get("/api/credentials", headers=ACCESS_HEADER)
    assert "SECRET" not in listing.text
    one = await http.get(f"/api/credentials/{body['id']}", headers=ACCESS_HEADER)
    assert "SECRET" not in one.text and one.json()["status"] == "ok"


async def test_credential_validation(api):
    _, http = api
    r = await http.post(
        "/api/credentials", json={"name": "A", "auth_kind": "api_key", "values": {}}, headers=MUT
    )
    assert r.status_code == 422 and "key" in r.text
    r = await http.post(
        "/api/credentials",
        json={"name": "B", "auth_kind": "service_account", "values": {"json": "{}"}},
        headers=MUT,
    )
    assert r.status_code == 422
    r = await http.post(
        "/api/credentials",
        json={"name": "C", "auth_kind": "oauth2", "values": {"client_secret": "s"}},
        headers=MUT,
    )
    assert r.status_code == 422 and "client_id" in r.text


async def test_attach_credential_respects_supported_auth(api):
    _, http = api
    r = await http.post(
        "/api/credentials",
        json={"name": "Key", "auth_kind": "api_key", "values": {"key": "k"}},
        headers=MUT,
    )
    cid = r.json()["id"]
    r = await http.patch("/api/toolsets/ping-b", json={"credential_id": cid}, headers=MUT)
    assert r.status_code == 422 and "does not support" in r.text


async def test_delete_in_use_is_refused_with_users(api):
    app, http = api
    r = await http.post(
        "/api/credentials",
        json={"name": "Key", "auth_kind": "api_key", "values": {"key": "k"}},
        headers=MUT,
    )
    cid = r.json()["id"]
    await app.state.repos["toolsets"].set_credential("ping-b", cid)
    r = await http.delete(f"/api/credentials/{cid}", headers=MUT)
    assert r.status_code == 409 and r.json()["detail"]["used_by"] == ["ping-b"]
    await http.patch("/api/toolsets/ping-b", json={"detach_credential": True}, headers=MUT)
    assert (await http.delete(f"/api/credentials/{cid}", headers=MUT)).status_code == 204


async def test_oauth2_credential_scope_change_forces_reconnect(api):
    app, http = api
    r = await http.post(
        "/api/credentials",
        json={
            "name": "Google",
            "auth_kind": "oauth2",
            "provider": "google",
            "scopes": ["a"],
            "values": {"client_id": "cid", "client_secret": "sec"},
        },
        headers=MUT,
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["status"] == "unconnected" and r.json()["meta"]["scopes"] == ["a"]
    assert "sec" not in json.dumps(r.json())
    creds = app.state.repos["credentials"]
    await creds.update_values(
        cid,
        {
            "client_secret": "sec",
            "refresh_token": "rt",
            "access_token": "at",
            "expires_at": 9999999999,
        },
    )
    await creds.set_status(cid, "ok")
    r = await http.patch(f"/api/credentials/{cid}", json={"scopes": ["a", "b"]}, headers=MUT)
    assert r.json()["status"] == "scopes_changed"
    assert r.json()["meta"]["scopes"] == ["a", "b"] and r.json()["meta"]["granted_scope"] is None
    assert "refresh_token" not in (await creds.get(cid)).values, "old refresh token discarded"
    r = await http.post(f"/api/credentials/{cid}/connect", headers=MUT)
    assert r.status_code == 200 and r.json()["authorize_url"].startswith(
        "https://accounts.google.com/"
    )
    assert "scope=a+b" in r.json()["authorize_url"]


async def test_toolset_using_scopes_changed_credential_is_not_mounted(api):
    app, http = api
    r = await http.post(
        "/api/credentials",
        json={
            "name": "G",
            "auth_kind": "oauth2",
            "provider": "google",
            "scopes": ["a"],
            "values": {"client_id": "c", "client_secret": "s"},
        },
        headers=MUT,
    )
    cid = r.json()["id"]
    # ping-b does not declare oauth2, so attach at the repo level to exercise the mount rule
    await app.state.repos["toolsets"].set_credential("ping-b", cid)
    r = await http.patch("/api/toolsets/ping-b", json={"enabled": True}, headers=MUT)
    assert r.json()["mounted"] is False
    assert r.json()["health"]["status"] == "down"


# -- audit, settings -----------------------------------------------------------------


async def test_audit_listing_and_filters(api):
    app, http = api
    await app.state.repos["audit"].record("manifold", "ping", "h" * 64, 3, True, None)
    await app.state.repos["audit"].record("ping-b", "ping", "h" * 64, 5, False, "boom")
    r = await http.get("/api/audit", headers=ACCESS_HEADER)
    assert [e["toolset_key"] for e in r.json()] == ["ping-b", "manifold"]
    r = await http.get("/api/audit?ok=false", headers=ACCESS_HEADER)
    assert [e["error"] for e in r.json()] == ["boom"]
    r = await http.get("/api/audit?toolset=manifold&limit=1", headers=ACCESS_HEADER)
    assert len(r.json()) == 1 and r.json()[0]["toolset_key"] == "manifold"


async def test_settings_get_patch_export(api):
    _, http = api
    r = await http.get("/api/settings", headers=ACCESS_HEADER)
    assert r.json()["log_level_source"] == "env" and r.json()["master_key"]["verified"] is True
    assert r.json()["schema_version"] >= 2
    r = await http.patch(
        "/api/settings", json={"log_level": "debug", "audit_retention_days": 30}, headers=MUT
    )
    assert r.json()["log_level"] == "debug" and r.json()["log_level_source"] == "database"
    assert r.json()["audit_retention_days"] == 30
    await http.post(
        "/api/credentials",
        json={"name": "Key", "auth_kind": "api_key", "values": {"key": "topsecret"}},
        headers=MUT,
    )
    r = await http.get("/api/settings/export", headers=ACCESS_HEADER)
    assert r.status_code == 200 and "topsecret" not in r.text and "name: Key" in r.text
    assert r.headers["content-disposition"].endswith('.yaml"')


async def test_disconnect_all_clears_oauth_clients(api):
    _, http = api
    from tests.oauth_helpers import obtain_tokens

    _, tokens = await obtain_tokens(http, "manifold")
    assert (await http.get("/api/settings", headers=ACCESS_HEADER)).json()["oauth_clients"] == 1
    r = await http.post("/api/settings/disconnect-all", headers=MUT)
    assert r.status_code == 200
    assert (await http.get("/api/settings", headers=ACCESS_HEADER)).json()["oauth_clients"] == 0
    hdr = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {tokens['access_token']}",
    }
    assert (
        await http.post(
            "/manifold", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=hdr
        )
    ).status_code == 401


async def test_reload_endpoint(api):
    _, http = api
    r = await http.post("/api/reload", headers=MUT)
    assert r.status_code == 200 and "unchanged" in r.json()


async def test_discover_tools_against_a_real_mcp_upstream(api, live_server):
    """Uses the live server's /manifold endpoint as the upstream, with a bearer credential."""
    _, http = api
    from tests.oauth_helpers import obtain_tokens

    async with httpx2.AsyncClient(base_url=live_server) as live:
        _, tokens = await obtain_tokens(live, "manifold")
    r = await http.post(
        "/api/credentials",
        json={"name": "Up", "auth_kind": "bearer", "values": {"token": tokens["access_token"]}},
        headers=MUT,
    )
    cid = r.json()["id"]
    r = await http.post(
        "/api/proxy/discover",
        json={"upstream_url": f"{live_server}/manifold", "credential_id": cid},
        headers=MUT,
    )
    assert r.status_code == 200, r.text
    assert [t["name"] for t in r.json()] == ["ping"]
    r = await http.post(
        "/api/proxy/discover", json={"upstream_url": f"{live_server}/manifold"}, headers=MUT
    )
    assert r.status_code == 502
