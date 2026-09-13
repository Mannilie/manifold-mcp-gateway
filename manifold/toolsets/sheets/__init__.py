"""Google Sheets toolset. Reads and writes spreadsheets by ID through the shared Google
client. Service account or Google OAuth credential (SPEC.md 10.1, DECISIONS.md Phase 4)."""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from manifold.gateway.manifest import Credentials, HealthResult, ToolsetConfig, ToolsetManifest
from manifold.google import GoogleError, client_for
from manifold.toolsets.sheets.a1 import A1, RangeError, index_to_col, parse_range, quote_sheet
from manifold.toolsets.sheets.formatting import (
    FormatError,
    column_width_requests,
    conditional_rule,
    repeat_cell,
)
from manifold.toolsets.sheets.shape import pad, shape, suffix_duplicates, truncate

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
RENDER = {"formatted": "FORMATTED_VALUE", "raw": "UNFORMATTED_VALUE", "formula": "FORMULA"}

MANIFEST = ToolsetManifest(
    key="sheets",
    display_name="Google Sheets",
    description="Read, write, find and format Google Sheets by spreadsheet ID.",
    kind="native",
    supported_auth=["service_account", "oauth2"],
    settings_schema={
        "type": "object",
        "properties": {
            "allowed_spreadsheet_ids": {
                "type": "array",
                "title": "Allowed spreadsheet IDs",
                "description": "Empty means any spreadsheet the credential can reach. "
                "When set, calls for other spreadsheets are refused before touching Google.",
                "items": {"type": "string", "minLength": 20},
                "default": [],
                "x-manifold": {
                    "placeholder": "1AbC…  (the long ID in the sheet URL)",
                    "help_url": "https://developers.google.com/sheets/api/guides/concepts#spreadsheet_id",
                },
            },
            "read_row_cap": {
                "type": "integer",
                "title": "Read row cap",
                "description": "Most rows one read_range or find_rows call returns.",
                "minimum": 1,
                "maximum": 50000,
                "default": 1000,
            },
        },
        "additionalProperties": False,
    },
    example_settings={"allowed_spreadsheet_ids": [], "read_row_cap": 1000},
    version="0.1.0",
)


class _Sheets:
    """One spreadsheet-agnostic helper per built server. Holds the client and settings."""

    def __init__(self, config: ToolsetConfig, credentials: Credentials) -> None:
        self.allowed = set(config.settings.get("allowed_spreadsheet_ids") or [])
        self.cap = int(config.settings.get("read_row_cap") or 1000)
        self.credentials = credentials
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = client_for(self.credentials, SCOPES)
        return self._client

    def check(self, spreadsheet_id: str) -> None:
        if not spreadsheet_id or not isinstance(spreadsheet_id, str):
            raise ToolError("spreadsheet_id is required: the long ID from the sheet's URL")
        if self.allowed and spreadsheet_id not in self.allowed:
            raise ToolError(
                f"spreadsheet {spreadsheet_id} is not on this toolset's allow list. "
                "Add it in the Manifold settings for the sheets toolset, or use an allowed one."
            )

    async def meta(self, spreadsheet_id: str) -> dict:
        return await self.client.get(
            f"spreadsheets/{spreadsheet_id}",
            params={
                "fields": "spreadsheetId,properties(title,locale,timeZone),"
                "sheets(properties(sheetId,title,index,gridProperties))"
            },
            context={"spreadsheet_id": spreadsheet_id},
        )

    async def sheet_id(self, spreadsheet_id: str, title: str) -> int:
        meta = await self.meta(spreadsheet_id)
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == title:
                return int(s["properties"]["sheetId"])
        names = ", ".join(s["properties"]["title"] for s in meta.get("sheets", []))
        raise ToolError(f"no sheet named {title!r} in {spreadsheet_id}. Sheets: {names}")

    async def resolve(self, spreadsheet_id: str, range_text: str) -> tuple[A1, int]:
        """Parse a range and resolve its sheet id, defaulting to the first sheet."""
        try:
            a1 = parse_range(range_text)
        except RangeError as exc:
            raise ToolError(f"bad range {range_text!r}: {exc}") from None
        meta = await self.meta(spreadsheet_id)
        sheets = meta.get("sheets", [])
        if a1.sheet is None:
            if not sheets:
                raise ToolError(f"{spreadsheet_id} has no sheets")
            a1 = a1.with_sheet(sheets[0]["properties"]["title"])
        for s in sheets:
            if s["properties"]["title"] == a1.sheet:
                return a1, int(s["properties"]["sheetId"])
        names = ", ".join(s["properties"]["title"] for s in sheets)
        raise ToolError(f"no sheet named {a1.sheet!r} in {spreadsheet_id}. Sheets: {names}")

    async def batch(self, spreadsheet_id: str, requests: list[dict], context: dict) -> dict:
        return await self.client.post(
            f"spreadsheets/{spreadsheet_id}:batchUpdate",
            json={"requests": requests},
            context={"spreadsheet_id": spreadsheet_id, **context},
        )

    async def read(
        self, spreadsheet_id: str, a1: A1, render: str, cap: int | None = None
    ) -> tuple[list[list[Any]], A1, bool]:
        """Values for a range, bounded to the cap. Returns (values, fetched range, truncated)."""
        cap = cap or self.cap
        fetch = a1
        if a1.start_row is None or a1.end_row is None:
            first = a1.start_row or 1
            fetch = a1.bounded_rows(first, first + cap)  # one extra row detects truncation
        params = {"valueRenderOption": RENDER[render], "majorDimension": "ROWS"}
        if render == "raw":
            params["dateTimeRenderOption"] = "FORMATTED_STRING"
        body = await self.client.get(
            f"spreadsheets/{spreadsheet_id}/values/{fetch.render()}",
            params=params,
            context={"spreadsheet_id": spreadsheet_id, "range": a1.render()},
        )
        values, truncated = truncate(body.get("values", []), cap)
        return values, fetch, truncated


def _render(value: str) -> str:
    if value not in RENDER:
        raise ToolError("render must be formatted, raw or formula")
    return value


def _rows_of(values: Any, what: str) -> list[list[Any]]:
    if not isinstance(values, list) or not all(isinstance(r, list) for r in values):
        raise ToolError(f"{what} must be a list of rows, each row a list of cell values")
    return values


def _next_range(a1: A1, rows_returned: int) -> str:
    first = (a1.start_row or 1) + rows_returned
    return a1.bounded_rows(first, first + 999).render()


def build(config: ToolsetConfig, credentials: Credentials) -> MCPServer:
    sheets = _Sheets(config, credentials)
    server = MCPServer(
        name="sheets",
        instructions=(
            "Google Sheets by spreadsheet ID. Start with get_spreadsheet to learn the sheet "
            "names, header rows and locale. Ranges use A1 notation with the sheet name, for "
            "example 'Orders'!A2:D. Writes overwrite without confirmation; confirm with the "
            "user before update_range, update_rows_by_key, clear_range or delete_sheet."
        ),
    )

    @server.tool()
    async def get_spreadsheet(spreadsheet_id: str) -> dict:
        """Describe a spreadsheet: title, locale, and every sheet with its header row.

        Returns title, locale (which decides whether dates are d/m/y or m/d/y when you
        write them), time_zone, and sheets, each with title, sheet_id, rows, columns,
        frozen_rows and headers (the first row only). Call this first so you can name
        sheets and columns without a read_range. Not for reading data: use read_range.
        """
        sheets.check(spreadsheet_id)
        try:
            meta = await sheets.meta(spreadsheet_id)
            titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
            headers: dict[str, list[str]] = {}
            if titles:
                body = await sheets.client.get(
                    f"spreadsheets/{spreadsheet_id}/values:batchGet",
                    params={"ranges": [f"{quote_sheet(t)}!1:1" for t in titles]},
                    context={"spreadsheet_id": spreadsheet_id, "range": "header rows"},
                )
                for title, vr in zip(titles, body.get("valueRanges", []), strict=False):
                    row = (vr.get("values") or [[]])[0]
                    headers[title] = [str(v) for v in pad(row, len(row))]
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        props = meta.get("properties", {})
        return {
            "spreadsheet_id": spreadsheet_id,
            "title": props.get("title"),
            "locale": props.get("locale"),
            "time_zone": props.get("timeZone"),
            "sheets": [
                {
                    "title": s["properties"]["title"],
                    "sheet_id": s["properties"]["sheetId"],
                    "rows": s["properties"].get("gridProperties", {}).get("rowCount"),
                    "columns": s["properties"].get("gridProperties", {}).get("columnCount"),
                    "frozen_rows": s["properties"]
                    .get("gridProperties", {})
                    .get("frozenRowCount", 0),
                    "headers": headers.get(s["properties"]["title"], []),
                }
                for s in meta.get("sheets", [])
            ],
        }

    @server.tool()
    async def read_range(
        spreadsheet_id: str,
        range: str,
        render: str = "formatted",
        has_header: bool | None = None,
    ) -> dict:
        """Read cells from a range such as 'Orders'!A1:D50, 'Orders'!A:D or just 'Orders'.

        Returns rows as objects keyed by the header row when has_header is true, plus
        row_numbers; otherwise values as a 2D array plus row_numbers. has_header defaults
        to true when the range starts at row 1 or is a bare sheet name, false otherwise;
        pass it explicitly to override. Duplicate headers are suffixed (Qty, Qty_2) and
        noted. Empty cells are empty strings.

        render: formatted (default, as displayed, dates as the sheet shows them), raw
        (unformatted numbers, dates as ISO strings) or formula (the cell's formula text).

        Reads are capped at the toolset's row cap (default 1000). When truncated is true,
        next_range is the range to request next. Not for finding a row by value: use
        find_rows.
        """
        sheets.check(spreadsheet_id)
        render = _render(render)
        try:
            a1, _ = await sheets.resolve(spreadsheet_id, range)
            values, fetched, truncated = await sheets.read(spreadsheet_id, a1, render)
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        header = a1.starts_at_row_one() if has_header is None else bool(has_header)
        out = {"range": a1.render(), "render": render, "has_header": header}
        out.update(shape(values, a1.start_row or 1, header))
        out["truncated"] = truncated
        if truncated:
            out["next_range"] = _next_range(fetched, len(values))
            out["notes"] = [
                *out.get("notes", []),
                f"only the first {len(values)} rows are shown; request next_range for more",
            ]
        return out

    @server.tool()
    async def append_rows(
        spreadsheet_id: str, sheet: str, rows: list[list[Any]], raw: bool = False
    ) -> dict:
        """Append rows after the last row with data on a sheet.

        rows is a list of rows, each a list of cell values in column order starting at
        column A. Values are entered as if typed: formulas starting with = evaluate,
        numeric strings become numbers, dates parse in the spreadsheet's locale. Pass
        raw=true to store everything as literal text, for example postcodes like "0412"
        that would otherwise lose the leading zero. Returns the updated range and count.
        Not for changing existing rows: use update_range or update_rows_by_key.
        """
        sheets.check(spreadsheet_id)
        rows = _rows_of(rows, "rows")
        try:
            body = await sheets.client.post(
                f"spreadsheets/{spreadsheet_id}/values/{quote_sheet(sheet)}:append",
                params={
                    "valueInputOption": "RAW" if raw else "USER_ENTERED",
                    "insertDataOption": "INSERT_ROWS",
                },
                json={"values": rows},
                context={"spreadsheet_id": spreadsheet_id, "range": sheet},
            )
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        updates = body.get("updates", {})
        return {"updated_range": updates.get("updatedRange"), "rows_appended": len(rows)}

    @server.tool()
    async def update_range(
        spreadsheet_id: str, range: str, values: list[list[Any]], raw: bool = False
    ) -> dict:
        """Overwrite cells in a range without confirmation. Confirm with the user first.

        values is a 2D list whose top-left cell lands on the range's first cell. Values
        are entered as if typed (formulas evaluate, numbers and dates parse in the
        spreadsheet's locale); pass raw=true to store literal text, for example a postcode
        "0412". Returns the updated range and row count. To add rows at the end use
        append_rows; to change rows by a key column use update_rows_by_key.
        """
        sheets.check(spreadsheet_id)
        values = _rows_of(values, "values")
        try:
            a1, _ = await sheets.resolve(spreadsheet_id, range)
            body = await sheets.client.put(
                f"spreadsheets/{spreadsheet_id}/values/{a1.render()}",
                params={"valueInputOption": "RAW" if raw else "USER_ENTERED"},
                json={"values": values},
                context={"spreadsheet_id": spreadsheet_id, "range": a1.render()},
            )
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"updated_range": body.get("updatedRange"), "rows_updated": body.get("updatedRows")}

    @server.tool()
    async def find_rows(
        spreadsheet_id: str,
        sheet: str,
        column: str,
        value: str,
        match: str = "exact",
        render: str = "formatted",
    ) -> dict:
        """Find rows on a sheet where a column matches a value.

        column is a header name from row 1 or a column letter. match is exact (default,
        case-insensitive), contains, or regex. Returns matches as objects keyed by header
        with a row_number each, so update_range can target 'Sheet'!A<row_number>. Scans
        up to the toolset's row cap and says if the sheet was longer. render works as in
        read_range. Not for reading a whole table: use read_range.
        """
        import re

        sheets.check(spreadsheet_id)
        render = _render(render)
        if match not in ("exact", "contains", "regex"):
            raise ToolError("match must be exact, contains or regex")
        try:
            a1, _ = await sheets.resolve(spreadsheet_id, sheet)
            values, _, truncated = await sheets.read(spreadsheet_id, a1, render)
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        if not values:
            return {"matches": [], "row_numbers": [], "scanned_rows": 0, "truncated": False}
        width = max(len(r) for r in values)
        headers, notes = suffix_duplicates([str(v) for v in pad(values[0], width)])
        if column in headers:
            col = headers.index(column)
        else:
            try:
                col = parse_range(f"{column}1").start_col
            except RangeError:
                col = None
            if col is None or col >= width:
                raise ToolError(
                    f"column {column!r} is not a header or column letter. Headers: {headers}"
                )
        needle = str(value)
        pattern = re.compile(needle, re.IGNORECASE) if match == "regex" else None
        matches, row_numbers = [], []
        for i, row in enumerate(values[1:], start=2):
            cell = str(pad(row, width)[col])
            hit = (
                cell.lower() == needle.lower()
                if match == "exact"
                else needle.lower() in cell.lower()
                if match == "contains"
                else bool(pattern.search(cell))
            )
            if hit:
                matches.append(dict(zip(headers, pad(row, width), strict=True)))
                row_numbers.append(i)
        out = {
            "sheet": a1.sheet,
            "matches": matches,
            "row_numbers": row_numbers,
            "scanned_rows": len(values) - 1,
            "truncated": truncated,
        }
        if notes:
            out["notes"] = notes
        if truncated:
            out["notes"] = [
                *out.get("notes", []),
                f"only the first {len(values) - 1} data rows were scanned",
            ]
        return out

    @server.tool()
    async def update_rows_by_key(
        spreadsheet_id: str,
        sheet: str,
        key_column: str,
        updates: list[dict[str, Any]],
        raw: bool = False,
    ) -> dict:
        """Update rows matched by a key column, overwriting without confirmation.

        key_column is a header name. updates is a list of objects, each containing the
        key column's value plus the columns to change, for example
        [{"SKU": "A100", "Qty": 5, "Status": "shipped"}]. Only the named columns are
        written; other cells in the row are untouched. Values are entered as if typed;
        raw=true stores literal text. Returns which keys were updated and which were not
        found. If a key matches several rows, all of them are updated.
        """
        sheets.check(spreadsheet_id)
        if not isinstance(updates, list) or not all(isinstance(u, dict) for u in updates):
            raise ToolError("updates must be a list of objects keyed by header name")
        try:
            a1, _ = await sheets.resolve(spreadsheet_id, sheet)
            values, _, truncated = await sheets.read(spreadsheet_id, a1, "formatted")
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        if not values:
            raise ToolError(f"sheet {a1.sheet!r} is empty; there is no header row to match on")
        width = max(len(r) for r in values)
        headers, _ = suffix_duplicates([str(v) for v in pad(values[0], width)])
        if key_column not in headers:
            raise ToolError(f"key_column {key_column!r} is not a header. Headers: {headers}")
        key_idx = headers.index(key_column)
        index: dict[str, list[int]] = {}
        for i, row in enumerate(values[1:], start=2):
            index.setdefault(str(pad(row, width)[key_idx]).lower(), []).append(i)
        data, updated, missing = [], [], []
        for upd in updates:
            if key_column not in upd:
                raise ToolError(f"every update needs the key column {key_column!r}")
            unknown = [c for c in upd if c not in headers]
            if unknown:
                raise ToolError(f"unknown columns {unknown}. Headers: {headers}")
            rows = index.get(str(upd[key_column]).lower(), [])
            if not rows:
                missing.append(upd[key_column])
                continue
            for r in rows:
                for col_name, v in upd.items():
                    if col_name == key_column:
                        continue
                    c = index_to_col(headers.index(col_name))
                    data.append({"range": f"{quote_sheet(a1.sheet)}!{c}{r}", "values": [[v]]})
            updated.append(upd[key_column])
        if data:
            try:
                await sheets.client.post(
                    f"spreadsheets/{spreadsheet_id}/values:batchUpdate",
                    json={"valueInputOption": "RAW" if raw else "USER_ENTERED", "data": data},
                    context={"spreadsheet_id": spreadsheet_id, "range": a1.sheet},
                )
            except GoogleError as exc:
                raise ToolError(str(exc)) from None
        out = {"updated": updated, "not_found": missing, "cells_written": len(data)}
        if truncated:
            out["notes"] = ["only rows within the row cap were considered"]
        return out

    @server.tool()
    async def clear_range(spreadsheet_id: str, range: str) -> dict:
        """Destructive: empties every cell in the range. Confirm with the user first.

        Clears values only; formatting stays. Returns the cleared range. To remove a whole
        sheet use delete_sheet.
        """
        sheets.check(spreadsheet_id)
        try:
            a1, _ = await sheets.resolve(spreadsheet_id, range)
            body = await sheets.client.post(
                f"spreadsheets/{spreadsheet_id}/values/{a1.render()}:clear",
                json={},
                context={"spreadsheet_id": spreadsheet_id, "range": a1.render()},
            )
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"cleared_range": body.get("clearedRange", a1.render())}

    @server.tool()
    async def format_range(spreadsheet_id: str, range: str, format: dict[str, Any]) -> dict:
        """Apply cell formatting to a range.

        format keys, any subset: bold (true/false), italic (true/false), font_size (12),
        text_colour ("#1a73e8"), background ("#fff2cc"), number_format (a Sheets pattern:
        "0.00", "#,##0", "$#,##0.00", "0%", "dd/mm/yyyy"), horizontal_align ("left",
        "center", "right"), wrap (true/false). Example:
        {"bold": true, "background": "#e8f0fe", "number_format": "$#,##0.00"}.
        Returns the range formatted. For rules that depend on cell values use
        add_conditional_format; for anything else use batch_update.
        """
        sheets.check(spreadsheet_id)
        try:
            a1, sid = await sheets.resolve(spreadsheet_id, range)
            await sheets.batch(
                spreadsheet_id, [repeat_cell(sid, a1, format)], {"range": a1.render()}
            )
        except FormatError as exc:
            raise ToolError(str(exc)) from None
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"formatted_range": a1.render(), "keys": sorted(format)}

    @server.tool()
    async def set_column_widths(spreadsheet_id: str, sheet: str, widths: dict[str, Any]) -> dict:
        """Set column widths on a sheet.

        widths maps column letters to pixels or "auto", for example
        {"A": 220, "B": "auto", "C": 90}. "auto" fits the column to its content. Returns
        the columns changed.
        """
        sheets.check(spreadsheet_id)
        try:
            sid = await sheets.sheet_id(spreadsheet_id, sheet)
            await sheets.batch(spreadsheet_id, column_width_requests(sid, widths), {"range": sheet})
        except (FormatError, RangeError) as exc:
            raise ToolError(str(exc)) from None
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"sheet": sheet, "columns": sorted(widths)}

    @server.tool()
    async def freeze_rows(spreadsheet_id: str, sheet: str, rows: int, columns: int = 0) -> dict:
        """Freeze the top rows and optionally the left columns of a sheet.

        rows=1 keeps the header visible while scrolling; rows=0 unfreezes. columns freezes
        that many columns from the left. Returns the frozen counts.
        """
        sheets.check(spreadsheet_id)
        if rows < 0 or columns < 0:
            raise ToolError("rows and columns must be 0 or more")
        try:
            sid = await sheets.sheet_id(spreadsheet_id, sheet)
            await sheets.batch(
                spreadsheet_id,
                [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sid,
                                "gridProperties": {
                                    "frozenRowCount": rows,
                                    "frozenColumnCount": columns,
                                },
                            },
                            "fields": (
                                "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
                            ),
                        }
                    }
                ],
                {"range": sheet},
            )
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"sheet": sheet, "frozen_rows": rows, "frozen_columns": columns}

    @server.tool()
    async def add_conditional_format(spreadsheet_id: str, range: str, rule: dict[str, Any]) -> dict:
        """Add a conditional formatting rule to a range.

        rule keys: condition, value, value2 (for between), format. Conditions:
        greater_than, less_than, between, equal (numbers); text_contains,
        text_starts_with (text); is_blank, not_blank; custom_formula (value is a formula
        such as "=$C2>100", written for the range's top-left cell). format accepts bold,
        italic, text_colour, background. Example:
        {"condition": "greater_than", "value": 100, "format": {"background": "#ffcccc"}}.
        The new rule takes priority over existing ones. Returns the range and condition.
        """
        sheets.check(spreadsheet_id)
        try:
            a1, sid = await sheets.resolve(spreadsheet_id, range)
            await sheets.batch(
                spreadsheet_id, [conditional_rule(sid, a1, rule)], {"range": a1.render()}
            )
        except FormatError as exc:
            raise ToolError(str(exc)) from None
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"range": a1.render(), "condition": rule.get("condition")}

    @server.tool()
    async def add_sheet(spreadsheet_id: str, title: str) -> dict:
        """Add a new empty sheet (tab) to the spreadsheet.

        Returns the new sheet's title and sheet_id. Fails if a sheet with that title
        exists. Not for adding rows to an existing sheet: use append_rows.
        """
        sheets.check(spreadsheet_id)
        if not title or not title.strip():
            raise ToolError("title is required")
        try:
            body = await sheets.batch(
                spreadsheet_id,
                [{"addSheet": {"properties": {"title": title.strip()}}}],
                {"range": title},
            )
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        props = (body.get("replies") or [{}])[0].get("addSheet", {}).get("properties", {})
        return {"title": props.get("title", title), "sheet_id": props.get("sheetId")}

    @server.tool()
    async def delete_sheet(spreadsheet_id: str, sheet: str) -> dict:
        """Destructive: deletes a whole sheet (tab) and everything on it. Confirm first.

        Refuses when it is the spreadsheet's only sheet. Returns the deleted title. To
        empty cells but keep the sheet use clear_range.
        """
        sheets.check(spreadsheet_id)
        try:
            meta = await sheets.meta(spreadsheet_id)
            if len(meta.get("sheets", [])) <= 1:
                raise ToolError("cannot delete the only sheet in a spreadsheet")
            sid = await sheets.sheet_id(spreadsheet_id, sheet)
            await sheets.batch(
                spreadsheet_id, [{"deleteSheet": {"sheetId": sid}}], {"range": sheet}
            )
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"deleted": sheet}

    @server.tool()
    async def sort_range(
        spreadsheet_id: str, range: str, by_column: str, descending: bool = False
    ) -> dict:
        """Sort the rows of a range by one column, in place.

        range should exclude the header row, for example 'Orders'!A2:F. by_column is a
        column letter within the range. Returns the range and sort key. Sorting rewrites
        the rows, so confirm with the user on shared sheets.
        """
        sheets.check(spreadsheet_id)
        try:
            a1, sid = await sheets.resolve(spreadsheet_id, range)
            col = parse_range(f"{by_column}1").start_col
        except RangeError:
            raise ToolError(f"by_column must be a column letter, got {by_column!r}") from None
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        from manifold.toolsets.sheets.a1 import grid_range

        try:
            await sheets.batch(
                spreadsheet_id,
                [
                    {
                        "sortRange": {
                            "range": grid_range(sid, a1),
                            "sortSpecs": [
                                {
                                    "dimensionIndex": col,
                                    "sortOrder": "DESCENDING" if descending else "ASCENDING",
                                }
                            ],
                        }
                    }
                ],
                {"range": a1.render()},
            )
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"range": a1.render(), "by_column": by_column.upper(), "descending": descending}

    @server.tool()
    async def batch_update(spreadsheet_id: str, requests: list[dict[str, Any]]) -> dict:
        """Escape hatch: send raw Sheets API batchUpdate requests.

        requests is the list from the Sheets API reference, for example
        [{"mergeCells": {...}}]. Prefer the named tools (format_range, freeze_rows,
        set_column_widths, add_conditional_format, add_sheet, delete_sheet, sort_range)
        and use this only for what they do not cover. Returns Google's replies verbatim.
        """
        sheets.check(spreadsheet_id)
        if not isinstance(requests, list) or not requests:
            raise ToolError("requests must be a non-empty list of Sheets API request objects")
        try:
            body = await sheets.batch(spreadsheet_id, requests, {"range": "batchUpdate"})
        except GoogleError as exc:
            raise ToolError(str(exc)) from None
        return {"replies": body.get("replies", [])}

    return server


async def healthcheck(config: ToolsetConfig, credentials: Credentials) -> HealthResult:
    """Prove the credential can get a token, and reach the first allowed spreadsheet."""
    if credentials.kind not in ("service_account", "oauth2"):
        return HealthResult(
            status="down", detail="sheets needs a service_account or oauth2 credential"
        )
    helper = _Sheets(config, credentials)
    try:
        if helper.allowed:
            first = sorted(helper.allowed)[0]
            await helper.client.get(
                f"spreadsheets/{first}",
                params={"fields": "spreadsheetId"},
                context={"spreadsheet_id": first},
            )
        else:
            await helper.client._get_token()
    except GoogleError as exc:
        return HealthResult(status="degraded", detail=str(exc))
    except Exception as exc:
        return HealthResult(status="degraded", detail=f"{type(exc).__name__}: {exc}")
    return HealthResult(status="ok")
