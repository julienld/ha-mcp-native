"""Bind Search's public contract to this manager's native Core service."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.search_tools import SearchFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints

from .native_operations.search import NativeSearch


def search_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        return await SearchFacade(NativeSearch(manager, actor)).ha_search(**arguments)

    return define_tool(
        manager.backend,
        SearchFacade,
        "ha_search",
        source_module="tools_search",
        tags=frozenset({"Search & Discovery"}),
        handler=invoke,
        annotations=read_only_hints("Search", open_world=False),
    )
