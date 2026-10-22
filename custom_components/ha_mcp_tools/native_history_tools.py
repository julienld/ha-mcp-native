"""Bind the shared recorder contract to the native Core service."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.history_tools import HistoryFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints

from .native_operations.history import NativeHistory


def history_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativeHistory(
            manager.hass,
            actor,
            authorize=manager.backend.authorize,
            guardrails=manager.policy.flag("enable_history_query_guardrails"),
        )
        return await HistoryFacade(service.get).ha_get_history(**arguments)

    return define_tool(
        manager.backend,
        HistoryFacade,
        "ha_get_history",
        source_module="tools_history",
        annotations=read_only_hints(
            "Get Entity History or Statistics", open_world=False
        ),
        tags=frozenset({"History & Statistics"}),
        handler=invoke,
    )
