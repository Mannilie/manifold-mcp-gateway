"""Boot-time proof that MANIFOLD_MASTER_KEY is the key this database was created with.

The row is encrypted under the derived credentials key, not the raw master key, so it
also proves the derivation is stable across Manifold versions.
"""

from __future__ import annotations

import logging

from manifold.crypto.box import SCHEME, DecryptError, decrypt, encrypt
from manifold.store.db import Database, utcnow

log = logging.getLogger(__name__)

KEY_CHECK_PLAINTEXT = b"manifold key check"


def _aad(scheme: str) -> bytes:
    return f"{scheme}|key-check".encode()


class MasterKeyError(RuntimeError):
    pass


async def verify_or_initialise(db: Database, credentials_key: bytes) -> bool:
    """Return True if the row was created now (first run), False if it verified.

    Raises MasterKeyError if the row exists and does not decrypt.
    """
    async with db.conn.execute(
        "SELECT scheme, nonce, ciphertext FROM key_check WHERE id = 1"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        nonce, ciphertext = encrypt(credentials_key, KEY_CHECK_PLAINTEXT, _aad(SCHEME))
        await db.conn.execute(
            "INSERT INTO key_check (id, scheme, nonce, ciphertext, created_at)"
            " VALUES (1, ?, ?, ?, ?)",
            (SCHEME, nonce, ciphertext, utcnow()),
        )
        log.warning(
            "first run: master key check written. Losing MANIFOLD_MASTER_KEY loses every "
            "stored credential. Keep it in a password manager."
        )
        return True
    scheme, nonce, ciphertext = row[0], row[1], row[2]
    if scheme != SCHEME:
        raise MasterKeyError(
            f"key_check row uses scheme {scheme!r}, this build only knows {SCHEME!r}"
        )
    try:
        plaintext = decrypt(credentials_key, nonce, ciphertext, _aad(scheme))
    except DecryptError as exc:
        raise MasterKeyError(
            "MANIFOLD_MASTER_KEY does not match the key this database was created with. "
            "Restore the original key from your password manager. If it is lost, every "
            "stored credential is unrecoverable: delete the database to start again."
        ) from exc
    if plaintext != KEY_CHECK_PLAINTEXT:
        raise MasterKeyError("key_check row decrypted to unexpected content")
    return False
