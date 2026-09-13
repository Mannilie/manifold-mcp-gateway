"""Server middleware that hides disabled tools and refuses calls to them."""

from __future__ import annotations

from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS


class ToolFilterMiddleware:
    def __init__(self, disabled: frozenset[str]) -> None:
        self._disabled = disabled

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if not self._disabled:
            return await call_next(ctx)
        if ctx.method == "tools/call":
            params = ctx.params if isinstance(ctx.params, dict) else {}
            if params.get("name") in self._disabled:
                raise MCPError(
                    INVALID_PARAMS, f"tool {params.get('name')!r} is disabled on this toolset"
                )
        result = await call_next(ctx)
        if ctx.method == "tools/list":
            tools = _tools_of(result)
            if tools is not None:
                kept = [t for t in tools if _name(t) not in self._disabled]
                return _with_tools(result, kept)
        return result


def _tools_of(result: HandlerResult) -> list | None:
    if isinstance(result, dict):
        return result.get("tools")
    return getattr(result, "tools", None)


def _name(tool: Any) -> str:
    return tool["name"] if isinstance(tool, dict) else tool.name


def _with_tools(result: HandlerResult, tools: list) -> HandlerResult:
    if isinstance(result, dict):
        return {**result, "tools": tools}
    return result.model_copy(update={"tools": tools})
