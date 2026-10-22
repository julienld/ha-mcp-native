"""Global visibility precedes MCP search and remains independent of Assist mode."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.native_llm import exposed_catalog  # noqa: E402
from custom_components.ha_mcp_tools.native_server_policy import (  # noqa: E402
    NativePolicy,
    catalogue_transform,
)
from ha_mcp._vendor.fastmcp import Client, FastMCP  # noqa: E402
from ha_mcp.tool_api import ToolDefinition  # noqa: E402
from ha_mcp.transforms import CategorizedSearchTransform  # noqa: E402


async def test_hidden_tools_are_absent_from_search_and_assist_keeps_full_catalogue() -> (
    None
):
    mcp = FastMCP("Native visibility")

    @mcp.tool(
        name="visible_read",
        description="Read the visible kitchen state",
        annotations={"readOnlyHint": True},
    )
    async def read() -> str:
        return "visible"

    @mcp.tool(
        name="hidden_write",
        description="Write the hidden kitchen state",
        annotations={"readOnlyHint": False},
    )
    async def write() -> str:
        return "hidden"

    definitions = {
        name: ToolDefinition(name, name, {}, AsyncMock(), source_module="tools_search")
        for name in ("visible_read", "hidden_write")
    }
    manager = SimpleNamespace(
        mcp=mcp,
        engine=SimpleNamespace(tools=definitions),
        policy=NativePolicy(tool_states={"hidden_write": "disabled"}),
        refresh_policy=AsyncMock(),
    )
    mcp.add_transform(catalogue_transform(manager))
    mcp.add_transform(CategorizedSearchTransform(always_visible=[]))
    async with Client(mcp) as client:
        listed = {tool.name for tool in await client.list_tools()}
        assert "ha_search_tools" in listed and "visible_read" not in listed
        found = await client.call_tool("ha_search_tools", {"query": "kitchen"})
        assert "visible_read" in json.dumps(found.data)
        assert "hidden_write" not in json.dumps(found.data)
        tools, _pinned = await exposed_catalog(manager)
        assert {tool.name for tool in tools} == {"visible_read"}
        manager.policy = NativePolicy()
        refreshed = await client.call_tool("ha_search_tools", {"query": "kitchen"})
        assert "hidden_write" in json.dumps(refreshed.data)
