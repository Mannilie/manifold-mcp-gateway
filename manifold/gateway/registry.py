"""Discovers native toolsets, builds their MCP servers and transports, and owns their lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import pkgutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Protocol

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

import manifold.toolsets
from manifold.gateway.dispatcher import ToolsetRoute
from manifold.gateway.manifest import (
    Credentials,
    HealthResult,
    ToolsetConfig,
    ToolsetManifest,
)

log = logging.getLogger(__name__)

HEALTH_CACHE_SECONDS = 60.0
HEALTH_TIMEOUT_SECONDS = 5.0

# Cloudflare terminates the public hostname, so the Host header is never localhost.
# Safe only because port 8800 is bound to the NAS loopback, never a LAN interface
# (DECISIONS.md, mounting gate and the 2026-09-13 loopback amendment).
TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


class NativeToolsetModule(Protocol):
    MANIFEST: ToolsetManifest

    def build(self, config: ToolsetConfig, credentials: Credentials) -> MCPServer: ...

    async def healthcheck(
        self, config: ToolsetConfig, credentials: Credentials
    ) -> HealthResult: ...


def discover_native_toolsets(package: ModuleType = manifold.toolsets) -> dict[str, ModuleType]:
    """Import every package under `manifold.toolsets` that exposes the toolset contract.

    Keyed by `MANIFEST.key`, never by directory name. Raises on a malformed toolset rather
    than skipping it, so a broken toolset fails the boot loudly.
    """
    found: dict[str, ModuleType] = {}
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for attr in ("MANIFEST", "build", "healthcheck"):
            if not hasattr(module, attr):
                raise TypeError(f"Toolset module {module.__name__} is missing {attr}")
        manifest = module.MANIFEST
        if not isinstance(manifest, ToolsetManifest):
            raise TypeError(f"{module.__name__}.MANIFEST is not a ToolsetManifest")
        if manifest.kind != "native":
            raise TypeError(
                f"{module.__name__} declares kind={manifest.kind!r}; code toolsets are native"
            )
        if manifest.key in found:
            raise ValueError(f"Duplicate toolset key {manifest.key!r}")
        found[manifest.key] = module
    return found


@dataclass
class ToolsetRuntime:
    manifest: ToolsetManifest
    config: ToolsetConfig
    server: MCPServer
    session_manager: StreamableHTTPSessionManager
    healthcheck: Callable[[], Awaitable[HealthResult]]
    _health_cache: tuple[float, dict[str, Any]] | None = field(default=None, init=False)

    @property
    def key(self) -> str:
        return self.manifest.key

    def route(self) -> ToolsetRoute:
        return ToolsetRoute(
            mcp_app=_without_get_stream(StreamableHTTPASGIApp(self.session_manager)),
            health=self.health,
        )

    async def health(self) -> dict[str, Any]:
        """Cached toolset health for `/<key>/healthz`. At most one real check per 60 seconds."""
        now = time.monotonic()
        if self._health_cache and now - self._health_cache[0] < HEALTH_CACHE_SECONDS:
            return self._health_cache[1]
        try:
            result = await asyncio.wait_for(self.healthcheck(), HEALTH_TIMEOUT_SECONDS)
        except TimeoutError:
            result = HealthResult(status="down", detail="healthcheck timed out")
        except Exception as exc:
            log.warning(
                "healthcheck raised", extra={"toolset": self.key, "error": type(exc).__name__}
            )
            result = HealthResult(status="down", detail=f"healthcheck raised {type(exc).__name__}")
        body = {
            "toolset": self.key,
            "status": result.status,
            "detail": result.detail,
            "version": self.manifest.version,
        }
        self._health_cache = (now, body)
        return body


def _without_get_stream(mcp_app: ASGIApp) -> ASGIApp:
    """Answer GET with 405. We run stateless, so there is no server-initiated stream to
    offer, but the SDK would still hold a GET open as an idle SSE stream. The MCP spec
    allows 405 here."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in ("GET", "HEAD"):
            body = b'{"error":"method_not_allowed"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"allow", b"POST, DELETE"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await mcp_app(scope, receive, send)

    return app


def build_runtime(
    module: NativeToolsetModule, config: ToolsetConfig, credentials: Credentials
) -> ToolsetRuntime:
    server = module.build(config, credentials)
    if not isinstance(server, MCPServer):
        raise TypeError(f"{config.key}.build() must return an MCPServer")
    session_manager = StreamableHTTPSessionManager(
        app=server._lowlevel_server,
        json_response=True,
        stateless=True,
        security_settings=TRANSPORT_SECURITY,
    )

    async def healthcheck() -> HealthResult:
        return await module.healthcheck(config, credentials)

    return ToolsetRuntime(
        manifest=module.MANIFEST,
        config=config,
        server=server,
        session_manager=session_manager,
        healthcheck=healthcheck,
    )


class Registry:
    """The set of mounted toolsets. `routes` is the live mapping the dispatcher reads."""

    def __init__(self, runtimes: dict[str, ToolsetRuntime]) -> None:
        self._runtimes = runtimes
        self.routes: dict[str, ToolsetRoute] = {k: r.route() for k, r in runtimes.items()}

    @classmethod
    def from_discovery(cls) -> Registry:
        # Phase 1 has no config store, so every discovered toolset is mounted with its
        # example settings and no credentials. Phase 2 reads enabled state and settings
        # from SQLite and applies the "new toolsets start disabled" rule.
        runtimes: dict[str, ToolsetRuntime] = {}
        for key, module in discover_native_toolsets().items():
            config = ToolsetConfig(key=key, settings=module.MANIFEST.example_settings)
            runtimes[key] = build_runtime(module, config, Credentials())
        return cls(runtimes)

    @property
    def keys(self) -> list[str]:
        return sorted(self._runtimes)

    def get(self, key: str) -> ToolsetRuntime:
        return self._runtimes[key]

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for runtime in self._runtimes.values():
                await stack.enter_async_context(runtime.session_manager.run())
                log.info(
                    "toolset mounted", extra={"toolset": runtime.key, "path": f"/{runtime.key}"}
                )
            yield
