"""Manifold as an OAuth2 client to upstream providers (SPEC.md section 7).

Connect: the admin clicks Connect on an oauth2 credential, we redirect the browser to the
provider with PKCE and a single-use state bound to the credential, the provider sends the
browser back to /oauth/callback, and we exchange the code for tokens.

Refresh: toolsets call `Credentials.access_token()`; the TokenManager refreshes when the
cached token is near expiry, one refresh per credential at a time. `invalid_grant` marks
the credential reconnect_required; a transient upstream error does not change anything.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx2

from manifold.store.credentials import CredentialsRepo
from manifold.store.db import Database, utcnow

log = logging.getLogger(__name__)

STATE_TTL = timedelta(minutes=10)
REFRESH_MARGIN_SECONDS = 60
DEFAULT_EXPIRES_IN = 3600


@dataclass(frozen=True)
class Preset:
    auth_url: str
    token_url: str
    authorize_extras: dict[str, str]


PRESETS: dict[str, Preset] = {
    "google": Preset(
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        # Without these Google returns no refresh token on repeat consents.
        authorize_extras={"access_type": "offline", "prompt": "consent"},
    ),
    "microsoft": Preset(
        auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        authorize_extras={},
    ),
}


class OAuthConfigError(ValueError):
    pass


class ConnectError(RuntimeError):
    pass


class ReconnectRequired(RuntimeError):
    """The refresh token is dead. The admin must reconnect the credential."""


class UpstreamUnavailable(RuntimeError):
    """The provider did not answer properly. Try again later; nothing was changed."""


def endpoints(meta: dict) -> tuple[str, str, dict[str, str]]:
    provider = meta.get("provider", "generic")
    if provider in PRESETS:
        preset = PRESETS[provider]
        return preset.auth_url, preset.token_url, preset.authorize_extras
    auth_url, token_url = meta.get("auth_url"), meta.get("token_url")
    if not auth_url or not token_url:
        raise OAuthConfigError("generic provider needs auth_url and token_url")
    return auth_url, token_url, {}


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    return verifier, challenge.rstrip("=")


class UpstreamOAuth:
    def __init__(
        self,
        db: Database,
        credentials: CredentialsRepo,
        base_url: str,
        http: httpx2.AsyncClient,
        on_change: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._db = db
        self._credentials = credentials
        self._redirect_uri = f"{base_url}/oauth/callback"
        self._http = http
        self._on_change = on_change

    @property
    def redirect_uri(self) -> str:
        return self._redirect_uri

    # -- connect -----------------------------------------------------------------------

    async def start_connect(self, credential_id: int) -> str:
        """Create the state row and return the provider URL to send the browser to."""
        summary = await self._credentials.get_summary(credential_id)
        if summary.auth_kind != "oauth2":
            raise OAuthConfigError("only oauth2 credentials can be connected")
        auth_url, _, extras = endpoints(summary.meta)
        client_id = summary.meta.get("client_id")
        scopes = summary.meta.get("scopes") or []
        if not client_id:
            raise OAuthConfigError("credential has no client_id")
        if not scopes:
            raise OAuthConfigError(
                "credential has no scopes; add at least one under Scopes before connecting"
            )
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(32)
        await self._db.conn.execute(
            "INSERT INTO oauth_state (state, credential_id, code_verifier, created_at)"
            " VALUES (?, ?, ?, ?)",
            (state, credential_id, verifier, utcnow()),
        )
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **extras,
        }
        log.info("upstream connect started", extra={"credential_id": credential_id})
        return f"{auth_url}?{urlencode(params)}"

    async def handle_callback(self, state: str | None, code: str | None, error: str | None) -> int:
        """Finish the connect. Returns the credential id. Raises ConnectError with a
        message safe to show the admin. The state is consumed before anything else, so
        a replayed callback can never touch the stored token."""
        if not state:
            raise ConnectError("callback is missing state")
        row = await self._consume_state(state)
        if row is None:
            raise ConnectError("unknown or already used state; start the connect again")
        credential_id, verifier, created_at = row
        if datetime.fromisoformat(created_at.replace("Z", "+00:00")) + STATE_TTL < datetime.now(
            UTC
        ):
            raise ConnectError("the connect took longer than 10 minutes; start it again")
        if error:
            raise ConnectError(f"provider returned {error}")
        if not code:
            raise ConnectError("callback is missing code")

        summary = await self._credentials.get_summary(credential_id)
        _, token_url, _ = endpoints(summary.meta)
        current = await self._credentials.get(credential_id)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "client_id": summary.meta.get("client_id", ""),
            "client_secret": current.values.get("client_secret", ""),
            "code_verifier": verifier,
        }
        tokens = await self._post_token(token_url, form)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise ConnectError(
                "provider did not return a refresh token. For Google the authorize request "
                "needs access_type=offline and prompt=consent; check the provider preset."
            )
        expires_at = int(time.time()) + int(tokens.get("expires_in") or DEFAULT_EXPIRES_IN)
        await self._credentials.update_values(
            credential_id,
            {
                **current.values,
                "refresh_token": refresh_token,
                "access_token": tokens.get("access_token", ""),
                "expires_at": expires_at,
            },
        )
        await self._credentials.update_meta(
            credential_id,
            {
                "granted_scope": tokens.get("scope"),
                "connected_at": utcnow(),
                "expires_at": expires_at,
            },
        )
        await self._credentials.set_status(credential_id, "ok")
        log.info("upstream connect completed", extra={"credential_id": credential_id})
        if self._on_change:
            await self._on_change()
        return credential_id

    async def _consume_state(self, state: str) -> tuple[int, str, str] | None:
        conn = self._db.conn
        async with conn.execute(
            "DELETE FROM oauth_state WHERE state = ?"
            " RETURNING credential_id, code_verifier, created_at",
            (state,),
        ) as c:
            row = await c.fetchone()
        return (int(row[0]), row[1], row[2]) if row else None

    async def purge_expired_states(self) -> None:
        cutoff = datetime.now(UTC) - STATE_TTL
        cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
        await self._db.conn.execute("DELETE FROM oauth_state WHERE created_at < ?", (cutoff_text,))
        await self._db.conn.execute("DELETE FROM oauth_state WHERE created_at < ?", (cutoff_text,))

    # -- token endpoint ----------------------------------------------------------------

    async def _post_token(self, token_url: str, form: dict[str, str]) -> dict:
        try:
            response = await self._http.post(
                token_url, data=form, headers={"Accept": "application/json"}, timeout=20
            )
        except httpx2.HTTPError as exc:
            raise UpstreamUnavailable(f"token endpoint unreachable: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise UpstreamUnavailable(f"token endpoint returned {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamUnavailable("token endpoint returned a non-JSON body") from exc
        if response.status_code != 200:
            err = body.get("error", "unknown_error") if isinstance(body, dict) else "unknown_error"
            if err == "invalid_grant":
                raise ReconnectRequired(body.get("error_description") or "invalid_grant")
            raise ConnectError(f"token endpoint refused: {err}")
        return body


class TokenManager:
    """Hands out access tokens for oauth2 credentials, refreshing when needed.

    One refresh per credential at a time: concurrent callers share the lock, and the
    second one finds the fresh token in the cache when the first finishes."""

    def __init__(self, credentials: CredentialsRepo, upstream: UpstreamOAuth) -> None:
        self._credentials = credentials
        self._upstream = upstream
        self._locks: dict[int, asyncio.Lock] = {}
        self._cache: dict[int, tuple[str, int]] = {}
        self.refresh_count = 0

    def getter(self, credential_id: int) -> Callable[[], Awaitable[str]]:
        return lambda: self.access_token(credential_id)

    def forget(self, credential_id: int) -> None:
        self._cache.pop(credential_id, None)

    async def access_token(self, credential_id: int) -> str:
        cached = self._cache.get(credential_id)
        if cached and cached[1] - REFRESH_MARGIN_SECONDS > time.time():
            return cached[0]
        lock = self._locks.setdefault(credential_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(credential_id)
            if cached and cached[1] - REFRESH_MARGIN_SECONDS > time.time():
                return cached[0]
            current = await self._credentials.get(credential_id)
            token, expires_at = current.values.get("access_token"), current.values.get("expires_at")
            if token and expires_at and int(expires_at) - REFRESH_MARGIN_SECONDS > time.time():
                self._cache[credential_id] = (token, int(expires_at))
                return token
            return await self._refresh(credential_id, current.values)

    async def _refresh(self, credential_id: int, values: dict) -> str:
        summary = await self._credentials.get_summary(credential_id)
        if summary.status in ("reconnect_required", "scopes_changed", "unconnected"):
            raise ReconnectRequired(f"credential is {summary.status}")
        refresh_token = values.get("refresh_token")
        if not refresh_token:
            await self._credentials.set_status(credential_id, "reconnect_required")
            raise ReconnectRequired("credential has no refresh token")
        _, token_url, _ = endpoints(summary.meta)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": summary.meta.get("client_id", ""),
            "client_secret": values.get("client_secret", ""),
        }
        try:
            tokens = await self._upstream._post_token(token_url, form)
        except ReconnectRequired:
            self._cache.pop(credential_id, None)
            await self._credentials.set_status(credential_id, "reconnect_required")
            log.warning("refresh token rejected", extra={"credential_id": credential_id})
            raise
        self.refresh_count += 1
        expires_at = int(time.time()) + int(tokens.get("expires_in") or DEFAULT_EXPIRES_IN)
        new_values = {
            **values,
            "access_token": tokens["access_token"],
            "expires_at": expires_at,
            "refresh_token": tokens.get("refresh_token") or refresh_token,
        }
        await self._credentials.update_values(credential_id, new_values, token_refresh=True)
        await self._credentials.update_meta(credential_id, {"expires_at": expires_at})
        self._cache[credential_id] = (tokens["access_token"], expires_at)
        log.info("access token refreshed", extra={"credential_id": credential_id})
        return tokens["access_token"]
