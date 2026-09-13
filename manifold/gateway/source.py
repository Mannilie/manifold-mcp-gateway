"""The config store as a `ToolsetSource`: enabled rows become desired toolsets."""

from __future__ import annotations

import logging
from types import ModuleType

from manifold.auth.upstream import TokenManager
from manifold.gateway.manifest import Credentials, HealthResult, ToolsetConfig
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
                log.warning("proxy toolsets arrive in Phase 5", extra={"toolset": row.key})
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
