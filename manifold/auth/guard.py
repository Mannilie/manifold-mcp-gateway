"""Rate limit and cap for dynamic client registration, which is reachable without login."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from manifold.auth.store import TokenStore

log = logging.getLogger(__name__)

DEFAULT_MAX_PER_WINDOW = 30
DEFAULT_WINDOW_SECONDS = 10 * 60
DEFAULT_MAX_CLIENTS = 100


class RegistrationGuard:
    """Refuses POSTs with 429 once the window is full or the client cap is reached.

    Single user, one origin, so the limit is global rather than per source address. It is
    a brake on abuse, not an accounting system.
    """

    def __init__(
        self,
        app: ASGIApp,
        store: TokenStore,
        max_per_window: int = DEFAULT_MAX_PER_WINDOW,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_clients: int = DEFAULT_MAX_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._store = store
        self._max_per_window = max_per_window
        self._window = window_seconds
        self._max_clients = max_clients
        self._clock = clock
        self._recent: deque[float] = deque()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self._app(scope, receive, send)
            return
        now = self._clock()
        while self._recent and now - self._recent[0] >= self._window:
            self._recent.popleft()
        if len(self._recent) >= self._max_per_window:
            retry = int(self._window - (now - self._recent[0])) + 1
            log.warning("registration rate limit tripped")
            await _too_many(send, "too_many_registrations", retry)
            return
        if await self._store.count_clients() >= self._max_clients:
            log.warning("registration refused, client cap reached")
            await _too_many(send, "client_cap_reached", int(self._window))
            return
        self._recent.append(now)
        await self._app(scope, receive, send)


async def _too_many(send: Send, error: str, retry_after: int) -> None:
    body = json.dumps({"error": error}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(retry_after).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
