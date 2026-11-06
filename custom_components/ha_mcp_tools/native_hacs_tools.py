"""Bind the shared HACS contracts to the existing in-process HACS owners."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.hacs_tools import HacsFacade
from ha_mcp.tool_api import CallContext, ToolDefinition
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .native_operations.hacs_writes import NativeHacs
from .native_operations.registry_access import registry_failure


def hacs_definitions(manager: Any) -> list[ToolDefinition]:
    async def read(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        try:
            return await HacsFacade(NativeHacs(manager, actor)).ha_get_hacs_info(
                **arguments
            )
        except Exception as error:
            raise registry_failure(
                error, {"tool": "ha_get_hacs_info", "action": arguments.get("action")}
            ) from error

    async def write(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        try:
            return await HacsFacade(NativeHacs(manager, actor)).ha_manage_hacs(
                **arguments
            )
        except Exception as error:
            raise registry_failure(
                error, {"tool": "ha_manage_hacs", "action": arguments.get("action")}
            ) from error

    return [
        define_tool(
            manager.backend,
            HacsFacade,
            "ha_get_hacs_info",
            handler=read,
            source_module="tools_hacs",
            tags=frozenset({"HACS"}),
            annotations=read_only_hints("Get HACS Info", open_world=True),
        ),
        define_tool(
            manager.backend,
            HacsFacade,
            "ha_manage_hacs",
            handler=write,
            source_module="tools_hacs",
            tags=frozenset({"HACS"}),
            annotations=write_hints(
                "Manage HACS", destructive=True, idempotent=False, open_world=True
            ),
        ),
    ]
