from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from manifold.api.deps import require_admin
from manifold.api.models import CredentialCreate, CredentialOut, CredentialPatch
from manifold.auth.upstream import OAuthConfigError
from manifold.store.credentials import CredentialInUse, CredentialNotFound, CredentialSummary

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/credentials", tags=["credentials"], dependencies=[Depends(require_admin)]
)

REQUIRED_VALUES: dict[str, tuple[str, ...]] = {
    "none": (),
    "api_key": ("key",),
    "basic": ("username", "password"),
    "bearer": ("token",),
    "service_account": ("json",),
    "oauth2": ("client_secret",),
}


def _out(s: CredentialSummary) -> CredentialOut:
    return CredentialOut(
        id=s.id,
        name=s.name,
        auth_kind=s.auth_kind,
        status=s.status,
        meta=s.meta,
        used_by=list(s.used_by),
        created_at=s.created_at,
        updated_at=s.updated_at,
        token_updated_at=s.token_updated_at,
    )


def _split(
    kind: str, values: dict, body: CredentialCreate | CredentialPatch | None
) -> tuple[dict, dict]:
    """Separate secret values from the non-secret metadata the UI may show."""
    missing = [k for k in REQUIRED_VALUES.get(kind, ()) if not values.get(k)]
    if missing:
        raise HTTPException(422, f"{kind} credential needs: {', '.join(missing)}")
    meta: dict = {}
    secrets = dict(values)
    if kind == "api_key":
        meta["header"] = values.get("header") or "X-API-Key"
        secrets = {"key": values["key"], "header": meta["header"]}
    elif kind == "basic":
        meta["username"] = values["username"]
    elif kind == "service_account":
        raw = values["json"]
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            raise HTTPException(422, "service account JSON is not valid JSON") from None
        if (
            not isinstance(parsed, dict)
            or "client_email" not in parsed
            or "private_key" not in parsed
        ):
            raise HTTPException(422, "service account JSON needs client_email and private_key")
        meta["client_email"] = parsed["client_email"]
        meta["project_id"] = parsed.get("project_id")
        secrets = {"json": parsed}
    elif kind == "oauth2":
        client_id = values.get("client_id")
        if not client_id:
            raise HTTPException(422, "oauth2 credential needs client_id")
        meta["client_id"] = client_id
        secrets = {"client_secret": values["client_secret"]}
        if isinstance(body, CredentialCreate):
            meta["provider"] = body.provider or "generic"
            meta["scopes"] = body.scopes or []
            if meta["provider"] == "generic":
                if not body.auth_url or not body.token_url:
                    raise HTTPException(422, "generic provider needs auth_url and token_url")
                meta["auth_url"], meta["token_url"] = body.auth_url, body.token_url
    return secrets, meta


@router.get("", response_model=list[CredentialOut])
async def list_credentials(request: Request):
    return [_out(s) for s in await request.app.state.repos["credentials"].list()]


@router.post("", response_model=CredentialOut, status_code=201)
async def create_credential(body: CredentialCreate, request: Request):
    repo = request.app.state.repos["credentials"]
    secrets, meta = _split(body.auth_kind, body.values, body)
    status = "unconnected" if body.auth_kind == "oauth2" else "ok"
    try:
        cid = await repo.create(body.name, body.auth_kind, secrets, meta=meta, status=status)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "a credential with that name exists") from None
        raise
    return _out(await repo.get_summary(cid))


@router.get("/{credential_id}", response_model=CredentialOut)
async def get_credential(credential_id: int, request: Request):
    try:
        return _out(await request.app.state.repos["credentials"].get_summary(credential_id))
    except CredentialNotFound:
        raise HTTPException(404, "credential not found") from None


@router.patch("/{credential_id}", response_model=CredentialOut)
async def patch_credential(credential_id: int, body: CredentialPatch, request: Request):
    state = request.app.state
    repo = state.repos["credentials"]
    try:
        summary = await repo.get_summary(credential_id)
    except CredentialNotFound:
        raise HTTPException(404, "credential not found") from None
    if body.name is not None:
        await repo.rename(credential_id, body.name)
    if body.values is not None:
        secrets, meta = _split(summary.auth_kind, body.values, body)
        if summary.auth_kind == "oauth2":
            # New client secret invalidates the stored tokens: force a reconnect.
            await repo.update_values(credential_id, secrets)
            await repo.set_status(credential_id, "scopes_changed")
            state.tokens.forget(credential_id)
        else:
            await repo.update_values(credential_id, secrets)
        await repo.update_meta(credential_id, meta)
    if body.scopes is not None:
        if summary.auth_kind != "oauth2":
            raise HTTPException(422, "only oauth2 credentials have scopes")
        if sorted(body.scopes) != sorted(summary.meta.get("scopes", [])):
            await repo.update_meta(credential_id, {"scopes": body.scopes, "granted_scope": None})
            current = await repo.get(credential_id)
            # Never silently reuse the old refresh token with a different scope set.
            await repo.update_values(
                credential_id,
                {
                    k: v
                    for k, v in current.values.items()
                    if k not in ("refresh_token", "access_token", "expires_at")
                },
            )
            await repo.set_status(credential_id, "scopes_changed")
            state.tokens.forget(credential_id)
            log.info(
                "credential scopes changed; reconnect required",
                extra={"credential_id": credential_id},
            )
    await state.registry.reload()
    return _out(await repo.get_summary(credential_id))


@router.delete("/{credential_id}", status_code=204)
async def delete_credential(credential_id: int, request: Request):
    repo = request.app.state.repos["credentials"]
    try:
        await repo.delete(credential_id)
    except CredentialNotFound:
        raise HTTPException(404, "credential not found") from None
    except CredentialInUse as exc:
        raise HTTPException(409, {"message": str(exc), "used_by": exc.toolset_keys}) from None


@router.post("/{credential_id}/connect")
async def connect_credential(credential_id: int, request: Request):
    """Start the upstream OAuth flow. The UI navigates the browser to `authorize_url`."""
    try:
        url = await request.app.state.upstream.start_connect(credential_id)
    except CredentialNotFound:
        raise HTTPException(404, "credential not found") from None
    except OAuthConfigError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"authorize_url": url}
