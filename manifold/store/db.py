"""SQLite connection and migrations (DECISIONS.md, Phase 2 gate 2).

Migrations are numbered SQL files in `manifold/store/migrations`, applied in order at boot
and tracked with `PRAGMA user_version`. Before the first pending migration on a database
that already has data, the file is copied aside; the last `BACKUP_KEEP` copies are kept.
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DB_FILENAME = "manifold.db"
BACKUP_KEEP = 5
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Return (version, path) pairs in order. Versions must be 1..N with no gaps."""
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(f"Migration file name is not NNNN_name.sql: {path.name}")
        found.append((int(match.group(1)), path))
    for expected, (version, path) in enumerate(found, start=1):
        if version != expected:
            raise MigrationError(f"Migration versions must be contiguous; got {path.name}")
    return found


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    """One connection for the process. Autocommit mode; callers open transactions
    explicitly with BEGIN when they need atomicity across statements."""

    def __init__(self, data_dir: Path, migrations_dir: Path = MIGRATIONS_DIR) -> None:
        self.data_dir = data_dir
        self.path = data_dir / DB_FILENAME
        self._migrations_dir = migrations_dir
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open")
        return self._conn

    async def open(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        self._conn = conn
        await self._enable_incremental_vacuum()

    async def _enable_incremental_vacuum(self) -> None:
        """Incremental vacuum needs auto_vacuum=INCREMENTAL, which SQLite can only switch
        with one full VACUUM. Do that once, and only while the file is small enough for it
        to be instant; a large file keeps reusing its free pages instead."""
        async with self.conn.execute("PRAGMA auto_vacuum") as c:
            mode = int((await c.fetchone())[0])
        if mode == 2:
            return
        size = self.path.stat().st_size if self.path.exists() else 0
        if size > 64 * 1024 * 1024:
            log.warning(
                "auto_vacuum not switched to incremental: database too large for a one-off VACUUM",
                extra={"bytes": size},
            )
            return
        await self.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        await self.conn.execute("VACUUM")
        log.info("auto_vacuum switched to incremental", extra={"bytes": size})

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def user_version(self) -> int:
        async with self.conn.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def data_version(self) -> int:
        """Changes whenever another connection commits. Used by the reload poller."""
        async with self.conn.execute("PRAGMA data_version") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def migrate(self, before: Callable[[int], Awaitable[None]] | None = None) -> int:
        """Apply pending migrations. Returns the resulting schema version. `before` runs
        once with the current version when there is something to apply and the database
        already holds data; the app passes the snapshot store's pre-migration snapshot."""
        current = await self.user_version()
        pending = [(v, p) for v, p in list_migrations(self._migrations_dir) if v > current]
        if not pending:
            return current
        if current > 0:
            if before is not None:
                await before(current)
            else:
                self._backup(current)
        for version, path in pending:
            sql = path.read_text()
            try:
                await self.conn.executescript(
                    f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
                )
            except Exception as exc:
                await self.conn.execute("ROLLBACK")
                raise MigrationError(f"Migration {path.name} failed: {exc}") from exc
            log.info("migration applied", extra={"version": version, "file": path.name})
        self._prune_backups()
        return await self.user_version()

    def _backup(self, version: int) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.data_dir / f"{DB_FILENAME}.pre-{version:04d}-{stamp}"
        # WAL pages must be folded into the main file before a plain copy is consistent.
        # This is a sync call on purpose: nothing else runs during migration.
        import sqlite3

        with sqlite3.connect(self.path) as sync_conn:
            sync_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(self.path, target)
        log.info("database copied before migration", extra={"backup": target.name})
        return target

    def _prune_backups(self) -> None:
        backups = sorted(self.data_dir.glob(f"{DB_FILENAME}.pre-*"))
        for old in backups[:-BACKUP_KEEP] if len(backups) > BACKUP_KEEP else []:
            old.unlink(missing_ok=True)
            log.info("old pre-migration copy removed", extra={"backup": old.name})
