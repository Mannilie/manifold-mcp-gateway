"""AES-256-GCM with associated data that binds a ciphertext to its row."""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCHEME = "aes256gcm-v1"
NONCE_BYTES = 12


class DecryptError(ValueError):
    """Wrong key, tampered ciphertext, or ciphertext moved to another row or scheme."""


def credential_aad(scheme: str, credential_id: int, auth_kind: str) -> bytes:
    """Associated data for a credential row. The scheme is included so editing the
    `scheme` column cannot downgrade a ciphertext; the id so it cannot move rows."""
    return f"{scheme}|{credential_id}|{auth_kind}".encode()


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptError("ciphertext did not authenticate") from exc
