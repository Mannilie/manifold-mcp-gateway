"""Master key rotation: re-encrypt every credential under a new key, in one transaction,
after a snapshot. Run inside the container with the OLD key in MANIFOLD_MASTER_KEY:

    python -m manifold rotate-key --new-key <base64 of 32 bytes>

Then change MANIFOLD_MASTER_KEY to the new value and restart the container.
"""

from __future__ import annotations

import logging

from manifold.crypto.box import SCHEME, credential_aad, decrypt, encrypt
from manifold.crypto.keycheck import KEY_CHECK_PLAINTEXT, MasterKeyError, _aad, verify_or_initialise
from manifold.store.db import Database, utcnow
from manifold.store.snapshots import SnapshotStore

log = logging.getLogger(__name__)


async def rotate(db: Database, old_key: bytes, new_key: bytes) -> int:
    """Returns the number of credentials re-encrypted. Raises if the snapshot fails."""
    if old_key == new_key:
        raise ValueError("the new key is the same as the current key")
    # Prove the current key before touching anything. With no credentials stored, nothing
    # else in the loop would catch a wrong key and the key_check row would be silently re-keyed.
    if await verify_or_initialise(db, old_key):
        await db.conn.execute("DELETE FROM key_check WHERE id = 1")
        await db.conn.commit()
        raise MasterKeyError("this database has never been booted, there is nothing to rotate")
    snapshots = SnapshotStore(db, old_key)
    snap = await snapshots.create("pre-rotation")  # refuses to continue if this raises
    log.info("pre-rotation snapshot written", extra={"snapshot": snap.name})
    conn = db.conn
    await conn.execute("BEGIN IMMEDIATE")
    try:
        async with conn.execute(
            "SELECT id, auth_kind, scheme, nonce, ciphertext FROM credentials"
        ) as c:
            rows = await c.fetchall()
        count = 0
        for row in rows:
            cid, kind, scheme, nonce, ciphertext = row
            aad = credential_aad(scheme, cid, kind)
            plain = decrypt(old_key, nonce, ciphertext, aad)
            new_nonce, new_cipher = encrypt(new_key, plain, credential_aad(SCHEME, cid, kind))
            await conn.execute(
                "UPDATE credentials SET nonce = ?, ciphertext = ?, scheme = ? WHERE id = ?",
                (new_nonce, new_cipher, SCHEME, cid),
            )
            count += 1
        nonce, ciphertext = encrypt(new_key, KEY_CHECK_PLAINTEXT, _aad(SCHEME))
        await conn.execute(
            "UPDATE key_check SET scheme = ?, nonce = ?, ciphertext = ?, created_at = ?"
            " WHERE id = 1",
            (SCHEME, nonce, ciphertext, utcnow()),
        )
        await conn.execute("COMMIT")
    except Exception:
        await conn.execute("ROLLBACK")
        raise
    log.info("master key rotated", extra={"credentials": count})
    return count
