"""Reload contract (DECISIONS.md, Phase 2 gate 3), against a fake source and real servers."""

from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer

from manifold.gateway.dispatcher import ToolsetDispatcher
from manifold.gateway.manifest import HealthResult
from manifold.gateway.registry import DesiredToolset, Registry

PING = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "ping", "arguments": {}},
}
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def make_spec(
    key: str, content: str = "v1", fail: bool = False, delay: float = 0.0
) -> DesiredToolset:
    async def build():
        if fail:
            raise RuntimeError(f"cannot build {key} ({content})")
        server = MCPServer(key)

        @server.tool()
        async def ping() -> str:
            """Reply with the toolset key and content version."""
            await asyncio.sleep(delay)
            return f"{key}:{content}"

        async def health() -> HealthResult:
            return HealthResult(status="ok")

        return server, health

    return DesiredToolset(key=key, display_name=key, version="t", content_hash=content, build=build)


class FakeSource:
    def __init__(self, specs: list[DesiredToolset]) -> None:
        self.specs = specs
        self.gate: asyncio.Event | None = None
        self.calls = 0

    async def desired(self) -> list[DesiredToolset]:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return list(self.specs)


async def fallback(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ui"})


@pytest.fixture
async def rig():
    source = FakeSource([make_spec("a"), make_spec("b")])
    registry = Registry(source, drain_seconds=1.0)
    dispatcher = ToolsetDispatcher(registry.routes, fallback=fallback)
    async with registry.running():
        transport = httpx2.ASGITransport(app=dispatcher)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as client:
            yield source, registry, client


async def call(client, key):
    r = await client.post(f"/{key}", json=PING, headers=HDR)
    return r.status_code, (
        r.json()["result"]["content"][0]["text"] if r.status_code == 200 else r.text
    )


async def test_initial_mount(rig):
    _, registry, client = rig
    assert registry.keys == ["a", "b"]
    assert await call(client, "a") == (200, "a:v1")
    assert await call(client, "b") == (200, "b:v1")


async def test_changing_one_toolset_keeps_the_other_runtime_instance(rig):
    source, registry, client = rig
    a_before, b_before = registry.get("a").runtime, registry.get("b").runtime
    source.specs = [make_spec("a", "v2"), make_spec("b")]
    report = await registry.reload()
    assert report.rebuilt == ("a",) and report.unchanged == ("b",)
    assert registry.get("b").runtime is b_before
    assert registry.get("a").runtime is not a_before
    assert await call(client, "a") == (200, "a:v2")
    assert await call(client, "b") == (200, "b:v1")


async def test_build_failure_leaves_prior_runtime_mounted_and_reports_down(rig):
    source, registry, client = rig
    a_before = registry.get("a").runtime
    source.specs = [make_spec("a", "v2", fail=True), make_spec("b")]
    report = await registry.reload()
    assert report.failed == ("a",)
    assert registry.get("a").runtime is a_before
    assert await call(client, "a") == (200, "a:v1"), "old runtime still serves"
    health = await client.get("/a/healthz")
    assert health.status_code == 503
    assert "cannot build a (v2)" in health.json()["detail"]
    # the fix arrives: rebuilt cleanly, health recovers
    source.specs = [make_spec("a", "v3"), make_spec("b")]
    await registry.reload()
    assert await call(client, "a") == (200, "a:v3")
    assert (await client.get("/a/healthz")).status_code == 200


async def test_new_toolset_that_fails_gets_a_503_placeholder(rig):
    source, registry, client = rig
    source.specs = [make_spec("a"), make_spec("b"), make_spec("c", fail=True)]
    report = await registry.reload()
    assert report.failed == ("c",)
    assert registry.get("c").runtime is None
    status, body = await call(client, "c")
    assert status == 503 and "cannot build c" in body
    assert (await client.get("/c/healthz")).json()["status"] == "down"
    assert (await client.get("/x")).text == "ui", "unknown keys still fall through"


async def test_failed_toolset_is_retried_on_next_reload(rig):
    source, registry, client = rig
    source.specs = [make_spec("a"), make_spec("b"), make_spec("c", fail=True)]
    await registry.reload()
    source.specs = [make_spec("a"), make_spec("b"), make_spec("c")]
    report = await registry.reload()
    assert report.added == ("c",)
    assert await call(client, "c") == (200, "c:v1")


async def test_removed_toolset_is_unmounted_and_stopped(rig):
    source, registry, client = rig
    source.specs = [make_spec("a")]
    report = await registry.reload()
    assert report.removed == ("b",)
    assert "b" not in registry.routes
    assert (await client.post("/b", json=PING, headers=HDR)).text == "ui"


async def test_concurrent_triggers_produce_exactly_one_extra_run(rig):
    source, registry, _ = rig
    runs_before = registry.reload_runs
    source.gate = asyncio.Event()
    first = asyncio.create_task(registry.reload())
    await asyncio.sleep(0.05)  # first run is inside desired(), holding the lock
    assert await registry.reload() is None
    assert await registry.reload() is None
    assert await registry.reload() is None
    source.gate.set()
    await first
    assert registry.reload_runs == runs_before + 2


async def test_inflight_call_completes_while_its_runtime_is_replaced(rig):
    source, registry, client = rig
    source.specs = [make_spec("a", "slow", delay=0.4), make_spec("b")]
    await registry.reload()
    slow = asyncio.create_task(call(client, "a"))
    await asyncio.sleep(0.1)
    source.specs = [make_spec("a", "v9"), make_spec("b")]
    await registry.reload()
    assert await slow == (200, "a:slow")
    assert await call(client, "a") == (200, "a:v9")


async def test_running_context_stops_everything(rig):
    _, registry, _ = rig
    assert list(registry.runtimes())


async def test_reload_report_is_json_friendly(rig):
    _, registry, _ = rig
    report = await registry.reload()
    json.dumps(report.__dict__)
