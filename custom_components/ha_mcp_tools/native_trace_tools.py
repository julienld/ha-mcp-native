"""Bind the shared trace contract to direct Core trace reads."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.trace_tools import TraceFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints

from .native_operations.traces import NativeTraces


def trace_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativeTraces(manager.hass, actor, authorize=manager.backend.authorize)
        return await TraceFacade(service.get).ha_get_automation_traces(**arguments)

    return define_tool(
        manager.backend,
        TraceFacade,
        "ha_get_automation_traces",
        source_module="tools_traces",
        annotations=read_only_hints("Get Automation Traces", open_world=False),
        tags=frozenset({"History & Statistics"}),
        handler=invoke,
    )
