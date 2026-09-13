"""ASGI entry point. Mounts the admin UI, the admin API and every toolset."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import httpx2
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route, request_response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from manifold import __version__
from manifold.api.router import api
from manifold.auth.access import AccessGate
from manifold.auth.provider import ManifoldOAuthProvider
from manifold.auth.routes import (
    PROTECTED_RESOURCE_PREFIX,
    BearerProtector,
    build_oauth_routes,
    protected_resource_metadata,
)
from manifold.auth.sqlite_store import SqliteTokenStore
from manifold.auth.upstream import ConnectError, TokenManager, UpstreamOAuth
from manifold.config.cache import cache_control
from manifold.config.logging import add_access_log_file, configure_logging
from manifold.config.settings import Settings
from manifold.crypto.keycheck import verify_or_initialise
from manifold.crypto.keys import INFO_CREDENTIALS, derive_key
from manifold.gateway import shutdown as shutdown_mod
from manifold.gateway.audit import AuditMiddleware
from manifold.gateway.dispatcher import ToolsetDispatcher
from manifold.gateway.prune import AuditPruner
from manifold.gateway.registry import Registry, discover_native_toolsets
from manifold.gateway.shutdown import ShutdownController, bounded
from manifold.gateway.source import DbToolsetSource
from manifold.gateway.toolfilter import ToolFilterMiddleware
from manifold.gateway.watch import POLL_SECONDS, ChangeWatcher
from manifold.store.audit import AuditRepo
from manifold.store.credentials import CredentialsRepo
from manifold.store.db import Database
from manifold.store.settings import LOG_LEVEL, GatewaySettingsRepo
from manifold.store.snapshots import SnapshotStore, apply_pending
from manifold.store.toolsets import ToolsetsRepo

log = logging.getLogger(__name__)

# The image ships the built UI inside the package (manifold/ui_dist); a source checkout
# uses the Astro build output directly.
UI_DIST = next(
    (
        p
        for p in (
            Path(__file__).resolve().parent / "ui_dist",
            Path(__file__).resolve().parent.parent / "ui" / "dist",
        )
        if (p / "index.html").is_file()
    ),
    Path(__file__).resolve().parent / "ui_dist",
)


def create_app(
    settings: Settings | None = None,
    reload_poll_seconds: float = POLL_SECONDS,
    upstream_http: httpx2.AsyncClient | None = None,
    ui_dir: Path | None = None,
    toolset_packages: tuple[ModuleType, ...] = (),
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    db = Database(settings.data_dir)
    credentials_key = derive_key(settings.master_key, INFO_CREDENTIALS)
    modules = discover_native_toolsets(*toolset_packages)
    toolsets_repo = ToolsetsRepo(db)
    credentials_repo = CredentialsRepo(db, credentials_key)
    settings_repo = GatewaySettingsRepo(db)
    audit_repo = AuditRepo(db)
    # `known_toolset` reads `registry` late on purpose: the provider must exist before the
    # registry so the bearer protector can wrap each toolset, and Phase 2 hot reload changes
    # the mounted set at runtime.
    oauth = ManifoldOAuthProvider(
        SqliteTokenStore(db),
        settings.admin_emails,
        settings.base_url,
        known_toolset=lambda key: key in registry.routes,
    )
    upstream = UpstreamOAuth(
        db,
        credentials_repo,
        settings.base_url,
        upstream_http or httpx2.AsyncClient(),
        on_change=lambda: _reload(),
    )
    tokens = TokenManager(credentials_repo, upstream)
    registry = Registry(
        DbToolsetSource(toolsets_repo, credentials_repo, modules, tokens),
        protect=BearerProtector(oauth, settings.base_url),
        middleware_for=lambda spec: [
            ToolFilterMiddleware(spec.disabled_tools),
            AuditMiddleware(spec.key, audit_repo, spec.tool_aliases),
        ],
    )
    watcher = ChangeWatcher(db, registry, reload_poll_seconds)
    pruner = AuditPruner(db, audit_repo, settings_repo)
    snapshots = SnapshotStore(db, credentials_key)

    async def _reload() -> None:
        await registry.reload()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        apply_pending(settings.data_dir)
        await db.open()
        try:
            add_access_log_file(settings.data_dir / "logs")

            async def snapshot_before_migration(_: int) -> None:
                await snapshots.create("pre-migration")

            schema_version = await db.migrate(before=snapshot_before_migration)
            await verify_or_initialise(db, credentials_key)
            level = await settings_repo.get(LOG_LEVEL)
            if level:
                configure_logging(str(level))
            added = await toolsets_repo.sync_native(m.MANIFEST for m in modules.values())
            if added:
                log.info("native toolsets registered", extra={"toolsets": added})
            async with registry.running():
                await watcher.start()
                await pruner.start()
                daily = asyncio.create_task(
                    _daily_snapshots(snapshots), name="manifold-daily-snapshot"
                )
                try:
                    log.info(
                        "manifold started",
                        extra={
                            "version": __version__,
                            "schema_version": schema_version,
                            "toolsets": registry.keys,
                        },
                    )
                    yield
                finally:
                    shutdown.begin()
                    daily.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await daily
                    await bounded("pruner stop", pruner.stop())
                    await bounded("watcher stop", watcher.stop())
        finally:
            await bounded("database close", db.close())
            shutdown.cancel_watchdog()

    shutdown = ShutdownController()
    shutdown_mod.current = shutdown

    app = FastAPI(
        title="Manifold",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.modules = modules
    app.state.registry = registry
    app.state.oauth = oauth
    app.state.db = db
    app.state.pruner = pruner
    app.state.snapshots = snapshots
    app.state.shutdown = shutdown
    app.state.upstream = upstream
    app.state.tokens = tokens
    app.state.repos = {
        "toolsets": toolsets_repo,
        "credentials": credentials_repo,
        "settings": settings_repo,
        "audit": audit_repo,
    }

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        if shutdown.shutting_down:
            return JSONResponse({"status": "shutting_down"}, status_code=503)
        return JSONResponse({"status": "ok", "version": __version__, "toolsets": registry.keys})

    @app.get(PROTECTED_RESOURCE_PREFIX + "/{key}")
    async def protected_resource(key: str) -> JSONResponse:
        # Reads the live registry so Phase 2 hot reload needs no route changes.
        if key not in registry.routes:
            raise HTTPException(status_code=404)
        body = protected_resource_metadata(settings.base_url, key, registry.get(key).display_name)
        return JSONResponse(body, headers={"cache-control": "no-store"})

    for route in build_oauth_routes(oauth, settings.base_url, settings.admin_emails):
        app.router.routes.append(route)

    async def oauth_callback(request: Request):
        """Upstream provider sends the browser here. Behind Access, admin only."""
        q = request.query_params
        try:
            credential_id = await upstream.handle_callback(
                q.get("state"), q.get("code"), q.get("error")
            )
        except ConnectError as exc:
            log.warning("upstream connect failed", extra={"reason": str(exc)})
            return PlainTextResponse(f"Connect failed: {exc}\n", status_code=400)
        return RedirectResponse(f"/credentials/{credential_id}?connected=1", status_code=303)

    app.router.routes.append(
        Route(
            "/oauth/callback",
            endpoint=AccessGate(request_response(oauth_callback), settings.admin_emails),
            methods=["GET"],
        )
    )

    app.include_router(api)

    # Everything not matched above goes through the dispatcher. Unknown keys fall through
    # to the UI. Mounted last so FastAPI's own routes win and nothing is ever redirected.
    app.mount("/", ToolsetDispatcher(registry.routes, fallback=_ui_app(ui_dir or UI_DIST)))
    # Outermost: cache headers on every response, including the MCP endpoints.
    app.add_middleware(_AsgiWrap, wrap=cache_control)
    return app


class _AsgiWrap:
    """Adapter so a plain ASGI wrapper can be registered via add_middleware."""

    def __init__(self, app: ASGIApp, wrap) -> None:
        self._app = wrap(app)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._app(scope, receive, send)


DAILY_SNAPSHOT_SECONDS = 24 * 60 * 60


async def _daily_snapshots(snapshots: SnapshotStore) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            snapshots.discard_incoming()
            await snapshots.create("daily")
        except Exception as exc:
            log.warning("daily snapshot failed", extra={"error": type(exc).__name__})
        await asyncio.sleep(DAILY_SNAPSHOT_SECONDS)


def _ui_app(ui_dir: Path) -> ASGIApp:
    if (ui_dir / "index.html").is_file():
        return _spa(StaticFiles(directory=ui_dir, html=True))
    return _placeholder_ui


def _spa(static: StaticFiles) -> ASGIApp:
    """Serve hashed assets from the build, and index.html for anything else so client-side
    routes survive a refresh. Unknown paths only reach here after the dispatcher has ruled
    out toolset keys and reserved paths."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await static(scope, receive, send)
            return
        path = scope["path"]
        if (
            path.startswith("/_astro/")
            or path.startswith("/assets/")
            or "." in path.rsplit("/", 1)[-1]
        ):
            await static(scope, receive, send)
            return
        await static(dict(scope, path="/index.html"), receive, send)

    return app


async def _placeholder_ui(scope: Scope, receive: Receive, send: Send) -> None:
    body = b"Manifold is running. The admin UI arrives in Phase 3.\n"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
