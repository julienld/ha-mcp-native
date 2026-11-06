"""Bind overview's one public signature to its Core-native owner."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.overview_tools import OverviewTools
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints

from .native_operations.overview import NativeOverview


def overview_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        return await OverviewTools(NativeOverview(manager, actor)).ha_get_overview(
            **arguments
        )

    return define_tool(
        manager.backend,
        OverviewTools,
        "ha_get_overview",
        source_module="tools_search",
        tags=frozenset({"Search & Discovery"}),
        handler=invoke,
        annotations=read_only_hints("Get System Overview", open_world=True),
    )
