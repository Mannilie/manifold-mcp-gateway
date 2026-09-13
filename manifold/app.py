"""ASGI entry point. Mounts the admin UI, the admin API and every toolset."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from manifold import __version__
from manifold.auth.provider import ManifoldOAuthProvider
from manifold.auth.routes import (
    PROTECTED_RESOURCE_PREFIX,
    BearerProtector,
    build_oauth_routes,
    protected_resource_metadata,
)
from manifold.auth.sqlite_store import SqliteTokenStore
from manifold.config.logging import configure_logging
from manifold.config.settings import Settings
from manifold.crypto.keycheck import verify_or_initialise
from manifold.crypto.keys import INFO_CREDENTIALS, derive_key
from manifold.gateway.audit import AuditMiddleware
from manifold.gateway.dispatcher import ToolsetDispatcher
from manifold.gateway.registry import Registry, discover_native_toolsets
from manifold.gateway.source import DbToolsetSource
from manifold.gateway.watch import POLL_SECONDS, ChangeWatcher
from manifold.store.audit import AuditRepo
from manifold.store.credentials import CredentialsRepo
from manifold.store.db import Database
from manifold.store.settings import LOG_LEVEL, GatewaySettingsRepo
from manifold.store.toolsets import ToolsetsRepo

log = logging.getLogger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def create_app(
    settings: Settings | None = None, reload_poll_seconds: float = POLL_SECONDS
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    db = Database(settings.data_dir)
    credentials_key = derive_key(settings.master_key, INFO_CREDENTIALS)
    modules = discover_native_toolsets()
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
    registry = Registry(
        DbToolsetSource(toolsets_repo, credentials_repo, modules),
        protect=BearerProtector(oauth, settings.base_url),
        middleware_for=lambda key: [AuditMiddleware(key, audit_repo)],
    )
    watcher = ChangeWatcher(db, registry, reload_poll_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await db.open()
        try:
            schema_version = await db.migrate()
            await verify_or_initialise(db, credentials_key)
            level = await settings_repo.get(LOG_LEVEL)
            if level:
                configure_logging(str(level))
            added = await toolsets_repo.sync_native(m.MANIFEST for m in modules.values())
            if added:
                log.info("native toolsets registered", extra={"toolsets": added})
            async with registry.running():
                await watcher.start()
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
                    await watcher.stop()
        finally:
            await db.close()

    app = FastAPI(
        title="Manifold",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.oauth = oauth
    app.state.db = db
    app.state.repos = {
        "toolsets": toolsets_repo,
        "credentials": credentials_repo,
        "settings": settings_repo,
        "audit": audit_repo,
    }

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "version": __version__, "toolsets": registry.keys}

    @app.get(PROTECTED_RESOURCE_PREFIX + "/{key}")
    async def protected_resource(key: str) -> JSONResponse:
        # Reads the live registry so Phase 2 hot reload needs no route changes.
        if key not in registry.routes:
            raise HTTPException(status_code=404)
        body = protected_resource_metadata(
            settings.base_url, key, registry.get(key).display_name
        )
        return JSONResponse(body, headers={"cache-control": "no-store"})

    for route in build_oauth_routes(oauth, settings.base_url, settings.admin_emails):
        app.router.routes.append(route)

    # Everything not matched above goes through the dispatcher. Unknown keys fall through
    # to the UI. Mounted last so FastAPI's own routes win and nothing is ever redirected.
    app.mount("/", ToolsetDispatcher(registry.routes, fallback=_ui_app()))
    return app


def _ui_app() -> ASGIApp:
    if (UI_DIST / "index.html").is_file():
        return StaticFiles(directory=UI_DIST, html=True)
    return _placeholder_ui


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
