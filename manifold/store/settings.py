"""Gateway settings that live in the database and override boot defaults."""

from __future__ import annotations

import json
from typing import Any

from manifold.store.db import Database

LOG_LEVEL = "log_level"
AUDIT_RETENTION_DAYS = "audit_retention_days"

DEFAULTS: dict[str, Any] = {AUDIT_RETENTION_DAYS: 90}


class GatewaySettingsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str, default: Any = None) -> Any:
        async with self._db.conn.execute(
            "SELECT value_json FROM gateway_settings WHERE key = ?", (key,)
        ) as c:
            row = await c.fetchone()
        if row is None:
            return DEFAULTS.get(key, default)
        return json.loads(row[0])

    async def set(self, key: str, value: Any) -> None:
        await self._db.conn.execute(
            "INSERT INTO gateway_settings (key, value_json) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (key, json.dumps(value)),
        )

    async def all(self) -> dict[str, Any]:
        values = dict(DEFAULTS)
        async with self._db.conn.execute("SELECT key, value_json FROM gateway_settings") as c:
            for row in await c.fetchall():
                values[row[0]] = json.loads(row[1])
        return values
