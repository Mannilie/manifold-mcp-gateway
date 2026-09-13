"""Upstream OAuth against the fake provider (DECISIONS.md, Phase 3 gate 3)."""

from __future__ import annotations

import asyncio
import time

import httpx2
import pytest

from manifold.auth.upstream import ReconnectRequired, UpstreamOAuth, UpstreamUnavailable
from manifold.config.settings import Settings
from tests.fake_oauth_provider import FakeProvider, code_from_redirect
from tests.oauth_helpers import ACCESS_HEADER

FAKE_BASE = "https://fake-provider.test"


@pytest.fixture
async def rig(env, tmp_path):
    from manifold.app import create_app

    provider = FakeProvider()
    provider_http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=provider.app))
    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    env["MANIFOLD_BASE_URL"] = "http://testserver"
    app = create_app(Settings.from_env(env), reload_poll_seconds=100, upstream_http=provider_http)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            creds = app.state.repos["credentials"]
            cid = await creds.create(
                "Google (Manny)",
                "oauth2",
                {"client_secret": "shh"},
                meta={
                    "provider": "generic",
                    "auth_url": f"{FAKE_BASE}/authorize",
                    "token_url": f"{FAKE_BASE}/token",
                    "client_id": "cid-123",
                    "scopes": ["scope.a", "scope.b"],
                },
                status="unconnected",
            )
            yield app, http, provider, provider_http, cid
    await provider_http.aclose()


async def drive_browser(provider_http, authorize_url: str) -> tuple[str, str]:
    r = await provider_http.get(authorize_url, follow_redirects=False)
    assert r.status_code == 302, r.text
    return code_from_redirect(r.headers["location"])


async def connect(app, http, provider_http, cid) -> None:
    url = await app.state.upstream.start_connect(cid)
    code, state = await drive_browser(provider_http, url)
    r = await http.get(f"/oauth/callback?code={code}&state={state}", headers=ACCESS_HEADER)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"/credentials/{cid}?connected=1"


# -- connect -----------------------------------------------------------------------


async def test_full_connect_stores_refresh_token_and_granted_scope(rig, offline_generic):
    app, http, provider, provider_http, cid = rig
    await connect(app, http, provider_http, cid)
    creds = app.state.repos["credentials"]
    summary = await creds.get_summary(cid)
    assert summary.status == "ok"
    assert summary.meta["scopes"] == ["scope.a", "scope.b"]
    assert summary.meta["granted_scope"] == "scope.a", "granted, not requested, is stored"
    values = (await creds.get(cid)).values
    assert values["refresh_token"] == "refresh-1"
    assert values["client_secret"] == "shh"
    assert provider.token_requests[0]["code_verifier"], "PKCE verifier sent"


async def test_connect_refused_without_scopes(rig):
    app, http, _, _, cid = rig
    await app.state.repos["credentials"].update_meta(cid, {"scopes": []})
    r = await http.post(
        f"/api/credentials/{cid}/connect", headers={**ACCESS_HEADER, "X-Manifold-Request": "1"}
    )
    assert r.status_code == 422 and "scopes" in r.text


async def test_google_preset_sends_offline_and_consent(rig):
    app, _, _, _, cid = rig
    creds = app.state.repos["credentials"]
    await creds.update_meta(cid, {"provider": "google"})
    url = await app.state.upstream.start_connect(cid)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "code_challenge_method=S256" in url


async def test_connect_fails_without_offline_and_consent(rig, monkeypatch):
    """The generic preset sends no extras, so the fake returns no refresh token, and the
    connect must fail loudly rather than store a credential that dies in an hour."""
    app, http, _, provider_http, cid = rig
    upstream: UpstreamOAuth = app.state.upstream
    url = await upstream.start_connect(cid)
    assert "access_type" not in url
    code, state = await drive_browser(provider_http, url)
    r = await http.get(f"/oauth/callback?code={code}&state={state}", headers=ACCESS_HEADER)
    assert r.status_code == 400
    assert "access_type=offline" in r.text
    assert (await app.state.repos["credentials"].get_summary(cid)).status == "unconnected"


@pytest.fixture
def offline_generic(rig):
    """Give the generic preset the Google extras so happy-path tests get a refresh token."""
    from manifold.auth import upstream as mod

    original = mod.endpoints

    def patched(meta):
        auth_url, token_url, _ = original(meta)
        return auth_url, token_url, {"access_type": "offline", "prompt": "consent"}

    mod.endpoints = patched
    yield
    mod.endpoints = original


async def test_state_is_single_use_and_replay_does_not_touch_tokens(rig, offline_generic):
    app, http, provider, provider_http, cid = rig
    upstream: UpstreamOAuth = app.state.upstream
    url = await upstream.start_connect(cid)
    code, state = await drive_browser(provider_http, url)
    assert (
        await http.get(f"/oauth/callback?code={code}&state={state}", headers=ACCESS_HEADER)
    ).status_code == 303
    before = (await app.state.repos["credentials"].get(cid)).values
    # replay, and also a replay carrying a different code
    for c in (code, "another-code"):
        r = await http.get(f"/oauth/callback?code={c}&state={state}", headers=ACCESS_HEADER)
        assert r.status_code == 400
        assert "already used" in r.text
    assert (await app.state.repos["credentials"].get(cid)).values == before
    assert len(provider.token_requests) == 1, "no second exchange was attempted"


async def test_callback_requires_access_identity(rig, offline_generic):
    app, http, _, provider_http, cid = rig
    url = await app.state.upstream.start_connect(cid)
    code, state = await drive_browser(provider_http, url)
    assert (await http.get(f"/oauth/callback?code={code}&state={state}")).status_code == 403
    assert (
        await http.get(f"/oauth/callback?code={code}&state={state}", headers=ACCESS_HEADER)
    ).status_code == 303


async def test_state_is_bound_to_credential(rig, offline_generic):
    app, http, _, provider_http, cid = rig
    creds = app.state.repos["credentials"]
    other = await creds.create(
        "Other",
        "oauth2",
        {"client_secret": "x"},
        meta={
            "provider": "generic",
            "auth_url": f"{FAKE_BASE}/authorize",
            "token_url": f"{FAKE_BASE}/token",
            "client_id": "c2",
            "scopes": ["s"],
        },
        status="unconnected",
    )
    url = await app.state.upstream.start_connect(other)
    code, state = await drive_browser(provider_http, url)
    r = await http.get(f"/oauth/callback?code={code}&state={state}", headers=ACCESS_HEADER)
    assert r.headers["location"] == f"/credentials/{other}?connected=1"
    assert (await creds.get_summary(cid)).status == "unconnected"
    assert (await creds.get_summary(other)).status == "ok"


async def test_expired_state_is_rejected(rig, offline_generic):
    app, http, _, provider_http, cid = rig
    url = await app.state.upstream.start_connect(cid)
    await app.state.db.conn.execute("UPDATE oauth_state SET created_at = '2020-01-01T00:00:00Z'")
    code, state = await drive_browser(provider_http, url)
    r = await http.get(f"/oauth/callback?code={code}&state={state}", headers=ACCESS_HEADER)
    assert r.status_code == 400 and "10 minutes" in r.text


async def test_provider_error_is_reported(rig, offline_generic):
    app, http, _, provider_http, cid = rig
    url = await app.state.upstream.start_connect(cid)
    _, state = await drive_browser(provider_http, url)
    r = await http.get(f"/oauth/callback?error=access_denied&state={state}", headers=ACCESS_HEADER)
    assert r.status_code == 400 and "access_denied" in r.text


# -- refresh -----------------------------------------------------------------------


async def expire(app, cid) -> None:
    creds = app.state.repos["credentials"]
    values = (await creds.get(cid)).values
    await creds.update_values(
        cid, {**values, "expires_at": int(time.time()) - 10}, token_refresh=True
    )
    app.state.tokens.forget(cid)


async def test_refresh_is_coalesced(rig, offline_generic):
    app, http, provider, provider_http, cid = rig
    await connect(app, http, provider_http, cid)
    await expire(app, cid)
    tokens = app.state.tokens
    results = await asyncio.gather(*(tokens.access_token(cid) for _ in range(5)))
    assert len(set(results)) == 1
    assert provider.refresh_calls == 1
    assert await tokens.access_token(cid) == results[0], "cached afterwards"


async def test_refresh_does_not_count_as_config_change(rig, offline_generic):
    app, http, _, provider_http, cid = rig
    await connect(app, http, provider_http, cid)
    creds = app.state.repos["credentials"]
    before = (await creds.get_summary(cid)).updated_at
    await expire(app, cid)
    await app.state.tokens.access_token(cid)
    after = await creds.get_summary(cid)
    assert after.updated_at == before
    assert after.token_updated_at is not None


async def test_invalid_grant_flips_to_reconnect_required(rig, offline_generic):
    app, http, provider, provider_http, cid = rig
    await connect(app, http, provider_http, cid)
    await expire(app, cid)
    provider.mode = "invalid_grant"
    with pytest.raises(ReconnectRequired):
        await app.state.tokens.access_token(cid)
    summary = await app.state.repos["credentials"].get_summary(cid)
    assert summary.status == "reconnect_required"
    assert "Reconnect" in summary.meta["last_error"]
    with pytest.raises(ReconnectRequired):
        await app.state.tokens.access_token(cid)
    assert provider.refresh_calls == 1, "no retry once marked"


async def test_transient_outage_does_not_change_status(rig, offline_generic):
    app, http, provider, provider_http, cid = rig
    await connect(app, http, provider_http, cid)
    await expire(app, cid)
    provider.mode = "outage"
    with pytest.raises(UpstreamUnavailable):
        await app.state.tokens.access_token(cid)
    assert (await app.state.repos["credentials"].get_summary(cid)).status == "ok"
    provider.mode = "ok"
    assert (await app.state.tokens.access_token(cid)).startswith("access-")


async def test_reconnect_after_invalid_grant_restores_service(rig, offline_generic):
    app, http, provider, provider_http, cid = rig
    await connect(app, http, provider_http, cid)
    await expire(app, cid)
    provider.mode = "invalid_grant"
    with pytest.raises(ReconnectRequired):
        await app.state.tokens.access_token(cid)
    provider.mode = "ok"
    await connect(app, http, provider_http, cid)
    assert (await app.state.repos["credentials"].get_summary(cid)).status == "ok"
    assert (await app.state.tokens.access_token(cid)).startswith("access-")
