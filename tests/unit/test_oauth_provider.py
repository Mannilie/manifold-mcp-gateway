from __future__ import annotations

import time

import pytest
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull

from manifold.auth.access import current_access_email
from manifold.auth.provider import ManifoldOAuthProvider, ToolsetTokenVerifier
from manifold.auth.store import InMemoryTokenStore

ADMINS = frozenset({"manny@example.com"})
BASE = "https://mcp.example"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
TOOLSETS = {"sheets", "unraid"}


def client_info(client_id: str = "c1", redirect: str = REDIRECT) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id, redirect_uris=[redirect], token_endpoint_auth_method="none"
    )


def params(resource: str | None = f"{BASE}/sheets") -> AuthorizationParams:
    return AuthorizationParams(
        state="s",
        scopes=None,
        code_challenge="challenge",
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
        resource=resource,
    )


@pytest.fixture
def provider() -> ManifoldOAuthProvider:
    return ManifoldOAuthProvider(
        InMemoryTokenStore(), ADMINS, BASE, known_toolset=lambda key: key in TOOLSETS
    )


@pytest.fixture
def as_admin():
    token = current_access_email.set("manny@example.com")
    yield
    current_access_email.reset(token)


async def issue(provider: ManifoldOAuthProvider, client, resource=f"{BASE}/sheets"):
    url = await provider.authorize(client, params(resource))
    code = url.split("code=")[1].split("&")[0]
    stored = await provider.load_authorization_code(client, code)
    assert stored is not None
    return await provider.exchange_authorization_code(client, stored)


async def test_register_rejects_non_claude_redirects(provider):
    for bad in (
        "https://evil.example/cb",
        "http://claude.ai/cb",
        "https://claude.ai.evil.example/cb",
    ):
        with pytest.raises(RegistrationError) as info:
            await provider.register_client(client_info(redirect=bad))
        assert info.value.error == "invalid_redirect_uri"
    await provider.register_client(client_info(redirect="https://claude.com/api/mcp/auth_callback"))
    assert await provider.get_client("c1") is not None


async def test_authorize_requires_access_identity(provider):
    with pytest.raises(AuthorizeError) as info:
        await provider.authorize(client_info(), params())
    assert info.value.error == "access_denied"


async def test_authorize_refuses_non_admin(provider):
    token = current_access_email.set("stranger@example.com")
    try:
        with pytest.raises(AuthorizeError):
            await provider.authorize(client_info(), params())
    finally:
        current_access_email.reset(token)


@pytest.mark.parametrize(
    "resource",
    [None, "", f"{BASE}/nope", f"{BASE}/sheets/deeper", "https://other.example/sheets", BASE],
)
async def test_authorize_requires_a_mounted_toolset_resource(provider, as_admin, resource):
    with pytest.raises(AuthorizeError) as info:
        await provider.authorize(client_info(), params(resource))
    assert info.value.error == "invalid_target"


async def test_resource_normalisation(provider):
    assert provider.toolset_for_resource(f"{BASE}/sheets/") == "sheets"
    assert provider.toolset_for_resource("HTTPS://MCP.EXAMPLE/Sheets") == "sheets"
    assert provider.toolset_for_resource(f"{BASE}/sheets?x=1") == "sheets"


async def test_authorize_redirects_with_code_and_state(provider, as_admin):
    url = await provider.authorize(client_info(), params())
    assert url.startswith(REDIRECT + "?")
    assert "code=" in url and "state=s" in url


async def test_code_is_bound_to_client_and_single_use(provider, as_admin):
    client = client_info()
    url = await provider.authorize(client, params())
    code = url.split("code=")[1].split("&")[0]
    assert await provider.load_authorization_code(client_info("other"), code) is None
    stored = await provider.load_authorization_code(client, code)
    tokens = await provider.exchange_authorization_code(client, stored)
    assert tokens.access_token and tokens.refresh_token
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, stored)


async def test_subject_and_resource_propagate(provider, as_admin):
    tokens = await issue(provider, client_info())
    stored = await provider.load_access_token(tokens.access_token)
    assert stored is not None
    assert stored.subject == "manny@example.com"
    assert stored.resource == f"{BASE}/sheets"


async def test_refresh_rotates_and_invalidates_old_pair(provider, as_admin):
    client = client_info()
    first = await issue(provider, client)
    stored_refresh = await provider.load_refresh_token(client, first.refresh_token)
    second = await provider.exchange_refresh_token(client, stored_refresh, [])
    assert second.access_token != first.access_token
    assert await provider.load_access_token(first.access_token) is None
    assert await provider.load_refresh_token(client, first.refresh_token) is None
    stored = await provider.load_access_token(second.access_token)
    assert stored is not None and stored.resource == f"{BASE}/sheets"


async def test_revoking_one_token_revokes_its_partner(provider, as_admin):
    client = client_info()
    tokens = await issue(provider, client)
    stored = await provider.load_access_token(tokens.access_token)
    await provider.revoke_token(stored)
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_expired_tokens_are_rejected(provider, as_admin, monkeypatch):
    client = client_info()
    tokens = await issue(provider, client)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 2 * 60 * 60)
    assert await provider.load_access_token(tokens.access_token) is None
    monkeypatch.setattr(time, "time", lambda: real() + 31 * 24 * 60 * 60)
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


async def test_raw_tokens_are_never_stored(provider, as_admin):
    tokens = await issue(provider, client_info())
    everything = repr(vars(provider.store))
    assert tokens.access_token not in everything
    assert tokens.refresh_token not in everything


async def test_unknown_token_is_none(provider):
    assert await provider.load_access_token("nope") is None
    assert await provider.load_refresh_token(client_info(), "nope") is None


async def test_verifier_accepts_only_its_own_toolset(provider, as_admin):
    sheets_token = await issue(provider, client_info(), f"{BASE}/sheets")
    sheets = ToolsetTokenVerifier(provider, f"{BASE}/sheets")
    unraid = ToolsetTokenVerifier(provider, f"{BASE}/unraid")
    assert await sheets.verify_token(sheets_token.access_token) is not None
    assert await unraid.verify_token(sheets_token.access_token) is None
    assert await sheets.verify_token("garbage") is None


async def test_verifier_comparison_ignores_case_and_slash(provider, as_admin):
    tokens = await issue(provider, client_info(), f"{BASE}/sheets/")
    verifier = ToolsetTokenVerifier(provider, "https://MCP.example/sheets")
    assert await verifier.verify_token(tokens.access_token) is not None
