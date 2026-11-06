"""Bind the shared radio signature directly to installed Core radio owners."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.radio_tools import RadioFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.radio_manager import NativeRadios
from .native_operations.registry_access import registry_failure


def radio_definition(manager: Any) -> ToolDefinition:
    async def manage(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativeRadios(manager.hass, actor, manager.backend.authorize)
        try:
            return await RadioFacade(service).ha_manage_radio(**arguments)
        except Exception as error:
            raise registry_failure(error, {"operation": "radio"}) from error

    return define_tool(
        manager.backend,
        RadioFacade,
        "ha_manage_radio",
        handler=manage,
        source_module="tools_radio",
        tags=frozenset({"Radio Management", "Z-Wave", "Zigbee", "Matter", "Thread"}),
        annotations=write_hints(
            "Manage Radios (Z-Wave / Zigbee / Matter / Thread)",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
