"""Snapshots, validated restore and key rotation (Phase 6 gate 2)."""

from __future__ import annotations

import base64
import os
import shutil
import sqlite3

import httpx2
import pytest

import manifold.toolsets
import tests.fixtures.toolsets
from manifold.config.settings import Settings
from manifold.crypto.keys import INFO_CREDENTIALS, derive_key
from manifold.crypto.rotate import rotate
from manifold.store.db import Database
from manifold.store.snapshots import KEEP, SnapshotStore, apply_pending
from tests.oauth_helpers import ACCESS_HEADER

PACKAGES = (manifold.toolsets, tests.fixtures.toolsets)
MUT = {**ACCESS_HEADER, "X-Manifold-Request": "1"}


async def boot(env, tmp_path):
    from manifold.app import create_app

    app = create_app(Settings.from_env(env), reload_poll_seconds=100, toolset_packages=PACKAGES)
    app.state.no_restart = True
    return app


@pytest.fixture
async def rig(env, tmp_path):
    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    app = await boot(env, tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield app, http


async def test_snapshot_now_list_download_and_prune(rig, tmp_path):
    app, http = rig
    r = await http.post("/api/backups", headers=MUT)
    assert r.status_code == 201 and r.json()["reason"] == "manual"
    name = r.json()["name"]
    listing = (await http.get("/api/backups", headers=ACCESS_HEADER)).json()
    assert listing[0]["name"] == name
    d = await http.get(f"/api/backups/{name}", headers=ACCESS_HEADER)
    assert d.status_code == 200 and d.content[:16] == b"SQLite format 3\x00"
    assert (await http.get("/api/backups/../manifold.db", headers=ACCESS_HEADER)).status_code in (
        404,
        422,
    )
    snaps: SnapshotStore = app.state.snapshots
    for _ in range(KEEP + 3):
        await snaps.create("manual")
    assert len(snaps.list()) == KEEP


async def test_migration_takes_a_snapshot_first(tmp_path, env):
    """A database at an older schema gets a pre-migration snapshot under backups/."""
    from manifold.store.db import MIGRATIONS_DIR, list_migrations

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old = tmp_path / "m"
    old.mkdir()
    for _v, path in list_migrations(MIGRATIONS_DIR)[:1]:
        shutil.copy(path, old / path.name)
    db = Database(data_dir, old)
    await db.open()
    await db.migrate()
    await db.conn.execute(
        "INSERT INTO toolsets"
        " (key, display_name, kind, enabled, settings_json, created_at, updated_at)"
        " VALUES ('manifold', 'M', 'native', 1, '{}', 'x', 'x')"
    )
    await db.close()
    env["MANIFOLD_DATA_DIR"] = str(data_dir)
    app = await boot(env, data_dir)
    async with app.router.lifespan_context(app):
        snaps = app.state.snapshots.list()
        assert [s.reason for s in snaps] == ["pre-migration"]
        assert not list(data_dir.glob("manifold.db.pre-*")), "old copy mechanism retired"


async def test_validate_rejects_bad_files(rig, tmp_path):
    _, http = rig
    for name, data, match in (
        ("junk", b"not a database at all", "not a SQLite database"),
        ("empty", b"", "empty upload"),
    ):
        r = await http.post(
            "/api/backups/restore/validate", files={"file": (name, data)}, headers=MUT
        )
        assert r.status_code in (422,), r.text
        assert match in r.text
    foreign = sqlite3.connect(tmp_path / "foreign.db")
    foreign.execute("CREATE TABLE x (a)")
    foreign.commit()
    foreign.close()
    r = await http.post(
        "/api/backups/restore/validate",
        files={"file": ("f.db", (tmp_path / "foreign.db").read_bytes())},
        headers=MUT,
    )
    assert r.status_code == 422 and "not a Manifold one" in r.text


async def test_validate_rejects_other_master_key(rig, tmp_path, env):
    app, http = rig
    snap = await app.state.snapshots.create("manual")
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_env = {
        **env,
        "MANIFOLD_DATA_DIR": str(other_dir),
        "MANIFOLD_MASTER_KEY": base64.b64encode(os.urandom(32)).decode(),
    }
    other = await boot(other_env, other_dir)
    async with other.router.lifespan_context(other):
        pass
    data = (other_dir / "manifold.db").read_bytes()
    r = await http.post(
        "/api/backups/restore/validate", files={"file": ("other.db", data)}, headers=MUT
    )
    assert r.status_code == 422 and "different MANIFOLD_MASTER_KEY" in r.text
    assert (app.state.snapshots.dir / snap.name).exists()


async def test_round_trip_snapshot_delete_restore(env, tmp_path):
    """The Phase 6 criterion, in process: snapshot, delete a toolset, restore, it is back."""
    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    app = await boot(env, tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            r = await http.post(
                "/api/toolsets",
                json={"key": "n8n", "display_name": "n8n", "upstream_url": "http://n8n:5678/mcp"},
                headers=MUT,
            )
            assert r.status_code == 201
            snap = (await http.post("/api/backups", headers=MUT)).json()
            snapshot_bytes = (
                await http.get(f"/api/backups/{snap['name']}", headers=ACCESS_HEADER)
            ).content
            assert (await http.delete("/api/toolsets/n8n", headers=MUT)).status_code == 204
            assert (await http.get("/api/toolsets/n8n", headers=ACCESS_HEADER)).status_code == 404
            r = await http.post(
                "/api/backups/restore/validate",
                files={"file": ("snap.db", snapshot_bytes)},
                headers=MUT,
            )
            assert r.status_code == 200, r.text
            report = r.json()
            assert report["counts"]["toolsets"] == report["live_counts"]["toolsets"] + 1
            # the only other difference is the audit rows written since the snapshot
            assert all(w.startswith("audit_log:") for w in report["warnings"]), report["warnings"]
            r = await http.post(
                "/api/backups/restore/confirm", json={"token": report["token"]}, headers=MUT
            )
            assert r.status_code == 200 and r.json()["pre_restore"].endswith("-pre-restore.db")
            assert (tmp_path / "manifold.db.restore").exists()
            actions = [
                e["tool_name"]
                for e in (await http.get("/api/audit?limit=5", headers=ACCESS_HEADER)).json()
            ]
            assert "admin:backup.restore" in actions
    # "restart": boot again on the same directory
    assert apply_pending(tmp_path) is True
    assert not (tmp_path / "manifold.db.restore").exists()
    app = await boot(env, tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            assert (await http.get("/api/toolsets/n8n", headers=ACCESS_HEADER)).status_code == 200
            reasons = [
                s["reason"] for s in (await http.get("/api/backups", headers=ACCESS_HEADER)).json()
            ]
            assert "pre-restore" in reasons and "manual" in reasons


async def test_confirm_without_validate_is_refused(rig):
    _, http = rig
    r = await http.post(
        "/api/backups/restore/confirm", json={"token": "incoming-20260101T000000Z-1"}, headers=MUT
    )
    assert r.status_code == 422 and "upload and validate" in r.text
    r = await http.post("/api/backups/restore/confirm", json={"token": "../../etc"}, headers=MUT)
    assert r.status_code == 422


async def test_key_rotation(env, tmp_path):
    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    app = await boot(env, tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            await http.post(
                "/api/credentials",
                json={"name": "Key", "auth_kind": "api_key", "values": {"key": "sk-live-1"}},
                headers=MUT,
            )
    old_master = base64.b64decode(env["MANIFOLD_MASTER_KEY"])
    new_master = os.urandom(32)
    db = Database(tmp_path)
    await db.open()
    try:
        await db.migrate()
        count = await rotate(
            db, derive_key(old_master, INFO_CREDENTIALS), derive_key(new_master, INFO_CREDENTIALS)
        )
    finally:
        await db.close()
    assert count == 1
    assert [s.reason for s in SnapshotStore(Database(tmp_path), b"").list()] == ["pre-rotation"]
    # old key now fails at boot, new key reads the credential back
    from manifold.crypto.keycheck import MasterKeyError

    app = await boot(env, tmp_path)
    with pytest.raises(MasterKeyError):
        async with app.router.lifespan_context(app):
            pass
    env["MANIFOLD_MASTER_KEY"] = base64.b64encode(new_master).decode()
    app = await boot(env, tmp_path)
    async with app.router.lifespan_context(app):
        creds = app.state.repos["credentials"]
        summary = (await creds.list())[0]
        assert (await creds.get(summary.id)).values["key"] == "sk-live-1"


async def test_rotation_refuses_wrong_current_key(env, tmp_path):
    """A wrong current key must fail even when no credentials exist to trip over."""
    from manifold.crypto.keycheck import MasterKeyError

    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    app = await boot(env, tmp_path)
    async with app.router.lifespan_context(app):
        pass
    db = Database(tmp_path)
    await db.open()
    try:
        await db.migrate()
        wrong = derive_key(os.urandom(32), INFO_CREDENTIALS)
        with pytest.raises(MasterKeyError):
            await rotate(db, wrong, derive_key(os.urandom(32), INFO_CREDENTIALS))
        assert SnapshotStore(db, b"").list() == [], "no snapshot before the key is proven"
    finally:
        await db.close()
    app = await boot(env, tmp_path)
    async with app.router.lifespan_context(app):
        pass  # original key still boots


async def test_rotation_refuses_same_key(env, tmp_path):
    env["MANIFOLD_DATA_DIR"] = str(tmp_path)
    db = Database(tmp_path)
    await db.open()
    try:
        await db.migrate()
        key = derive_key(b"\x01" * 32, INFO_CREDENTIALS)
        with pytest.raises(ValueError):
            await rotate(db, key, key)
    finally:
        await db.close()
