"""Proxy toolsets: re-export an upstream MCP server's tools (DECISIONS.md, Phase 5 gate 2).

One client session per proxy runtime, kept in a task of its own so anyio cancel scopes
stay in one place, with bounded reconnect backoff. Allowed upstream tools are registered
on a low-level server whose list and call handlers forward to the session, so upstream
input schemas pass through untouched. A call while the upstream is down returns a
readable error within the call budget rather than hanging.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError

from manifold.gateway.manifest import Credentials, HealthResult
from manifold.gateway.upstream_auth import auth_headers

log = logging.getLogger(__name__)

CALL_BUDGET_SECONDS = 25.0
STOP_TIMEOUT_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 10.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0


class UpstreamUnreachable(Exception):
    pass


@dataclass(frozen=True)
class SnapshotTool:
    name: str
    description: str
    input_schema: dict[str, Any]

    @classmethod
    def from_tool(cls, tool: types.Tool) -> SnapshotTool:
        return cls(tool.name, tool.description or "", dict(tool.input_schema or {}))

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SnapshotTool:
        return cls(data["name"], data.get("description", ""), data.get("input_schema", {}))


class TokenAuth(httpx2.Auth):
    """httpx auth that asks the credential for a fresh bearer token per request, so an
    oauth2 upstream credential refreshes through the gateway's token manager."""

    requires_request_body = False

    def __init__(self, getter: Callable[[], Awaitable[str]]) -> None:
        self._getter = getter

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncIterator[httpx2.Request]:
        request.headers["Authorization"] = f"Bearer {await self._getter()}"
        yield request

    def sync_auth_flow(self, request: httpx2.Request):  # pragma: no cover - never used sync
        raise RuntimeError("TokenAuth is async only")


def http_client_for(
    credentials: Credentials, transport: httpx2.AsyncBaseTransport | None = None
) -> httpx2.AsyncClient:
    kwargs: dict[str, Any] = {"timeout": CONNECT_TIMEOUT_SECONDS}
    if transport is not None:
        kwargs["transport"] = transport
    if credentials.kind == "oauth2":
        return httpx2.AsyncClient(auth=TokenAuth(credentials.access_token), **kwargs)
    return httpx2.AsyncClient(headers=auth_headers(credentials), **kwargs)


# Tests point this at an in-process ASGI transport. Production leaves it None.
transport_factory: Callable[[str], httpx2.AsyncBaseTransport | None] = lambda url: None  # noqa: E731


class ProxyConnection:
    """A persistent client session to one upstream, reconnecting with backoff."""

    def __init__(self, display_name: str, url: str, credentials: Credentials) -> None:
        self.display_name = display_name
        self._url = url
        self._credentials = credentials
        self._session: ClientSession | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._kick = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.connected_at: float | None = None
        self.attempts = 0

    async def start(self, wait: float = CONNECT_TIMEOUT_SECONDS) -> None:
        self._task = asyncio.create_task(self._run(), name=f"manifold-proxy-{self.display_name}")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), wait)

    async def stop(self, timeout: float | None = None) -> None:
        """Close the session. An upstream that will not let go is abandoned after
        `timeout` seconds so a restart never waits on it."""
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        budget = STOP_TIMEOUT_SECONDS if timeout is None else timeout
        try:
            await asyncio.wait_for(asyncio.shield(task), budget)
            return
        except TimeoutError:
            pass
        except BaseException:
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), budget)
        log.warning(
            "proxy session did not close in time, abandoned",
            extra={"upstream": self.display_name},
        )

    @property
    def connected(self) -> bool:
        return self._session is not None

    async def _run(self) -> None:
        while not self._stop.is_set():
            self.attempts += 1
            try:
                async with (
                    http_client_for(self._credentials, transport_factory(self._url)) as http,
                    streamable_http_client(self._url, http_client=http) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT_SECONDS)
                    self._session = session
                    self.last_error = None
                    self.connected_at = time.time()
                    self.attempts = 0
                    self._ready.set()
                    self._kick.clear()
                    log.info("proxy connected", extra={"upstream": self.display_name})
                    stop = asyncio.ensure_future(self._stop.wait())
                    kick = asyncio.ensure_future(self._kick.wait())
                    done, pending = await asyncio.wait(
                        {stop, kick}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    if kick in done:
                        self.last_error = "session dropped; reconnecting"
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self.last_error = _describe(exc)
                log.warning(
                    "proxy connection failed",
                    extra={"upstream": self.display_name, "error": self.last_error},
                )
            finally:
                self._session = None
                self._ready.clear()
            if self._stop.is_set():
                break
            delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** min(self.attempts, 5)))
            delay += random.uniform(0, 0.5)
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except TimeoutError:
                continue

    def _drop(self, reason: str) -> None:
        """A call failed on what looked like a live session: reconnect rather than trust it."""
        self.last_error = reason
        self._session = None
        self._ready.clear()
        self._kick.set()

    async def _wait_ready(self, budget: float) -> ClientSession:
        try:
            await asyncio.wait_for(self._ready.wait(), budget)
        except TimeoutError:
            raise UpstreamUnreachable(
                f"upstream {self.display_name} is unreachable"
                + (f": {self.last_error}" if self.last_error else "")
            ) from None
        assert self._session is not None
        return self._session

    async def list_tools(self, budget: float = CALL_BUDGET_SECONDS) -> list[types.Tool]:
        session = await self._wait_ready(budget / 2)
        try:
            result = await asyncio.wait_for(session.list_tools(), budget / 2)
        except (TimeoutError, MCPError, OSError, httpx2.HTTPError) as exc:
            self._drop(_describe(exc))
            raise UpstreamUnreachable(
                f"upstream {self.display_name} did not list tools: {_describe(exc)}"
            ) from None
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        started = time.monotonic()
        session = await self._wait_ready(CALL_BUDGET_SECONDS / 2)
        remaining = CALL_BUDGET_SECONDS - (time.monotonic() - started)
        try:
            result = await asyncio.wait_for(session.call_tool(name, arguments), remaining)
        except TimeoutError:
            self._drop("call timed out")
            raise UpstreamUnreachable(
                f"upstream {self.display_name} did not answer {name} within "
                f"{int(CALL_BUDGET_SECONDS)}s"
            ) from None
        except (OSError, httpx2.HTTPError) as exc:
            self._drop(_describe(exc))
            raise UpstreamUnreachable(
                f"upstream {self.display_name} is unreachable: {_describe(exc)}"
            ) from None
        if isinstance(result, types.CallToolResult):
            return result
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(result))])


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        inner = exc.exceptions[0] if exc.exceptions else exc
        return _describe(inner)
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def apply_lists(
    snapshot: list[SnapshotTool], allow: tuple[str, ...], deny: tuple[str, ...]
) -> list[SnapshotTool]:
    """Exported tools: the allow list when set, otherwise everything not denied."""
    if allow:
        return [t for t in snapshot if t.name in allow and t.name not in deny]
    return [t for t in snapshot if t.name not in deny]


class ProxyServer:
    """Looks enough like an MCPServer for the registry: `_lowlevel_server`, `middleware`,
    `list_tools()`. Every exported tool forwards to the upstream session."""

    def __init__(
        self,
        key: str,
        display_name: str,
        connection: ProxyConnection,
        exported: list[SnapshotTool],
        prefix: str | None,
    ) -> None:
        self.key = key
        self.display_name = display_name
        self.connection = connection
        self.prefix = prefix or ""
        self._exported = exported
        self.aliases: dict[str, str] = {self.prefix + t.name: t.name for t in exported}
        self._tools = [
            types.Tool(
                name=self.prefix + t.name,
                description=f"Via {display_name}: {t.description}".strip(),
                input_schema=t.input_schema or {"type": "object", "properties": {}},
            )
            for t in exported
        ]
        self._lowlevel_server = Server(
            name=key, on_list_tools=self._on_list_tools, on_call_tool=self._on_call_tool
        )

    @property
    def middleware(self) -> list[Any]:
        return self._lowlevel_server.middleware

    async def list_tools(self) -> list[types.Tool]:
        return list(self._tools)

    async def _on_list_tools(self, ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list(self._tools))

    async def _on_call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        upstream_name = self.aliases.get(params.name)
        if upstream_name is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"unknown tool {params.name!r}")],
                is_error=True,
            )
        try:
            # Upstream results, including errors, pass through with their content intact.
            return await self.connection.call_tool(upstream_name, params.arguments or {})
        except UpstreamUnreachable as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))], is_error=True
            )
        except MCPError as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text", text=f"upstream {self.display_name} error: {exc.error.message}"
                    )
                ],
                is_error=True,
            )


def diff_names(stored: list[str], live: list[str]) -> tuple[list[str], list[str]]:
    """(added, removed) between the stored snapshot and the live upstream list."""
    return sorted(set(live) - set(stored)), sorted(set(stored) - set(live))


async def health_of(connection: ProxyConnection, stored: list[SnapshotTool]) -> HealthResult:
    if not connection.connected:
        return HealthResult(
            status="down",
            detail=f"upstream {connection.display_name} is unreachable"
            + (f": {connection.last_error}" if connection.last_error else ""),
        )
    try:
        live = await connection.list_tools(budget=8.0)
    except UpstreamUnreachable as exc:
        return HealthResult(status="down", detail=str(exc))
    added, removed = diff_names([t.name for t in stored], [t.name for t in live])
    if added or removed:
        parts = []
        if added:
            plural = "s" if len(added) != 1 else ""
            parts.append(f"upstream added {len(added)} tool{plural} ({', '.join(added)})")
        if removed:
            parts.append(f"removed {len(removed)} ({', '.join(removed)})")
        return HealthResult(
            status="degraded",
            detail="; ".join(parts) + ". Reload to re-sync; new tools are denied until allowed.",
            needs_rebuild=True,
        )
    return HealthResult(status="ok", detail=f"{len(stored)} upstream tools")
