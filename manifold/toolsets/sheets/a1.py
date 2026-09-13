"""A1 notation helpers. Pure functions, no Google calls."""

from __future__ import annotations

import re
from dataclasses import dataclass

_CELL = re.compile(r"^\$?([A-Za-z]{1,3})?\$?(\d+)?$")


class RangeError(ValueError):
    pass


def col_to_index(letters: str) -> int:
    """'A' -> 0, 'Z' -> 25, 'AA' -> 26."""
    n = 0
    for ch in letters.upper():
        if not "A" <= ch <= "Z":
            raise RangeError(f"bad column letters {letters!r}")
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def index_to_col(index: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA'."""
    if index < 0:
        raise RangeError("column index below zero")
    out = ""
    n = index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def quote_sheet(name: str) -> str:
    """Quote a sheet title for A1 notation. Always quoted, which Google accepts."""
    return "'" + name.replace("'", "''") + "'"


@dataclass(frozen=True)
class A1:
    sheet: str | None
    cells: str | None  # 'A1:B5', 'A:C', '2:9', 'B3' or None for the whole sheet
    start_row: int | None  # 1-based; None when open
    start_col: int | None  # 0-based; None when open
    end_row: int | None
    end_col: int | None

    def with_sheet(self, sheet: str) -> A1:
        return A1(sheet, self.cells, self.start_row, self.start_col, self.end_row, self.end_col)

    def starts_at_row_one(self) -> bool:
        return self.start_row in (None, 1)

    def render(self) -> str:
        if self.sheet is None:
            return self.cells or ""
        return quote_sheet(self.sheet) + (f"!{self.cells}" if self.cells else "")

    def bounded_rows(self, first: int, last: int) -> A1:
        """The same columns, restricted to rows first..last (1-based, inclusive)."""
        c0 = index_to_col(self.start_col) if self.start_col is not None else ""
        c1 = index_to_col(self.end_col) if self.end_col is not None else ""
        if c0 == "" and c1 == "":
            cells = f"{first}:{last}"
        else:
            cells = f"{c0 or 'A'}{first}:{c1 or 'ZZZ'}{last}"
        return parse_range(self.render_sheet_prefix() + cells)

    def render_sheet_prefix(self) -> str:
        return quote_sheet(self.sheet) + "!" if self.sheet is not None else ""


def parse_range(text: str) -> A1:
    """Parse "'Sheet 1'!A2:C", "Sheet1", "A1:B2" or "3:10"."""
    text = text.strip()
    if not text:
        raise RangeError("range is empty")
    sheet: str | None = None
    cells: str | None = text
    if "!" in text:
        raw_sheet, cells = text.rsplit("!", 1)
        sheet = raw_sheet.strip()
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    elif not _looks_like_cells(text):
        sheet, cells = text.strip("'").replace("''", "'"), None
    if cells is None:
        return A1(sheet, None, None, None, None, None)
    parts = cells.split(":")
    if len(parts) > 2:
        raise RangeError(f"cannot parse range {text!r}")
    start = _CELL.match(parts[0])
    end = _CELL.match(parts[1]) if len(parts) == 2 else start
    if not start or not end or (not start.group(1) and not start.group(2)):
        raise RangeError(f"cannot parse range {text!r}")
    start_col = col_to_index(start.group(1)) if start.group(1) else None
    start_row = int(start.group(2)) if start.group(2) else None
    end_col = col_to_index(end.group(1)) if end.group(1) else None
    end_row = int(end.group(2)) if end.group(2) else None
    if len(parts) == 1:
        end_col, end_row = start_col, start_row
    if start_row == 0 or end_row == 0:
        raise RangeError("rows start at 1")
    return A1(sheet, cells, start_row, start_col, end_row, end_col)


def _looks_like_cells(text: str) -> bool:
    parts = text.split(":")
    return len(parts) <= 2 and all(_CELL.match(p) and (p != "") for p in parts)


def grid_range(sheet_id: int, a1: A1) -> dict:
    """Google GridRange for a parsed A1. Open ends are omitted, which Google reads as
    'to the edge of the sheet'."""
    out: dict = {"sheetId": sheet_id}
    if a1.start_row is not None:
        out["startRowIndex"] = a1.start_row - 1
    if a1.end_row is not None:
        out["endRowIndex"] = a1.end_row
    if a1.start_col is not None:
        out["startColumnIndex"] = a1.start_col
    if a1.end_col is not None:
        out["endColumnIndex"] = a1.end_col + 1
    return out
