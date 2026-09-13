"""`python -m manifold` runs the gateway with uvicorn on port 8800.
`python -m manifold rotate-key --new-key <base64>` re-encrypts credentials under a new key."""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys

import uvicorn

from manifold.config.settings import Settings, SettingsError
from manifold.crypto.keycheck import MasterKeyError
from manifold.gateway import shutdown


def rotate_key(new_key_b64: str) -> int:
    from manifold.crypto.keys import INFO_CREDENTIALS, derive_key
    from manifold.crypto.rotate import rotate
    from manifold.store.db import Database

    settings = Settings.from_env()
    try:
        new_master = base64.b64decode(new_key_b64, validate=True)
    except ValueError:
        print("rotate-key: --new-key must be base64", file=sys.stderr)
        return 2
    if len(new_master) != 32:
        print("rotate-key: --new-key must decode to 32 bytes", file=sys.stderr)
        return 2

    async def run() -> int:
        db = Database(settings.data_dir)
        await db.open()
        try:
            await db.migrate()
            count = await rotate(
                db,
                derive_key(settings.master_key, INFO_CREDENTIALS),
                derive_key(new_master, INFO_CREDENTIALS),
            )
        finally:
            await db.close()
        print(
            f"rotated {count} credential(s). Now set MANIFOLD_MASTER_KEY to the new value "
            "and restart the container."
        )
        return 0

    return asyncio.run(run())


class Server(uvicorn.Server):
    """uvicorn's server with shutdown hooked to the app's controller, so a SIGTERM from
    Docker arms the watchdog and flips /healthz exactly as the restart button does."""

    def handle_exit(self, sig: int, frame: object) -> None:
        if shutdown.current is not None:
            shutdown.current.begin()
        super().handle_exit(sig, frame)


def serve() -> None:
    config = uvicorn.Config(
        "manifold.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8800,
        log_config=None,  # logging is configured by create_app
        proxy_headers=True,
        forwarded_allow_ips="*",  # cloudflared is the only thing that can reach us
        timeout_graceful_shutdown=shutdown.SHUTDOWN_TIMEOUT_SECONDS,
    )
    Server(config).run()


def main() -> int:
    parser = argparse.ArgumentParser(prog="manifold")
    sub = parser.add_subparsers(dest="command")
    rot = sub.add_parser("rotate-key", help="re-encrypt credentials under a new master key")
    rot.add_argument("--new-key", required=True)
    args = parser.parse_args()
    if args.command == "rotate-key":
        try:
            return rotate_key(args.new_key)
        except (SettingsError, MasterKeyError, ValueError) as exc:
            print(f"manifold: {exc}", file=sys.stderr)
            return 2
    try:
        serve()
    except (SettingsError, MasterKeyError) as exc:
        print(f"manifold: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
