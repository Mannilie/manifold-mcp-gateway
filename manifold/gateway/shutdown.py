"""Shutdown that always finishes.

The restore and restart paths exit the process and rely on Docker starting it again. That
only works if the process actually exits. Graceful shutdown can stall on an open
connection or an upstream session that will not close, so three things bound it:

- uvicorn's graceful timeout (`SHUTDOWN_TIMEOUT_SECONDS`) stops waiting for connections;
- every teardown step in the lifespan is wrapped in its own timeout;
- a watchdog thread calls `os._exit` if the process is still alive `WATCHDOG_SECONDS`
  after shutdown began. A thread, not a task, so a stuck event loop cannot stop it.

`/healthz` reports 503 once shutdown has begun so Docker's status is honest.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT_SECONDS = 5
WATCHDOG_SECONDS = 15
EXIT_DELAY_SECONDS = 0.5
WATCHDOG_EXIT_CODE = 3


class ShutdownController:
    def __init__(self, exit_fn: Callable[[int], None] = os._exit) -> None:
        self._exit = exit_fn
        self._begun = threading.Event()
        self._watchdog: threading.Timer | None = None

    @property
    def shutting_down(self) -> bool:
        return self._begun.is_set()

    def begin(self, watchdog_seconds: float | None = WATCHDOG_SECONDS) -> None:
        """Mark shutdown as started and arm the watchdog. Idempotent."""
        if self._begun.is_set():
            return
        self._begun.set()
        if watchdog_seconds is not None:
            self._watchdog = threading.Timer(watchdog_seconds, self._force_exit)
            self._watchdog.daemon = True
            self._watchdog.start()
        log.warning("shutdown started", extra={"watchdog_seconds": watchdog_seconds})

    def _force_exit(self) -> None:
        log.error("shutdown did not finish in time, forcing exit")
        self._exit(WATCHDOG_EXIT_CODE)

    def exit_process(self, delay: float = EXIT_DELAY_SECONDS) -> None:
        """Ask the running server to stop, shortly after the caller's response is sent."""
        self.begin()
        loop = asyncio.get_running_loop()
        loop.call_later(delay, os.kill, os.getpid(), signal.SIGTERM)

    def cancel_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None


# The controller of the most recently created app. `python -m manifold` hooks uvicorn's
# signal handling to it, so a SIGTERM from Docker begins shutdown the same way the
# restart button does.
current: ShutdownController | None = None


async def bounded(step: str, coro, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> bool:
    """Await a teardown step, giving up after `timeout`. Returns False on timeout."""
    try:
        await asyncio.wait_for(coro, timeout)
        return True
    except TimeoutError:
        log.warning("shutdown step timed out, continuing", extra={"step": step})
        return False
