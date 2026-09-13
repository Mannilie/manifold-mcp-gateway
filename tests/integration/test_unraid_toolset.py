"""Unraid toolset against the fake GraphQL API."""

from __future__ import annotations

import contextlib
import json

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from manifold.gateway.manifest import Credentials, ToolsetConfig
from manifold.toolsets import unraid as unraid_module
from manifold.unraid import client as client_mod
from tests.fake_unraid import API_KEY, FakeUnraid

URL = "http://unraid.test"


@pytest.fixture
async def rig(monkeypatch):
    fake = FakeUnraid()
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fake.app))
    monkeypatch.setattr(client_mod, "_shared_http", {True: http, False: http})
    monkeypatch.setattr(client_mod, "RETRY_BASE_SECONDS", 0.01)
    yield fake
    await http.aclose()


def creds(key: str = API_KEY) -> Credentials:
    return Credentials(kind="api_key", values={"key": key, "header": "x-api-key"})


def config() -> ToolsetConfig:
    return ToolsetConfig(key="unraid", settings={"server_url": URL, "verify_tls": True})


def kib_gb(kib: float) -> float:
    return round(kib * 1024 / 1024**3, 1)


async def call(server, tool, **args):
    result = await server.call_tool(tool, args)
    if result.is_error:
        raise ToolError(result.content[0].text)
    return json.loads(result.content[0].text)


async def test_system_overview(rig):
    out = await call(unraid_module.build(config(), creds()), "system_overview")
    assert out["hostname"] == "manny-nas" and out["unraid_version"] == "7.2.0"
    assert out["memory"]["total_gb"] == 64.0 and out["cpu"]["load_percent"] == 12.3
    assert out["array"]["state"] == "STARTED"
    assert out["array"]["total_gb"] == kib_gb(30_000_000_000)
    assert out["mover_running"] is False and out["parity_sync_running"] is False


async def test_array_status_includes_pools(rig):
    out = await call(unraid_module.build(config(), creds()), "array_status")
    assert [d["name"] for d in out["parity"]] == ["parity"]
    assert [d["name"] for d in out["data"]] == ["disk1", "disk2"]
    assert [d["name"] for d in out["pools"]] == ["cache", "cache2"], "NVMe pool devices present"
    assert out["pools"][0]["type"] == "CACHE"
    assert out["pools"][0]["fs_used_gb"] == kib_gb(1_000_000_000)
    assert out["parity_check"]["status"] == "COMPLETED"
    assert out["boot"]["name"] == "flash"


async def test_disk_health_cached_by_default_and_smart_on_request(rig):
    server = unraid_module.build(config(), creds())
    cached = await call(server, "disk_health")
    assert cached["source"] == "cached" and cached["warnings"] == ["disk2"]
    assert rig.calls == 1
    fresh = await call(server, "disk_health", spin_up=True)
    assert fresh["source"] == "smart"
    assert [d["smart_status"] for d in fresh["disks"]] == ["OK", "UNKNOWN"]
    assert fresh["warnings"] == ["disk2"]


async def test_ups_shares_vms_mover_notifications(rig):
    server = unraid_module.build(config(), creds())
    ups = await call(server, "ups_status")
    assert ups["devices"][0]["charge_percent"] == 100 and ups["devices"][0]["runtime_minutes"] == 42
    shares = await call(server, "list_shares")
    assert shares["shares"][0]["name"] == "media"
    assert shares["shares"][0]["used_gb"] == kib_gb(1_000_000_000)
    vms = await call(server, "list_vms")
    assert vms["vms"] == [
        {"name": "HomeAssistant", "state": "RUNNING"},
        {"name": "Win11", "state": "SHUTOFF"},
    ]
    mover = await call(server, "mover_status")
    assert mover["schedule"] == "0 3 * * *" and mover["running"] is False
    notes = await call(server, "list_notifications", importance="warning")
    assert [n["id"] for n in notes["notifications"]] == ["n:2"] and notes["counts"]["unread"][
        "total"
    ] == 2
    with pytest.raises(ToolError, match="importance must be"):
        await call(server, "list_notifications", importance="loud")


async def test_mutations_refuse_without_confirm(rig):
    server = unraid_module.build(config(), creds())
    for name, args in (
        ("parity_check", {"action": "start"}),
        ("vm_control", {"name": "Win11", "action": "start"}),
        ("archive_notification", {"id": "n:1"}),
    ):
        with pytest.raises(ToolError, match="confirm=true"):
            await call(server, name, **args)
    assert rig.mutations == []


async def test_mutations_with_confirm(rig):
    server = unraid_module.build(config(), creds())
    out = await call(server, "parity_check", action="start", correct=False, confirm=True)
    assert out["done"] is True and rig.mutations[-1] == ("parityCheck.start", {"correct": False})
    out = await call(server, "vm_control", name="win11", action="start", confirm=True)
    assert out["vm"] == "Win11" and rig.mutations[-1] == ("vm.start", {"id": "vm:2"})
    with pytest.raises(ToolError, match="no VM named 'Nope'"):
        await call(server, "vm_control", name="Nope", action="stop", confirm=True)
    out = await call(server, "archive_notification", id="n:1", confirm=True)
    assert out["archived"] == "n:1" and rig.mutations[-1] == ("archiveNotification", {"id": "n:1"})
    with pytest.raises(ToolError, match="action must be"):
        await call(server, "parity_check", action="explode", confirm=True)


async def test_errors_are_readable(rig):
    server = unraid_module.build(config(), creds("wrong"))
    with pytest.raises(ToolError, match="rejected the API key"):
        await call(server, "system_overview")
    server = unraid_module.build(config(), creds())
    rig.forbidden.add("vms")
    with pytest.raises(ToolError, match=r"not allowed to list VMs.*READ_ANY on VMS"):
        await call(server, "list_vms")
    rig.forbidden.clear()
    rig.renamed.add("temp")
    with pytest.raises(ToolError, match="no field 'temp' on type 'ArrayDisk'"):
        await call(server, "array_status")


async def test_docstrings_say_confirm_and_spin_up():
    server = unraid_module.build(config(), creds())
    tools = {t.name: t.description for t in await server.list_tools()}
    assert len(tools) == 11
    for name in ("parity_check", "vm_control", "archive_notification"):
        assert "Does nothing unless confirm is true" in tools[name]
    assert "spin" in tools["disk_health"] and "cached" in tools["disk_health"]
    assert "no way to start or stop the mover" in tools["mover_status"]


async def test_healthcheck_names_a_renamed_field(rig):
    assert (await unraid_module.healthcheck(config(), creds())).status == "ok"
    rig.renamed.add("shareMoverActive")
    result = await unraid_module.healthcheck(config(), creds())
    assert result.status == "degraded" and "'shareMoverActive'" in result.detail
    rig.renamed = {"smartStatus"}  # only used by the SMART query; caught by introspection
    result = await unraid_module.healthcheck(config(), creds())
    assert result.status == "degraded" and "Disk.smartStatus" in result.detail
    rig.renamed.clear()
    rig.introspection = False
    result = await unraid_module.healthcheck(config(), creds())
    assert result.status == "ok" and "introspection unavailable" in result.detail
    rig.introspection = True
    rig.forbidden.add("array")
    result = await unraid_module.healthcheck(config(), creds())
    assert result.status == "degraded" and "READ_ANY on ARRAY" in result.detail
    wrong = await unraid_module.healthcheck(config(), creds("bad"))
    assert wrong.status == "down"


async def test_healthcheck_never_runs_the_smart_query(rig):
    await unraid_module.healthcheck(config(), creds())
    # every call was either the health query or introspection: none touched `disks {`
    assert rig.calls == 1 + len(unraid_module.q.FIELDS_USED)


class TlsDead(httpx2.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx2.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
        )


async def test_tls_failure_is_named_and_not_retried(monkeypatch):
    http = httpx2.AsyncClient(transport=TlsDead())
    monkeypatch.setattr(client_mod, "_shared_http", {True: http, False: http})
    cfg = ToolsetConfig(key="unraid", settings={"server_url": "https://192.168.0.69"})
    server = unraid_module.build(cfg, creds())
    with pytest.raises(ToolError, match=r"certificate .* is not trusted .*set verify_tls to false"):
        await call(server, "system_overview")
    health = await unraid_module.healthcheck(cfg, creds())
    assert health.status == "down" and "verify_tls" in health.detail
    await http.aclose()


async def test_healthcheck_and_tools_share_the_client_path(rig, monkeypatch):
    """Test connection must fail exactly when the tools would."""
    seen: list[str] = []
    original = client_mod.UnraidClient.query

    async def spy(self, document, variables=None, *, context):
        seen.append(context)
        return await original(self, document, variables, context=context)

    monkeypatch.setattr(client_mod.UnraidClient, "query", spy)
    await unraid_module.healthcheck(config(), creds())
    await call(unraid_module.build(config(), creds()), "system_overview")
    assert seen[0] == "health query" and seen[-1] == "read system overview"


async def test_verify_tls_setting_selects_the_client(monkeypatch):
    chosen: list[bool] = []
    real = client_mod.shared_http

    def spy(verify_tls: bool):
        chosen.append(verify_tls)
        return real(verify_tls)

    monkeypatch.setattr(client_mod, "shared_http", spy)
    cfg = ToolsetConfig(
        key="unraid", settings={"server_url": "https://192.168.0.69", "verify_tls": False}
    )
    unraid_module._Unraid(cfg, creds()).client  # noqa: B018 - property builds the client
    assert chosen == [False]


class Answer(httpx2.AsyncBaseTransport):
    def __init__(self, status: int, text: str, headers: dict | None = None) -> None:
        self.status, self.text, self.headers = status, text, headers or {}
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx2.Response(self.status, text=self.text, headers=self.headers, request=request)


async def test_csrf_and_redirect_are_their_own_errors(monkeypatch):
    for transport, pattern in (
        (Answer(200, "Invalid CSRF token"), r"answered by the Unraid web GUI, not the API"),
        (Answer(403, "Invalid CSRF token"), r"answered by the Unraid web GUI"),
        (
            Answer(302, "", {"location": "https://192.168.0.69/login"}),
            r"redirected .* to https://192.168.0.69/login",
        ),
    ):
        http = httpx2.AsyncClient(transport=transport)
        monkeypatch.setattr(client_mod, "_shared_http", {True: http, False: http})
        server = unraid_module.build(config(), creds())
        with pytest.raises(ToolError, match=pattern):
            await call(server, "system_overview")
        await http.aclose()


async def test_request_shape_matches_curl(monkeypatch, caplog):
    """POST <origin>/graphql, x-api-key and content-type only, no cookie, no redirects."""
    transport = Answer(200, '{"data": {}}')
    http = httpx2.AsyncClient(transport=transport)
    http.cookies.set("unraid_session", "stale", domain="unraid.test")
    monkeypatch.setattr(client_mod, "_shared_http", {True: http, False: http})
    server = unraid_module.build(config(), creds())
    with caplog.at_level("INFO", logger="manifold.unraid.client"), contextlib.suppress(ToolError):
        await call(server, "mover_status")  # empty data is fine for this test
    req = transport.requests[0]
    assert req.method == "POST" and str(req.url) == f"{URL}/graphql"
    assert req.headers["x-api-key"] == API_KEY
    assert req.headers["content-type"] == "application/json"
    assert "cookie" not in req.headers and "authorization" not in req.headers
    assert "origin" not in req.headers and "referer" not in req.headers
    logged = [r for r in caplog.records if r.getMessage() == "unraid request"]
    assert logged and logged[0].url == f"{URL}/graphql" and "x-api-key" in logged[0].header_names
    assert API_KEY not in str(logged[0].__dict__)
    await http.aclose()
