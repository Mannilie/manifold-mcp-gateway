"""`python -m manifold` runs the gateway with uvicorn on port 8800."""

from __future__ import annotations

import sys

import uvicorn

from manifold.config.settings import SettingsError


def main() -> int:
    try:
        uvicorn.run(
            "manifold.app:create_app",
            factory=True,
            host="0.0.0.0",
            port=8800,
            log_config=None,  # logging is configured by create_app
            proxy_headers=True,
            forwarded_allow_ips="*",  # cloudflared is the only thing that can reach us
        )
    except SettingsError as exc:
        print(f"manifold: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
