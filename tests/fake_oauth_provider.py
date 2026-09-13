"""A fake upstream OAuth provider that enforces the Google-specific parameters.

authorize: records the request, refuses PKCE-less requests, and only marks the resulting
code as refresh-capable when access_type=offline and prompt=consent were present.
token: authorization_code returns a refresh token only for refresh-capable codes;
refresh_token behaves according to `mode` (ok, invalid_grant, outage).
"""

from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route


class FakeProvider:
    def __init__(self) -> None:
        self.mode = "ok"
        self.codes: dict[str, dict] = {}
        self.authorize_requests: list[dict] = []
        self.token_requests: list[dict] = []
        self.refresh_calls = 0
        self.issued_access = 0
        self.app = Starlette(
            routes=[
                Route("/authorize", self.authorize, methods=["GET"]),
                Route("/token", self.token, methods=["POST"]),
            ]
        )

    async def authorize(self, request: Request):
        params = dict(request.query_params)
        self.authorize_requests.append(params)
        if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
            return JSONResponse({"error": "invalid_request", "detail": "PKCE required"}, 400)
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "offline": params.get("access_type") == "offline" and params.get("prompt") == "consent",
            "scope": params.get("scope", ""),
            "challenge": params["code_challenge"],
        }
        return RedirectResponse(
            f"{params['redirect_uri']}?code={code}&state={params.get('state', '')}", 302
        )

    async def token(self, request: Request):
        form = dict(await request.form())
        self.token_requests.append(form)
        if form.get("grant_type") == "authorization_code":
            entry = self.codes.pop(form.get("code", ""), None)
            if entry is None:
                return JSONResponse({"error": "invalid_grant"}, 400)
            if not form.get("code_verifier"):
                return JSONResponse({"error": "invalid_request"}, 400)
            self.issued_access += 1
            body = {
                "access_token": f"access-{self.issued_access}",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": " ".join(entry["scope"].split()[:1]),  # grants less than asked
            }
            if entry["offline"]:
                body["refresh_token"] = f"refresh-{self.issued_access}"
            return JSONResponse(body)
        if form.get("grant_type") == "refresh_token":
            self.refresh_calls += 1
            if self.mode == "invalid_grant":
                return JSONResponse({"error": "invalid_grant", "error_description": "revoked"}, 400)
            if self.mode == "outage":
                return JSONResponse({"error": "server_error"}, 503)
            self.issued_access += 1
            return JSONResponse(
                {"access_token": f"access-{self.issued_access}", "expires_in": 3600}
            )
        return JSONResponse({"error": "unsupported_grant_type"}, 400)


def code_from_redirect(location: str) -> tuple[str, str]:
    q = parse_qs(urlparse(location).query)
    return q["code"][0], q["state"][0]
