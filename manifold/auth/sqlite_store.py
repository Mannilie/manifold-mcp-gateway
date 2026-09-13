"""`TokenStore` on SQLite. Survives restarts, so claude.ai keeps its connectors."""

from __future__ import annotations

import json

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from manifold.store.db import Database, utcnow


class SqliteTokenStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- clients -----------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with self._db.conn.execute(
            "SELECT metadata_json FROM oauth_clients WHERE client_id = ?", (client_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        await self._db.conn.execute(
            "INSERT INTO oauth_clients (client_id, metadata_json, created_at) VALUES (?, ?, ?)"
            " ON CONFLICT(client_id) DO UPDATE SET metadata_json = excluded.metadata_json",
            (client.client_id, client.model_dump_json(), utcnow()),
        )

    async def count_clients(self) -> int:
        async with self._db.conn.execute("SELECT COUNT(*) FROM oauth_clients") as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    # -- codes -------------------------------------------------------------------------

    async def save_code(self, code: AuthorizationCode) -> None:
        await self._insert("code", code.code, code, partner=None)

    async def get_code(self, code_hash: str) -> AuthorizationCode | None:
        payload = await self._payload(code_hash, "code")
        return AuthorizationCode.model_validate_json(payload) if payload else None

    async def delete_code(self, code_hash: str) -> bool:
        cursor = await self._db.conn.execute(
            "DELETE FROM oauth_tokens WHERE token_hash = ? AND kind = 'code'", (code_hash,)
        )
        return cursor.rowcount > 0

    # -- tokens ------------------------------------------------------------------------

    async def save_token_pair(self, access: AccessToken, refresh: RefreshToken) -> None:
        await self._db.conn.execute("BEGIN")
        try:
            await self._insert("access", access.token, access, partner=refresh.token)
            await self._insert("refresh", refresh.token, refresh, partner=access.token)
            await self._db.conn.execute("COMMIT")
        except Exception:
            await self._db.conn.execute("ROLLBACK")
            raise

    async def get_access_token(self, token_hash: str) -> AccessToken | None:
        payload = await self._payload(token_hash, "access")
        return AccessToken.model_validate_json(payload) if payload else None

    async def get_refresh_token(self, token_hash: str) -> RefreshToken | None:
        payload = await self._payload(token_hash, "refresh")
        return RefreshToken.model_validate_json(payload) if payload else None

    async def revoke(self, token_hash: str) -> None:
        await self._db.conn.execute(
            "DELETE FROM oauth_tokens WHERE token_hash = ? OR partner_hash = ?",
            (token_hash, token_hash),
        )

    async def sweep(self, now: float) -> None:
        # Expired refresh tokens take their access partner with them, and vice versa.
        await self._db.conn.execute(
            "DELETE FROM oauth_tokens WHERE token_hash IN ("
            "  SELECT token_hash FROM oauth_tokens WHERE expires_at IS NOT NULL AND expires_at < ?"
            "  UNION"
            "  SELECT partner_hash FROM oauth_tokens"
            "  WHERE partner_hash IS NOT NULL AND expires_at IS NOT NULL AND expires_at < ?"
            ")",
            (now, now),
        )

    async def clear(self) -> None:
        # oauth_tokens cascades from oauth_clients.
        await self._db.conn.execute("DELETE FROM oauth_clients")
        await self._db.conn.execute("DELETE FROM oauth_tokens")

    # -- internals ---------------------------------------------------------------------

    async def _insert(
        self,
        kind: str,
        token_hash: str,
        model: AuthorizationCode | AccessToken | RefreshToken,
        partner: str | None,
    ) -> None:
        await self._db.conn.execute(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, subject, resource,"
            " scopes_json, expires_at, partner_hash, payload_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token_hash,
                kind,
                model.client_id,
                model.subject,
                model.resource,
                json.dumps(model.scopes),
                model.expires_at,
                partner,
                model.model_dump_json(),
                utcnow(),
            ),
        )

    async def _payload(self, token_hash: str, kind: str) -> str | None:
        async with self._db.conn.execute(
            "SELECT payload_json FROM oauth_tokens WHERE token_hash = ? AND kind = ?",
            (token_hash, kind),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None
