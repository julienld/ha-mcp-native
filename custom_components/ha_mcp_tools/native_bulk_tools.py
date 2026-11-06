"""Bind the public bulk and status contracts to this manager's Core owners."""

from typing import Any

from ha_mcp.runtime.bulk_tools import BulkFacade
from ha_mcp.runtime.catalog import define_tool
from ha_mcp.tool_api import CallContext, ToolDefinition, ToolResult
from ha_mcp.tools.tool_hints import read_only_hints, write_hints

from .native_operations.bulk_control import NativeBulkControl


def bulk_definitions(manager: Any) -> list[ToolDefinition]:
    async def status(arguments: dict[str, Any], actor: CallContext) -> dict[str, Any]:
        result: dict[str, Any] = await manager.device_operations.status(
            actor,
            manager.backend.authorize,
            arguments["operation_id"],
            arguments["timeout_seconds"],
        )
        return result

    async def control(arguments: dict[str, Any], actor: CallContext) -> Any:
        result = await NativeBulkControl(manager, actor).control(**arguments)
        if any(
            row.get("operation_outcome") == "outcome_unknown"
            for row in result.get("results", [])
        ):
            return ToolResult(result, status="outcome_unknown")
        return result

    return [
        define_tool(
            manager.backend,
            BulkFacade,
            "ha_bulk_control",
            handler=control,
            source_module="tools_service",
            tags=frozenset({"Service & Device Control"}),
            annotations=write_hints(
                "Bulk Control", destructive=True, idempotent=False, open_world=False
            ),
            writes=lambda arguments: not arguments["dry_run"],
        ),
        define_tool(
            manager.backend,
            BulkFacade,
            "ha_get_operation_status",
            handler=status,
            source_module="tools_service",
            tags=frozenset({"Service & Device Control"}),
            annotations=read_only_hints("Get Operation Status", open_world=False),
        ),
    ]
