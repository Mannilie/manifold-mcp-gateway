"""Phase 2 completion criterion: a direct database edit takes effect without a rebuild."""

from __future__ import annotations

import asyncio
import os
import sqlite3

import httpx2
import pytest

from tests.oauth_helpers import obtain_tokens

PING = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "ping", "arguments": {}},
}
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def edit(sql: str, *params) -> None:
    """A separate connection, like the sqlite3 CLI on the NAS."""
    path = os.path.join(os.environ["MANIFOLD_DATA_DIR"], "manifold.db")
    with sqlite3.connect(path) as conn:
        conn.execute(sql, params)
        conn.commit()


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def http(live_server):
    async with httpx2.AsyncClient(base_url=live_server, follow_redirects=False) as c:
        yield c


async def test_disable_and_enable_via_direct_db_edit(http):
    _, tokens = await obtain_tokens(http, "ping-b")
    auth = {**HDR, "Authorization": f"Bearer {tokens['access_token']}"}
    assert (await http.post("/ping-b", json=PING, headers=auth)).status_code == 200
    try:
        edit("UPDATE toolsets SET enabled = 0 WHERE key = 'ping-b'")

        async def gone():
            r = await http.post("/ping-b", json=PING, headers=auth)
            return r.status_code == 200 and "Manifold is running" in r.text

        await wait_for(gone)
        assert (await http.get("/healthz")).json()["toolsets"] == ["manifold"]
    finally:
        edit("UPDATE toolsets SET enabled = 1 WHERE key = 'ping-b'")

        async def back():
            r = await http.post("/ping-b", json=PING, headers=auth)
            return r.status_code == 200 and "pong from ping-b" in r.text

        await wait_for(back)


async def test_native_toolsets_were_registered_in_the_store(http):
    path = os.path.join(os.environ["MANIFOLD_DATA_DIR"], "manifold.db")
    with sqlite3.connect(path) as conn:
        rows = dict(conn.execute("SELECT key, enabled FROM toolsets").fetchall())
    assert rows["manifold"] == 1
    assert "ping-b" in rows


async def test_tool_calls_are_audited_with_hashed_args(http):
    _, tokens = await obtain_tokens(http, "manifold")
    auth = {**HDR, "Authorization": f"Bearer {tokens['access_token']}"}
    assert (await http.post("/manifold", json=PING, headers=auth)).status_code == 200
    path = os.path.join(os.environ["MANIFOLD_DATA_DIR"], "manifold.db")

    async def audited():
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT toolset_key, tool_name, args_hash, ok FROM audit_log"
                " WHERE toolset_key = 'manifold' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row is not None and row[1] == "ping" and row[3] == 1 and len(row[2]) == 64

    await wait_for(audited)
