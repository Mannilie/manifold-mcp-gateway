"""Turning Google's value grids into what Claude reads (DECISIONS.md, Phase 4 gate 3)."""

from __future__ import annotations

from typing import Any


def suffix_duplicates(headers: list[str]) -> tuple[list[str], list[str]]:
    """'Qty, Qty, Qty' -> 'Qty, Qty_2, Qty_3'. Returns (headers, notes)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    notes: list[str] = []
    for i, raw in enumerate(headers):
        name = str(raw).strip() or f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            new = f"{name}_{seen[name]}"
            notes.append(f"duplicate header {name!r} renamed to {new!r}")
            out.append(new)
        else:
            seen[name] = 1
            out.append(name)
    return out, notes


def pad(row: list[Any], width: int) -> list[Any]:
    """Google drops trailing empty cells; every row comes back the same width."""
    return [("" if v is None else v) for v in row] + [""] * (width - len(row))


def shape(values: list[list[Any]], start_row: int, has_header: bool) -> dict[str, Any]:
    """Objects keyed by header when `has_header`, otherwise a 2D array. Always row numbers."""
    width = max((len(r) for r in values), default=0)
    if has_header and values:
        headers, notes = suffix_duplicates([str(v) for v in pad(values[0], width)])
        rows = [dict(zip(headers, pad(r, width), strict=True)) for r in values[1:]]
        out: dict[str, Any] = {
            "headers": headers,
            "rows": rows,
            "row_numbers": list(range(start_row + 1, start_row + 1 + len(rows))),
        }
        if notes:
            out["notes"] = notes
        return out
    return {
        "values": [pad(r, width) for r in values],
        "row_numbers": list(range(start_row, start_row + len(values))),
    }


def truncate(values: list[list[Any]], cap: int) -> tuple[list[list[Any]], bool]:
    if len(values) <= cap:
        return values, False
    return values[:cap], True
