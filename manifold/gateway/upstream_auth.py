"""Headers Manifold sends to an upstream MCP server, built from a stored credential."""

from __future__ import annotations

import base64

from manifold.gateway.manifest import Credentials


def auth_headers(credentials: Credentials) -> dict[str, str]:
    v = credentials.values
    match credentials.kind:
        case "none":
            return {}
        case "api_key":
            return {v.get("header", "X-API-Key"): v["key"]}
        case "bearer":
            return {"Authorization": f"Bearer {v['token']}"}
        case "basic":
            raw = f"{v['username']}:{v['password']}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        case _:
            raise ValueError(
                f"credential kind {credentials.kind!r} cannot authenticate an upstream"
            )
