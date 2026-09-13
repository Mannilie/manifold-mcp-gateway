"""The config store as a `ToolsetSource`: enabled rows become desired toolsets."""

from __future__ import annotations

import logging
from types import ModuleType

from manifold.gateway.manifest import Credentials, HealthResult, ToolsetConfig
from manifold.gateway.registry import DesiredToolset
from manifold.store.credentials import CredentialsRepo
from manifold.store.toolsets import ToolsetRow, ToolsetsRepo

log = logging.getLogger(__name__)


class DbToolsetSource:
    def __init__(
        self,
        toolsets: ToolsetsRepo,
        credentials: CredentialsRepo,
        modules: dict[str, ModuleType],
    ) -> None:
        self._toolsets = toolsets
        self._credentials = credentials
        self._modules = modules

    async def desired(self) -> list[DesiredToolset]:
        out: list[DesiredToolset] = []
        for row in await self._toolsets.list():
            if not row.enabled:
                continue
            if row.kind == "proxy":
                log.warning("proxy toolsets arrive in Phase 5", extra={"toolset": row.key})
                continue
            module = self._modules.get(row.key)
            if module is None:
                log.warning("toolset row has no code on disk", extra={"toolset": row.key})
                continue
            out.append(self._desired(row, module))
        return out

    def _desired(self, row: ToolsetRow, module: ModuleType) -> DesiredToolset:
        config = ToolsetConfig(key=row.key, settings=row.settings)

        async def build():
            creds = (
                await self._credentials.get(row.credential_id)
                if row.credential_id is not None
                else Credentials()
            )

            async def health() -> HealthResult:
                return await module.healthcheck(config, creds)

            return module.build(config, creds), health

        return DesiredToolset(
            key=row.key,
            display_name=row.display_name,
            version=module.MANIFEST.version,
            content_hash=row.content_hash(),
            build=build,
        )
