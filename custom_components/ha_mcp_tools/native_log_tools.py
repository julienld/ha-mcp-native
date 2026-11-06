"""Bind one log contract to the native Core source owners."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.log_tools import LogsFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints

from .native_operations.logs import NativeLogs


def logs_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativeLogs(manager.hass, actor, authorize=manager.backend.authorize)
        return await LogsFacade(service.get).ha_get_logs(**arguments)

    return define_tool(
        manager.backend,
        LogsFacade,
        "ha_get_logs",
        source_module="tools_logs",
        annotations=read_only_hints("Get Logs", open_world=False),
        tags=frozenset({"History & Statistics"}),
        handler=invoke,
    )
