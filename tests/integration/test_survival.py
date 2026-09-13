"""Every row survives a migration and an image upgrade (Phase 6 loose end)."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx2
import pytest

import manifold.toolsets
import tests.fixtures.toolsets
from manifold.config.settings import Settings
from manifold.crypto.keys import INFO_CREDENTIALS, derive_key
from manifold.store.credentials import CredentialsRepo
from manifold.store.db import MIGRATIONS_DIR, Database, list_migrations
from manifold.store.toolsets import ToolsetsRepo
from tests.oauth_helpers import ACCESS_HEADER

PACKAGES = (manifold.toolsets, tests.fixtures.toolsets)
MUT = {**ACCESS_HEADER, "X-Manifold-Request": "1"}
KEY = derive_key(b"\x01" * 32, INFO_CREDENTIALS)


def migrations_up_to(tmp_path: Path, version: int) -> Path:
    target = tmp_path / f"migrations-{version}"
    target.mkdir()
    for v, path in list_migrations(MIGRATIONS_DIR):
        if v <= version:
            shutil.copy(path, target / path.name)
    return target


async def populate(data_dir: Path, version: int) -> dict:
    """A database at schema `version` with one of everything, written with plain SQL the
    way that version's code would have, never through today's repositories."""
    from manifold.crypto.box import SCHEME, credential_aad, encrypt

    db = Database(data_dir, migrations_up_to(data_dir.parent, version))
    await db.open()
    try:
        await db.migrate()
        now = "2026-09-13T12:35:00Z"
        nonce, ciphertext = encrypt(
            KEY, b'{"token":"tok-123"}', credential_aad(SCHEME, 1, "bearer")
        )
        await db.conn.execute(
            "INSERT INTO credentials (id, name, auth_kind, scheme, nonce, ciphertext, created_at,"
            " updated_at) VALUES (1, 'n8n MCP', 'bearer', ?, ?, ?, ?, ?)",
            (SCHEME, nonce, ciphertext, now, now),
        )
        for key, name, kind, enabled, cid, settings in (
            ("manifold", "Manifold", "native", 1, None, "{}"),
            (
                "sheets",
                "Google Sheets",
                "native",
                1,
                None,
                '{"allowed_spreadsheet_ids": ["1abcdefghijklmnopqrstuvwxyz"], "read_row_cap": 500}',
            ),
            ("n8n", "n8n", "proxy", 0, 1, "{}"),
        ):
            await db.conn.execute(
                "INSERT INTO toolsets (key, display_name, kind, enabled, credential_id,"
                " settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key, name, kind, enabled, cid, settings, now, now),
            )
        await db.conn.execute(
            "INSERT INTO proxy_upstreams (toolset_key, upstream_url, prefix, allow_json, deny_json)"
            " VALUES ('n8n', 'http://n8n:5678/mcp', 'n8n_', '[]', '[\"secret\"]')"
        )
        await db.conn.execute(
            "INSERT INTO oauth_clients (client_id, metadata_json, created_at)"
            " VALUES ('c1', '{}', ?)",
            (now,),
        )
        return {"cid": 1}
    finally:
        await db.close()


async def snapshot(data_dir: Path) -> dict:
    db = Database(data_dir)
    await db.open()
    try:
        await db.migrate()
        toolsets = {t.key: t for t in await ToolsetsRepo(db).list()}
        creds = {c.name: c for c in await CredentialsRepo(db, KEY).list()}
        n8n_values = (await CredentialsRepo(db, KEY).get(creds["n8n MCP"].id)).values
        async with db.conn.execute("SELECT client_id FROM oauth_clients") as c:
            clients = [r[0] for r in await c.fetchall()]
        return {"toolsets": toolsets, "creds": creds, "n8n_values": n8n_values, "clients": clients}
    finally:
        await db.close()


@pytest.mark.parametrize("from_version", [v for v, _ in list_migrations(MIGRATIONS_DIR)][:-1])
async def test_rows_survive_migrating_from_every_older_schema(tmp_path, from_version):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await populate(data_dir, from_version)
    after = await snapshot(data_dir)
    assert set(after["toolsets"]) >= {"n8n", "sheets", "manifold"}
    n8n = after["toolsets"]["n8n"]
    assert (
        n8n.kind == "proxy" and n8n.upstream.deny == ("secret",) and n8n.credential_id is not None
    )
    assert after["toolsets"]["sheets"].settings["read_row_cap"] == 500
    assert after["n8n_values"] == {"token": "tok-123"}
    assert after["clients"] == ["c1"]


async def test_rows_survive_an_image_upgrade_boot(tmp_path, env):
    """Boot the whole app twice on the same data dir, like Watchtower replacing the image."""
    from manifold.app import create_app

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    await populate(data_dir, 1)
    env["MANIFOLD_DATA_DIR"] = str(data_dir)
    for boot in range(2):
        app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http:
                keys = {
                    t["key"]
                    for t in (await http.get("/api/toolsets", headers=ACCESS_HEADER)).json()
                }
                assert {"n8n", "sheets", "manifold"} <= keys, f"boot {boot}"
                names = {
                    c["name"]
                    for c in (await http.get("/api/credentials", headers=ACCESS_HEADER)).json()
                }
                assert "n8n MCP" in names, f"boot {boot}"


async def test_admin_actions_are_audited(env, tmp_path):
    from manifold.app import create_app

    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            r = await http.post(
                "/api/credentials",
                json={"name": "n8n MCP", "auth_kind": "bearer", "values": {"token": "tok-123"}},
                headers=MUT,
            )
            cid = r.json()["id"]
            await http.post(
                "/api/toolsets",
                json={
                    "key": "n8n",
                    "display_name": "n8n",
                    "upstream_url": "http://n8n:5678/mcp",
                    "credential_id": cid,
                },
                headers=MUT,
            )
            await http.patch("/api/toolsets/n8n", json={"display_name": "n8n prod"}, headers=MUT)
            assert (await http.delete("/api/toolsets/n8n", headers=MUT)).status_code == 204
            assert (await http.delete(f"/api/credentials/{cid}", headers=MUT)).status_code == 204
            rows = (await http.get("/api/audit?limit=10", headers=ACCESS_HEADER)).json()
            actions = [(r["tool_name"], r["toolset_key"], r["actor"], r["detail"]) for r in rows]
            assert (
                "admin:credential.delete",
                f"credential:{cid}",
                "manny@example.com",
                "'n8n MCP' (bearer)",
            ) in actions
            assert (
                "admin:toolset.delete",
                "n8n",
                "manny@example.com",
                "proxy 'n8n prod'",
            ) in actions
            assert any(
                a[0] == "admin:toolset.update" and "display_name" in (a[3] or "") for a in actions
            )
            assert "tok-123" not in str(rows)
            summary = (await http.get("/api/toolsets", headers=ACCESS_HEADER)).json()
            assert all(t["last_call_at"] is None for t in summary), (
                "admin rows do not count as calls"
            )
