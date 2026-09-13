from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from manifold.api.deps import require_admin
from manifold.api.models import (
    CloudflareChecklist,
    DiscoveredTool,
    DiscoverRequest,
    HealthOut,
    ProxyCreate,
    ToolOut,
    ToolsetDetail,
    ToolsetPatch,
    ToolsetRename,
    ToolsetSummary,
)
from manifold.gateway.manifest import validate_key
from manifold.gateway.schema import validate_settings
from manifold.store.credentials import CredentialNotFound
from manifold.store.toolsets import ProxyUpstream, ToolsetNotFound, ToolsetProtected, ToolsetRow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/toolsets", tags=["toolsets"], dependencies=[Depends(require_admin)])


def _checklist(base_url: str, key: str) -> CloudflareChecklist:
    return CloudflareChecklist(
        connector_url=f"{base_url}/{key}",
        bypass_path=f"{base_url.split('://', 1)[1]}/{key}",
        covers=[f"/{key}", f"/{key}/healthz"],
    )


async def _summary(request: Request, row: ToolsetRow, cred_names: dict, last: dict) -> dict:
    state = request.app.state
    mounted = state.registry.mounted.get(row.key)
    if mounted is None:
        health = HealthOut(status="disabled" if not row.enabled else "down", detail="not mounted")
        tool_count, error, version = 0, None, None
    else:
        body = await mounted.route.health()
        health = HealthOut(status=body["status"], detail=body.get("detail", ""))
        error, version = mounted.error, mounted.version
        tool_count = 0
        if mounted.runtime is not None:
            tools = await mounted.runtime.server.list_tools()
            tool_count = len([t for t in tools if t.name not in row.disabled_tools])
    return {
        "key": row.key,
        "display_name": row.display_name,
        "kind": row.kind,
        "enabled": row.enabled,
        "mounted": mounted is not None and mounted.runtime is not None,
        "health": health,
        "endpoint_url": f"{state.settings.base_url}/{row.key}",
        "tool_count": tool_count,
        "last_call_at": last.get(row.key),
        "credential_id": row.credential_id,
        "credential_name": cred_names.get(row.credential_id),
        "error": error,
        "cloudflare": _checklist(state.settings.base_url, row.key),
        "_version": version,
    }


@router.get("", response_model=list[ToolsetSummary])
async def list_toolsets(request: Request):
    repos = request.app.state.repos
    cred_names = {c.id: c.name for c in await repos["credentials"].list()}
    last = await repos["audit"].last_call_at()
    return [
        ToolsetSummary(
            **{
                k: v
                for k, v in (await _summary(request, r, cred_names, last)).items()
                if k != "_version"
            }
        )
        for r in await repos["toolsets"].list()
    ]


@router.get("/{key}", response_model=ToolsetDetail)
async def get_toolset(key: str, request: Request):
    state = request.app.state
    repos = state.repos
    try:
        row = await repos["toolsets"].get(key)
    except ToolsetNotFound:
        raise HTTPException(404, "toolset not found") from None
    cred_names = {c.id: c.name for c in await repos["credentials"].list()}
    summary = await _summary(request, row, cred_names, await repos["audit"].last_call_at())
    module = state.modules.get(key)
    tools: list[ToolOut] = []
    mounted = state.registry.mounted.get(key)
    if mounted is not None and mounted.runtime is not None:
        for t in await mounted.runtime.server.list_tools():
            tools.append(
                ToolOut(
                    name=t.name,
                    description=t.description or "",
                    enabled=t.name not in row.disabled_tools,
                )
            )
    elif module is not None:
        # Not mounted: build a throwaway server with example settings just to list tools.
        from manifold.gateway.manifest import Credentials, ToolsetConfig

        try:
            server = module.build(
                ToolsetConfig(key=key, settings=module.MANIFEST.example_settings), Credentials()
            )
            for t in await server.list_tools():
                tools.append(
                    ToolOut(
                        name=t.name,
                        description=t.description or "",
                        enabled=t.name not in row.disabled_tools,
                    )
                )
        except Exception as exc:
            log.info(
                "could not list tools for unmounted toolset",
                extra={"toolset": key, "error": type(exc).__name__},
            )
    upstream = None
    if row.upstream is not None:
        upstream = {
            "upstream_url": row.upstream.upstream_url,
            "prefix": row.upstream.prefix,
            "allow": list(row.upstream.allow),
            "deny": list(row.upstream.deny),
        }
    manifest = module.MANIFEST if module is not None else None
    return ToolsetDetail(
        **{k: v for k, v in summary.items() if k != "_version"},
        version=manifest.version if manifest else summary["_version"],
        supported_auth=list(manifest.supported_auth)
        if manifest
        else ["none", "api_key", "bearer", "basic"],
        settings_schema=manifest.settings_schema
        if manifest
        else {"type": "object", "properties": {}},
        settings=row.settings,
        tools=tools,
        disabled_tools=list(row.disabled_tools),
        upstream=upstream,
    )


@router.patch("/{key}", response_model=ToolsetDetail)
async def patch_toolset(key: str, patch: ToolsetPatch, request: Request):
    state = request.app.state
    repo = state.repos["toolsets"]
    try:
        await repo.get(key)
    except ToolsetNotFound:
        raise HTTPException(404, "toolset not found") from None
    module = state.modules.get(key)
    try:
        if patch.settings is not None:
            schema = module.MANIFEST.settings_schema if module else {"type": "object"}
            problems = validate_settings(schema, patch.settings)
            if problems:
                raise HTTPException(422, {"settings": problems})
            await repo.update_settings(key, patch.settings)
        if patch.display_name is not None:
            await repo.set_display_name(key, patch.display_name)
        if patch.detach_credential:
            await repo.set_credential(key, None)
        elif patch.credential_id is not None:
            try:
                summary = await state.repos["credentials"].get_summary(patch.credential_id)
            except CredentialNotFound:
                raise HTTPException(422, "credential not found") from None
            if module is not None and summary.auth_kind not in module.MANIFEST.supported_auth:
                raise HTTPException(422, f"{key} does not support {summary.auth_kind} credentials")
            await repo.set_credential(key, patch.credential_id)
        if patch.disabled_tools is not None:
            await repo.set_disabled_tools(key, patch.disabled_tools)
        if patch.upstream is not None:
            await repo.update_upstream(
                key,
                ProxyUpstream(
                    upstream_url=patch.upstream["upstream_url"],
                    prefix=patch.upstream.get("prefix") or None,
                    allow=tuple(patch.upstream.get("allow", [])),
                    deny=tuple(patch.upstream.get("deny", [])),
                ),
            )
        if patch.enabled is not None:
            await repo.set_enabled(key, patch.enabled)
    except ToolsetProtected as exc:
        raise HTTPException(409, str(exc)) from None
    await state.registry.reload()
    return await get_toolset(key, request)


@router.post("/{key}/test")
async def test_toolset(key: str, request: Request):
    mounted = request.app.state.registry.mounted.get(key)
    if mounted is None:
        return {"status": "down", "detail": "not mounted"}
    if mounted.runtime is not None:
        mounted.runtime._health_cache = None
    body = await mounted.route.health()
    return {"status": body["status"], "detail": body.get("detail", "")}


@router.post("/{key}/rename", response_model=ToolsetDetail)
async def rename_toolset(key: str, body: ToolsetRename, request: Request):
    state = request.app.state
    try:
        validate_key(body.new_key)
        await state.repos["toolsets"].rename(key, body.new_key)
    except ToolsetNotFound:
        raise HTTPException(404, "toolset not found") from None
    except (ValueError, ToolsetProtected) as exc:
        raise HTTPException(422, str(exc)) from None
    await state.registry.reload()
    log.warning(
        "toolset renamed; its connector URL changed", extra={"old": key, "new": body.new_key}
    )
    return await get_toolset(body.new_key, request)


@router.delete("/{key}", status_code=204)
async def delete_toolset(key: str, request: Request):
    state = request.app.state
    try:
        await state.repos["toolsets"].delete(key)
    except ToolsetNotFound:
        raise HTTPException(404, "toolset not found") from None
    except ToolsetProtected as exc:
        raise HTTPException(409, str(exc)) from None
    await state.registry.reload()


@router.post("", response_model=ToolsetDetail, status_code=201)
async def create_proxy(body: ProxyCreate, request: Request):
    state = request.app.state
    try:
        validate_key(body.key)
        await state.repos["toolsets"].create_proxy(
            body.key,
            body.display_name,
            body.upstream_url,
            body.prefix,
            body.allow,
            body.deny,
            body.credential_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:
        if "UNIQUE" in str(exc) or "PRIMARY KEY" in str(exc):
            raise HTTPException(409, "a toolset with that key exists") from None
        raise
    return await get_toolset(body.key, request)


discover_router = APIRouter(prefix="/proxy", tags=["proxy"], dependencies=[Depends(require_admin)])


@discover_router.post("/discover", response_model=list[DiscoveredTool])
async def discover_tools(body: DiscoverRequest, request: Request):
    """List the tools an upstream MCP server offers, using a stored credential."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from manifold.gateway.manifest import Credentials
    from manifold.gateway.upstream_auth import auth_headers

    state = request.app.state
    creds = Credentials()
    if body.credential_id is not None:
        try:
            creds = await state.repos["credentials"].get(body.credential_id)
        except CredentialNotFound:
            raise HTTPException(422, "credential not found") from None
    try:
        headers = auth_headers(creds)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        async with (
            httpx2.AsyncClient(headers=headers, timeout=15) as http,
            streamable_http_client(body.upstream_url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listing = await session.list_tools()
    except Exception as exc:
        raise HTTPException(502, f"upstream did not answer: {type(exc).__name__}") from None
    return [DiscoveredTool(name=t.name, description=t.description or "") for t in listing.tools]
