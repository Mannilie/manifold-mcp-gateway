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
    upstream_tool: str | None = None
    actor: str | None = None
    detail: str | None = None


ADMIN_PREFIX = "admin:"


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
        upstream_tool: str | None = None,
    ) -> None:
        await self._db.conn.execute(
            "INSERT INTO audit_log (ts, toolset_key, tool_name, args_hash, duration_ms, ok, error,"
            " upstream_tool) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(),
                toolset_key,
                tool_name,
                args_hash,
                duration_ms,
                1 if ok else 0,
                None if error is None else error[:ERROR_MAX_CHARS],
                upstream_tool,
            ),
        )

    async def record_admin(
        self, actor: str, action: str, target: str, detail: str | None = None
    ) -> None:
        """An admin API mutation: who did what to which key. Never a credential value."""
        await self._db.conn.execute(
            "INSERT INTO audit_log (ts, toolset_key, tool_name, args_hash, duration_ms, ok, error,"
            " actor, detail) VALUES (?, ?, ?, ?, 0, 1, NULL, ?, ?)",
            (utcnow(), target, ADMIN_PREFIX + action, hash_args({"target": target}), actor, detail),
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
            "SELECT id, ts, toolset_key, tool_name, args_hash, duration_ms, ok, error,"
            " upstream_tool, actor, detail"
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
                upstream_tool=r["upstream_tool"],
                actor=r["actor"],
                detail=r["detail"],
            )
            for r in rows
        ]

    async def stats(self) -> dict:
        async with self._db.conn.execute("SELECT COUNT(*), MIN(ts) FROM audit_log") as c:
            count, oldest = await c.fetchone()
        async with self._db.conn.execute(
            "SELECT ts, by_age, by_cap, rows_after FROM audit_prune_log ORDER BY id DESC LIMIT 1"
        ) as c:
            row = await c.fetchone()
        last = (
            None
            if row is None
            else {"ts": row[0], "by_age": row[1], "by_cap": row[2], "rows_after": row[3]}
        )
        return {"rows": int(count or 0), "oldest_ts": oldest, "last_prune": last}

    async def prune_batch(self, older_than: str, cap: int, batch: int) -> tuple[int, int]:
        """Delete up to `batch` rows: first by age, then by count cap, oldest first.
        Returns (deleted_by_age, deleted_by_cap). Small batches keep the write lock short."""
        cursor = await self._db.conn.execute(
            "DELETE FROM audit_log WHERE id IN"
            " (SELECT id FROM audit_log WHERE ts < ? ORDER BY id LIMIT ?)",
            (older_than, batch),
        )
        by_age = cursor.rowcount
        remaining = batch - by_age
        by_cap = 0
        if remaining > 0:
            async with self._db.conn.execute("SELECT COUNT(*) FROM audit_log") as c:
                count = int((await c.fetchone())[0])
            excess = count - cap
            if excess > 0:
                cursor = await self._db.conn.execute(
                    "DELETE FROM audit_log WHERE id IN"
                    " (SELECT id FROM audit_log ORDER BY id LIMIT ?)",
                    (min(excess, remaining),),
                )
                by_cap = cursor.rowcount
        return by_age, by_cap

    async def record_prune(self, by_age: int, by_cap: int) -> None:
        async with self._db.conn.execute("SELECT COUNT(*) FROM audit_log") as c:
            rows_after = int((await c.fetchone())[0])
        await self._db.conn.execute(
            "INSERT INTO audit_prune_log (ts, by_age, by_cap, rows_after) VALUES (?, ?, ?, ?)",
            (utcnow(), by_age, by_cap, rows_after),
        )
        await self._db.conn.execute(
            "DELETE FROM audit_prune_log WHERE id NOT IN"
            " (SELECT id FROM audit_prune_log ORDER BY id DESC LIMIT 30)"
        )

    async def last_call_at(self) -> dict[str, str]:
        """Most recent call timestamp per toolset, for the dashboard."""
        async with self._db.conn.execute(
            "SELECT toolset_key, MAX(ts) FROM audit_log WHERE actor IS NULL GROUP BY toolset_key"
        ) as c:
            return {r[0]: r[1] for r in await c.fetchall()}
