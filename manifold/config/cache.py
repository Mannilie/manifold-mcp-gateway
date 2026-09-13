"""Cache-Control per path (DECISIONS.md, Phase 3 gate 1)."""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

NO_STORE = b"no-store"
IMMUTABLE = b"public, max-age=31536000, immutable"


def cache_control(app: ASGIApp) -> ASGIApp:
    async def wrapper(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        path: str = scope["path"]
        if path.startswith("/_astro/"):
            value = IMMUTABLE
        elif (
            path.startswith("/api/")
            or path == "/api"
            or path.startswith("/oauth/")
            or path.startswith("/.well-known/")
        ):
            value = NO_STORE
        else:
            value = NO_STORE if not _looks_like_asset(path) else None

        async def send_with_header(message):
            if message["type"] == "http.response.start" and value is not None:
                headers = [(k, v) for k, v in message["headers"] if k.lower() != b"cache-control"]
                headers.append((b"cache-control", value))
                message = {**message, "headers": headers}
            await send(message)

        await app(scope, receive, send_with_header)

    return wrapper


def _looks_like_asset(path: str) -> bool:
    last = path.rsplit("/", 1)[-1]
    return "." in last and not last.endswith(".html")
