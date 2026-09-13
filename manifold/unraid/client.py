"""GraphQL client for the Unraid 7.2 API."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import ssl
import time
from typing import Any

import httpx2

from manifold.gateway.manifest import Credentials

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 0.5
RETRY_JITTER_SECONDS = 0.3
TOTAL_BUDGET_SECONDS = 12.0
REQUEST_TIMEOUT_SECONDS = 20.0

_FIELD_ERROR = re.compile(r'Cannot query field "(?P<field>[^"]+)" on type "(?P<type>[^"]+)"')

_shared_http: dict[bool, httpx2.AsyncClient] = {}


def shared_http(verify_tls: bool) -> httpx2.AsyncClient:
    if verify_tls not in _shared_http:
        _shared_http[verify_tls] = httpx2.AsyncClient(
            verify=verify_tls, timeout=REQUEST_TIMEOUT_SECONDS
        )
    return _shared_http[verify_tls]


class UnraidError(Exception):
    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class UnraidSchemaError(UnraidError):
    """The API no longer has a field a tool relies on. Names the field and type."""

    def __init__(self, field: str, type_name: str) -> None:
        super().__init__(
            f"the Unraid API has no field {field!r} on type {type_name!r}; the API may have "
            "changed in an Unraid upgrade and this toolset needs updating",
            "schema",
        )
        self.field = field
        self.type_name = type_name


class UnraidClient:
    def __init__(
        self, server_url: str, api_key: str, header: str, http: httpx2.AsyncClient
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self._api_key = api_key
        self._header = header
        self._http = http
        self._logged_request = False

    async def query(
        self, document: str, variables: dict[str, Any] | None = None, *, context: str
    ) -> dict:
        """Run a GraphQL document and return its `data`. Raises UnraidError with a message
        Claude can act on."""
        url = f"{self.server_url}/graphql"
        headers = {self._header: self._api_key, "Accept": "application/json"}
        started = time.monotonic()
        attempt = 0
        while True:
            # The GUI sets session cookies; a cookie on a GraphQL POST wakes emhttp's CSRF
            # check. The API is authenticated by the key header alone, never by a cookie.
            self._http.cookies.clear()
            if not self._logged_request:
                self._logged_request = True
                log.info(
                    "unraid request",
                    extra={
                        "method": "POST",
                        "url": url,
                        "header_names": sorted([*headers, "content-type"]),
                        "follow_redirects": False,
                    },
                )
            try:
                response = await self._http.post(
                    url,
                    json={"query": document, "variables": variables or {}},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    follow_redirects=False,
                )
            except httpx2.HTTPError as exc:
                if _is_tls_failure(exc):
                    raise UnraidError(
                        f"the certificate at {self.server_url} is not trusted (self-signed?). "
                        "For a LAN address set verify_tls to false in the unraid toolset "
                        "settings, or use the http:// URL.",
                        "tls",
                    ) from exc
                if attempt >= MAX_RETRIES or time.monotonic() - started > TOTAL_BUDGET_SECONDS:
                    raise UnraidError(
                        f"Unraid is unreachable at {self.server_url} ({type(exc).__name__}). "
                        "Check the server URL setting and that the API is enabled under "
                        "Settings, Management Access.",
                        "unreachable",
                    ) from exc
                await self._sleep(attempt)
                attempt += 1
                continue
            if response.status_code >= 500:
                if attempt >= MAX_RETRIES or time.monotonic() - started > TOTAL_BUDGET_SECONDS:
                    raise UnraidError(
                        f"Unraid returned {response.status_code} for {context} after retries",
                        "upstream_error",
                    )
                await self._sleep(attempt)
                attempt += 1
                continue
            if 300 <= response.status_code < 400:
                raise UnraidError(
                    f"Unraid redirected {url} to {response.headers.get('location', '?')}. "
                    "Set server_url to the exact origin the GUI answers on, with no path, "
                    "matching the scheme (http or https) it serves.",
                    "redirect",
                )
            if "invalid csrf token" in response.text.lower():
                raise UnraidError(
                    f"{url} was answered by the Unraid web GUI, not the API (Invalid CSRF "
                    "token). The GUI's nginx did not hand the request to the API. Check that "
                    "the API is enabled under Settings, Management Access, that server_url is "
                    "the GUI origin with no path, and compare the 'unraid request' log line "
                    "with a working curl.",
                    "csrf",
                )
            if response.status_code in (401, 403):
                raise UnraidError(
                    "Unraid rejected the API key. Check the credential and that the key has "
                    "the permissions this toolset needs.",
                    "unauthorised",
                )
            if response.status_code >= 400:
                raise UnraidError(
                    f"Unraid returned {response.status_code} for {context}: {response.text[:200]}",
                    "bad_request",
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise UnraidError(
                    f"Unraid answered {context} with something that is not JSON; is the server "
                    "URL pointing at the web GUI rather than the API?",
                    "bad_response",
                ) from exc
            errors = body.get("errors") or []
            if errors:
                raise self._map_graphql_error(errors, context)
            return body.get("data") or {}

    def _map_graphql_error(self, errors: list[dict], context: str) -> UnraidError:
        first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
        message = str(first.get("message", "unknown error"))
        match = _FIELD_ERROR.search(message)
        if match:
            return UnraidSchemaError(match.group("field"), match.group("type"))
        code = str((first.get("extensions") or {}).get("code", "")).upper()
        if code in ("FORBIDDEN", "UNAUTHENTICATED") or "permission" in message.lower():
            return UnraidError(
                f"the API key is not allowed to {context}: {message}. Grant the matching "
                "permission on the key under Settings, Management Access, API Keys.",
                "forbidden",
            )
        return UnraidError(f"Unraid refused {context}: {message}", "graphql")

    async def _sleep(self, attempt: int) -> None:
        delay = RETRY_BASE_SECONDS * (2**attempt) + random.uniform(0, RETRY_JITTER_SECONDS)
        await asyncio.sleep(delay)


def _is_tls_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(exc).upper():
            return True
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return False


def client_for(
    credentials: Credentials,
    server_url: str,
    verify_tls: bool = True,
    http: httpx2.AsyncClient | None = None,
) -> UnraidClient:
    if credentials.kind != "api_key" or not credentials.values.get("key"):
        raise UnraidError(
            "the unraid toolset needs an api_key credential holding the Unraid API key"
        )
    header = credentials.values.get("header") or "x-api-key"
    return UnraidClient(
        server_url, credentials.values["key"], header, http or shared_http(verify_tls)
    )
