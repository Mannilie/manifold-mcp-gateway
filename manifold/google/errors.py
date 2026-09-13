"""Readable errors for Claude, built from Google's error bodies."""

from __future__ import annotations

from typing import Any


class GoogleError(Exception):
    """A Google API call failed in a way the caller cannot fix by retrying."""

    def __init__(self, message: str, status: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason


def _google_message(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("status") or "unknown error")
        if isinstance(err, str):
            return err
    return "unknown error"


def map_error(
    status: int,
    body: Any,
    *,
    context: dict[str, str] | None = None,
    identity: str | None = None,
) -> GoogleError:
    """Turn an HTTP status and Google error body into something Claude can act on.

    `context` carries what the tool knows: spreadsheet_id, range, sheet.
    `identity` is the service account email when one is in use.
    """
    context = context or {}
    target = context.get("spreadsheet_id", "the resource")
    where = f" range {context['range']}" if context.get("range") else ""
    google_says = _google_message(body)
    if status == 400:
        return GoogleError(
            f"Google rejected the request for {target}{where}: {google_says}", status, "bad_request"
        )
    if status == 401:
        return GoogleError(
            "Google rejected the credential (401). Reconnect or replace the credential.",
            status,
            "unauthorised",
        )
    if status == 403:
        if identity:
            return GoogleError(
                f"the credential does not have access to {target}. "
                f"Share it with {identity} (Editor for writes, Viewer for reads).",
                status,
                "forbidden",
            )
        return GoogleError(
            f"the credential does not have access to {target}: {google_says}", status, "forbidden"
        )
    if status == 404:
        return GoogleError(f"spreadsheet or range not found: {target}{where}", status, "not_found")
    if status == 429:
        return GoogleError(
            f"Google rate-limited this request for {target}; retries were exhausted. "
            "Wait a minute and try again.",
            status,
            "rate_limited",
        )
    if status >= 500:
        return GoogleError(
            f"Google returned {status} for {target}{where} after retries: {google_says}",
            status,
            "upstream_error",
        )
    return GoogleError(f"Google returned {status} for {target}{where}: {google_says}", status)
