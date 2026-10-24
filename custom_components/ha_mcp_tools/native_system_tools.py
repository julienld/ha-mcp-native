"""Bind the unique restart/reload contracts to the native Core lifecycle."""

from typing import Any

from ha_mcp.runtime.catalog import define_tool
from ha_mcp.runtime.system_actions import SystemActionsFacade
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult
from ha_mcp.tools.tool_hints import write_hints

from .native_operations.registry_access import registry_failure
from .native_operations.system_actions import NativeSystemActions


def system_definitions(manager: Any) -> list[ToolDefinition]:
    async def restart(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        service = NativeSystemActions(manager, actor)
        try:
            return await SystemActionsFacade(
                service.restart, service.reload
            ).ha_restart(**arguments)
        except Exception as error:
            raise registry_failure(error, {"operation": "restart"}) from error

    async def reload(arguments: dict[str, Any], actor: CallContext) -> Any:
        service = NativeSystemActions(manager, actor)
        try:
            result = await SystemActionsFacade(
                service.restart, service.reload
            ).ha_reload_core(**arguments)
        except Exception as error:
            raise registry_failure(error, {"operation": "reload"}) from error
        if result.get("operation_outcome") == "outcome_unknown":
            return ToolResult(result, status="outcome_unknown")
        return result

    return [
        define_tool(
            manager.backend,
            SystemActionsFacade,
            "ha_restart",
            handler=restart,
            source_module="tools_system",
            tags=frozenset({"System"}),
            annotations=write_hints(
                "Restart Home Assistant",
                destructive=True,
                idempotent=False,
                open_world=False,
            ),
        ),
        define_tool(
            manager.backend,
            SystemActionsFacade,
            "ha_reload_core",
            handler=reload,
            source_module="tools_system",
            tags=frozenset({"System"}),
            annotations=write_hints(
                "Reload Core Components",
                destructive=True,
                idempotent=False,
                open_world=False,
            ),
        ),
    ]
