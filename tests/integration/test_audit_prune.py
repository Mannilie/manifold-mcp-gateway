"""Audit retention (Phase 6 gate 1)."""

from __future__ import annotations

import httpx2
import pytest

import manifold.toolsets
import tests.fixtures.toolsets
from manifold.config.settings import Settings
from manifold.gateway.prune import AuditPruner
from manifold.store.audit import AuditRepo
from manifold.store.db import Database
from manifold.store.settings import AUDIT_RETENTION_DAYS, AUDIT_ROW_CAP, GatewaySettingsRepo
from tests.oauth_helpers import ACCESS_HEADER

PACKAGES = (manifold.toolsets, tests.fixtures.toolsets)
MUT = {**ACCESS_HEADER, "X-Manifold-Request": "1"}


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path)
    await database.open()
    await database.migrate()
    yield database
    await database.close()


async def seed(db: Database, count: int, ts: str) -> None:
    for _ in range(count):
        await db.conn.execute(
            "INSERT INTO audit_log (ts, toolset_key, tool_name, args_hash, duration_ms, ok)"
            " VALUES (?, 'sheets', 'read_range', 'h', 1, 1)",
            (ts,),
        )


async def test_prune_by_age_then_cap_in_batches(db, monkeypatch):
    monkeypatch.setattr("manifold.gateway.prune.BATCH_ROWS", 100)
    monkeypatch.setattr("manifold.gateway.prune.BATCH_PAUSE_SECONDS", 0.0)
    audit, settings = AuditRepo(db), GatewaySettingsRepo(db)
    await settings.set(AUDIT_RETENTION_DAYS, 30)
    await settings.set(AUDIT_ROW_CAP, 10_000)
    await seed(db, 250, "2020-01-01T00:00:00Z")  # old
    await seed(db, 300, "2099-01-01T00:00:00Z")  # young
    pruner = AuditPruner(db, audit, settings, interval=3600, startup_delay=0)
    result = await pruner.run_once()
    assert result == {"by_age": 250, "by_cap": 0, "rows": 300}
    stats = await audit.stats()
    assert stats["rows"] == 300 and stats["oldest_ts"] == "2099-01-01T00:00:00Z"
    assert stats["last_prune"]["by_age"] == 250 and stats["last_prune"]["rows_after"] == 300


async def test_cap_fires_and_is_reported(db, monkeypatch):
    monkeypatch.setattr("manifold.gateway.prune.BATCH_ROWS", 100)
    monkeypatch.setattr("manifold.gateway.prune.BATCH_PAUSE_SECONDS", 0.0)
    audit, settings = AuditRepo(db), GatewaySettingsRepo(db)
    await settings.set(AUDIT_ROW_CAP, 10_000)
    await seed(db, 1000, "2099-01-01T00:00:00Z")
    # the repo validates the cap range at the API; the pruner just honours the stored value
    await settings.set(AUDIT_ROW_CAP, 400)
    pruner = AuditPruner(db, audit, settings, interval=3600, startup_delay=0)
    result = await pruner.run_once()
    assert result["by_cap"] == 600 and result["rows"] == 400
    assert (await audit.stats())["last_prune"]["by_cap"] == 600


async def test_incremental_vacuum_mode_is_enabled(db):
    async with db.conn.execute("PRAGMA auto_vacuum") as c:
        assert int((await c.fetchone())[0]) == 2


async def test_settings_api_validates_and_prunes(env, tmp_path):
    from manifold.app import create_app

    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            r = await http.get("/api/settings", headers=ACCESS_HEADER)
            assert r.json()["audit_row_cap"] == 200_000 and r.json()["audit"]["rows"] == 0
            for bad in (
                {"audit_retention_days": 0},
                {"audit_retention_days": 366},
                {"audit_row_cap": 9_999},
                {"audit_row_cap": 1_000_001},
            ):
                assert (await http.patch("/api/settings", json=bad, headers=MUT)).status_code == 422
            r = await http.patch(
                "/api/settings",
                json={"audit_retention_days": 7, "audit_row_cap": 10_000},
                headers=MUT,
            )
            assert r.json()["audit_retention_days"] == 7 and r.json()["audit_row_cap"] == 10_000
            await seed(app.state.db, 5, "2020-01-01T00:00:00Z")
            r = await http.post("/api/settings/prune-audit", headers=MUT)
            assert r.json()["by_age"] == 5
            r = await http.get("/api/settings", headers=ACCESS_HEADER)
            assert r.json()["audit"]["last_prune"]["by_age"] == 5


async def test_access_log_is_written_under_data(env, tmp_path):
    from manifold.app import create_app

    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
    async with app.router.lifespan_context(app):
        import logging

        logging.getLogger("uvicorn.access").info('127.0.0.1 - "DELETE /api/toolsets/n8n" 204')
    log_file = tmp_path / "logs" / "access.log"
    assert log_file.exists() and "DELETE /api/toolsets/n8n" in log_file.read_text()
