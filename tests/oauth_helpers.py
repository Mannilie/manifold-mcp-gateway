"""Drive Manifold's OAuth flow the way claude.ai does, for tests."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx2

CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ADMIN_EMAIL = "manny@example.com"
ACCESS_HEADER = {"Cf-Access-Authenticated-User-Email": ADMIN_EMAIL}


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


async def register(http: httpx2.AsyncClient, auth_method: str = "none") -> dict:
    r = await http.post(
        "/oauth/register",
        json={
            "client_name": "test client",
            "redirect_uris": [CLAUDE_REDIRECT],
            "token_endpoint_auth_method": auth_method,
            "grant_types": ["authorization_code", "refresh_token"],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def resource_url(http: httpx2.AsyncClient, key: str) -> str:
    return f"{str(http.base_url).rstrip('/')}/{key}"


async def authorize(
    http: httpx2.AsyncClient,
    client_id: str,
    challenge: str,
    resource: str | None = None,
    headers: dict | None = None,
    state: str = "xyz",
    method: str = "S256",
) -> httpx2.Response:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CLAUDE_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": method,
        "state": state,
        "resource": resource_url(http, "manifold") if resource is None else resource,
    }
    if resource == "":
        del params["resource"]
    return await http.get(
        "/oauth/authorize", params=params, headers=ACCESS_HEADER if headers is None else headers
    )


def code_from(response: httpx2.Response) -> tuple[str, str | None]:
    assert response.status_code == 302, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert "error" not in query, query
    return query["code"][0], query.get("state", [None])[0]


async def exchange(
    http: httpx2.AsyncClient, client: dict, code: str, verifier: str, resource: str | None = None
) -> httpx2.Response:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client["client_id"],
        "redirect_uri": CLAUDE_REDIRECT,
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    if resource:
        data["resource"] = resource
    return await http.post("/oauth/token", data=data)


async def obtain_tokens(http: httpx2.AsyncClient, key: str = "manifold") -> tuple[dict, dict]:
    """Full happy path for one toolset. Returns (client, token response body)."""
    client = await register(http)
    verifier, challenge = pkce()
    resource = resource_url(http, key)
    code, _ = code_from(await authorize(http, client["client_id"], challenge, resource))
    r = await exchange(http, client, code, verifier, resource)
    assert r.status_code == 200, r.text
    return client, r.json()
