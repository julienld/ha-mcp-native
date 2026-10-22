"""MCP search proxies and Assist share the live resource initialization effect."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp.tool_api import CallContext

from ._embedded_stubs import install
from .test_native_engine_backend import native as native
from .test_native_resource_service import resources as resources

install()

from custom_components.ha_mcp_tools import native_server  # noqa: E402


@pytest.fixture
def channels(resources, monkeypatch):
    from custom_components.ha_mcp_tools.native_server_policy import (
        NativePolicy,
        catalogue_transform,
    )

    source, engine, owner, *_ = resources
    manager = native_server.NativeServerManager.__new__(
        native_server.NativeServerManager
    )
    manager.hass, manager.backend = source.hass, source.backend
    manager.engine = engine
    manager.policy = NativePolicy(
        flags={"enable_tool_search": True},
        pinned=frozenset(),
        llm_overrides=dict.fromkeys(engine.tools, True),
    )
    manager.refresh_policy = AsyncMock()
    manager.mcp = FastMCP("Resource channel contracts")
    monkeypatch.setattr(native_server, "current_context", lambda: CallContext("owner"))
    for tool in engine.tools.values():
        manager.mcp.add_tool(native_server._project_tool(manager, tool))
    manager.mcp.add_transform(catalogue_transform(manager))
    manager._configure_search()
    return manager, owner


async def test_resource_proxy_rechecks_loaded_state_without_mislabeling_read_route(
    channels,
):
    manager, owner = channels
    owner.loaded = False
    args = {"name": "ha_config_list_dashboard_resources", "arguments": {}}
    async with Client(manager.mcp) as client:
        found = await client.call_tool("ha_search_tools", {"tools": [args["name"]]})
        assert found.data[0]["annotations"]["readOnlyHint"] is False
        refused = await client.call_tool(
            "ha_call_read_tool", args, raise_on_error=False
        )
        assert refused.is_error and owner.loads == 0
        allowed = await client.call_tool("ha_call_write_tool", args)
        assert not allowed.is_error and owner.loads == 1
        read = await client.call_tool("ha_call_read_tool", args)
        assert not read.is_error and owner.loads == 1
        owner.loaded = False
        stale = await client.call_tool("ha_call_read_tool", args, raise_on_error=False)
        assert stale.is_error and owner.loads == 1


async def test_assist_resource_read_respects_global_readonly_and_core_loaded_state(
    channels,
):
    from custom_components.ha_mcp_tools.native_llm import call_exposed

    manager, owner = channels
    manager.policy = replace(manager.policy, read_only=True)
    manager.engine.read_only = True
    owner.loaded = False
    context = SimpleNamespace(
        context=SimpleNamespace(user_id="owner"), platform="test", language="en"
    )
    refused = await call_exposed(
        manager, "ha_config_list_dashboard_resources", {}, context
    )
    assert refused["isError"] is True and owner.loads == 0
    owner.loaded = True
    allowed = await call_exposed(
        manager, "ha_config_list_dashboard_resources", {}, context
    )
    assert allowed.get("isError") is not True and owner.loads == 0
