"""Bind the one theme contract to this manager's real Frontend owners."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.theme_tools import ThemeTools
from ha_mcp.runtime.theme_values import theme_writes
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.themes import NativeThemes


def theme_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        return await ThemeTools(NativeThemes(manager, actor)).ha_manage_theme(
            **arguments
        )

    return define_tool(
        manager.backend,
        ThemeTools,
        "ha_manage_theme",
        handler=invoke,
        source_module="tools_themes",
        tags=frozenset({"System"}),
        annotations=write_hints(
            "Manage Frontend Themes",
            destructive=True,
            idempotent=True,
            open_world=False,
        ),
        writes=theme_writes,
    )
