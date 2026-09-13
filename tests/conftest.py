from __future__ import annotations

import asyncio
import base64
import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from manifold.config.settings import Settings

TEST_MASTER_KEY = base64.b64encode(b"\x01" * 32).decode()


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    return {
        "MANIFOLD_MASTER_KEY": TEST_MASTER_KEY,
        "MANIFOLD_ADMIN_EMAILS": "manny@example.com",
        "MANIFOLD_BASE_URL": "http://testserver",
        "MANIFOLD_LOG_LEVEL": "warning",
        "MANIFOLD_DATA_DIR": str(tmp_path),
    }


@pytest.fixture
def settings(env) -> Settings:
    return Settings.from_env(env)


async def _seed(data_dir) -> None:
    """New native toolsets start disabled; the integration suite wants ping-b on."""
    from manifold.gateway.registry import discover_native_toolsets
    from manifold.store.db import Database
    from manifold.store.toolsets import ToolsetsRepo

    db = Database(data_dir)
    await db.open()
    try:
        await db.migrate()
        repo = ToolsetsRepo(db)
        await repo.sync_native(m.MANIFEST for m in discover_native_toolsets().values())
        await repo.set_enabled("ping-b", True)
    finally:
        await db.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def live_server(tmp_path_factory) -> Iterator[str]:
    """The real app under real uvicorn on a random port, so the SDK client and raw HTTP
    both exercise the same stack claude.ai will hit."""
    port = _free_port()
    os.environ["MANIFOLD_MASTER_KEY"] = TEST_MASTER_KEY
    os.environ["MANIFOLD_ADMIN_EMAILS"] = "manny@example.com"
    os.environ["MANIFOLD_BASE_URL"] = f"http://127.0.0.1:{port}"
    data_dir = tmp_path_factory.mktemp("data")
    os.environ["MANIFOLD_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("MANIFOLD_LOG_LEVEL", "warning")
    asyncio.run(_seed(data_dir))
    from manifold.app import create_app

    config = uvicorn.Config(
        create_app(reload_poll_seconds=0.2), host="127.0.0.1", port=port, log_config=None
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
