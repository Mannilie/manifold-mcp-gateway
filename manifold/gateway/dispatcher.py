"""Routes `/<toolset>` and `/<toolset>/healthz` to the right toolset (DECISIONS.md, mounting gate).

This module is deliberately free of Starlette beyond the ASGI type aliases so it can be
exercised in isolation with fake ASGI apps. It never redirects.

Paths handled, where `<key>` is a registered toolset:

    /<key>            the MCP Streamable HTTP endpoint
    /<key>/           same endpoint, trailing slash tolerated
    /<key>/healthz    GET, toolset health JSON, no auth
    /<key>/<other>    404
    /<reserved>/...   404 (served by the parent app; reaching here means no route), except
                      the UI asset prefixes, which go to the fallback
    anything else     handed to the fallback app (the admin UI)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from manifold.gateway.manifest import RESERVED_KEYS, UI_ASSET_PREFIXES

HealthFn = Callable[[], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolsetRoute:
    mcp_app: ASGIApp
    health: HealthFn


class ToolsetDispatcher:
    def __init__(
        self,
        routes: Mapping[str, ToolsetRoute],
        fallback: ASGIApp,
        reserved: frozenset[str] = RESERVED_KEYS,
        ui_prefixes: frozenset[str] = UI_ASSET_PREFIXES,
    ) -> None:
        # Held by reference on purpose: Phase 2 hot reload swaps entries in this mapping.
        self._routes = routes
        self._fallback = fallback
        self._reserved = reserved - ui_prefixes
        self._ui_prefixes = ui_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._fallback(scope, receive, send)
            return

        key, rest = _split(scope)
        if key in self._reserved:
            await _send_json(send, 404, {"error": "not_found"})
            return
        if key in self._ui_prefixes:
            await self._fallback(scope, receive, send)
            return
        route = self._routes.get(key)
        if route is None:
            await self._fallback(scope, receive, send)
            return
        if rest == "":
            await route.mcp_app(scope, receive, send)
            return
        if rest == "healthz":
            if scope["method"] not in ("GET", "HEAD"):
                await _send_json(send, 405, {"error": "method_not_allowed"}, allow="GET, HEAD")
                return
            body = await route.health()
            status = 200 if body.get("status") == "ok" else 503
            await _send_json(send, status, body)
            return
        await _send_json(send, 404, {"error": "not_found"})


def _split(scope: Scope) -> tuple[str, str]:
    """Return (first segment, remainder) of the path relative to where we are mounted."""
    path: str = scope["path"]
    root: str = scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root) :]
    key, _, rest = path.strip("/").partition("/")
    return key, rest.strip("/")


async def _send_json(
    send: Send, status: int, payload: dict[str, Any], allow: str | None = None
) -> None:
    body = json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if allow:
        headers.append((b"allow", allow.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
