"""Config export as YAML, credentials redacted (SPEC.md section 8)."""

from __future__ import annotations

import yaml

from manifold import __version__
from manifold.store.credentials import CredentialsRepo
from manifold.store.settings import GatewaySettingsRepo
from manifold.store.toolsets import ToolsetsRepo

EXPORT_FORMAT = 1


async def export_config(
    toolsets: ToolsetsRepo, credentials: CredentialsRepo, settings: GatewaySettingsRepo
) -> str:
    creds = await credentials.list()
    names = {c.id: c.name for c in creds}
    document = {
        "manifold_export": EXPORT_FORMAT,
        "manifold_version": __version__,
        "settings": await settings.all(),
        "credentials": [
            {"name": c.name, "auth_kind": c.auth_kind, "used_by": list(c.used_by)} for c in creds
        ],
        "toolsets": [
            {
                "key": t.key,
                "display_name": t.display_name,
                "kind": t.kind,
                "enabled": t.enabled,
                "credential": names.get(t.credential_id) if t.credential_id else None,
                "settings": t.settings,
                **(
                    {
                        "upstream": {
                            "url": t.upstream.upstream_url,
                            "prefix": t.upstream.prefix,
                            "allow": list(t.upstream.allow),
                            "deny": list(t.upstream.deny),
                        }
                    }
                    if t.upstream
                    else {}
                ),
            }
            for t in await toolsets.list()
        ],
    }
    header = "# Manifold config export. Credentials are redacted; this file cannot be imported.\n"
    return header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
