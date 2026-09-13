"""Cloudflare Access identity for the authorize page.

`/oauth/authorize` sits inside the Cloudflare Access Allow application, so by the time a
request reaches Manifold, Cloudflare has authenticated the browser and set
`Cf-Access-Authenticated-User-Email`. This module trusts that header only because of that
placement. If the path is ever moved outside the Access app, this check is worthless.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

ACCESS_EMAIL_HEADER = b"cf-access-authenticated-user-email"

current_access_email: ContextVar[str | None] = ContextVar("current_access_email", default=None)


def access_email_from_scope(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == ACCESS_EMAIL_HEADER:
            email = value.decode("latin-1").strip().lower()
            return email or None
    return None


class AccessGate:
    """Refuse with 403 unless Cloudflare Access authenticated one of `allowed`.

    On success the email is placed in `current_access_email` for the duration of the
    request so the OAuth provider can bind the authorization code to a subject. This is a
    class rather than a closure because Starlette's `Route` treats a bare function as a
    request handler, not an ASGI app.
    """

    def __init__(self, app: ASGIApp, allowed: frozenset[str]) -> None:
        self._app = app
        self._allowed = allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        email = access_email_from_scope(scope)
        if email is None or email not in self._allowed:
            log.warning(
                "authorize refused",
                extra={"reason": "no_access_identity" if email is None else "not_admin"},
            )
            body = b"Cloudflare Access did not authenticate an admin for this request.\n"
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token = current_access_email.set(email)
        try:
            await self._app(scope, receive, send)
        finally:
            current_access_email.reset(token)
