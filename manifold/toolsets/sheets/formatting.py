"""Mapping the flat format and rule dicts Claude sends to Sheets API requests."""

from __future__ import annotations

from typing import Any

from manifold.toolsets.sheets.a1 import A1, col_to_index, grid_range

FORMAT_KEYS = {
    "bold": "bool",
    "italic": "bool",
    "font_size": "int",
    "text_colour": "hex",
    "background": "hex",
    "number_format": "str",
    "horizontal_align": "left|center|right",
    "wrap": "bool",
}

CONDITIONS = {
    "greater_than": "NUMBER_GREATER",
    "less_than": "NUMBER_LESS",
    "between": "NUMBER_BETWEEN",
    "equal": "NUMBER_EQ",
    "text_contains": "TEXT_CONTAINS",
    "text_starts_with": "TEXT_STARTS_WITH",
    "is_blank": "BLANK",
    "not_blank": "NOT_BLANK",
    "custom_formula": "CUSTOM_FORMULA",
}


class FormatError(ValueError):
    pass


def hex_colour(value: str) -> dict[str, float]:
    v = str(value).lstrip("#")
    if len(v) == 3:
        v = "".join(ch * 2 for ch in v)
    if len(v) != 6:
        raise FormatError(f"colour must be a hex string like #ffcc00, got {value!r}")
    try:
        r, g, b = (int(v[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError as exc:
        raise FormatError(f"colour must be a hex string like #ffcc00, got {value!r}") from exc
    return {"red": r, "green": g, "blue": b}


def cell_format(fmt: dict[str, Any]) -> tuple[dict, list[str]]:
    """Returns (CellFormat, fieldMask parts)."""
    unknown = set(fmt) - set(FORMAT_KEYS)
    if unknown:
        raise FormatError(
            f"unknown format keys {sorted(unknown)}; accepted: {', '.join(FORMAT_KEYS)}"
        )
    cell: dict[str, Any] = {}
    fields: list[str] = []
    text: dict[str, Any] = {}
    if "bold" in fmt:
        text["bold"] = bool(fmt["bold"])
        fields.append("userEnteredFormat.textFormat.bold")
    if "italic" in fmt:
        text["italic"] = bool(fmt["italic"])
        fields.append("userEnteredFormat.textFormat.italic")
    if "font_size" in fmt:
        text["fontSize"] = int(fmt["font_size"])
        fields.append("userEnteredFormat.textFormat.fontSize")
    if "text_colour" in fmt:
        text["foregroundColor"] = hex_colour(fmt["text_colour"])
        fields.append("userEnteredFormat.textFormat.foregroundColor")
    if text:
        cell["textFormat"] = text
    if "background" in fmt:
        cell["backgroundColor"] = hex_colour(fmt["background"])
        fields.append("userEnteredFormat.backgroundColor")
    if "number_format" in fmt:
        pattern = str(fmt["number_format"])
        kind = (
            "DATE"
            if any(ch in pattern.lower() for ch in "dmy") and "$" not in pattern
            else "NUMBER"
        )
        cell["numberFormat"] = {"type": kind, "pattern": pattern}
        fields.append("userEnteredFormat.numberFormat")
    if "horizontal_align" in fmt:
        align = str(fmt["horizontal_align"]).upper()
        if align not in ("LEFT", "CENTER", "RIGHT"):
            raise FormatError("horizontal_align must be left, center or right")
        cell["horizontalAlignment"] = align
        fields.append("userEnteredFormat.horizontalAlignment")
    if "wrap" in fmt:
        cell["wrapStrategy"] = "WRAP" if fmt["wrap"] else "OVERFLOW_CELL"
        fields.append("userEnteredFormat.wrapStrategy")
    if not fields:
        raise FormatError(f"format is empty; accepted keys: {', '.join(FORMAT_KEYS)}")
    return cell, fields


def repeat_cell(sheet_id: int, a1: A1, fmt: dict[str, Any]) -> dict:
    cell, fields = cell_format(fmt)
    return {
        "repeatCell": {
            "range": grid_range(sheet_id, a1),
            "cell": {"userEnteredFormat": cell},
            "fields": ",".join(fields),
        }
    }


def conditional_rule(sheet_id: int, a1: A1, rule: dict[str, Any]) -> dict:
    condition = rule.get("condition")
    if condition not in CONDITIONS:
        raise FormatError(f"condition must be one of {', '.join(CONDITIONS)}")
    values: list[dict] = []
    if condition in ("greater_than", "less_than", "equal", "text_contains", "text_starts_with"):
        if "value" not in rule:
            raise FormatError(f"condition {condition} needs a value")
        values = [{"userEnteredValue": str(rule["value"])}]
    elif condition == "between":
        if "value" not in rule or "value2" not in rule:
            raise FormatError("condition between needs value and value2")
        values = [
            {"userEnteredValue": str(rule["value"])},
            {"userEnteredValue": str(rule["value2"])},
        ]
    elif condition == "custom_formula":
        formula = str(rule.get("value", ""))
        if not formula.startswith("="):
            raise FormatError("custom_formula value must start with =")
        values = [{"userEnteredValue": formula}]
    fmt = rule.get("format")
    if not isinstance(fmt, dict) or not fmt:
        raise FormatError("rule needs a format dict, for example {'background': '#ffcccc'}")
    cell, _ = cell_format(
        {k: v for k, v in fmt.items() if k in ("bold", "italic", "text_colour", "background")}
    )
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [grid_range(sheet_id, a1)],
                "booleanRule": {
                    "condition": {"type": CONDITIONS[condition], "values": values},
                    "format": cell,
                },
            },
            "index": 0,
        }
    }


def column_width_requests(sheet_id: int, widths: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    for letters, width in widths.items():
        idx = col_to_index(str(letters))
        rng = {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": idx, "endIndex": idx + 1}
        if width == "auto":
            out.append({"autoResizeDimensions": {"dimensions": rng}})
        else:
            try:
                pixels = int(width)
            except (TypeError, ValueError) as exc:
                raise FormatError(f"width for column {letters} must be pixels or 'auto'") from exc
            out.append(
                {
                    "updateDimensionProperties": {
                        "range": rng,
                        "properties": {"pixelSize": pixels},
                        "fields": "pixelSize",
                    }
                }
            )
    if not out:
        raise FormatError("widths is empty; give a map like {'A': 120, 'B': 'auto'}")
    return out
