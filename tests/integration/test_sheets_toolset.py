"""Sheets toolset against the fake Google API and token endpoint."""

from __future__ import annotations

import json

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from manifold.gateway.manifest import Credentials, ToolsetConfig
from manifold.google import auth, client, endpoints
from manifold.toolsets import sheets as sheets_module
from tests.fake_google import SERVICE_ACCOUNT, FakeGoogle

SS = "1abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"


@pytest.fixture
async def rig(monkeypatch):
    g = FakeGoogle()
    g.add_spreadsheet(
        SS,
        "Inventory",
        {
            "Stock": [
                ["SKU", "Name", "Qty", "Qty"],
                ["A100", "Bolt", 12, 3],
                ["B200", "Nut", 0, 1],
                ["C300", "Washer", 7, ""],
            ],
            "Notes": [],
        },
    )
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=g.app), base_url="https://fake")
    monkeypatch.setattr(endpoints, "TOKEN_URL", "https://fake/token")
    monkeypatch.setattr(endpoints, "SHEETS_BASE", "https://fake/v4")
    monkeypatch.setattr(client, "_shared_http", http)
    monkeypatch.setattr(client, "RETRY_BASE_SECONDS", 0.01)
    auth.clear_cache()
    yield g
    await http.aclose()


def server_for(settings: dict | None = None, creds: Credentials | None = None):
    config = ToolsetConfig(
        key="sheets", settings=settings or sheets_module.MANIFEST.example_settings
    )
    creds = creds or Credentials(kind="service_account", values={"json": SERVICE_ACCOUNT})
    return sheets_module.build(config, creds)


async def call(server, name, **args):
    result = await server.call_tool(name, args)
    if result.is_error:
        raise ToolError(result.content[0].text)
    return json.loads(result.content[0].text)


# -- get_spreadsheet, read_range ----------------------------------------------------------


async def test_get_spreadsheet_lists_sheets_headers_and_locale(rig):
    out = await call(server_for(), "get_spreadsheet", spreadsheet_id=SS)
    assert out["title"] == "Inventory" and out["locale"] == "en_AU"
    assert [s["title"] for s in out["sheets"]] == ["Stock", "Notes"]
    assert out["sheets"][0]["headers"] == ["SKU", "Name", "Qty", "Qty"]
    assert out["sheets"][1]["headers"] == []


async def test_read_range_header_objects_with_duplicate_suffix_and_padding(rig):
    out = await call(server_for(), "read_range", spreadsheet_id=SS, range="Stock")
    assert out["has_header"] is True
    assert out["headers"] == ["SKU", "Name", "Qty", "Qty_2"]
    assert out["rows"][2] == {"SKU": "C300", "Name": "Washer", "Qty": 7, "Qty_2": ""}
    assert out["row_numbers"] == [2, 3, 4]
    assert out["notes"] == ["duplicate header 'Qty' renamed to 'Qty_2'"]
    assert out["truncated"] is False


async def test_read_range_below_header_is_a_2d_array(rig):
    out = await call(server_for(), "read_range", spreadsheet_id=SS, range="'Stock'!A2:B3")
    assert out["has_header"] is False
    assert out["values"] == [["A100", "Bolt"], ["B200", "Nut"]]
    assert out["row_numbers"] == [2, 3]


async def test_read_range_has_header_override(rig):
    out = await call(
        server_for(), "read_range", spreadsheet_id=SS, range="'Stock'!A2:B3", has_header=True
    )
    assert out["headers"] == ["A100", "Bolt"] and out["row_numbers"] == [3]


async def test_read_range_render_modes(rig):
    server = server_for()
    formula = await call(
        server, "read_range", spreadsheet_id=SS, range="'Stock'!C2", render="formula"
    )
    assert formula["values"] == [["=12"]]
    with pytest.raises(ToolError, match="render must be"):
        await call(server, "read_range", spreadsheet_id=SS, range="Stock", render="pretty")
    raw_call = [c for c in rig.calls if "valueRenderOption" in c[1]]
    assert any("FORMULA" in c[1] for c in raw_call)


async def test_read_range_cap_and_next_range(rig):
    rig.grid(SS, "Stock").extend([[f"Z{i}", "x", i, ""] for i in range(50)])
    out = await call(
        server_for({"read_row_cap": 10, "allowed_spreadsheet_ids": []}),
        "read_range",
        spreadsheet_id=SS,
        range="Stock",
    )
    assert out["truncated"] is True and len(out["rows"]) == 9  # header plus 9 data rows = cap 10
    assert out["next_range"] == "'Stock'!11:1010"
    assert "only the first 10 rows" in out["notes"][-1]
    fetched = [c for c in rig.calls if c[1].startswith("values/'Stock'!1:11")]
    assert fetched, "fetch was bounded to cap plus one row, not the whole sheet"


# -- writes ---------------------------------------------------------------------------


async def test_append_rows_user_entered_and_raw(rig):
    server = server_for()
    await call(server, "append_rows", spreadsheet_id=SS, sheet="Notes", rows=[["12", "0412"]])
    await call(
        server, "append_rows", spreadsheet_id=SS, sheet="Notes", rows=[["12", "0412"]], raw=True
    )
    assert rig.grid(SS, "Notes") == [[12, "0412"], ["12", "0412"]]


async def test_update_range_and_clear_range(rig):
    server = server_for()
    out = await call(server, "update_range", spreadsheet_id=SS, range="'Stock'!C2", values=[["99"]])
    assert out["rows_updated"] == 1 and rig.grid(SS, "Stock")[1][2] == 99
    await call(server, "clear_range", spreadsheet_id=SS, range="'Stock'!C2:C3")
    assert rig.grid(SS, "Stock")[1][2] == "" and rig.grid(SS, "Stock")[2][2] == ""


async def test_update_range_defaults_to_first_sheet(rig):
    await call(server_for(), "update_range", spreadsheet_id=SS, range="B2", values=[["Bolt M6"]])
    assert rig.grid(SS, "Stock")[1][1] == "Bolt M6"


# -- find and update by key ---------------------------------------------------------------


async def test_find_rows_exact_contains_regex_with_row_numbers(rig):
    server = server_for()
    out = await call(
        server, "find_rows", spreadsheet_id=SS, sheet="Stock", column="Name", value="nut"
    )
    assert out["row_numbers"] == [3] and out["matches"][0]["SKU"] == "B200"
    out = await call(
        server, "find_rows", spreadsheet_id=SS, sheet="Stock", column="C", value="0", match="exact"
    )
    assert out["row_numbers"] == [3]
    out = await call(
        server,
        "find_rows",
        spreadsheet_id=SS,
        sheet="Stock",
        column="SKU",
        value="^[AB]",
        match="regex",
    )
    assert out["row_numbers"] == [2, 3]
    out = await call(
        server,
        "find_rows",
        spreadsheet_id=SS,
        sheet="Stock",
        column="Name",
        value="o",
        match="contains",
    )
    assert out["row_numbers"] == [2]
    with pytest.raises(ToolError, match="not a header"):
        await call(
            server, "find_rows", spreadsheet_id=SS, sheet="Stock", column="Weight", value="1"
        )


async def test_update_rows_by_key(rig):
    out = await call(
        server_for(),
        "update_rows_by_key",
        spreadsheet_id=SS,
        sheet="Stock",
        key_column="SKU",
        updates=[{"SKU": "b200", "Qty": 42, "Name": "Nut M8"}, {"SKU": "ZZZ", "Qty": 1}],
    )
    assert out["updated"] == ["b200"] and out["not_found"] == ["ZZZ"] and out["cells_written"] == 2
    assert rig.grid(SS, "Stock")[2][:3] == ["B200", "Nut M8", 42]
    with pytest.raises(ToolError, match="unknown columns"):
        await call(
            server_for(),
            "update_rows_by_key",
            spreadsheet_id=SS,
            sheet="Stock",
            key_column="SKU",
            updates=[{"SKU": "A100", "Nope": 1}],
        )


# -- formatting and structure -------------------------------------------------------------


async def test_format_range_builds_repeat_cell(rig):
    out = await call(
        server_for(),
        "format_range",
        spreadsheet_id=SS,
        range="'Stock'!A1:D1",
        format={"bold": True, "background": "#e8f0fe", "number_format": "$#,##0.00"},
    )
    assert out["formatted_range"] == "'Stock'!A1:D1"
    req = rig.batch_updates[-1]["requests"][0]["repeatCell"]
    assert req["range"] == {
        "sheetId": 0,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 0,
        "endColumnIndex": 4,
    }
    assert req["cell"]["userEnteredFormat"]["textFormat"]["bold"] is True
    assert req["cell"]["userEnteredFormat"]["numberFormat"] == {
        "type": "NUMBER",
        "pattern": "$#,##0.00",
    }
    assert "userEnteredFormat.backgroundColor" in req["fields"]
    with pytest.raises(ToolError, match="unknown format keys"):
        await call(
            server_for(), "format_range", spreadsheet_id=SS, range="A1", format={"colour": "red"}
        )


async def test_conditional_format_widths_freeze_sort(rig):
    server = server_for()
    await call(
        server,
        "add_conditional_format",
        spreadsheet_id=SS,
        range="'Stock'!C2:C",
        rule={"condition": "less_than", "value": 5, "format": {"background": "#ffcccc"}},
    )
    rule = rig.batch_updates[-1]["requests"][0]["addConditionalFormatRule"]["rule"]
    assert rule["booleanRule"]["condition"] == {
        "type": "NUMBER_LESS",
        "values": [{"userEnteredValue": "5"}],
    }
    with pytest.raises(ToolError, match="condition must be"):
        await call(
            server,
            "add_conditional_format",
            spreadsheet_id=SS,
            range="A1",
            rule={"condition": "red", "format": {}},
        )
    await call(
        server,
        "set_column_widths",
        spreadsheet_id=SS,
        sheet="Stock",
        widths={"A": 120, "B": "auto"},
    )
    reqs = rig.batch_updates[-1]["requests"]
    assert "updateDimensionProperties" in reqs[0] and "autoResizeDimensions" in reqs[1]
    out = await call(server, "freeze_rows", spreadsheet_id=SS, sheet="Stock", rows=1, columns=1)
    assert out["frozen_rows"] == 1
    meta = await call(server, "get_spreadsheet", spreadsheet_id=SS)
    assert meta["sheets"][0]["frozen_rows"] == 1
    out = await call(
        server,
        "sort_range",
        spreadsheet_id=SS,
        range="'Stock'!A2:D",
        by_column="c",
        descending=True,
    )
    assert rig.batch_updates[-1]["requests"][0]["sortRange"]["sortSpecs"] == [
        {"dimensionIndex": 2, "sortOrder": "DESCENDING"}
    ]
    assert out["by_column"] == "C"


async def test_add_and_delete_sheet(rig):
    server = server_for()
    out = await call(server, "add_sheet", spreadsheet_id=SS, title="Archive")
    assert out["title"] == "Archive" and out["sheet_id"] == 2
    await call(server, "delete_sheet", spreadsheet_id=SS, sheet="Archive")
    assert [
        s["title"] for s in (await call(server, "get_spreadsheet", spreadsheet_id=SS))["sheets"]
    ] == ["Stock", "Notes"]
    await call(server, "delete_sheet", spreadsheet_id=SS, sheet="Notes")
    with pytest.raises(ToolError, match="only sheet"):
        await call(server, "delete_sheet", spreadsheet_id=SS, sheet="Stock")
    with pytest.raises(ToolError, match="no sheet named 'Gone'"):
        await call(server, "freeze_rows", spreadsheet_id=SS, sheet="Gone", rows=1)


async def test_batch_update_passthrough(rig):
    out = await call(
        server_for(),
        "batch_update",
        spreadsheet_id=SS,
        requests=[{"addSheet": {"properties": {"title": "Raw"}}}],
    )
    assert out["replies"][0]["addSheet"]["properties"]["title"] == "Raw"


# -- guards and errors ----------------------------------------------------------------


async def test_allow_list_refuses_before_touching_google(rig):
    server = server_for(
        {"allowed_spreadsheet_ids": ["1other_spreadsheet_id_xxxxxxxxxxxx"], "read_row_cap": 1000}
    )
    before = len(rig.calls)
    with pytest.raises(ToolError, match="not on this toolset's allow list"):
        await call(server, "read_range", spreadsheet_id=SS, range="Stock")
    assert len(rig.calls) == before


async def test_google_errors_are_readable(rig):
    server = server_for()
    rig.forbidden.add(SS)
    with pytest.raises(ToolError, match=r"Share it with bot@test-proj\.iam"):
        await call(server, "read_range", spreadsheet_id=SS, range="Stock")
    rig.forbidden.clear()
    with pytest.raises(ToolError, match="spreadsheet or range not found: 1nope"):
        await call(server, "get_spreadsheet", spreadsheet_id="1nope")
    with pytest.raises(ToolError, match="bad range"):
        await call(server, "read_range", spreadsheet_id=SS, range="'Stock'!A1:B2:C3")
    rig.fail_next = [429, 429, 429, 429]
    with pytest.raises(ToolError, match="rate-limited"):
        await call(server, "get_spreadsheet", spreadsheet_id=SS)


async def test_healthcheck_service_account_and_allow_list(rig):
    config = ToolsetConfig(
        key="sheets", settings={"allowed_spreadsheet_ids": [SS], "read_row_cap": 1000}
    )
    creds = Credentials(kind="service_account", values={"json": SERVICE_ACCOUNT})
    assert (await sheets_module.healthcheck(config, creds)).status == "ok"
    rig.forbidden.add(SS)
    result = await sheets_module.healthcheck(config, creds)
    assert result.status == "degraded" and "Share it with" in result.detail
    wrong = Credentials(kind="api_key", values={"key": "x"})
    assert (await sheets_module.healthcheck(config, wrong)).status == "down"


async def test_oauth_credential_path(rig):
    async def getter() -> str:
        return "oauth-abc"

    creds = Credentials(kind="oauth2", values={}).with_token_getter(getter)
    out = await call(server_for(creds=creds), "get_spreadsheet", spreadsheet_id=SS)
    assert out["title"] == "Inventory" and rig.token_issued == 0


async def test_destructive_and_overwrite_docstrings():
    server = server_for()
    tools = {t.name: t.description for t in await server.list_tools()}
    assert tools["delete_sheet"].splitlines()[0].startswith("Destructive:")
    assert tools["clear_range"].splitlines()[0].startswith("Destructive:")
    assert "without confirmation" in tools["update_range"].splitlines()[0]
    assert "without confirmation" in tools["update_rows_by_key"].splitlines()[0]
    assert "postcode" in tools["append_rows"] and '"0412"' in tools["update_range"]
    assert (
        "background" in tools["format_range"]
        and "custom_formula" in tools["add_conditional_format"]
    )
    assert len(tools) == 15
