from __future__ import annotations

from pathlib import Path

import pytest

from manifold.store.db import BACKUP_KEEP, Database, MigrationError, list_migrations


async def test_fresh_database_reaches_latest_version(tmp_path):
    db = Database(tmp_path)
    await db.open()
    try:
        version = await db.migrate()
        assert version == len(list_migrations())
        assert await db.migrate() == version
        async with db.conn.execute("PRAGMA journal_mode") as c:
            assert (await c.fetchone())[0] == "wal"
        async with db.conn.execute("PRAGMA foreign_keys") as c:
            assert (await c.fetchone())[0] == 1
        assert not list(tmp_path.glob("manifold.db.pre-*")), "fresh database needs no copy"
    finally:
        await db.close()


def write_migrations(directory: Path, count: int, bad: int | None = None) -> None:
    directory.mkdir(exist_ok=True)
    for n in range(1, count + 1):
        body = f"CREATE TABLE t{n} (id INTEGER PRIMARY KEY);"
        if n == bad:
            body = "CREATE TABLE ok (id INTEGER PRIMARY KEY); THIS IS NOT SQL;"
        (directory / f"{n:04d}_step.sql").write_text(body)


async def test_existing_database_is_copied_before_migrating(tmp_path):
    migrations = tmp_path / "m"
    write_migrations(migrations, 1)
    db = Database(tmp_path, migrations)
    await db.open()
    assert await db.migrate() == 1
    await db.conn.execute("INSERT INTO t1 DEFAULT VALUES")
    await db.close()

    write_migrations(migrations, 2)
    db = Database(tmp_path, migrations)
    await db.open()
    try:
        assert await db.migrate() == 2
        copies = list(tmp_path.glob("manifold.db.pre-0001-*"))
        assert len(copies) == 1
        async with db.conn.execute("SELECT COUNT(*) FROM t1") as c:
            assert (await c.fetchone())[0] == 1, "data survives the migration"
    finally:
        await db.close()


async def test_failed_migration_rolls_back_and_keeps_version(tmp_path):
    migrations = tmp_path / "m"
    write_migrations(migrations, 2, bad=2)
    db = Database(tmp_path, migrations)
    await db.open()
    try:
        with pytest.raises(MigrationError, match=r"0002_step\.sql"):
            await db.migrate()
        assert await db.user_version() == 1
        async with db.conn.execute("SELECT name FROM sqlite_master WHERE name = 'ok'") as c:
            assert await c.fetchone() is None, "partial migration was rolled back"
    finally:
        await db.close()


async def test_backup_retention_keeps_last_n(tmp_path):
    migrations = tmp_path / "m"
    write_migrations(migrations, 1)
    db = Database(tmp_path, migrations)
    await db.open()
    await db.migrate()
    await db.close()
    for n in range(2, BACKUP_KEEP + 4):
        write_migrations(migrations, n)
        db = Database(tmp_path, migrations)
        await db.open()
        await db.migrate()
        await db.close()
        # distinct timestamps are not guaranteed within a second; disambiguate by version
    copies = sorted(tmp_path.glob("manifold.db.pre-*"))
    assert len(copies) == BACKUP_KEEP
    assert copies[-1].name.startswith(f"manifold.db.pre-{BACKUP_KEEP + 2:04d}")


def test_migration_names_must_be_contiguous(tmp_path):
    (tmp_path / "0001_a.sql").write_text("")
    (tmp_path / "0003_b.sql").write_text("")
    with pytest.raises(MigrationError, match="contiguous"):
        list_migrations(tmp_path)


def test_migration_names_must_match_pattern(tmp_path):
    (tmp_path / "init.sql").write_text("")
    with pytest.raises(MigrationError, match="NNNN_name"):
        list_migrations(tmp_path)


def test_shipped_migrations_are_well_formed():
    assert [v for v, _ in list_migrations()] == list(range(1, len(list_migrations()) + 1))
