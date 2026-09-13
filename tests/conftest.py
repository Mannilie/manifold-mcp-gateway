from __future__ import annotations

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
    os.environ["MANIFOLD_DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    os.environ.setdefault("MANIFOLD_LOG_LEVEL", "warning")
    from manifold.app import create_app

    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_config=None)
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
