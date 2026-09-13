from __future__ import annotations

import base64
import os

import pytest

from manifold.crypto.box import SCHEME
from manifold.crypto.keycheck import MasterKeyError, verify_or_initialise
from manifold.crypto.keys import INFO_CREDENTIALS, derive_key
from manifold.store.credentials import (
    CredentialInUse,
    CredentialNotFound,
    CredentialsRepo,
    CredentialUnreadable,
)
from manifold.store.db import Database, utcnow

KEY = derive_key(b"\x01" * 32, INFO_CREDENTIALS)
OTHER_KEY = derive_key(b"\x02" * 32, INFO_CREDENTIALS)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path)
    await database.open()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
async def repo(db) -> CredentialsRepo:
    await verify_or_initialise(db, KEY)
    return CredentialsRepo(db, KEY)


# -- key check -----------------------------------------------------------------------


async def test_first_run_writes_then_later_runs_verify(db):
    assert await verify_or_initialise(db, KEY) is True
    assert await verify_or_initialise(db, KEY) is False


async def test_wrong_master_key_fails_at_boot_with_clear_message(db):
    await verify_or_initialise(db, KEY)
    with pytest.raises(MasterKeyError, match="does not match the key this database"):
        await verify_or_initialise(db, OTHER_KEY)


async def test_key_check_survives_reopen(tmp_path):
    db = Database(tmp_path)
    await db.open()
    await db.migrate()
    await verify_or_initialise(db, KEY)
    await db.close()
    db = Database(tmp_path)
    await db.open()
    try:
        assert await verify_or_initialise(db, KEY) is False
    finally:
        await db.close()


async def test_boot_fails_on_wrong_key_end_to_end(tmp_path, env, monkeypatch):
    """The whole app, not just the helper: create_app boots once, then refuses a new key."""
    from manifold.app import create_app
    from manifold.config.settings import Settings

    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    app = create_app(Settings.from_env(env))
    async with app.router.lifespan_context(app):
        pass
    env["MANIFOLD_MASTER_KEY"] = base64.b64encode(os.urandom(32)).decode()
    app = create_app(Settings.from_env(env))
    with pytest.raises(MasterKeyError, match="Restore the original key"):
        async with app.router.lifespan_context(app):
            pass


# -- credentials repo ----------------------------------------------------------------


async def test_create_get_round_trip(repo):
    cid = await repo.create("Google (Manny)", "api_key", {"key": "sk-live-123", "header": "X"})
    got = await repo.get(cid)
    assert got.kind == "api_key"
    assert got.values == {"key": "sk-live-123", "header": "X"}


async def test_list_never_contains_values(repo):
    await repo.create("A", "bearer", {"token": "very-secret-token"})
    listing = await repo.list()
    assert [s.name for s in listing] == ["A"]
    assert "very-secret-token" not in repr(listing)
    assert not hasattr(listing[0], "values")


async def test_values_are_not_stored_in_plaintext(repo, db):
    await repo.create("A", "bearer", {"token": "very-secret-token"})
    async with db.conn.execute("SELECT ciphertext, nonce FROM credentials") as c:
        row = await c.fetchone()
    assert b"very-secret-token" not in row[0] + row[1]


async def test_ciphertext_moved_to_another_row_fails(repo, db):
    a = await repo.create("A", "bearer", {"token": "a"})
    b = await repo.create("B", "bearer", {"token": "b"})
    async with db.conn.execute("SELECT nonce, ciphertext FROM credentials WHERE id = ?", (a,)) as c:
        nonce, ciphertext = await c.fetchone()
    await db.conn.execute(
        "UPDATE credentials SET nonce = ?, ciphertext = ? WHERE id = ?", (nonce, ciphertext, b)
    )
    with pytest.raises(CredentialUnreadable):
        await repo.get(b)
    assert (await repo.get(a)).values == {"token": "a"}


async def test_scheme_column_edit_fails(repo, db):
    cid = await repo.create("A", "bearer", {"token": "a"})
    await db.conn.execute("UPDATE credentials SET scheme = 'aes256gcm-v0' WHERE id = ?", (cid,))
    with pytest.raises(CredentialUnreadable):
        await repo.get(cid)


async def test_auth_kind_edit_fails(repo, db):
    cid = await repo.create("A", "bearer", {"token": "a"})
    await db.conn.execute("UPDATE credentials SET auth_kind = 'api_key' WHERE id = ?", (cid,))
    with pytest.raises(CredentialUnreadable):
        await repo.get(cid)


async def test_wrong_key_cannot_read(repo, db):
    cid = await repo.create("A", "bearer", {"token": "a"})
    with pytest.raises(CredentialUnreadable):
        await CredentialsRepo(db, OTHER_KEY).get(cid)


async def test_update_values_and_rename(repo):
    cid = await repo.create("A", "bearer", {"token": "old"})
    await repo.update_values(cid, {"token": "new"})
    await repo.rename(cid, "Renamed")
    assert (await repo.get(cid)).values == {"token": "new"}
    assert (await repo.get_summary(cid)).name == "Renamed"


async def test_delete_refused_while_in_use_and_lists_users(repo, db):
    cid = await repo.create("Google (Manny)", "service_account", {"client_email": "x"})
    now = utcnow()
    for key in ("sheets", "drive"):
        await db.conn.execute(
            "INSERT INTO toolsets (key, display_name, kind, enabled, credential_id, settings_json,"
            " created_at, updated_at) VALUES (?, ?, 'native', 0, ?, '{}', ?, ?)",
            (key, key, cid, now, now),
        )
    with pytest.raises(CredentialInUse) as info:
        await repo.delete(cid)
    assert info.value.toolset_keys == ["drive", "sheets"]
    assert (await repo.get_summary(cid)).used_by == ("drive", "sheets")
    await db.conn.execute("UPDATE toolsets SET credential_id = NULL")
    await repo.delete(cid)
    with pytest.raises(CredentialNotFound):
        await repo.get(cid)


async def test_names_are_unique(repo):
    import sqlite3

    await repo.create("A", "bearer", {"token": "a"})
    with pytest.raises(sqlite3.IntegrityError):
        await repo.create("A", "bearer", {"token": "b"})


async def test_scheme_recorded(repo, db):
    await repo.create("A", "bearer", {"token": "a"})
    async with db.conn.execute("SELECT scheme FROM credentials") as c:
        assert (await c.fetchone())[0] == SCHEME
