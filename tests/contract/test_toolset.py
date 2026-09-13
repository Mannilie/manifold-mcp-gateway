"""Every native toolset must pass these (SPEC.md section 6)."""

from __future__ import annotations

import asyncio

import jsonschema
import pytest
from mcp.server.mcpserver import MCPServer

from manifold.gateway.manifest import (
    RESERVED_KEYS,
    Credentials,
    HealthResult,
    ToolsetConfig,
    ToolsetManifest,
    validate_key,
)
from manifold.gateway.registry import discover_native_toolsets

TOOLSETS = discover_native_toolsets()


@pytest.fixture(params=sorted(TOOLSETS), ids=sorted(TOOLSETS))
def toolset(request):
    return TOOLSETS[request.param]


@pytest.fixture
def config(toolset) -> ToolsetConfig:
    return ToolsetConfig(key=toolset.MANIFEST.key, settings=toolset.MANIFEST.example_settings)


def test_manifest_is_valid(toolset):
    manifest = toolset.MANIFEST
    assert isinstance(manifest, ToolsetManifest)
    assert validate_key(manifest.key) == manifest.key
    assert manifest.key not in RESERVED_KEYS
    assert manifest.kind == "native"


def test_example_settings_validate_against_schema(toolset):
    jsonschema.validate(toolset.MANIFEST.example_settings, toolset.MANIFEST.settings_schema)


def test_build_returns_server_with_tools(toolset, config):
    server = toolset.build(config, Credentials())
    assert isinstance(server, MCPServer)
    tools = asyncio.run(server.list_tools())
    assert tools, "a toolset with no tools is pointless"


def test_every_tool_has_a_description(toolset, config):
    server = toolset.build(config, Credentials())
    for tool in asyncio.run(server.list_tools()):
        assert tool.description and tool.description.strip(), f"{tool.name} has no description"
        assert len(tool.description.strip()) >= 20, f"{tool.name} description is too short"


async def test_healthcheck_returns_within_five_seconds(toolset, config):
    result = await asyncio.wait_for(toolset.healthcheck(config, Credentials()), 5)
    assert isinstance(result, HealthResult)


def test_manifold_toolset_is_present():
    assert "manifold" in TOOLSETS
