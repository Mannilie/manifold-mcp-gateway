"""Audit log retention (DECISIONS.md, Phase 6 gate 1): age and row-count caps, applied in
small batches with a pause between them so a large first prune never stalls a tool call,
followed by incremental vacuum only."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from manifold.store.audit import AuditRepo
from manifold.store.db import Database
from manifold.store.settings import AUDIT_RETENTION_DAYS, AUDIT_ROW_CAP, GatewaySettingsRepo

log = logging.getLogger(__name__)

BATCH_ROWS = 500
BATCH_PAUSE_SECONDS = 0.05
INTERVAL_SECONDS = 24 * 60 * 60
STARTUP_DELAY_SECONDS = 30.0
VACUUM_PAGES_PER_STEP = 256


class AuditPruner:
    def __init__(
        self,
        db: Database,
        audit: AuditRepo,
        settings: GatewaySettingsRepo,
        interval: float = INTERVAL_SECONDS,
        startup_delay: float = STARTUP_DELAY_SECONDS,
    ) -> None:
        self._db = db
        self._audit = audit
        self._settings = settings
        self._interval = interval
        self._startup_delay = startup_delay
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="manifold-audit-prune")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(self._startup_delay)
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                log.warning("audit prune failed", extra={"error": type(exc).__name__})
            await asyncio.sleep(self._interval)

    async def run_once(self) -> dict:
        """Prune to the current settings. Safe to call from the API at any time."""
        async with self._lock:
            days = int(await self._settings.get(AUDIT_RETENTION_DAYS))
            cap = int(await self._settings.get(AUDIT_ROW_CAP))
            cutoff = datetime.now(UTC) - timedelta(days=days)
            cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
            by_age = by_cap = 0
            while True:
                age, capped = await self._audit.prune_batch(cutoff_text, cap, BATCH_ROWS)
                by_age += age
                by_cap += capped
                if age + capped < BATCH_ROWS:
                    break
                await asyncio.sleep(BATCH_PAUSE_SECONDS)
            if by_age or by_cap:
                await self._audit.record_prune(by_age, by_cap)
                await self._vacuum()
                log.info("audit pruned", extra={"by_age": by_age, "by_cap": by_cap})
            stats = await self._audit.stats()
            return {"by_age": by_age, "by_cap": by_cap, "rows": stats["rows"]}

    async def _vacuum(self) -> None:
        """Return freed pages to the filesystem a few at a time. Never a full VACUUM."""
        async with self._db.conn.execute("PRAGMA auto_vacuum") as c:
            mode = int((await c.fetchone())[0])
        if mode != 2:
            return
        for _ in range(64):
            async with self._db.conn.execute(
                f"PRAGMA incremental_vacuum({VACUUM_PAGES_PER_STEP})"
            ) as c:
                await c.fetchall()
            async with self._db.conn.execute("PRAGMA freelist_count") as c:
                if int((await c.fetchone())[0]) == 0:
                    break
            await asyncio.sleep(BATCH_PAUSE_SECONDS)
