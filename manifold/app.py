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
from manifold.auth.store import InMemoryTokenStore
from manifold.config.logging import configure_logging
from manifold.config.settings import Settings
from manifold.gateway.dispatcher import ToolsetDispatcher
from manifold.gateway.registry import Registry

log = logging.getLogger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    # Phase 1: OAuth state is in memory, so a restart means claude.ai reconnects.
    # `known_toolset` reads `registry` late on purpose: the provider must exist before the
    # registry so the bearer protector can wrap each toolset, and Phase 2 hot reload changes
    # the mounted set at runtime.
    oauth = ManifoldOAuthProvider(
        InMemoryTokenStore(),
        settings.admin_emails,
        settings.base_url,
        known_toolset=lambda key: key in registry.routes,
    )
    registry = Registry.from_discovery(protect=BearerProtector(oauth, settings.base_url))

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with registry.running():
            log.info("manifold started", extra={"version": __version__, "toolsets": registry.keys})
            yield

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

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "version": __version__, "toolsets": registry.keys}

    @app.get(PROTECTED_RESOURCE_PREFIX + "/{key}")
    async def protected_resource(key: str) -> JSONResponse:
        # Reads the live registry so Phase 2 hot reload needs no route changes.
        if key not in registry.routes:
            raise HTTPException(status_code=404)
        body = protected_resource_metadata(
            settings.base_url, key, registry.get(key).manifest.display_name
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
