"""The `manifold` toolset: gateway self-management from inside Claude. Always on."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from manifold import __version__
from manifold.gateway.manifest import Credentials, HealthResult, ToolsetConfig, ToolsetManifest

MANIFEST = ToolsetManifest(
    key="manifold",
    display_name="Manifold",
    description="Read-only view of the gateway itself: toolsets, health and recent activity.",
    kind="native",
    supported_auth=["none"],
    settings_schema={"type": "object", "properties": {}, "additionalProperties": False},
    example_settings={},
    version=__version__,
)


def build(config: ToolsetConfig, credentials: Credentials) -> MCPServer:
    server = MCPServer(
        name="manifold",
        instructions="Manifold is the MCP gateway these connectors run on. Use it to check "
        "that the gateway is reachable. Management tools arrive in a later phase.",
    )

    @server.tool()
    def ping() -> str:
        """Check that the Manifold gateway is reachable.

        Returns the string "pong" with the gateway version. Takes no arguments.
        Use it to confirm the connector works; it tells you nothing about other toolsets.
        """
        return f"pong from manifold {__version__}"

    return server


async def healthcheck(config: ToolsetConfig, credentials: Credentials) -> HealthResult:
    return HealthResult(status="ok")
