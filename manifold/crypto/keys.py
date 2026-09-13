"""Key derivation. Every purpose gets its own subkey from the master key via HKDF-SHA256.

The `info` strings below are the complete list. Add a new purpose here and to
DECISIONS.md in the same commit. Never reuse one for a different purpose.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INFO_CREDENTIALS = b"manifold/credentials/v1"

KEY_BYTES = 32


def derive_key(master_key: bytes, info: bytes) -> bytes:
    if len(master_key) != KEY_BYTES:
        raise ValueError("master key must be 32 bytes")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info).derive(
        master_key
    )
