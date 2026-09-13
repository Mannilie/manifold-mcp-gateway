from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from starlette.responses import FileResponse

from manifold.api.deps import require_admin
from manifold.store.snapshots import SnapshotError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/backups", tags=["backups"])

MAX_UPLOAD_BYTES = 512 * 1024 * 1024


@router.get("", dependencies=[Depends(require_admin)])
async def list_backups(request: Request):
    return [asdict(s) for s in request.app.state.snapshots.list()]


@router.post("", status_code=201)
async def snapshot_now(request: Request, actor: str = Depends(require_admin)):
    try:
        snap = await request.app.state.snapshots.create("manual")
    except SnapshotError as exc:
        raise HTTPException(500, str(exc)) from None
    await request.app.state.repos["audit"].record_admin(
        actor, "backup.snapshot", "settings", snap.name
    )
    return asdict(snap)


@router.get("/{name}", dependencies=[Depends(require_admin)])
async def download(name: str, request: Request):
    try:
        path = request.app.state.snapshots.path_for(name)
    except SnapshotError as exc:
        raise HTTPException(404, str(exc)) from None
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)


@router.post("/restore/validate")
async def validate_restore(request: Request, file: UploadFile, actor: str = Depends(require_admin)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "backup file too large")
    if not data:
        raise HTTPException(422, "empty upload")
    try:
        report = await request.app.state.snapshots.validate_upload(data)
    except SnapshotError as exc:
        raise HTTPException(422, str(exc)) from None
    await request.app.state.repos["audit"].record_admin(
        actor, "backup.validate", "settings", f"{file.filename} schema {report.schema_version}"
    )
    return asdict(report)


@router.post("/restore/confirm")
async def confirm_restore(request: Request, body: dict, actor: str = Depends(require_admin)):
    """Second step. Snapshots the live database, stages the file, restarts the gateway."""
    token = str(body.get("token", ""))
    try:
        before = await request.app.state.snapshots.confirm_restore(token)
    except SnapshotError as exc:
        raise HTTPException(422, str(exc)) from None
    await request.app.state.repos["audit"].record_admin(
        actor, "backup.restore", "settings", f"pre-restore snapshot {before.name}"
    )
    if not getattr(request.app.state, "no_restart", False):
        request.app.state.shutdown.exit_process()
    return {"ok": True, "pre_restore": before.name, "message": "restarting to apply the restore"}
