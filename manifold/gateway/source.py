"""The config store as a `ToolsetSource`: enabled rows become desired toolsets."""

from __future__ import annotations

import logging
from types import ModuleType

from manifold.auth.upstream import TokenManager
from manifold.gateway.manifest import Credentials, HealthResult, ToolsetConfig
from manifold.gateway.proxy import (
    ProxyConnection,
    ProxyServer,
    SnapshotTool,
    UpstreamUnreachable,
    apply_lists,
    diff_names,
    health_of,
)
from manifold.gateway.registry import DesiredToolset
from manifold.store.credentials import CredentialsRepo, CredentialSummary
from manifold.store.toolsets import ToolsetRow, ToolsetsRepo

log = logging.getLogger(__name__)


class DbToolsetSource:
    def __init__(
        self,
        toolsets: ToolsetsRepo,
        credentials: CredentialsRepo,
        modules: dict[str, ModuleType],
        tokens: TokenManager | None = None,
    ) -> None:
        self._toolsets = toolsets
        self._credentials = credentials
        self._modules = modules
        self._tokens = tokens

    async def desired(self) -> list[DesiredToolset]:
        out: list[DesiredToolset] = []
        summaries = {c.id: c for c in await self._credentials.list()}
        for row in await self._toolsets.list():
            if not row.enabled:
                continue
            cred = summaries.get(row.credential_id) if row.credential_id is not None else None
            if cred is not None and cred.status in ("unconnected", "scopes_changed"):
                # Not mounted until the credential is (re)connected (Phase 3 gate 4).
                log.info(
                    "toolset waiting for credential",
                    extra={"toolset": row.key, "credential_status": cred.status},
                )
                continue
            if row.kind == "proxy":
                out.append(self._desired_proxy(row, cred))
                continue
            module = self._modules.get(row.key)
            if module is None:
                log.warning("toolset row has no code on disk", extra={"toolset": row.key})
                continue
            out.append(self._desired(row, module, cred))
        return out

    def _desired(
        self, row: ToolsetRow, module: ModuleType, cred: CredentialSummary | None
    ) -> DesiredToolset:
        config = ToolsetConfig(key=row.key, settings=row.settings)
        credential_id = row.credential_id

        async def build():
            creds = (
                await self._credentials.get(credential_id)
                if credential_id is not None
                else Credentials()
            )
            if creds.kind == "oauth2" and self._tokens is not None and credential_id is not None:
                creds = creds.with_token_getter(self._tokens.getter(credential_id))

            async def health() -> HealthResult:
                if credential_id is not None:
                    summary = await self._credentials.get_summary(credential_id)
                    if summary.status != "ok":
                        cause = summary.meta.get("last_error") or f"credential is {summary.status}"
                        return HealthResult(
                            status="degraded", detail=f"credential needs reconnecting: {cause}"
                        )
                return await module.healthcheck(config, creds)

            return module.build(config, creds), health

        return DesiredToolset(
            key=row.key,
            display_name=row.display_name,
            version=module.MANIFEST.version,
            content_hash=row.content_hash(),
            build=build,
            disabled_tools=frozenset(row.disabled_tools),
        )

    def _desired_proxy(self, row: ToolsetRow, cred: CredentialSummary | None) -> DesiredToolset:
        upstream = row.upstream
        assert upstream is not None
        credential_id = row.credential_id
        state: dict = {}
        prefix = upstream.prefix or ""
        # Filled by build() from the live tool list; the audit middleware reads it per call.
        aliases: dict[str, str] = {prefix + t["name"]: t["name"] for t in upstream.tools}

        async def build():
            creds = (
                await self._credentials.get(credential_id)
                if credential_id is not None
                else Credentials()
            )
            if creds.kind == "oauth2" and self._tokens is not None and credential_id is not None:
                creds = creds.with_token_getter(self._tokens.getter(credential_id))
            connection = ProxyConnection(row.display_name, upstream.upstream_url, creds)
            state["connection"] = connection
            await connection.start()
            stored = [SnapshotTool.from_json(t) for t in upstream.tools]
            try:
                live = [SnapshotTool.from_tool(t) for t in await connection.list_tools(budget=12.0)]
            except UpstreamUnreachable as exc:
                if not stored:
                    await connection.stop()
                    raise RuntimeError(str(exc)) from None
                # Upstream down at build time: serve the last snapshot, health says down.
                log.warning("proxy built from stored snapshot", extra={"toolset": row.key})
                live = None
            if live is not None:
                # Tools new since the last snapshot are denied until allowed. The very first
                # snapshot has nothing to compare against: the allow and deny lists chosen at
                # creation stand.
                added, _ = diff_names([t.name for t in stored], [t.name for t in live])
                if not stored:
                    added = []
                if added or [t.to_json() for t in stored] != [t.to_json() for t in live]:
                    await self._toolsets.sync_snapshot(row.key, [t.to_json() for t in live], added)
                fresh = await self._toolsets.get(row.key)
                snapshot, allow, deny = live, fresh.upstream.allow, fresh.upstream.deny
                if added:
                    log.info(
                        "proxy snapshot updated", extra={"toolset": row.key, "denied_new": added}
                    )
            else:
                snapshot, allow, deny = stored, upstream.allow, upstream.deny
            exported = apply_lists(snapshot, allow, deny)
            server = ProxyServer(row.key, row.display_name, connection, exported, upstream.prefix)
            aliases.clear()
            aliases.update(server.aliases)
            stored_now = snapshot

            async def health() -> HealthResult:
                if credential_id is not None:
                    summary = await self._credentials.get_summary(credential_id)
                    if summary.status != "ok":
                        cause = summary.meta.get("last_error") or f"credential is {summary.status}"
                        return HealthResult(
                            status="degraded", detail=f"credential needs reconnecting: {cause}"
                        )
                return await health_of(connection, stored_now)

            return server, health

        async def on_stop() -> None:
            connection = state.get("connection")
            if connection is not None:
                await connection.stop()

        async def rehash() -> str:
            return (await self._toolsets.get(row.key)).content_hash()

        return DesiredToolset(
            key=row.key,
            display_name=row.display_name,
            version="proxy",
            content_hash=row.content_hash(),
            build=build,
            disabled_tools=frozenset(row.disabled_tools),
            tool_aliases=aliases,
            on_stop=on_stop,
            rehash=rehash,
        )
