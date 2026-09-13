from __future__ import annotations

import pytest

import manifold.toolsets
import tests.fixtures.toolsets
from manifold.crypto.keys import INFO_CREDENTIALS, derive_key
from manifold.gateway.registry import discover_native_toolsets
from manifold.store.credentials import CredentialsRepo
from manifold.store.db import Database
from manifold.store.export import export_config
from manifold.store.settings import LOG_LEVEL, GatewaySettingsRepo
from manifold.store.toolsets import ToolsetNotFound, ToolsetProtected, ToolsetsRepo

KEY = derive_key(b"\x01" * 32, INFO_CREDENTIALS)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path)
    await database.open()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
async def repo(db) -> ToolsetsRepo:
    r = ToolsetsRepo(db)
    await r.sync_native(
        m.MANIFEST
        for m in discover_native_toolsets(manifold.toolsets, tests.fixtures.toolsets).values()
    )
    return r


async def test_sync_registers_disabled_except_manifold(repo):
    rows = {t.key: t for t in await repo.list()}
    assert rows["manifold"].enabled is True
    assert rows["ping-b"].enabled is False
    assert rows["ping-b"].kind == "native"


async def test_sync_is_idempotent_and_never_disables_manifold(repo, db):
    await db.conn.execute("UPDATE toolsets SET enabled = 0 WHERE key = 'manifold'")
    added = await repo.sync_native(
        m.MANIFEST
        for m in discover_native_toolsets(manifold.toolsets, tests.fixtures.toolsets).values()
    )
    assert added == []
    assert (await repo.get("manifold")).enabled is True


async def test_manifold_cannot_be_disabled_or_deleted(repo):
    with pytest.raises(ToolsetProtected):
        await repo.set_enabled("manifold", False)
    with pytest.raises(ToolsetProtected):
        await repo.delete("manifold")


async def test_content_hash_tracks_only_runtime_inputs(repo):
    before = (await repo.get("ping-b")).content_hash()
    await repo.set_display_name("ping-b", "Renamed")
    assert (await repo.get("ping-b")).content_hash() == before
    await repo.update_settings("ping-b", {"x": 1})
    after_settings = (await repo.get("ping-b")).content_hash()
    assert after_settings != before
    await repo.set_enabled("ping-b", True)
    assert (await repo.get("ping-b")).content_hash() != after_settings


async def test_content_hash_changes_when_credential_rotates(repo, db):
    creds = CredentialsRepo(db, KEY)
    cid = await creds.create("Google (Manny)", "service_account", {"v": 1})
    await repo.set_credential("ping-b", cid)
    before = (await repo.get("ping-b")).content_hash()
    import asyncio

    await asyncio.sleep(1.1)  # updated_at has second resolution
    await creds.update_values(cid, {"v": 2})
    assert (await repo.get("ping-b")).content_hash() != before


async def test_unknown_key_raises(repo):
    with pytest.raises(ToolsetNotFound):
        await repo.get("nope")
    with pytest.raises(ToolsetNotFound):
        await repo.set_enabled("nope", True)


async def test_create_proxy_and_delete(repo):
    await repo.create_proxy(
        "unraid", "Unraid", "http://unraid-mcp:6970/mcp", "unraid_", deny=["exec"]
    )
    row = await repo.get("unraid")
    assert row.kind == "proxy" and row.upstream.deny == ("exec",) and row.enabled is False
    await repo.delete("unraid")
    with pytest.raises(ToolsetNotFound):
        await repo.get("unraid")


async def test_export_redacts_credentials(repo, db):
    creds = CredentialsRepo(db, KEY)
    cid = await creds.create("Google (Manny)", "service_account", {"private_key": "SECRET-PEM"})
    await repo.set_credential("ping-b", cid)
    settings = GatewaySettingsRepo(db)
    await settings.set(LOG_LEVEL, "debug")
    text = await export_config(repo, creds, settings)
    assert "SECRET-PEM" not in text
    assert "private_key" not in text
    assert "Google (Manny)" in text
    assert "credential: Google (Manny)" in text
    assert "log_level: debug" in text
    assert "cannot be imported" in text


async def test_settings_defaults_and_override(db):
    settings = GatewaySettingsRepo(db)
    assert await settings.get("audit_retention_days") == 90
    assert await settings.get(LOG_LEVEL) is None
    await settings.set(LOG_LEVEL, "warning")
    assert (await settings.all())[LOG_LEVEL] == "warning"
