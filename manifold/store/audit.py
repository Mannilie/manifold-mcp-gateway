"""Audit log: one row per tool call, arguments hashed and never stored."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from manifold.store.db import Database, utcnow

ERROR_MAX_CHARS = 200


def hash_args(arguments: dict[str, Any] | None) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: str
    toolset_key: str
    tool_name: str
    args_hash: str
    duration_ms: int
    ok: bool
    error: str | None


class AuditRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        toolset_key: str,
        tool_name: str,
        args_hash: str,
        duration_ms: int,
        ok: bool,
        error: str | None,
    ) -> None:
        await self._db.conn.execute(
            "INSERT INTO audit_log (ts, toolset_key, tool_name, args_hash, duration_ms, ok, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                toolset_key,
                tool_name,
                args_hash,
                duration_ms,
                1 if ok else 0,
                None if error is None else error[:ERROR_MAX_CHARS],
            ),
        )

    async def recent(
        self,
        limit: int = 50,
        offset: int = 0,
        toolset_key: str | None = None,
        tool_name: str | None = None,
        ok: bool | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[AuditEntry]:
        clauses, params = [], []
        for column, value in (
            ("toolset_key", toolset_key),
            ("tool_name", tool_name),
            ("ok", None if ok is None else int(ok)),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._db.conn.execute(
            "SELECT id, ts, toolset_key, tool_name, args_hash, duration_ms, ok, error"
            f" FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ) as c:
            rows = await c.fetchall()
        return [
            AuditEntry(
                id=r["id"],
                ts=r["ts"],
                toolset_key=r["toolset_key"],
                tool_name=r["tool_name"],
                args_hash=r["args_hash"],
                duration_ms=r["duration_ms"],
                ok=bool(r["ok"]),
                error=r["error"],
            )
            for r in rows
        ]

    async def last_call_at(self) -> dict[str, str]:
        """Most recent call timestamp per toolset, for the dashboard."""
        async with self._db.conn.execute(
            "SELECT toolset_key, MAX(ts) FROM audit_log GROUP BY toolset_key"
        ) as c:
            return {r[0]: r[1] for r in await c.fetchall()}
