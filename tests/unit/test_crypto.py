from __future__ import annotations

import pytest

from manifold.crypto.box import SCHEME, DecryptError, credential_aad, decrypt, encrypt
from manifold.crypto.keys import INFO_CREDENTIALS, derive_key

MASTER = b"\x01" * 32


def test_derivation_is_deterministic_and_purpose_bound():
    a = derive_key(MASTER, INFO_CREDENTIALS)
    assert a == derive_key(MASTER, INFO_CREDENTIALS)
    assert a != derive_key(MASTER, b"manifold/other/v1")
    assert a != MASTER
    assert len(a) == 32


def test_derivation_rejects_wrong_length():
    with pytest.raises(ValueError):
        derive_key(b"short", INFO_CREDENTIALS)


def test_round_trip_and_fresh_nonce():
    key = derive_key(MASTER, INFO_CREDENTIALS)
    aad = credential_aad(SCHEME, 7, "api_key")
    n1, c1 = encrypt(key, b"secret", aad)
    n2, c2 = encrypt(key, b"secret", aad)
    assert n1 != n2 and c1 != c2
    assert decrypt(key, n1, c1, aad) == b"secret"


@pytest.mark.parametrize(
    "bad_aad",
    [
        credential_aad(SCHEME, 8, "api_key"),  # moved to another row
        credential_aad("aes256gcm-v0", 7, "api_key"),  # scheme column edited
        credential_aad(SCHEME, 7, "bearer"),  # auth kind edited
    ],
)
def test_associated_data_binds_row_scheme_and_kind(bad_aad):
    key = derive_key(MASTER, INFO_CREDENTIALS)
    nonce, ciphertext = encrypt(key, b"secret", credential_aad(SCHEME, 7, "api_key"))
    with pytest.raises(DecryptError):
        decrypt(key, nonce, ciphertext, bad_aad)


def test_wrong_key_fails():
    nonce, ciphertext = encrypt(derive_key(MASTER, INFO_CREDENTIALS), b"s", b"aad")
    with pytest.raises(DecryptError):
        decrypt(derive_key(b"\x02" * 32, INFO_CREDENTIALS), nonce, ciphertext, b"aad")


def test_tampered_ciphertext_fails():
    key = derive_key(MASTER, INFO_CREDENTIALS)
    nonce, ciphertext = encrypt(key, b"secret", b"aad")
    tampered = bytes([ciphertext[0] ^ 1]) + ciphertext[1:]
    with pytest.raises(DecryptError):
        decrypt(key, nonce, tampered, b"aad")
