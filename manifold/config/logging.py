"""Structured JSON logging to stdout (SPEC.md section 12)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

_STANDARD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
    "color_message",  # uvicorn's ANSI duplicate of msg
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def add_access_log_file(
    directory: Path, max_bytes: int = 5 * 1024 * 1024, backups: int = 5
) -> None:
    """Keep the HTTP access log on the data volume so it survives container replacement.
    Rotated by size; JSON lines, same shape as stdout."""
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "access.log", maxBytes=max_bytes, backupCount=backups)
    handler.setFormatter(JsonFormatter())
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)  # the file gets every request whatever the stdout level
    for old in [h for h in access.handlers if isinstance(h, RotatingFileHandler)]:
        access.removeHandler(old)
        old.close()
    access.addHandler(handler)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(level.upper())  # loggers with their own level still respect stdout's
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn installs its own handlers unless told not to; route everything through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        logger.propagate = True
