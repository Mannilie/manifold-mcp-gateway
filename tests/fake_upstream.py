"""An in-process upstream MCP server for proxy tests, with an API key check."""

from __future__ import annotations

import contextlib

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY = "upstream-key-123"


def make_upstream(tools: tuple[str, ...] = ("echo", "fail", "secret")) -> MCPServer:
    server = MCPServer("fake-upstream")
    if "echo" in tools:

        @server.tool()
        def echo(text: str, times: int = 1) -> str:
            """Echo the text back, repeated."""
            return text * times

    if "fail" in tools:

        @server.tool()
        def fail() -> str:
            """Always fails with an upstream message."""
            raise ToolError("upstream boom: the widget is on fire")

    if "secret" in tools:

        @server.tool()
        def secret() -> str:
            """Should be denied by the proxy."""
            return "s3cr3t"

    if "later" in tools:

        @server.tool()
        def later() -> str:
            """Appeared after the snapshot."""
            return "new"

    return server


def require_key(app: ASGIApp, header: str = "x-api-key") -> ASGIApp:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            if headers.get(header) != API_KEY and headers.get("authorization") != "Bearer oauth-up":
                await JSONResponse({"error": "unauthorised"}, 401)(scope, receive, send)
                return
        await app(scope, receive, send)

    return wrapped


@contextlib.asynccontextmanager
async def running_upstream(server: MCPServer):
    """Yields an ASGI transport to the upstream with its session manager running."""
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, host="0.0.0.0"
    )
    async with server.session_manager.run():
        yield httpx2.ASGITransport(app=require_key(app))
