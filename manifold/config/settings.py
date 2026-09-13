"""Environment loading.

The only runtime configuration that comes from the environment is listed in
SPEC.md section 11. Everything else lives in SQLite from Phase 2 onwards.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})
MASTER_KEY_BYTES = 32


class SettingsError(ValueError):
    """Raised when the environment is missing or malformed. Never contains a secret."""


@dataclass(frozen=True)
class Settings:
    master_key: bytes = field(repr=False)
    admin_emails: frozenset[str]
    base_url: str
    log_level: str
    data_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            master_key=_parse_master_key(env.get("MANIFOLD_MASTER_KEY", "")),
            admin_emails=_parse_emails(env.get("MANIFOLD_ADMIN_EMAILS", "")),
            base_url=env.get("MANIFOLD_BASE_URL", "http://localhost:8800").rstrip("/"),
            log_level=_parse_log_level(env.get("MANIFOLD_LOG_LEVEL", "info")),
            data_dir=Path(env.get("MANIFOLD_DATA_DIR", "/data")),
        )


def _parse_master_key(raw: str) -> bytes:
    raw = raw.strip()
    if not raw:
        raise SettingsError(
            "MANIFOLD_MASTER_KEY is not set. Generate one with `openssl rand -base64 32` "
            "and store it in a password manager before starting Manifold."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SettingsError("MANIFOLD_MASTER_KEY is not valid base64.") from exc
    if len(key) != MASTER_KEY_BYTES:
        raise SettingsError(
            f"MANIFOLD_MASTER_KEY must decode to {MASTER_KEY_BYTES} bytes, got {len(key)}."
        )
    return key


def _parse_emails(raw: str) -> frozenset[str]:
    emails = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not emails:
        raise SettingsError(
            "MANIFOLD_ADMIN_EMAILS is not set. It is the only identity allowed to authorise "
            "claude.ai connectors and open the admin UI."
        )
    for email in emails:
        if "@" not in email:
            raise SettingsError(f"MANIFOLD_ADMIN_EMAILS entry is not an email address: {email!r}")
    return frozenset(emails)


def _parse_log_level(raw: str) -> str:
    level = raw.strip().lower()
    if level not in LOG_LEVELS:
        raise SettingsError(f"MANIFOLD_LOG_LEVEL must be one of {sorted(LOG_LEVELS)}, got {raw!r}.")
    return level
