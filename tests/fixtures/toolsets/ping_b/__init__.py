"""The `ping-b` test toolset. Test-only since Phase 5.

It proved two connectors from one image in Phase 1 and now gives the integration suite a
second harmless native toolset. The directory is `ping_b` because Python packages cannot
contain hyphens; the served key comes from MANIFEST.key.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from manifold.gateway.manifest import Credentials, HealthResult, ToolsetConfig, ToolsetManifest

MANIFEST = ToolsetManifest(
    key="ping-b",
    display_name="Ping B",
    description="Placeholder second toolset used to verify multi-connector serving.",
    kind="native",
    supported_auth=["none"],
    settings_schema={"type": "object", "properties": {}, "additionalProperties": False},
    example_settings={},
    version="0.1.0",
)


def build(config: ToolsetConfig, credentials: Credentials) -> MCPServer:
    server = MCPServer(name="ping-b")

    @server.tool()
    def ping() -> str:
        """Check that the ping-b placeholder connector is reachable.

        Returns the string "pong from ping-b". Takes no arguments. This toolset does
        nothing else and will be removed once the gateway is proven end to end.
        """
        return "pong from ping-b"

    return server


async def healthcheck(config: ToolsetConfig, credentials: Credentials) -> HealthResult:
    return HealthResult(status="ok")
