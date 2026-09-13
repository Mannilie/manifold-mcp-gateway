"""OAuth 2.1 authorization server logic on top of a `TokenStore`.

Tokens are opaque random strings; only their SHA-256 is stored, so the `token` field on
the stored SDK models is the hash. Every token is bound to exactly one toolset via the
RFC 8707 resource indicator: one connector, one token, one toolset.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from collections.abc import Callable
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from manifold.auth.access import current_access_email
from manifold.auth.store import TokenStore

log = logging.getLogger(__name__)

# Dynamic client registration is open by design (RFC 7591), so the redirect target is the
# only thing that stops a crafted authorize link from handing a code to a stranger.
ALLOWED_REDIRECT_HOSTS = frozenset({"claude.ai", "claude.com"})

CODE_TTL_SECONDS = 5 * 60
ACCESS_TOKEN_TTL_SECONDS = 60 * 60
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_resource(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/").lower()


class ManifoldOAuthProvider:
    """Implements `mcp.server.auth.provider.OAuthAuthorizationServerProvider`.

    `known_toolset(key)` says whether a toolset is currently mounted, so a token can only
    be issued for a resource that exists. It is a callable because the registry outlives
    any one mount in Phase 2.
    """

    def __init__(
        self,
        store: TokenStore,
        admin_emails: frozenset[str],
        base_url: str,
        known_toolset: Callable[[str], bool],
        allowed_redirect_hosts: frozenset[str] = ALLOWED_REDIRECT_HOSTS,
    ) -> None:
        self.store = store
        self._admin_emails = admin_emails
        self._base = normalise_resource(base_url)
        self._known_toolset = known_toolset
        self._allowed_redirect_hosts = allowed_redirect_hosts

    # -- clients -----------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            host = (uri.host or "").lower()
            if uri.scheme != "https" or host not in self._allowed_redirect_hosts:
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "redirect_uris must be https URLs on "
                    + ", ".join(sorted(self._allowed_redirect_hosts)),
                )
        await self.store.save_client(client_info)
        log.info(
            "oauth client registered",
            extra={"client_id": client_info.client_id, "client_name": client_info.client_name},
        )

    # -- authorization code ------------------------------------------------------------

    def toolset_for_resource(self, resource: str | None) -> str | None:
        """The toolset key a resource indicator names, or None if it is not one of ours."""
        if not resource:
            return None
        normalised = normalise_resource(resource)
        prefix = self._base + "/"
        if not normalised.startswith(prefix):
            return None
        key = normalised[len(prefix) :]
        if "/" in key or not self._known_toolset(key):
            return None
        return key

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        email = current_access_email.get()
        if email is None or email not in self._admin_emails:
            raise AuthorizeError("access_denied", "Cloudflare Access did not authenticate an admin")
        key = self.toolset_for_resource(params.resource)
        if key is None:
            raise AuthorizeError(
                "invalid_target",
                f"resource must be the URL of a mounted toolset, for example {self._base}/manifold",
            )
        await self.store.sweep(time.time())
        code = secrets.token_urlsafe(32)
        await self.store.save_code(
            AuthorizationCode(
                code=digest(code),
                scopes=params.scopes or [],
                expires_at=time.time() + CODE_TTL_SECONDS,
                client_id=client.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=f"{self._base}/{key}",
                subject=email,
            )
        )
        log.info("authorization code issued", extra={"client_id": client.client_id, "toolset": key})
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = await self.store.get_code(digest(authorization_code))
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not await self.store.delete_code(authorization_code.code):
            raise TokenError("invalid_grant", "authorization code already used")
        return await self._issue(
            client.client_id,
            authorization_code.scopes,
            authorization_code.resource,
            authorization_code.subject,
        )

    # -- tokens ------------------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        stored = await self.store.get_refresh_token(digest(refresh_token))
        if stored is None or stored.client_id != client.client_id:
            return None
        if stored.expires_at and stored.expires_at < time.time():
            await self.store.revoke(stored.token)
            return None
        return stored

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        await self.store.revoke(refresh_token.token)
        return await self._issue(
            client.client_id,
            scopes or refresh_token.scopes,
            refresh_token.resource,
            refresh_token.subject,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        stored = await self.store.get_access_token(digest(token))
        if stored is None:
            return None
        if stored.expires_at and stored.expires_at < time.time():
            await self.store.revoke(stored.token)
            return None
        return stored

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self.store.revoke(token.token)

    # -- internals ---------------------------------------------------------------------

    async def _issue(
        self, client_id: str, scopes: list[str], resource: str | None, subject: str | None
    ) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        await self.store.save_token_pair(
            AccessToken(
                token=digest(access),
                client_id=client_id,
                scopes=scopes,
                expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
                resource=resource,
                subject=subject,
            ),
            RefreshToken(
                token=digest(refresh),
                client_id=client_id,
                scopes=scopes,
                expires_at=now + REFRESH_TOKEN_TTL_SECONDS,
                resource=resource,
                subject=subject,
            ),
        )
        log.info("tokens issued", extra={"client_id": client_id, "resource": resource})
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )


class ToolsetTokenVerifier:
    """Accepts only a token issued for this toolset's resource URL."""

    def __init__(self, provider: ManifoldOAuthProvider, resource_url: str) -> None:
        self._provider = provider
        self._resource = normalise_resource(resource_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        stored = await self._provider.load_access_token(token)
        if stored is None:
            return None
        if not stored.resource or normalise_resource(stored.resource) != self._resource:
            log.warning(
                "token refused for wrong resource",
                extra={"client_id": stored.client_id, "wanted": self._resource},
            )
            return None
        return stored
