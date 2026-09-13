"""Access tokens for Google calls.

Service accounts: an RS256 JWT signed with the key from the stored JSON, exchanged at the
token endpoint. Tokens are cached until near expiry in a module-level cache keyed by
client_email and scopes, so every toolset sharing a credential shares the token, and one
exchange at a time per key (the same coalescing rule as OAuth refresh).

OAuth2 credentials: the gateway's token manager, via `Credentials.access_token()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

import httpx2
import jwt

from manifold.gateway.manifest import Credentials
from manifold.google import endpoints
from manifold.google.errors import GoogleError

log = logging.getLogger(__name__)

JWT_LIFETIME_SECONDS = 3600
REFRESH_MARGIN_SECONDS = 120

TokenGetter = Callable[[], Awaitable[str]]

_cache: dict[tuple[str, str], tuple[str, float]] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def clear_cache() -> None:
    _cache.clear()
    _locks.clear()


def service_account_email(credentials: Credentials) -> str | None:
    sa = credentials.values.get("json") if credentials.kind == "service_account" else None
    return sa.get("client_email") if isinstance(sa, dict) else None


def token_getter(
    credentials: Credentials, scopes: list[str], http: httpx2.AsyncClient
) -> TokenGetter:
    if credentials.kind == "oauth2":
        return credentials.access_token
    if credentials.kind == "service_account":
        sa = credentials.values.get("json")
        if not isinstance(sa, dict) or "client_email" not in sa or "private_key" not in sa:
            raise GoogleError("service account credential is missing client_email or private_key")
        return lambda: _service_account_token(sa, scopes, http)
    raise GoogleError(f"credential kind {credentials.kind!r} cannot call Google APIs")


async def _service_account_token(sa: dict, scopes: list[str], http: httpx2.AsyncClient) -> str:
    key = (sa["client_email"], " ".join(sorted(scopes)))
    cached = _cache.get(key)
    if cached and cached[1] - REFRESH_MARGIN_SECONDS > time.time():
        return cached[0]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if cached and cached[1] - REFRESH_MARGIN_SECONDS > time.time():
            return cached[0]
        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": sa["client_email"],
                "scope": " ".join(scopes),
                "aud": endpoints.TOKEN_URL,
                "iat": now,
                "exp": now + JWT_LIFETIME_SECONDS,
            },
            sa["private_key"],
            algorithm="RS256",
            headers={"kid": sa.get("private_key_id")} if sa.get("private_key_id") else None,
        )
        try:
            response = await http.post(
                endpoints.TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                timeout=20,
            )
        except httpx2.HTTPError as exc:
            raise GoogleError(f"Google token endpoint unreachable: {type(exc).__name__}") from exc
        if response.status_code != 200:
            detail = ""
            with contextlib.suppress(ValueError):
                body = response.json()
                detail = body.get("error_description") or body.get("error", "")
            raise GoogleError(
                f"Google refused the service account token request ({response.status_code}): "
                f"{detail or 'no detail'}. Check the service account JSON and that the "
                "Sheets API is enabled on its project."
            )
        body = response.json()
        expires_at = now + int(body.get("expires_in") or JWT_LIFETIME_SECONDS)
        _cache[key] = (body["access_token"], expires_at)
        log.info("service account token issued", extra={"client_email": sa["client_email"]})
        return body["access_token"]
