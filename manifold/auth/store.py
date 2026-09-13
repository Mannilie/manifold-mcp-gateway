"""Token store interface. Phase 1 ships the in-memory implementation; Phase 2 adds SQLite
behind the same protocol so the OAuth handlers and provider do not change.

Every `token` field on the stored SDK models holds a SHA-256 hex digest, never the raw
token. Expiry is the provider's business; the store returns whatever it holds.
"""

from __future__ import annotations

from typing import Protocol

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull


class TokenStore(Protocol):
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None: ...
    async def save_client(self, client: OAuthClientInformationFull) -> None: ...
    async def count_clients(self) -> int: ...

    async def save_code(self, code: AuthorizationCode) -> None: ...
    async def get_code(self, code_hash: str) -> AuthorizationCode | None: ...
    async def delete_code(self, code_hash: str) -> bool:
        """Remove the code. Returns False if it was not there, which makes codes single use."""
        ...

    async def save_token_pair(self, access: AccessToken, refresh: RefreshToken) -> None: ...
    async def get_access_token(self, token_hash: str) -> AccessToken | None: ...
    async def get_refresh_token(self, token_hash: str) -> RefreshToken | None: ...
    async def revoke(self, token_hash: str) -> None:
        """Revoke the token with this hash and its partner, whichever kind it is."""
        ...

    async def sweep(self, now: float) -> None:
        """Drop expired codes and tokens."""
        ...

    async def clear(self) -> None:
        """Forget every client, code and token. The Phase 3 "disconnect all" action."""
        ...


class InMemoryTokenStore:
    def __init__(self) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access: dict[str, AccessToken] = {}
        self._refresh: dict[str, RefreshToken] = {}
        self._partner: dict[str, str] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        self._clients[client.client_id] = client

    async def count_clients(self) -> int:
        return len(self._clients)

    async def save_code(self, code: AuthorizationCode) -> None:
        self._codes[code.code] = code

    async def get_code(self, code_hash: str) -> AuthorizationCode | None:
        return self._codes.get(code_hash)

    async def delete_code(self, code_hash: str) -> bool:
        return self._codes.pop(code_hash, None) is not None

    async def save_token_pair(self, access: AccessToken, refresh: RefreshToken) -> None:
        self._access[access.token] = access
        self._refresh[refresh.token] = refresh
        self._partner[access.token] = refresh.token
        self._partner[refresh.token] = access.token

    async def get_access_token(self, token_hash: str) -> AccessToken | None:
        return self._access.get(token_hash)

    async def get_refresh_token(self, token_hash: str) -> RefreshToken | None:
        return self._refresh.get(token_hash)

    async def revoke(self, token_hash: str) -> None:
        partner = self._partner.pop(token_hash, None)
        self._access.pop(token_hash, None)
        self._refresh.pop(token_hash, None)
        if partner is not None:
            self._partner.pop(partner, None)
            self._access.pop(partner, None)
            self._refresh.pop(partner, None)

    async def sweep(self, now: float) -> None:
        for key in [k for k, c in self._codes.items() if c.expires_at < now]:
            del self._codes[key]
        for key in [k for k, t in self._access.items() if t.expires_at and t.expires_at < now]:
            self._access.pop(key, None)
        for key in [k for k, t in self._refresh.items() if t.expires_at and t.expires_at < now]:
            await self.revoke(key)

    async def clear(self) -> None:
        self._clients.clear()
        self._codes.clear()
        self._access.clear()
        self._refresh.clear()
        self._partner.clear()
