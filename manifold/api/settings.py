from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from starlette.responses import PlainTextResponse

from manifold import __version__
from manifold.api.deps import require_admin
from manifold.api.models import SettingsOut, SettingsPatch
from manifold.config.logging import configure_logging
from manifold.store.export import export_config
from manifold.store.settings import AUDIT_RETENTION_DAYS, AUDIT_ROW_CAP, LOG_LEVEL

log = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])


async def _out(request: Request) -> SettingsOut:
    state = request.app.state
    repo = state.repos["settings"]
    db_level = await repo.get(LOG_LEVEL)
    async with state.db.conn.execute("SELECT created_at FROM key_check WHERE id = 1") as c:
        row = await c.fetchone()
    return SettingsOut(
        log_level=db_level or state.settings.log_level,
        log_level_source="database" if db_level else "env",
        audit_retention_days=await repo.get(AUDIT_RETENTION_DAYS),
        audit_row_cap=await repo.get(AUDIT_ROW_CAP),
        audit=await state.repos["audit"].stats(),
        master_key={"verified": row is not None, "first_run_at": row[0] if row else None},
        base_url=state.settings.base_url,
        admin_emails=sorted(state.settings.admin_emails),
        version=__version__,
        schema_version=await state.db.user_version(),
        oauth_clients=await state.oauth.store.count_clients(),
    )


@router.get("", response_model=SettingsOut, dependencies=[Depends(require_admin)])
async def get_settings(request: Request):
    return await _out(request)


@router.patch("", response_model=SettingsOut)
async def patch_settings(
    body: SettingsPatch, request: Request, actor: str = Depends(require_admin)
):
    repo = request.app.state.repos["settings"]
    changed = ", ".join(f"{k}={v}" for k, v in body.model_dump(exclude_none=True).items())
    await request.app.state.repos["audit"].record_admin(
        actor, "settings.update", "settings", changed
    )
    if body.log_level is not None:
        await repo.set(LOG_LEVEL, body.log_level)
        configure_logging(body.log_level)
    if body.audit_retention_days is not None:
        await repo.set(AUDIT_RETENTION_DAYS, body.audit_retention_days)
    if body.audit_row_cap is not None:
        await repo.set(AUDIT_ROW_CAP, body.audit_row_cap)
    return await _out(request)


@router.post("/prune-audit")
async def prune_audit(request: Request, actor: str = Depends(require_admin)):
    """Run the audit prune now rather than waiting for the daily task."""
    result = await request.app.state.pruner.run_once()
    await request.app.state.repos["audit"].record_admin(actor, "audit.prune", "settings")
    return result


@router.get("/export", dependencies=[Depends(require_admin)])
async def export(request: Request):
    repos = request.app.state.repos
    text = await export_config(repos["toolsets"], repos["credentials"], repos["settings"])
    return PlainTextResponse(
        text,
        media_type="application/yaml",
        headers={"content-disposition": 'attachment; filename="manifold-config.yaml"'},
    )


@router.post("/disconnect-all")
async def disconnect_all(request: Request, actor: str = Depends(require_admin)):
    """Forget every claude.ai client and token. Each connector must re-authorise."""
    await request.app.state.oauth.store.clear()
    await request.app.state.repos["audit"].record_admin(
        actor, "connectors.disconnect_all", "settings"
    )
    log.warning("all connectors disconnected by admin")
    return {"ok": True}


@router.post("/restart")
async def restart(request: Request, actor: str = Depends(require_admin)):
    """Exit the process. Docker's restart policy brings it back with the same config."""
    log.warning("restart requested by admin")
    await request.app.state.repos["audit"].record_admin(actor, "gateway.restart", "settings")
    if not getattr(request.app.state, "no_restart", False):
        request.app.state.shutdown.exit_process()
    return {"ok": True, "message": "restarting"}


me_router = APIRouter(tags=["me"])


@me_router.get("/me")
async def me(email: str = Depends(require_admin)):
    return {"email": email}


reload_router = APIRouter(tags=["reload"], dependencies=[Depends(require_admin)])


@reload_router.post("/reload")
async def reload(request: Request):
    report = await request.app.state.registry.reload()
    return report.__dict__ if report else {"coalesced": True}
