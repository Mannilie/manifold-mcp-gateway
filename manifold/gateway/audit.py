"""Server middleware that records every tools/call in the audit log."""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError

from manifold.store.audit import AuditRepo, hash_args

log = logging.getLogger(__name__)


class AuditMiddleware:
    def __init__(self, toolset_key: str, audit: AuditRepo) -> None:
        self._key = toolset_key
        self._audit = audit

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method != "tools/call" or ctx.request_id is None:
            return await call_next(ctx)
        params = ctx.params if isinstance(ctx.params, dict) else {}
        tool_name = str(params.get("name", "?"))
        args_hash = hash_args(params.get("arguments"))
        started = time.perf_counter()
        ok, error = True, None
        try:
            result = await call_next(ctx)
        except MCPError as exc:
            ok, error = False, f"{type(exc).__name__}: {exc.error.message}"
            raise
        except Exception as exc:
            # Never persist the message: it may echo argument values.
            ok, error = False, type(exc).__name__
            raise
        else:
            if _is_error_result(result):
                ok, error = False, "tool returned isError"
            return result
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            try:
                await self._audit.record(self._key, tool_name, args_hash, duration_ms, ok, error)
            except Exception as exc:
                log.warning("audit write failed", extra={"error": type(exc).__name__})


def _is_error_result(result: HandlerResult) -> bool:
    if result is None:
        return False
    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error"))
    return bool(getattr(result, "is_error", False))
