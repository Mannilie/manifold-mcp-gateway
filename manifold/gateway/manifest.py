"""Toolset contract types (SPEC.md section 6)."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

KEY_PATTERN = re.compile(r"^[a-z0-9-]+$")

# Paths the gateway serves itself. A toolset with one of these keys would shadow them.
# The UI asset prefixes are reserved so a toolset cannot take them, but requests for them
# go to the UI; the rest are served by the parent app and answer 404 if they reach the
# dispatcher.
UI_ASSET_PREFIXES = frozenset({"assets", "_astro", "static"})
RESERVED_KEYS = frozenset({"api", "healthz", "oauth"}) | UI_ASSET_PREFIXES

AuthKind = Literal["none", "api_key", "basic", "bearer", "service_account", "oauth2"]
ToolsetKind = Literal["native", "proxy"]
HealthStatus = Literal["ok", "degraded", "down"]


def validate_key(key: str) -> str:
    """Return the key if it is a valid, unreserved toolset key. Raise ValueError otherwise."""
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError(f"Toolset key {key!r} must match {KEY_PATTERN.pattern}")
    if key in RESERVED_KEYS:
        raise ValueError(f"Toolset key {key!r} is reserved")
    return key


class ToolsetManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    kind: ToolsetKind
    supported_auth: list[AuthKind]
    settings_schema: dict[str, Any]
    example_settings: dict[str, Any]
    version: str = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        return validate_key(value)

    @field_validator("supported_auth")
    @classmethod
    def _check_auth(cls, value: list[AuthKind]) -> list[AuthKind]:
        if not value:
            raise ValueError("supported_auth must list at least one auth kind")
        if len(set(value)) != len(value):
            raise ValueError("supported_auth contains duplicates")
        return value


class ToolsetConfig(BaseModel):
    """Per-toolset configuration as stored in the config store. Phase 1 has no store, so
    this is built from the manifest's example settings."""

    model_config = ConfigDict(frozen=True)

    key: str
    settings: dict[str, Any] = Field(default_factory=dict)
    portal_url: str | None = None


class Credentials(BaseModel):
    """Decrypted credential values handed to a toolset. Never logged, never serialised
    by anything but the crypto layer. The repr hides every value."""

    model_config = ConfigDict(frozen=True)

    kind: AuthKind = "none"
    values: dict[str, Any] = Field(default_factory=dict, repr=False)
    _token_getter: Callable[[], Awaitable[str]] | None = PrivateAttr(default=None)

    def __repr__(self) -> str:
        return f"Credentials(kind={self.kind!r}, values=<redacted {len(self.values)} keys>)"

    __str__ = __repr__

    def with_token_getter(self, getter: Callable[[], Awaitable[str]]) -> Credentials:
        """Attach the gateway's token refresher. Toolsets call `access_token()` per
        request and never touch refresh tokens themselves."""
        copy = self.model_copy()
        copy._token_getter = getter
        return copy

    async def access_token(self) -> str:
        """A currently valid OAuth2 access token, refreshed by the gateway when needed.

        Raises ReconnectRequired when the refresh token is dead and UpstreamUnavailable
        when the provider is temporarily unreachable."""
        if self._token_getter is None:
            if self.kind == "oauth2":
                raise RuntimeError("no token getter attached to this oauth2 credential")
            raise RuntimeError(f"credential kind {self.kind!r} has no access token")
        return await self._token_getter()


class HealthResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    detail: str = ""
