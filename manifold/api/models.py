"""Wire shapes for the admin API. Nothing here ever carries a credential value."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from manifold.gateway.manifest import AuthKind


class HealthOut(BaseModel):
    status: str
    detail: str = ""


class ToolOut(BaseModel):
    name: str
    description: str
    enabled: bool


class CloudflareChecklist(BaseModel):
    connector_url: str
    bypass_path: str
    covers: list[str]


class ToolsetSummary(BaseModel):
    key: str
    display_name: str
    kind: str
    enabled: bool
    mounted: bool
    health: HealthOut
    endpoint_url: str
    tool_count: int
    last_call_at: str | None
    credential_id: int | None
    credential_name: str | None
    error: str | None
    cloudflare: CloudflareChecklist


class ToolsetDetail(ToolsetSummary):
    version: str | None
    supported_auth: list[str]
    settings_schema: dict[str, Any]
    settings: dict[str, Any]
    tools: list[ToolOut]
    disabled_tools: list[str]
    upstream: dict[str, Any] | None


class ToolsetPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    settings: dict[str, Any] | None = None
    credential_id: int | None = None
    detach_credential: bool = False
    disabled_tools: list[str] | None = None
    upstream: dict[str, Any] | None = None


class ToolsetRename(BaseModel):
    new_key: str


class ProxyCreate(BaseModel):
    key: str
    display_name: str = Field(min_length=1, max_length=80)
    upstream_url: str
    prefix: str | None = None
    credential_id: int | None = None
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class DiscoverRequest(BaseModel):
    upstream_url: str
    credential_id: int | None = None


class DiscoveredTool(BaseModel):
    name: str
    description: str


class CredentialOut(BaseModel):
    id: int
    name: str
    auth_kind: AuthKind
    status: str
    meta: dict[str, Any]
    used_by: list[str]
    created_at: str
    updated_at: str
    token_updated_at: str | None


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    auth_kind: AuthKind
    values: dict[str, Any] = Field(default_factory=dict)
    provider: Literal["google", "microsoft", "generic"] | None = None
    scopes: list[str] | None = None
    auth_url: str | None = None
    token_url: str | None = None


class CredentialPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    values: dict[str, Any] | None = None
    scopes: list[str] | None = None


class AuditOut(BaseModel):
    id: int
    ts: str
    toolset_key: str
    tool_name: str
    args_hash: str
    duration_ms: int
    ok: bool
    error: str | None
    upstream_tool: str | None = None
    actor: str | None = None
    detail: str | None = None


class SettingsOut(BaseModel):
    log_level: str
    log_level_source: Literal["env", "database"]
    audit_retention_days: int
    audit_row_cap: int
    audit: dict[str, Any]
    master_key: dict[str, Any]
    base_url: str
    admin_emails: list[str]
    version: str
    schema_version: int
    oauth_clients: int


class SettingsPatch(BaseModel):
    log_level: Literal["debug", "info", "warning", "error"] | None = None
    audit_retention_days: int | None = Field(default=None, ge=1, le=365)
    audit_row_cap: int | None = Field(default=None, ge=10_000, le=1_000_000)
