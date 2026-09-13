"""Credentials repository. Values are encrypted at rest and decrypted only on `get()`.

Nothing here logs a value, and list results never include one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from manifold.crypto.box import SCHEME, DecryptError, credential_aad, decrypt, encrypt
from manifold.gateway.manifest import AuthKind, Credentials
from manifold.store.db import Database, utcnow


class CredentialNotFound(LookupError):
    pass


class CredentialInUse(ValueError):
    def __init__(self, credential_id: int, toolset_keys: list[str]) -> None:
        self.credential_id = credential_id
        self.toolset_keys = toolset_keys
        super().__init__(
            f"credential {credential_id} is used by {', '.join(toolset_keys)}; "
            "detach it from those toolsets first"
        )


class CredentialUnreadable(RuntimeError):
    """The row exists but does not decrypt: tampered, moved, or wrong key."""


@dataclass(frozen=True)
class CredentialSummary:
    id: int
    name: str
    auth_kind: AuthKind
    created_at: str
    updated_at: str
    used_by: tuple[str, ...]


class CredentialsRepo:
    def __init__(self, db: Database, key: bytes) -> None:
        self._db = db
        self._key = key

    async def create(self, name: str, auth_kind: AuthKind, values: dict) -> int:
        conn = self._db.conn
        await conn.execute("BEGIN IMMEDIATE")
        try:
            async with conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM credentials") as c:
                credential_id = int((await c.fetchone())[0])
            nonce, ciphertext = self._seal(credential_id, auth_kind, values)
            now = utcnow()
            await conn.execute(
                "INSERT INTO credentials (id, name, auth_kind, scheme, nonce, ciphertext,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (credential_id, name, auth_kind, SCHEME, nonce, ciphertext, now, now),
            )
            await conn.execute("COMMIT")
        except Exception:
            await conn.execute("ROLLBACK")
            raise
        return credential_id

    async def get(self, credential_id: int) -> Credentials:
        async with self._db.conn.execute(
            "SELECT auth_kind, scheme, nonce, ciphertext FROM credentials WHERE id = ?",
            (credential_id,),
        ) as c:
            row = await c.fetchone()
        if row is None:
            raise CredentialNotFound(credential_id)
        auth_kind, scheme, nonce, ciphertext = row[0], row[1], row[2], row[3]
        if scheme != SCHEME:
            raise CredentialUnreadable(f"credential {credential_id} uses unknown scheme {scheme!r}")
        try:
            plaintext = decrypt(
                self._key, nonce, ciphertext, credential_aad(scheme, credential_id, auth_kind)
            )
        except DecryptError as exc:
            raise CredentialUnreadable(f"credential {credential_id} did not decrypt") from exc
        return Credentials(kind=auth_kind, values=json.loads(plaintext))

    async def update_values(self, credential_id: int, values: dict) -> None:
        summary = await self.get_summary(credential_id)
        nonce, ciphertext = self._seal(credential_id, summary.auth_kind, values)
        await self._db.conn.execute(
            "UPDATE credentials SET nonce = ?, ciphertext = ?, scheme = ?, updated_at = ?"
            " WHERE id = ?",
            (nonce, ciphertext, SCHEME, utcnow(), credential_id),
        )

    async def rename(self, credential_id: int, name: str) -> None:
        cursor = await self._db.conn.execute(
            "UPDATE credentials SET name = ?, updated_at = ? WHERE id = ?",
            (name, utcnow(), credential_id),
        )
        if cursor.rowcount == 0:
            raise CredentialNotFound(credential_id)

    async def delete(self, credential_id: int) -> None:
        summary = await self.get_summary(credential_id)
        if summary.used_by:
            raise CredentialInUse(credential_id, list(summary.used_by))
        await self._db.conn.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))

    async def get_summary(self, credential_id: int) -> CredentialSummary:
        for summary in await self.list():
            if summary.id == credential_id:
                return summary
        raise CredentialNotFound(credential_id)

    async def list(self) -> list[CredentialSummary]:
        async with self._db.conn.execute(
            "SELECT c.id, c.name, c.auth_kind, c.created_at, c.updated_at,"
            " COALESCE(GROUP_CONCAT(t.key, ','), '') AS used_by"
            " FROM credentials c LEFT JOIN toolsets t ON t.credential_id = c.id"
            " GROUP BY c.id ORDER BY c.name"
        ) as c:
            rows = await c.fetchall()
        return [
            CredentialSummary(
                id=int(r["id"]),
                name=r["name"],
                auth_kind=r["auth_kind"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                used_by=tuple(sorted(k for k in r["used_by"].split(",") if k)),
            )
            for r in rows
        ]

    def _seal(self, credential_id: int, auth_kind: str, values: dict) -> tuple[bytes, bytes]:
        plaintext = json.dumps(values, separators=(",", ":")).encode()
        return encrypt(self._key, plaintext, credential_aad(SCHEME, credential_id, auth_kind))
