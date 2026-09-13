"""Reloads the registry when another connection commits to the database."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from manifold.gateway.registry import Registry
from manifold.store.db import Database

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0


class ChangeWatcher:
    """Polls `PRAGMA data_version`, which SQLite bumps on every commit made by a
    connection other than ours. Our own writes trigger a reload explicitly."""

    def __init__(self, db: Database, registry: Registry, interval: float = POLL_SECONDS) -> None:
        self._db = db
        self._registry = registry
        self._interval = interval
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._last = await self._db.data_version()
        self._task = asyncio.create_task(self._loop(), name="manifold-change-watcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                version = await self._db.data_version()
            except Exception as exc:
                log.warning("data_version poll failed", extra={"error": type(exc).__name__})
                continue
            if version != self._last:
                self._last = version
                log.info("database changed by another connection, reloading")
                await self._registry.reload()
