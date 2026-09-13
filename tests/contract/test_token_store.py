"""Every TokenStore implementation must pass these.

Tokens and codes always belong to a registered client; the provider guarantees it and the
SQLite schema enforces it, so every test registers the client first."""

from __future__ import annotations

import pytest
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from manifold.auth.sqlite_store import SqliteTokenStore
from manifold.auth.store import InMemoryTokenStore, TokenStore
from manifold.store.db import Database


@pytest.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path) -> TokenStore:
    if request.param == "memory":
        yield InMemoryTokenStore()
        return
    db = Database(tmp_path)
    await db.open()
    await db.migrate()
    yield SqliteTokenStore(db)
    await db.close()


def client(client_id: str = "c1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=["https://claude.ai/cb"],
        token_endpoint_auth_method="none",
    )


def code(code_hash: str = "h-code", expires_at: float = 10**12) -> AuthorizationCode:
    return AuthorizationCode(
        code=code_hash,
        scopes=[],
        expires_at=expires_at,
        client_id="c1",
        code_challenge="x",
        redirect_uri="https://claude.ai/cb",
        redirect_uri_provided_explicitly=True,
        resource="https://mcp.example/manifold",
        subject="manny@example.com",
    )


def pair(a: str = "h-access", r: str = "h-refresh", expires_at: int = 10**12):
    return (
        AccessToken(token=a, client_id="c1", scopes=[], expires_at=expires_at),
        RefreshToken(token=r, client_id="c1", scopes=[], expires_at=expires_at),
    )


async def test_clients_round_trip_and_count(store):
    assert await store.get_client("c1") is None
    assert await store.count_clients() == 0
    await store.save_client(client("c1"))
    await store.save_client(client("c2"))
    await store.save_client(client("c1"))
    assert (await store.get_client("c1")).client_id == "c1"
    assert await store.count_clients() == 2


async def test_code_is_single_use(store):
    await store.save_client(client())
    await store.save_code(code())
    assert (await store.get_code("h-code")).client_id == "c1"
    assert await store.delete_code("h-code") is True
    assert await store.get_code("h-code") is None
    assert await store.delete_code("h-code") is False


async def test_token_pair_round_trip(store):
    await store.save_client(client())
    await store.save_token_pair(*pair())
    assert (await store.get_access_token("h-access")).client_id == "c1"
    assert (await store.get_refresh_token("h-refresh")).client_id == "c1"
    assert await store.get_access_token("h-refresh") is None
    assert await store.get_refresh_token("h-access") is None


@pytest.mark.parametrize("which", ["h-access", "h-refresh"])
async def test_revoking_either_removes_both(store, which):
    await store.save_client(client())
    await store.save_token_pair(*pair())
    await store.save_token_pair(*pair("other-a", "other-r"))
    await store.revoke(which)
    assert await store.get_access_token("h-access") is None
    assert await store.get_refresh_token("h-refresh") is None
    assert await store.get_access_token("other-a") is not None


async def test_revoking_unknown_is_a_noop(store):
    await store.save_client(client())
    await store.revoke("nothing")


async def test_sweep_drops_expired_only(store):
    await store.save_client(client())
    await store.save_code(code("old", expires_at=100))
    await store.save_code(code("new", expires_at=10**12))
    await store.save_token_pair(*pair("a-old", "r-old", expires_at=100))
    await store.save_token_pair(*pair("a-new", "r-new"))
    await store.sweep(now=1000)
    assert await store.get_code("old") is None
    assert await store.get_code("new") is not None
    assert await store.get_access_token("a-old") is None
    assert await store.get_refresh_token("r-old") is None
    assert await store.get_access_token("a-new") is not None


async def test_clear_forgets_everything(store):
    await store.save_client(client())
    await store.save_code(code())
    await store.save_token_pair(*pair())
    await store.clear()
    assert await store.count_clients() == 0
    assert await store.get_code("h-code") is None
    assert await store.get_access_token("h-access") is None
