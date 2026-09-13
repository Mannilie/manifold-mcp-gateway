"""ASGI entry point. Mounts the admin UI, the admin API and every toolset."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from manifold import __version__
from manifold.config.logging import configure_logging
from manifold.config.settings import Settings
from manifold.gateway.dispatcher import ToolsetDispatcher
from manifold.gateway.registry import Registry

log = logging.getLogger(__name__)

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    registry = Registry.from_discovery()

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

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "version": __version__, "toolsets": registry.keys}

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
