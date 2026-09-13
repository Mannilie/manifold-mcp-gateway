"""Toolset rows. The registry mounts whatever this says is enabled."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from manifold.gateway.manifest import ToolsetKind, ToolsetManifest, validate_key
from manifold.store.db import Database, utcnow


class ToolsetNotFound(LookupError):
    pass


class ToolsetProtected(ValueError):
    """`manifold` cannot be disabled or deleted."""


@dataclass(frozen=True)
class ProxyUpstream:
    upstream_url: str
    prefix: str | None
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    tools: tuple[dict, ...] = ()  # snapshot of upstream tools at the last build


@dataclass(frozen=True)
class ToolsetRow:
    key: str
    display_name: str
    kind: ToolsetKind
    enabled: bool
    credential_id: int | None
    settings: dict
    created_at: str
    updated_at: str
    upstream: ProxyUpstream | None
    credential_updated_at: str | None
    disabled_tools: tuple[str, ...] = ()

    def content_hash(self) -> str:
        """Changes only when something that affects the built runtime changes
        (DECISIONS.md, Phase 2 gate 3). Includes the credential's updated_at so a
        rotated secret rebuilds the toolsets using it."""
        material = {
            "settings": self.settings,
            "credential_id": self.credential_id,
            "credential_updated_at": self.credential_updated_at,
            "enabled": self.enabled,
            "disabled_tools": sorted(self.disabled_tools),
            "upstream": None
            if self.upstream is None
            else [
                self.upstream.upstream_url,
                self.upstream.prefix,
                list(self.upstream.allow),
                list(self.upstream.deny),
            ],
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


_SELECT = (
    "SELECT t.key, t.display_name, t.kind, t.enabled, t.credential_id, t.settings_json,"
    " t.created_at, t.updated_at, t.disabled_tools_json,"
    " p.upstream_url, p.prefix, p.allow_json, p.deny_json, p.tools_json,"
    " c.updated_at AS credential_updated_at"
    " FROM toolsets t"
    " LEFT JOIN proxy_upstreams p ON p.toolset_key = t.key"
    " LEFT JOIN credentials c ON c.id = t.credential_id"
)


def _row(r) -> ToolsetRow:
    upstream = None
    if r["upstream_url"] is not None:
        upstream = ProxyUpstream(
            upstream_url=r["upstream_url"],
            prefix=r["prefix"],
            allow=tuple(json.loads(r["allow_json"])),
            deny=tuple(json.loads(r["deny_json"])),
            tools=tuple(json.loads(r["tools_json"])),
        )
    return ToolsetRow(
        key=r["key"],
        display_name=r["display_name"],
        kind=r["kind"],
        enabled=bool(r["enabled"]),
        credential_id=r["credential_id"],
        settings=json.loads(r["settings_json"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        upstream=upstream,
        credential_updated_at=r["credential_updated_at"],
        disabled_tools=tuple(json.loads(r["disabled_tools_json"])),
    )


class ToolsetsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> list[ToolsetRow]:
        async with self._db.conn.execute(_SELECT + " ORDER BY t.key") as c:
            return [_row(r) for r in await c.fetchall()]

    async def get(self, key: str) -> ToolsetRow:
        async with self._db.conn.execute(_SELECT + " WHERE t.key = ?", (key,)) as c:
            r = await c.fetchone()
        if r is None:
            raise ToolsetNotFound(key)
        return _row(r)

    async def sync_native(self, manifests: Iterable[ToolsetManifest]) -> list[str]:
        """Register discovered native toolsets that have no row yet. New rows start
        disabled with the manifest's example settings; `manifold` is always enabled.
        Returns the keys that were added."""
        added: list[str] = []
        now = utcnow()
        for manifest in manifests:
            cursor = await self._db.conn.execute(
                "INSERT OR IGNORE INTO toolsets (key, display_name, kind, enabled, settings_json,"
                " created_at, updated_at) VALUES (?, ?, 'native', ?, ?, ?, ?)",
                (
                    manifest.key,
                    manifest.display_name,
                    1 if manifest.key == "manifold" else 0,
                    json.dumps(manifest.example_settings),
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                added.append(manifest.key)
        await self._db.conn.execute("UPDATE toolsets SET enabled = 1 WHERE key = 'manifold'")
        return added

    async def set_enabled(self, key: str, enabled: bool) -> None:
        if key == "manifold" and not enabled:
            raise ToolsetProtected("manifold cannot be disabled")
        await self._update(key, "enabled = ?", (1 if enabled else 0,))

    async def update_settings(self, key: str, settings: dict) -> None:
        await self._update(key, "settings_json = ?", (json.dumps(settings),))

    async def set_credential(self, key: str, credential_id: int | None) -> None:
        await self._update(key, "credential_id = ?", (credential_id,))

    async def set_display_name(self, key: str, display_name: str) -> None:
        await self._update(key, "display_name = ?", (display_name,))

    async def set_disabled_tools(self, key: str, names: Iterable[str]) -> None:
        await self._update(key, "disabled_tools_json = ?", (json.dumps(sorted(set(names))),))

    async def update_upstream(self, key: str, upstream: ProxyUpstream) -> None:
        row = await self.get(key)
        if row.kind != "proxy":
            raise ValueError(f"{key} is not a proxy toolset")
        await self._db.conn.execute(
            "UPDATE proxy_upstreams SET upstream_url = ?, prefix = ?, allow_json = ?, deny_json = ?"
            " WHERE toolset_key = ?",
            (
                upstream.upstream_url,
                upstream.prefix,
                json.dumps(list(upstream.allow)),
                json.dumps(list(upstream.deny)),
                key,
            ),
        )
        await self._db.conn.execute(
            "UPDATE toolsets SET updated_at = ? WHERE key = ?", (utcnow(), key)
        )

    async def sync_snapshot(self, key: str, tools: list[dict], new_deny: list[str]) -> None:
        """Store the upstream tool snapshot and deny tools that are new since the last one.
        Does not bump updated_at: a re-snapshot is not a config change."""
        row = await self.get(key)
        if row.upstream is None:
            raise ValueError(f"{key} is not a proxy toolset")
        live_names = {t["name"] for t in tools}
        deny = sorted((set(row.upstream.deny) | set(new_deny)) & live_names)
        allow = sorted(set(row.upstream.allow) & live_names)
        await self._db.conn.execute(
            "UPDATE proxy_upstreams SET tools_json = ?, deny_json = ?, allow_json = ?"
            " WHERE toolset_key = ?",
            (json.dumps(tools), json.dumps(deny), json.dumps(allow), key),
        )

    async def rename(self, old_key: str, new_key: str) -> None:
        """Change a toolset's key, which changes its endpoint URL. The UI warns first."""
        if old_key == "manifold":
            raise ToolsetProtected("manifold cannot be renamed")
        validate_key(new_key)
        row = await self.get(old_key)
        conn = self._db.conn
        await conn.execute("BEGIN")
        try:
            now = utcnow()
            await conn.execute(
                "INSERT INTO toolsets (key, display_name, kind, enabled, credential_id,"
                " settings_json, disabled_tools_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_key,
                    row.display_name,
                    row.kind,
                    1 if row.enabled else 0,
                    row.credential_id,
                    json.dumps(row.settings),
                    json.dumps(list(row.disabled_tools)),
                    row.created_at,
                    now,
                ),
            )
            if row.upstream is not None:
                await conn.execute(
                    "INSERT INTO proxy_upstreams (toolset_key, upstream_url, prefix, allow_json,"
                    " deny_json, tools_json) SELECT ?, upstream_url, prefix, allow_json, deny_json,"
                    " tools_json FROM proxy_upstreams WHERE toolset_key = ?",
                    (new_key, old_key),
                )
            await conn.execute("DELETE FROM toolsets WHERE key = ?", (old_key,))
            await conn.execute("COMMIT")
        except Exception:
            await conn.execute("ROLLBACK")
            raise

    async def create_proxy(
        self,
        key: str,
        display_name: str,
        upstream_url: str,
        prefix: str | None = None,
        allow: Iterable[str] = (),
        deny: Iterable[str] = (),
        credential_id: int | None = None,
    ) -> None:
        validate_key(key)
        now = utcnow()
        conn = self._db.conn
        await conn.execute("BEGIN")
        try:
            await conn.execute(
                "INSERT INTO toolsets (key, display_name, kind, enabled, credential_id,"
                " settings_json, created_at, updated_at) VALUES (?, ?, 'proxy', 0, ?, '{}', ?, ?)",
                (key, display_name, credential_id, now, now),
            )
            await conn.execute(
                "INSERT INTO proxy_upstreams (toolset_key, upstream_url, prefix, allow_json,"
                " deny_json) VALUES (?, ?, ?, ?, ?)",
                (key, upstream_url, prefix, json.dumps(list(allow)), json.dumps(list(deny))),
            )
            await conn.execute("COMMIT")
        except Exception:
            await conn.execute("ROLLBACK")
            raise

    async def delete(self, key: str) -> None:
        if key == "manifold":
            raise ToolsetProtected("manifold cannot be deleted")
        cursor = await self._db.conn.execute("DELETE FROM toolsets WHERE key = ?", (key,))
        if cursor.rowcount == 0:
            raise ToolsetNotFound(key)

    async def _update(self, key: str, assignment: str, params: tuple) -> None:
        cursor = await self._db.conn.execute(
            f"UPDATE toolsets SET {assignment}, updated_at = ? WHERE key = ?",
            (*params, utcnow(), key),
        )
        if cursor.rowcount == 0:
            raise ToolsetNotFound(key)
