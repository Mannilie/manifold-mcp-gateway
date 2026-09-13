from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from manifold.api.deps import require_admin
from manifold.api.models import AuditOut

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(require_admin)])


@router.get("", response_model=list[AuditOut])
async def list_audit(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    toolset: str | None = None,
    tool: str | None = None,
    ok: bool | None = None,
    since: str | None = None,
    until: str | None = None,
):
    entries = await request.app.state.repos["audit"].recent(
        limit=limit,
        offset=offset,
        toolset_key=toolset,
        tool_name=tool,
        ok=ok,
        since=since,
        until=until,
    )
    return [AuditOut(**e.__dict__) for e in entries]
