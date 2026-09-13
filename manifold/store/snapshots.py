"""Database snapshots and validated restore (DECISIONS.md, Phase 6 gate 2).

Snapshots are written with SQLite's online backup API, so the live connection never holds
its write lock for the copy. They live under /data/backups, the newest 14 are kept, and
every snapshot names its reason: daily, manual, pre-migration, pre-restore, pre-rotation.

Restore is two steps. `validate_upload` opens the uploaded file read-only, checks integrity,
schema version and that its key check row decrypts under the current master key, and
returns a comparison with the live database. `confirm_restore` snapshots the live database,
stages the validated file beside it and asks for a restart; `apply_pending` runs before
the database opens on the next boot and swaps the file in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from manifold.crypto.box import DecryptError, decrypt
from manifold.crypto.keycheck import KEY_CHECK_PLAINTEXT, _aad
from manifold.store.db import DB_FILENAME, Database, list_migrations

log = logging.getLogger(__name__)

BACKUP_DIR = "backups"
KEEP = 14
PENDING_NAME = f"{DB_FILENAME}.restore"
_NAME = re.compile(r"^manifold-(?P<stamp>\d{8}T\d{6}Z)-(?P<reason>[a-z-]+)\.db$")
_REASONS = frozenset({"daily", "manual", "pre-migration", "pre-restore", "pre-rotation"})


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    name: str
    reason: str
    created_at: str
    size_bytes: int


@dataclass(frozen=True)
class RestoreReport:
    token: str
    snapshot_created_at: str | None
    schema_version: int
    live_schema_version: int
    counts: dict[str, int]
    live_counts: dict[str, int]
    warnings: list[str]


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


class SnapshotStore:
    def __init__(self, db: Database, credentials_key: bytes) -> None:
        self._db = db
        self._key = credentials_key
        self.dir = db.data_dir / BACKUP_DIR
        self._lock = asyncio.Lock()

    # -- snapshots ---------------------------------------------------------------------

    async def create(self, reason: str) -> Snapshot:
        if reason not in _REASONS:
            raise SnapshotError(f"unknown snapshot reason {reason!r}")
        async with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            name = f"manifold-{_stamp()}-{reason}.db"
            target = self.dir / name
            if target.exists():  # two snapshots in one second
                await asyncio.sleep(1)
                name = f"manifold-{_stamp()}-{reason}.db"
                target = self.dir / name
            tmp = target.with_suffix(".db.part")
            try:
                async with aiosqlite.connect(tmp) as dest:
                    await self._db.conn.backup(dest, pages=256, sleep=0.01)
                os.replace(tmp, target)
            except Exception as exc:
                with contextlib.suppress(FileNotFoundError):
                    tmp.unlink()
                raise SnapshotError(f"snapshot failed: {type(exc).__name__}: {exc}") from exc
            self._prune()
            snap = self._describe(target)
            log.info("snapshot written", extra={"snapshot": name, "bytes": snap.size_bytes})
            return snap

    def list(self) -> list[Snapshot]:
        if not self.dir.exists():
            return []
        found = [self._describe(p) for p in self.dir.glob("manifold-*.db") if _NAME.match(p.name)]
        return sorted(found, key=lambda s: s.name, reverse=True)

    def path_for(self, name: str) -> Path:
        if not _NAME.match(name):
            raise SnapshotError("not a snapshot name")
        path = self.dir / name
        if not path.is_file():
            raise SnapshotError("snapshot not found")
        return path

    def _prune(self) -> None:
        snaps = self.list()
        for old in snaps[KEEP:]:
            (self.dir / old.name).unlink(missing_ok=True)
            log.info("old snapshot removed", extra={"snapshot": old.name})

    @staticmethod
    def _describe(path: Path) -> Snapshot:
        match = _NAME.match(path.name)
        stamp = match.group("stamp") if match else ""
        created = (
            datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).isoformat()
            if stamp
            else ""
        )
        return Snapshot(
            name=path.name,
            reason=match.group("reason") if match else "?",
            created_at=created,
            size_bytes=path.stat().st_size,
        )

    # -- restore -----------------------------------------------------------------------

    async def validate_upload(self, data: bytes) -> RestoreReport:
        """Write the upload beside the snapshots and check it. Returns a report with a
        token that `confirm_restore` needs; nothing live is touched."""
        self.dir.mkdir(parents=True, exist_ok=True)
        token = f"incoming-{_stamp()}-{os.getpid()}"
        path = self.dir / f"{token}.db"
        path.write_bytes(data)
        try:
            report = await asyncio.to_thread(self._inspect, path)
        except SnapshotError:
            path.unlink(missing_ok=True)
            raise
        live_version = await self._db.user_version()
        live_counts = await self._counts_live()
        warnings = list(report["warnings"])
        if report["version"] > live_version:
            path.unlink(missing_ok=True)
            raise SnapshotError(
                f"the file has schema version {report['version']} but this build knows "
                f"{live_version}; upgrade Manifold before restoring it"
            )
        if report["version"] < live_version:
            warnings.append(
                f"schema version {report['version']} will be migrated to {live_version} on restart"
            )
        for key, live in live_counts.items():
            if report["counts"].get(key, 0) < live:
                warnings.append(
                    f"{key}: the file has {report['counts'].get(key, 0)}, live has {live}"
                )
        return RestoreReport(
            token=token,
            snapshot_created_at=report["created_at"],
            schema_version=report["version"],
            live_schema_version=live_version,
            counts=report["counts"],
            live_counts=live_counts,
            warnings=warnings,
        )

    def _inspect(self, path: Path) -> dict:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise SnapshotError(f"the file is not a SQLite database: {exc}") from exc
        try:
            try:
                ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise SnapshotError(f"the file is not a SQLite database: {exc}") from exc
            if ok != "ok":
                raise SnapshotError(f"integrity check failed: {ok}")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "key_check" not in tables or "toolsets" not in tables:
                raise SnapshotError("the file is a SQLite database but not a Manifold one")
            row = conn.execute(
                "SELECT scheme, nonce, ciphertext, created_at FROM key_check"
            ).fetchone()
            if row is None:
                raise SnapshotError("the file has no master key check row; it was never booted")
            try:
                plain = decrypt(self._key, row[1], row[2], _aad(row[0]))
            except DecryptError as exc:
                raise SnapshotError(
                    "the file was encrypted under a different MANIFOLD_MASTER_KEY; restore "
                    "the matching key first"
                ) from exc
            if plain != KEY_CHECK_PLAINTEXT:
                raise SnapshotError("the file's key check row is corrupt")
            counts = {}
            for table in ("toolsets", "credentials", "oauth_clients", "audit_log"):
                if table in tables:
                    counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            warnings: list[str] = []
            if version > len(list_migrations()):
                warnings.append("schema newer than this build")
            return {
                "version": version,
                "counts": counts,
                "created_at": row[3],
                "warnings": warnings,
            }
        finally:
            conn.close()

    async def _counts_live(self) -> dict[str, int]:
        out = {}
        for table in ("toolsets", "credentials", "oauth_clients", "audit_log"):
            async with self._db.conn.execute(f"SELECT COUNT(*) FROM {table}") as c:
                out[table] = int((await c.fetchone())[0])
        return out

    async def confirm_restore(self, token: str) -> Snapshot:
        """Snapshot the live database, stage the validated file, and return the snapshot.
        The caller restarts the process; `apply_pending` swaps the file in on boot."""
        if not re.fullmatch(r"incoming-\d{8}T\d{6}Z-\d+", token):
            raise SnapshotError("bad restore token")
        staged = self.dir / f"{token}.db"
        if not staged.is_file():
            raise SnapshotError("nothing to restore; upload and validate a file first")
        before = await self.create("pre-restore")
        pending = self._db.data_dir / PENDING_NAME
        os.replace(staged, pending)
        log.warning("restore staged; restart to apply", extra={"pre_restore": before.name})
        return before

    def discard_incoming(self) -> None:
        for stale in self.dir.glob("incoming-*.db"):
            if time.time() - stale.stat().st_mtime > 3600:
                stale.unlink(missing_ok=True)


def apply_pending(data_dir: Path) -> bool:
    """Called before the database opens. Swaps a staged restore into place."""
    pending = data_dir / PENDING_NAME
    if not pending.is_file():
        return False
    live = data_dir / DB_FILENAME
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            (data_dir / (DB_FILENAME + suffix)).unlink()
    shutil.move(pending, live)
    log.warning("restore applied: database replaced from staged file")
    return True
