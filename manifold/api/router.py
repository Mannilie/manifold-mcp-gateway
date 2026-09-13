from __future__ import annotations

from fastapi import APIRouter

from manifold.api import audit, backups, credentials, settings, toolsets

api = APIRouter(prefix="/api")
api.include_router(toolsets.router)
api.include_router(toolsets.discover_router)
api.include_router(credentials.router)
api.include_router(audit.router)
api.include_router(settings.router)
api.include_router(settings.me_router)
api.include_router(settings.reload_router)
api.include_router(backups.router)
