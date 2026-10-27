"""Bind the health contract to the canonical Core service."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.health_tools import HealthFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints

from .native_operations.health import NativeHealth


def health_definition(manager: Any) -> ToolDefinition:
    async def invoke(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativeHealth(manager.hass, actor, authorize=manager.backend.authorize)
        return await HealthFacade(service.get).ha_get_system_health(**arguments)

    return define_tool(
        manager.backend,
        HealthFacade,
        "ha_get_system_health",
        source_module="tools_system",
        annotations=read_only_hints(
            "Get System Health (incl. ZHA/Z-Wave/integration diagnostics)",
            open_world=True,
        ),
        tags=frozenset(
            {"System", "Zigbee", "Z-Wave", "Thread", "Matter", "Integrations"}
        ),
        handler=invoke,
    )
