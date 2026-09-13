"""The one Google HTTP client. Relative paths in, parsed JSON out, readable errors up."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import httpx2

from manifold.gateway.manifest import Credentials
from manifold.google import endpoints
from manifold.google.auth import TokenGetter, service_account_email, token_getter
from manifold.google.errors import GoogleError, map_error

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 0.5
RETRY_JITTER_SECONDS = 0.3
TOTAL_BUDGET_SECONDS = 12.0
REQUEST_TIMEOUT_SECONDS = 20.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_shared_http: httpx2.AsyncClient | None = None


def shared_http() -> httpx2.AsyncClient:
    """One connection pool for every Google toolset. Tests replace this."""
    global _shared_http
    if _shared_http is None:
        _shared_http = httpx2.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
    return _shared_http


class GoogleClient:
    def __init__(
        self,
        get_token: TokenGetter,
        *,
        base_url: str,
        http: httpx2.AsyncClient,
        identity: str | None = None,
    ) -> None:
        self._get_token = get_token
        self._base = base_url.rstrip("/")
        self._http = http
        self.identity = identity

    async def get(self, path: str, *, params: dict | None = None, context: dict | None = None):
        return await self._request("GET", path, params=params, context=context)

    async def post(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        context: dict | None = None,
    ):
        return await self._request("POST", path, json=json, params=params, context=context)

    async def put(
        self,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        context: dict | None = None,
    ):
        return await self._request("PUT", path, json=json, params=params, context=context)

    async def delete(self, path: str, *, params: dict | None = None, context: dict | None = None):
        return await self._request("DELETE", path, params=params, context=context)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        context: dict | None = None,
    ) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"
        started = time.monotonic()
        attempt = 0
        while True:
            token = await self._get_token()
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except httpx2.HTTPError as exc:
                if attempt >= MAX_RETRIES or time.monotonic() - started > TOTAL_BUDGET_SECONDS:
                    raise GoogleError(
                        f"Google was unreachable ({type(exc).__name__}) after retries"
                    ) from exc
                await self._sleep(attempt)
                attempt += 1
                continue
            if response.status_code in RETRY_STATUSES:
                if attempt >= MAX_RETRIES or time.monotonic() - started > TOTAL_BUDGET_SECONDS:
                    raise map_error(
                        response.status_code,
                        _json(response),
                        context=context,
                        identity=self.identity,
                    )
                log.info(
                    "google retry",
                    extra={"status": response.status_code, "attempt": attempt + 1, "path": path},
                )
                await self._sleep(attempt, response.headers.get("retry-after"))
                attempt += 1
                continue
            if response.status_code >= 400:
                raise map_error(
                    response.status_code, _json(response), context=context, identity=self.identity
                )
            if response.status_code == 204 or not response.content:
                return None
            return _json(response)

    async def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        delay = RETRY_BASE_SECONDS * (2**attempt) + random.uniform(0, RETRY_JITTER_SECONDS)
        if retry_after and retry_after.isdigit():
            delay = max(delay, min(float(retry_after), TOTAL_BUDGET_SECONDS))
        await asyncio.sleep(delay)


def _json(response: httpx2.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"error": {"message": response.text[:200] or f"HTTP {response.status_code}"}}


def client_for(
    credentials: Credentials,
    scopes: list[str],
    *,
    base_url: str | None = None,
    http: httpx2.AsyncClient | None = None,
) -> GoogleClient:
    """A client for one API base, authenticated by a stored credential."""
    http = http or shared_http()
    return GoogleClient(
        token_getter(credentials, scopes, http),
        base_url=base_url or endpoints.SHEETS_BASE,
        http=http,
        identity=service_account_email(credentials),
    )
