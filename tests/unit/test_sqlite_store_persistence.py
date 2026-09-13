"""The reason the SQLite store exists: state survives a process restart."""

from __future__ import annotations

from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from manifold.auth.sqlite_store import SqliteTokenStore
from manifold.store.db import Database


async def test_clients_and_tokens_survive_reopen(tmp_path):
    db = Database(tmp_path)
    await db.open()
    await db.migrate()
    store = SqliteTokenStore(db)
    await store.save_client(
        OAuthClientInformationFull(
            client_id="c1",
            redirect_uris=["https://claude.ai/cb"],
            token_endpoint_auth_method="none",
        )
    )
    await store.save_token_pair(
        AccessToken(token="a", client_id="c1", scopes=[], expires_at=10**12, resource="r"),
        RefreshToken(token="r", client_id="c1", scopes=[], expires_at=10**12, resource="r"),
    )
    await db.close()

    db = Database(tmp_path)
    await db.open()
    await db.migrate()
    store = SqliteTokenStore(db)
    try:
        assert (await store.get_client("c1")).client_id == "c1"
        assert (await store.get_access_token("a")).resource == "r"
        assert (await store.get_refresh_token("r")).client_id == "c1"
    finally:
        await db.close()


async def test_clearing_clients_cascades_to_tokens(tmp_path):
    db = Database(tmp_path)
    await db.open()
    await db.migrate()
    store = SqliteTokenStore(db)
    try:
        await store.save_client(
            OAuthClientInformationFull(
                client_id="c1",
                redirect_uris=["https://claude.ai/cb"],
                token_endpoint_auth_method="none",
            )
        )
        await store.save_token_pair(
            AccessToken(token="a", client_id="c1", scopes=[], expires_at=10**12),
            RefreshToken(token="r", client_id="c1", scopes=[], expires_at=10**12),
        )
        await db.conn.execute("DELETE FROM oauth_clients WHERE client_id = 'c1'")
        assert await store.get_access_token("a") is None
    finally:
        await db.close()
