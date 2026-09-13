"""Live test against a real throwaway spreadsheet. Skipped unless both env vars are set.

    MANIFOLD_TEST_SA_JSON        path to a service account JSON file
    MANIFOLD_TEST_SPREADSHEET_ID id of a spreadsheet shared with that service account as Editor

Run with: uv run pytest tests/live -q
The test writes to a sheet named `manifold-live-test`, which it creates and deletes.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from manifold.gateway.manifest import Credentials, ToolsetConfig
from manifold.toolsets import sheets as sheets_module

SA_PATH = os.environ.get("MANIFOLD_TEST_SA_JSON")
SPREADSHEET = os.environ.get("MANIFOLD_TEST_SPREADSHEET_ID")

pytestmark = pytest.mark.skipif(
    not SA_PATH or not SPREADSHEET,
    reason="MANIFOLD_TEST_SA_JSON and MANIFOLD_TEST_SPREADSHEET_ID not set",
)


async def call(server, name, **args):
    result = await server.call_tool(name, args)
    if result.is_error:
        raise ToolError(result.content[0].text)
    return json.loads(result.content[0].text)


@pytest.fixture
async def server():
    with open(SA_PATH) as f:
        sa = json.load(f)
    creds = Credentials(kind="service_account", values={"json": sa})
    config = ToolsetConfig(
        key="sheets", settings={"allowed_spreadsheet_ids": [SPREADSHEET], "read_row_cap": 1000}
    )
    srv = sheets_module.build(config, creds)
    title = f"manifold-live-test-{int(time.time())}"
    await call(srv, "add_sheet", spreadsheet_id=SPREADSHEET, title=title)
    yield srv, title
    await call(srv, "delete_sheet", spreadsheet_id=SPREADSHEET, sheet=title)


async def test_round_trip_against_google(server):
    srv, title = server
    meta = await call(srv, "get_spreadsheet", spreadsheet_id=SPREADSHEET)
    assert title in [s["title"] for s in meta["sheets"]]
    assert meta["locale"]
    await call(
        srv,
        "append_rows",
        spreadsheet_id=SPREADSHEET,
        sheet=title,
        rows=[["SKU", "Qty", "Price"], ["A1", "3", "=B2*2"], ["B2", "0412", "10.5"]],
    )
    read = await call(srv, "read_range", spreadsheet_id=SPREADSHEET, range=title)
    assert read["headers"] == ["SKU", "Qty", "Price"]
    assert read["rows"][0]["Price"] == "6", "formula evaluated under USER_ENTERED"
    assert read["rows"][1]["Qty"] == "412", "leading zero lost as when typed; raw=true keeps it"
    formula = await call(
        srv, "read_range", spreadsheet_id=SPREADSHEET, range=f"'{title}'!C2", render="formula"
    )
    assert formula["values"] == [["=B2*2"]]
    found = await call(
        srv, "find_rows", spreadsheet_id=SPREADSHEET, sheet=title, column="SKU", value="b2"
    )
    assert found["row_numbers"] == [3]
    upd = await call(
        srv,
        "update_rows_by_key",
        spreadsheet_id=SPREADSHEET,
        sheet=title,
        key_column="SKU",
        updates=[{"SKU": "A1", "Qty": 9}],
    )
    assert upd["updated"] == ["A1"]
    await call(
        srv,
        "format_range",
        spreadsheet_id=SPREADSHEET,
        range=f"'{title}'!A1:C1",
        format={"bold": True, "background": "#e8f0fe"},
    )
    await call(srv, "freeze_rows", spreadsheet_id=SPREADSHEET, sheet=title, rows=1)
    await call(
        srv,
        "set_column_widths",
        spreadsheet_id=SPREADSHEET,
        sheet=title,
        widths={"A": 140, "B": "auto"},
    )
    await call(
        srv,
        "add_conditional_format",
        spreadsheet_id=SPREADSHEET,
        range=f"'{title}'!B2:B",
        rule={"condition": "greater_than", "value": 5, "format": {"background": "#ffcccc"}},
    )
    await call(
        srv,
        "sort_range",
        spreadsheet_id=SPREADSHEET,
        range=f"'{title}'!A2:C",
        by_column="A",
        descending=True,
    )
    after = await call(srv, "read_range", spreadsheet_id=SPREADSHEET, range=title)
    assert [r["SKU"] for r in after["rows"]] == ["B2", "A1"]
    await call(srv, "clear_range", spreadsheet_id=SPREADSHEET, range=f"'{title}'!A2:C3")
    cleared = await call(srv, "read_range", spreadsheet_id=SPREADSHEET, range=title)
    assert cleared["rows"] == []


async def test_healthcheck_live(server):
    config = ToolsetConfig(
        key="sheets", settings={"allowed_spreadsheet_ids": [SPREADSHEET], "read_row_cap": 1000}
    )
    with open(SA_PATH) as f:
        creds = Credentials(kind="service_account", values={"json": json.load(f)})
    assert (await sheets_module.healthcheck(config, creds)).status == "ok"
