"""Discovers native toolsets, builds their MCP servers and transports, and reloads the
mounted set when the config store changes (DECISIONS.md, Phase 2 gate 3).

Reload rules:
- Diff by content hash. Only toolsets whose hash changed, or that failed last time,
  are rebuilt. Everything else keeps its runtime object.
- Build first, swap once. New runtimes are built and started before the dispatcher
  mapping is replaced, with no await between clearing and refilling it.
- A build failure never unmounts. If the key was mounted, the old runtime stays and
  its health reports the error. If it was not, a placeholder answers 503 and health
  reports the error.
- Reloads are serialised. A trigger during a run sets a flag; one follow-up run
  happens after the current one, however many triggers arrived.
- Old runtimes drain in-flight calls for a short window before they close.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import pkgutil
import time
from collections.abc import Awaitable, Callable, Iterator
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
DRAIN_SECONDS = 3.0

# Cloudflare terminates the public hostname, so the Host header is never localhost.
# Safe only because port 8800 is bound to the NAS loopback, never a LAN interface
# (DECISIONS.md, mounting gate and the 2026-09-13 loopback amendment).
TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

Protector = Callable[[str, ASGIApp], ASGIApp]


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


# -- desired state -----------------------------------------------------------------


@dataclass(frozen=True)
class DesiredToolset:
    """What the registry should mount for one key. `build` does the expensive work:
    load credentials, call the module's build(), return the server and a healthcheck."""

    key: str
    display_name: str
    version: str
    content_hash: str
    build: Callable[[], Awaitable[tuple[MCPServer, Callable[[], Awaitable[HealthResult]]]]]
    disabled_tools: frozenset[str] = frozenset()


class ToolsetSource(Protocol):
    async def desired(self) -> list[DesiredToolset]: ...


class StaticSource:
    """Every discovered native toolset, example settings, no credentials. Used by tests
    and as the fallback when there is no config store."""

    def __init__(self, modules: dict[str, ModuleType] | None = None) -> None:
        self._modules = discover_native_toolsets() if modules is None else modules

    async def desired(self) -> list[DesiredToolset]:
        out: list[DesiredToolset] = []
        for key, module in self._modules.items():
            manifest = module.MANIFEST
            config = ToolsetConfig(key=key, settings=manifest.example_settings)

            async def build(module=module, config=config):
                creds = Credentials()

                async def health() -> HealthResult:
                    return await module.healthcheck(config, creds)

                return module.build(config, creds), health

            out.append(
                DesiredToolset(
                    key=key,
                    display_name=manifest.display_name,
                    version=manifest.version,
                    content_hash=json.dumps(manifest.example_settings, sort_keys=True),
                    build=build,
                )
            )
        return out


# -- runtimes ----------------------------------------------------------------------


@dataclass
class ToolsetRuntime:
    key: str
    display_name: str
    version: str
    server: MCPServer
    session_manager: StreamableHTTPSessionManager
    healthcheck: Callable[[], Awaitable[HealthResult]]
    build_error: str | None = None
    inflight: int = 0
    _task: asyncio.Task | None = field(default=None, init=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _health_cache: tuple[float, dict[str, Any]] | None = field(default=None, init=False)

    async def start(self) -> None:
        """Run the session manager in a task of its own. anyio cancel scopes must exit in
        the task that entered them, and reloads start and stop runtimes from whichever
        task triggered them."""
        self._stop_event = asyncio.Event()
        started = asyncio.Event()
        failure: list[BaseException] = []

        async def runner() -> None:
            try:
                async with self.session_manager.run():
                    started.set()
                    await self._stop_event.wait()
            except BaseException as exc:
                failure.append(exc)
                started.set()
                raise

        self._task = asyncio.create_task(runner(), name=f"manifold-toolset-{self.key}")
        await started.wait()
        if failure:
            raise failure[0]

    async def stop(self, drain_seconds: float = DRAIN_SECONDS) -> None:
        deadline = time.monotonic() + drain_seconds
        while self.inflight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self._task is not None:
            self._stop_event.set()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None

    def route(self, protect: Protector | None) -> ToolsetRoute:
        app: ASGIApp = self._counting(
            _without_get_stream(StreamableHTTPASGIApp(self.session_manager))
        )
        if protect is not None:
            app = protect(self.key, app)
        return ToolsetRoute(mcp_app=app, health=self.health)

    def _counting(self, app: ASGIApp) -> ASGIApp:
        async def counted(scope: Scope, receive: Receive, send: Send) -> None:
            self.inflight += 1
            try:
                await app(scope, receive, send)
            finally:
                self.inflight -= 1

        return counted

    async def health(self) -> dict[str, Any]:
        """Cached toolset health for `/<key>/healthz`. At most one real check per 60 seconds.
        A pending build error wins over the check: the old code is serving but the new
        config could not be applied."""
        if self.build_error is not None:
            return self._body("down", f"reload failed: {self.build_error}")
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
        body = self._body(result.status, result.detail)
        self._health_cache = (now, body)
        return body

    def _body(self, status: str, detail: str) -> dict[str, Any]:
        return {"toolset": self.key, "status": status, "detail": detail, "version": self.version}


def _without_get_stream(mcp_app: ASGIApp) -> ASGIApp:
    """Answer GET with 405. We run stateless, so there is no server-initiated stream to
    offer, but the SDK would still hold a GET open as an idle SSE stream."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in ("GET", "HEAD"):
            await _send_json(send, 405, {"error": "method_not_allowed"}, allow=b"POST, DELETE")
            return
        await mcp_app(scope, receive, send)

    return app


def _failed_route(key: str, display_name: str, version: str, error: str) -> ToolsetRoute:
    async def unavailable(scope: Scope, receive: Receive, send: Send) -> None:
        await _send_json(send, 503, {"error": "toolset_unavailable", "detail": error})

    async def health() -> dict[str, Any]:
        return {"toolset": key, "status": "down", "detail": error, "version": version}

    return ToolsetRoute(mcp_app=unavailable, health=health)


async def _send_json(send: Send, status: int, payload: dict, allow: bytes | None = None) -> None:
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if allow:
        headers.append((b"allow", allow))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def build_runtime(
    spec: DesiredToolset, middleware: list[Any] | None = None
) -> ToolsetRuntime:
    server, healthcheck = await spec.build()
    if not isinstance(server, MCPServer):
        raise TypeError(f"{spec.key}.build() must return an MCPServer")
    for mw in middleware or ():
        server.middleware.append(mw)
    session_manager = StreamableHTTPSessionManager(
        app=server._lowlevel_server,
        json_response=True,
        stateless=True,
        security_settings=TRANSPORT_SECURITY,
    )
    return ToolsetRuntime(
        key=spec.key,
        display_name=spec.display_name,
        version=spec.version,
        server=server,
        session_manager=session_manager,
        healthcheck=healthcheck,
    )


# -- registry ----------------------------------------------------------------------


@dataclass
class Mounted:
    key: str
    display_name: str
    version: str
    content_hash: str
    runtime: ToolsetRuntime | None
    route: ToolsetRoute
    error: str | None = None


@dataclass(frozen=True)
class ReloadReport:
    added: tuple[str, ...]
    rebuilt: tuple[str, ...]
    removed: tuple[str, ...]
    failed: tuple[str, ...]
    unchanged: tuple[str, ...]


MiddlewareFactory = Callable[[DesiredToolset], list[Any]]


class Registry:
    """The set of mounted toolsets. `routes` is the live mapping the dispatcher reads."""

    def __init__(
        self,
        source: ToolsetSource,
        protect: Protector | None = None,
        middleware_for: MiddlewareFactory | None = None,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        self._source = source
        self._protect = protect
        self._middleware_for = middleware_for or (lambda spec: [])
        self._drain = drain_seconds
        self.routes: dict[str, ToolsetRoute] = {}
        self.mounted: dict[str, Mounted] = {}
        self._lock = asyncio.Lock()
        self._pending = False
        self.reload_runs = 0

    @classmethod
    def from_discovery(cls, protect: Protector | None = None) -> Registry:
        return cls(StaticSource(), protect=protect)

    @property
    def keys(self) -> list[str]:
        return sorted(self.mounted)

    def get(self, key: str) -> Mounted:
        return self.mounted[key]

    def runtimes(self) -> Iterator[ToolsetRuntime]:
        for m in self.mounted.values():
            if m.runtime is not None:
                yield m.runtime

    async def reload(self) -> ReloadReport | None:
        """Bring the mounted set in line with the source. Serialised; a call that arrives
        during a run returns None immediately and one follow-up run happens afterwards."""
        if self._lock.locked():
            self._pending = True
            return None
        async with self._lock:
            report = await self._reload_once()
            while self._pending:
                self._pending = False
                report = await self._reload_once()
            return report

    async def _reload_once(self) -> ReloadReport:
        self.reload_runs += 1
        desired = await self._source.desired()
        new_mounted: dict[str, Mounted] = {}
        to_stop: list[ToolsetRuntime] = []
        added, rebuilt, failed, unchanged = [], [], [], []

        for spec in desired:
            current = self.mounted.get(spec.key)
            if (
                current is not None
                and current.runtime is not None
                and current.error is None
                and current.content_hash == spec.content_hash
            ):
                new_mounted[spec.key] = current
                unchanged.append(spec.key)
                continue
            try:
                runtime = await build_runtime(spec, self._middleware_for(spec))
                await runtime.start()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                log.error("toolset build failed", extra={"toolset": spec.key, "error": error})
                failed.append(spec.key)
                if current is not None and current.runtime is not None:
                    current.runtime.build_error = error
                    new_mounted[spec.key] = Mounted(
                        key=spec.key,
                        display_name=current.display_name,
                        version=current.version,
                        content_hash=current.content_hash,
                        runtime=current.runtime,
                        route=current.route,
                        error=error,
                    )
                else:
                    new_mounted[spec.key] = Mounted(
                        key=spec.key,
                        display_name=spec.display_name,
                        version=spec.version,
                        content_hash=spec.content_hash,
                        runtime=None,
                        route=_failed_route(spec.key, spec.display_name, spec.version, error),
                        error=error,
                    )
                continue
            new_mounted[spec.key] = Mounted(
                key=spec.key,
                display_name=spec.display_name,
                version=spec.version,
                content_hash=spec.content_hash,
                runtime=runtime,
                route=runtime.route(self._protect),
            )
            if current is not None and current.runtime is not None:
                to_stop.append(current.runtime)
                rebuilt.append(spec.key)
            else:
                added.append(spec.key)

        removed = [k for k in self.mounted if k not in new_mounted]
        to_stop.extend(m.runtime for k in removed if (m := self.mounted[k]).runtime is not None)

        # The swap. No await between these two statements, so no request can observe a
        # half-empty mapping.
        self.routes.clear()
        self.routes.update({k: m.route for k, m in new_mounted.items()})
        self.mounted = new_mounted

        for key in added + rebuilt:
            log.info("toolset mounted", extra={"toolset": key, "path": f"/{key}"})
        for key in removed:
            log.info("toolset unmounted", extra={"toolset": key})
        if to_stop:
            await asyncio.gather(*(rt.stop(self._drain) for rt in to_stop))
        return ReloadReport(
            added=tuple(added),
            rebuilt=tuple(rebuilt),
            removed=tuple(removed),
            failed=tuple(failed),
            unchanged=tuple(unchanged),
        )

    @contextlib.asynccontextmanager
    async def running(self):
        await self.reload()
        try:
            yield
        finally:
            runtimes = list(self.runtimes())
            self.routes.clear()
            self.mounted = {}
            await asyncio.gather(*(rt.stop(0) for rt in runtimes))
