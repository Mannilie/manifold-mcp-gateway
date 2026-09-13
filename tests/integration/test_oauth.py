"""The OAuth flow claude.ai runs, end to end over real uvicorn."""

from __future__ import annotations

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.oauth_helpers import (
    ACCESS_HEADER,
    CLAUDE_REDIRECT,
    authorize,
    code_from,
    exchange,
    obtain_tokens,
    pkce,
    register,
    resource_url,
)

INIT_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
PING = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "ping", "arguments": {}},
}


def auth(tokens: dict) -> dict[str, str]:
    return {**INIT_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
async def http(live_server):
    async with httpx2.AsyncClient(base_url=live_server, follow_redirects=False) as c:
        yield c


async def test_authorization_server_metadata(http, live_server):
    r = await http.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["issuer"] == live_server
    assert body["authorization_endpoint"] == f"{live_server}/oauth/authorize"
    assert body["token_endpoint"] == f"{live_server}/oauth/token"
    assert body["registration_endpoint"] == f"{live_server}/oauth/register"
    assert body["code_challenge_methods_supported"] == ["S256"]


@pytest.mark.parametrize("key", ["manifold", "ping-b"])
async def test_protected_resource_metadata(http, live_server, key):
    r = await http.get(f"/.well-known/oauth-protected-resource/{key}")
    assert r.status_code == 200
    assert r.json()["resource"] == f"{live_server}/{key}"
    assert r.json()["authorization_servers"] == [live_server]


async def test_protected_resource_metadata_unknown_key(http):
    assert (await http.get("/.well-known/oauth-protected-resource/nope")).status_code == 404


@pytest.mark.parametrize("key", ["manifold", "ping-b"])
async def test_endpoint_without_token_is_401_with_discovery_hint(http, live_server, key):
    r = await http.post(f"/{key}", json=PING, headers=INIT_HEADERS)
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert f'resource_metadata="{live_server}/.well-known/oauth-protected-resource/{key}"' in www


async def test_endpoint_with_bad_token_is_401(http):
    r = await http.post(
        "/manifold", json=PING, headers={**INIT_HEADERS, "Authorization": "Bearer nope"}
    )
    assert r.status_code == 401


async def test_healthz_stays_open(http):
    assert (await http.get("/manifold/healthz")).status_code == 200


async def test_register_rejects_foreign_redirect(http):
    r = await http.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


async def test_authorize_without_access_identity_is_403(http):
    client = await register(http)
    _, challenge = pkce()
    r = await authorize(http, client["client_id"], challenge, headers={})
    assert r.status_code == 403
    assert "location" not in r.headers


async def test_authorize_with_non_admin_identity_is_403(http):
    client = await register(http)
    _, challenge = pkce()
    r = await authorize(
        http,
        client["client_id"],
        challenge,
        headers={"Cf-Access-Authenticated-User-Email": "stranger@example.com"},
    )
    assert r.status_code == 403


async def test_authorize_unknown_client_is_an_error_not_a_redirect(http):
    _, challenge = pkce()
    r = await authorize(http, "not-a-client", challenge)
    assert r.status_code == 400


async def test_full_flow_issues_tokens_and_ping_works(http, live_server):
    _, tokens = await obtain_tokens(http, "manifold")
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    r = await http.post(
        "/manifold",
        json=PING,
        headers={**INIT_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert r.status_code == 200, r.text
    assert "pong from manifold" in r.json()["result"]["content"][0]["text"]


def error_from(response):
    assert response.status_code == 302, response.text
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(response.headers["location"]).query)


async def test_pkce_plain_is_rejected(http):
    client = await register(http)
    _, challenge = pkce()
    r = await authorize(http, client["client_id"], challenge, method="plain")
    assert error_from(r)["error"] == ["invalid_request"]


async def test_authorize_without_pkce_is_rejected(http):
    client = await register(http)
    r = await http.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": CLAUDE_REDIRECT,
            "resource": resource_url(http, "manifold"),
        },
        headers=ACCESS_HEADER,
    )
    assert error_from(r)["error"] == ["invalid_request"]


async def test_authorize_without_resource_is_invalid_target(http):
    client = await register(http)
    _, challenge = pkce()
    r = await authorize(http, client["client_id"], challenge, resource="")
    assert error_from(r)["error"] == ["invalid_target"]


@pytest.mark.parametrize("bad", ["/nope", "/manifold/deeper", "/healthz"])
async def test_authorize_for_unknown_resource_is_invalid_target(http, live_server, bad):
    client = await register(http)
    _, challenge = pkce()
    r = await authorize(http, client["client_id"], challenge, resource=f"{live_server}{bad}")
    assert error_from(r)["error"] == ["invalid_target"]


async def test_authorize_for_foreign_origin_is_invalid_target(http):
    client = await register(http)
    _, challenge = pkce()
    r = await authorize(
        http, client["client_id"], challenge, resource="https://other.example/manifold"
    )
    assert error_from(r)["error"] == ["invalid_target"]


async def test_state_round_trips(http):
    client = await register(http)
    _, challenge = pkce()
    _, state = code_from(await authorize(http, client["client_id"], challenge, state="abc123"))
    assert state == "abc123"


async def test_wrong_verifier_is_rejected_and_code_survives_only_once(http):
    client = await register(http)
    verifier, challenge = pkce()
    code, _ = code_from(await authorize(http, client["client_id"], challenge))
    bad = await exchange(http, client, code, "wrong-verifier")
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_grant"
    good = await exchange(http, client, code, verifier)
    assert good.status_code == 200
    again = await exchange(http, client, code, verifier)
    assert again.status_code == 400


async def test_confidential_client_needs_its_secret(http):
    client = await register(http, auth_method="client_secret_post")
    assert client["client_secret"]
    verifier, challenge = pkce()
    code, _ = code_from(await authorize(http, client["client_id"], challenge))
    r = await http.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client["client_id"],
            "redirect_uri": CLAUDE_REDIRECT,
        },
    )
    assert r.status_code == 401


async def test_refresh_rotates(http):
    client, first = await obtain_tokens(http)
    r = await http.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client["client_id"],
        },
    )
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["access_token"] != first["access_token"]
    old = await http.post(
        "/manifold",
        json=PING,
        headers={**INIT_HEADERS, "Authorization": f"Bearer {first['access_token']}"},
    )
    assert old.status_code == 401
    new = await http.post(
        "/manifold",
        json=PING,
        headers={**INIT_HEADERS, "Authorization": f"Bearer {second['access_token']}"},
    )
    assert new.status_code == 200


async def test_revoke(http):
    client, tokens = await obtain_tokens(http)
    r = await http.post(
        "/oauth/revoke",
        # The SDK's revocation model requires the client_secret key even for public clients.
        data={
            "token": tokens["access_token"],
            "client_id": client["client_id"],
            "client_secret": "",
        },
    )
    assert r.status_code == 200, r.text
    gone = await http.post(
        "/manifold",
        json=PING,
        headers={**INIT_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert gone.status_code == 401


async def test_token_bound_to_one_toolset_is_refused_by_another(http):
    _, bound = await obtain_tokens(http, "ping-b")
    assert (await http.post("/ping-b", json=PING, headers=auth(bound))).status_code == 200
    assert (await http.post("/manifold", json=PING, headers=auth(bound))).status_code == 401


@pytest.mark.parametrize(
    ("key", "expected"), [("manifold", "pong from manifold"), ("ping-b", "pong from ping-b")]
)
async def test_sdk_client_with_bearer(http, live_server, key, expected):
    _, tokens = await obtain_tokens(http, key)
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {tokens['access_token']}"}) as authed,
        streamable_http_client(f"{live_server}/{key}", http_client=authed) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("ping")
        assert not result.is_error
        assert result.content[0].text.startswith(expected)


async def test_oauth_paths_never_redirect_except_authorize(http):
    for method, path in [
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/.well-known/oauth-protected-resource/manifold"),
        ("POST", "/oauth/token"),
        ("POST", "/oauth/register"),
        ("POST", "/oauth/revoke"),
        ("GET", "/oauth"),
        ("GET", "/oauth/"),
        ("GET", "/oauth/callback"),
    ]:
        r = await http.request(method, path, headers=ACCESS_HEADER)
        assert not 300 <= r.status_code < 400, (method, path, r.status_code)
